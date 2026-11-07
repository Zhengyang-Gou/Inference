# T002-r03：三处时序边界补证据 — 审核意见

- 审核 agent：指挥 agent（Codex）
- 审核时间：2026-11-07T19:11:16+08:00
- 执行报告：/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T002-bounded-baseline-validation-r03/REPORT.md
- 结论：**accepted**
- 系列结论：结合 r02 的两轮端到端产物与本轮补充证据，**T002 有界样本基础链路验收 A1–A6 全部通过**。

## 核心判断

已独立读取原始 boundary-events.json、audit-run 的 CSV/事件、冻结输入、源码归档及后端身份，重新配对三处边界。没有启动新仿真。

本次独立 60 项检查全部通过。匹配使用实际 KV 字节区间、Decode batch_id、成员及 context 推导的 token_index，并使用整数纳秒比较，无需依赖执行脚本的时间排序配对与 1ns 容差。r03 采集运行与 r02 的 run-a、run-b 语义结果均一致。

r02 已证明的 A1/A2/A4/A5/A6 与本轮补齐的 A3 共同构成完整验收；合并结论写在本轮 results/acceptance-summary.json。保留原 T002 blocked、r02 changes_requested 的历史状态及所有原文件，本轮仅把 r03 设为 accepted。

## 检查记录

证据根目录：
/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T002-bounded-baseline-validation-r03/

| 项目 | 独立检查方法与原始结果 | 判断 |
| --- | --- | --- |
| B1 KV 就绪→发送 | 按 request_id/layer_id/字节偏移匹配，偏移由 cached_tokens×1152 得到；244 条一一对应，无缺失/重复；核对每条大小、P/D GPU 端点、ready_at 与捕获顺序。实际 ready 事件先于 send 回调记录，仿真时刻相同；总 payload 115878528 字节 | 通过 |
| B2 完整 Prefill→首次 Decode | 逐请求检查 chunk 长度/索引/历史偏移；每个 chunk 的 61 层计算完成与 layer-ready 一一对应；完整输入处理完毕的最终 chunk 完成时刻等于首 token 时刻。3 请求均在全部 KV 接收后进入首个 Decode batch，首批 context 等于输入长度；首次计算提交与 batch-start 的关系由冻结同步调用链确认，详见下文 | 通过 |
| B3 61 层→对应 token | 205 个 batch_id 各有且仅有按顺序完成的层 0–60；成员和 D 端点匹配。通过 token_index=context−input_tokens+2 连接到 CSV，覆盖全部 205 个后续 token，逐请求 43/108/54，无漏项或重复。各 token 时刻等于第 60 层及对应 batch 完成时刻 | 通过 |
| 输入身份 | 5 个输入与 r02 逐字节相同，T001 五件冻结文件哈希保持原值；实际运行导出的配置/profile/workload/绑定与 r02 相同，复现文件哈希自洽 | 通过 |
| 机制与资源 | EPFlowAudit 12122 次调用、141968 条逻辑流核对通过，无活动流/步骤/批次；各 EP、P/D、资源与通信摘要同 r02 两轮，32 个显存域最终 occupied/reserved 为 0 | 通过 |
| 语义一致性 | 与 r02 的 run-a、run-b 分别比较全部生命周期事件，requests/tokens/timeseries CSV，EP（含队列等待时间）、P/D、资源、绑定、通信统计、指标摘要、EPFlowAudit 和 hook 计数，均相同 | 通过 |
| 运行与源码身份 | ns-3 fingerprint 23ec96146135d0706e39，与 r02 相同；实际可执行文件哈希等于复现身份。源码树哈希等于 r02，归档哈希与所有归档文件内容核验通过；当前核心源码与归档一致 | 通过 |
| 原结果保留 | r02 REPORT 与 acceptance-summary 哈希符合原值，原审核与 changes_requested 状态保留；本次审核前后完整 r02 文件哈希清单相同。r03 执行前后 Git 状态也一致 | 通过 |

具体时刻（纳秒记录换算为秒）：

| 请求后缀 | 完整 Prefill / 首 token | 全 KV 接收 / 首 Decode batch 启动 | 两边界间隔 |
| --- | ---: | ---: | ---: |
| 000000000 | 0.084032527 | 0.084045361 | 12.834 μs |
| 000000001 | 4.401182672 | 4.401200096 | 17.424 μs |
| 000000002 | 4.727121063 | 4.727133734 | 12.671 μs |

