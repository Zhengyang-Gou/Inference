# P 池内部的 token 预算合批

开启后，一个 P 实例每轮可以共同执行多个请求的新 token，P/D 继续分离。
这是按显式计算代价和 token 预算推进的 Prefill 组批仿真。

## 调度规则

参考 [vLLM v0.10.0 V1 scheduler](https://github.com/vllm-project/vllm/blob/v0.10.0/vllm/v1/core/sched/scheduler.py)：
先安排已经接纳、尚未完成Prefill的请求，再按等待队列FCFS接纳新请求。
每个请求都有已处理token位置，分配量为“剩余输入、单请求chunk上限、全批剩余token预算”的最小值。
因此一个请求可被剩余预算切成更小的块，下一轮从准确的历史位置续算。

```json
"runtime": {
  "prefill_chunk_tokens": 512,
  "prefill_batch_token_budget": 512,
  "prefill_batch_size": 32,
  "decode_batch_size": 32
}
```

- `prefill_chunk_tokens`：一个请求一轮至多处理多少新token。
- `prefill_batch_token_budget`：同一P实例一轮所有请求合计的新token上限。
- `prefill_batch_size`：同一P实例已接纳、尚未完成Prefill的请求数上限，不只是本轮选择的数量。
- 预算不足时允许切出尾块/部分块；有剩余预算才接纳更多等待请求。
- 已接纳请求优先，不沿用旧单请求chunk轮转的公平性策略。一个长请求若能占满全批预算，仍可能独占该轮。
- 每个请求本轮只分配一个块；若它达到单请求上限后仍有预算，预算供其他请求使用，不重复选择自己。
- 不等待未来请求凑满batch；1ns调度tick仅合并同一模拟时刻已经到达的回调，截止时间之外不启动新批。

默认 `prefill_batch_token_budget=0`、`prefill_batch_size=1`，保留旧单请求执行流程。
预算非零时，必须提供正的单请求chunk上限；批量大于1时必须开启token预算。
设置batch size=1、保持相同预算和新profile，可作为新组批器的单请求对照，避免混入计时模型差异。

## 计算与通信

计算profile必须显式分解线性层等共享算子与attention，旧合计时延无法自动推导合批时延。
公开生成器现在会随chunked profile导出：

- `phases.prefill.chunked.linear_points`：按整批新token数取样的非attention层算子、embedding和单次集合通信消息大小。
- 每个chunk/context点的 `attention_seconds`：独立请求attention的时延估计。
- 原 `compute_seconds` 保留，旧单请求chunk模式继续使用原计时路径。

每层的批次计算时间为“按总新token数查询一次共享算子 + 所有请求各自attention估计之和”。
embedding按整批新token数计算，只加在第一层。每个请求的query和已有上下文分别查表，不把独立序列拼成一个长序列做attention，也不按请求数简单乘整段前向时间。
每层集合通信只执行一组，消息大小对应整批token数，全部实际完成后才进入后续层。
KV依旧逐请求、逐层、逐token范围形成和发送，不合并不同请求的缓存身份。

公开attention数据只有单请求Prefill采样，当前多请求attention的加和是明确的建模近似。
真实varlen/fused attention可能有不同的批处理效率；旧留出误差不能直接解释为本模式端到端精度。
目前使用同构Transformer层时延；配置启动前保守检查完整query/history覆盖，不做范围外推。

生成新的分解profile：

```bash
./inference build-public-profile \
  --mlp /path/to/mlp.csv --attention /path/to/attention.csv \
  --source-commit VIDUR_COMMIT --tp 4 --prefill-chunk-tokens 512 \
  --target-model meta-llama/Meta-Llama-3-70B --output /path/to/new-profile.json
```

生成的共享线性层数据可覆盖比512更大的批总token数；实际覆盖范围以linear_points为准。
增大总预算之前仍需检查时延覆盖、显存和TTFT，不能默认更大的预算更优。

## 状态、容量与依赖

每个请求仅在第一次被接纳时提交现有P/D容量预留；续算不重复预留。
容量不足时停止接纳新的FCFS队首，已持有资源的running请求继续排空，不被新请求阻塞。
同一批次的所有成员完成状态都更新后，P实例才释放执行占用并安排下一轮，避免回调重入导致两个批次在同一实例重叠。
首token仅在该请求最后一块的全部层与集合通信完成后返回；D仍同时等待Prefill完成和完整输入KV实际到达。
批次中途截止时保留活动成员、已完成token位置和资源，不伪造首token或释放未完成依赖。

容量仍沿用现有整请求字节预留和源端读pin，不是vLLM的逐页KV分配/显存抢占实现；尚未单独核算批次activation和临时workspace显存。
实际执行规则以resolved_config.json中的effective_baseline为准，数据profile中的旧默认执行说明不决定调度。
可与自有prefix_cache、绑定预测和联合选择组合。首次准入复用共享缓存布局与绑定时原子预留，
命中从缓存上下文之后续算；最后一个查询重算不重复物化或传输已共享的KV。
执行器与预测器复用running优先/等待FCFS/token预算的成员选择规则及同一计算profile。
预测按共享批次更新全部成员，资源网络工作在chunk结束处聚合，未逐层重放网络和KV门控，
因此仍属于有界近似，不能把预测SLO当作达标保证。
KV SLO剩余P工作估计将活动公共批次计入一次、未来块按单请求profile估算，是因果启发式而非精确组批重放；不调整SLO阈值。

## 记录与验证

`prefill_batch_summary.json` 始终保存：批次数、请求批量分布、总token分布、容量推迟次数、每实例峰值running数、活动批次和待执行tick。
对完成的批次，还保存每实例的模型计算时间、总busy时长及其差值对应的通信等待。
这些是仿真时间统计，不是实际GPU算力利用率；截止中的活动批次单列，不冒充完整时长样本。
debug模式记录 `prefill_batch_started/completed`、成员、每成员调度token数和cached位置，并关联共享计算/通信。

原生ns-3测试覆盖五种KV模式、剩余预算截块、running优先、迟到请求下一轮加入、请求槽上限、
独立attention、共享通信次数、字节和token守恒、显存不足时继续排空、截止状态以及旧profile启动前拒绝。