这里的 12.671–17.424 μs 是 Prefill 结束到 D 首批启动的间隔；研究指标 handoff 仍按 token2−token1 计算，本样本均值约 17.004 ms。

## 证据口径与代码确认

1. **首次 Decode 计算开始并未直接单独捕获。** 本轮直接保存的是 decode_batch_started 和各层完成。已核对同一运行源码快照的同步调用链：
   - /home/m0nesy/Postgraduate/Inference/joint_scheduling/backend/execution/decode.py:43：发出 batch-start 后同步调用 layer(0)；
   - /home/m0nesy/Postgraduate/Inference/joint_scheduling/backend/execution/compute.py:11：进入 execute_ep_step；
   - /home/m0nesy/Postgraduate/Inference/joint_scheduling/backend/expert_parallel.py:59：在任何计时器或 GPU 工作入队前发出 compute_step_started。
   
   因而 layer0 的 compute-step 提交与 batch-start 处于同一仿真时刻，GPU 实际服务开始可因队列而更晚。动态边界与已核验的冻结调用链共同足以证明它不会早于完整 Prefill / 全 KV 接收。本审核明确区分直接记录和代码推导，不将 batch-start 冒称为直接采集的 compute-start。

2. **逐条 KV 完成标志未单独保存。** 已核对 /home/m0nesy/Postgraduate/Inference/joint_scheduling/backend/rpc.py:142 的 submit.done：每个逻辑传输所有分片完成后，统一构造 TransferProgress(..., True, True)；client 等待全部分片，network 完成回调随后释放源读保护并 materialize 目的 KV。相同 ns-3 后端身份声明 sender-ACK 完成语义，且本轮完成字节/消息守恒、全 KV 事件及最终资源与 r02 相同。
   
   前次审核要求逐条保存 source_reads_complete/destination_ready；核对实现后确认这两个字段是完成回调的常量。接受上述冻结代码不变量、后端身份及实际完成守恒组成的等价证明，无需再为保存重复常量而补跑。本轮不声称拥有逐条 KV ACK 完成时间线。

3. 执行脚本的配对与失败处理仍不宜作为通用验收入口：analyze_boundary.py 主要依靠时间排序，且未在汇总失败时返回非零；capture_boundary.py 也未将 EP audit failed 转换为失败退出码。r02 的旧 A1 检查问题亦保留在历史草稿中。本轮由指挥的独立检查直接核验实际输入、字节区间、批次/context 与完整层序列，任一检查失败均返回非零。旧草稿不再承担最终验收判定，未修改其历史产物。这些草稿问题不影响本次已经逐项重算通过的结论；将来复用时需先修正。

4. 报告把前置 r02 状态写成 submitted，是历史状态误记；审核时真实状态为 changes_requested。该文字误记已在本审核与合并验收中纠正，不改写执行报告。

## 审核产物

- results/review-verification.json：60 项检查及所有 244/3/205 配对明细、两项代码推导说明和 r02 文件哈希清单。
- results/acceptance-summary.json：T002 系列 A1–A6 合并验收，以本轮审核为准。
- logs/review-boundaries.txt：只读复核命令输出。
- 审核脚本：/home/m0nesy/Postgraduate/Inference/work/agents/2026-11-07/T002-bounded-baseline-validation-r03/review_boundaries.py。

## 问题与返工要求

**无阻塞问题，无需继续补跑 T002。** 本轮额外采集运行约 282.634 秒，配置与原结果语义一致，输出独立目录并保留了原始轮次，接受为本次补证据。

## 下一轮条件

T001 的基线冻结与 T002 的有界基础链路验收均已完成，可进入主线下一步：确认固定 P/D 绑定、同一基线下 KV 与 EP 是否实际竞争，以及竞争对请求指标的影响。具体范围和运行预算需另发任务，本审核不自动派发实验。

验收结论仅覆盖当前 3 请求、4 个 Prefill chunk、205 个单成员 Decode batch 的功能样本。尚不证明 batch>1、持续负载或承载能力，也不证明 KV/EP 竞争强度或任何调度算法收益。
