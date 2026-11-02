# P/D 联合选择、绑定窗口与有限预测

`fast.py` 实现候选剪枝＋增量贪心，`runtime.py` 接入实际 backend 派发与整批资源预留。`window.py` 使用 ns-3 唯一时间轴。`predictor.py` 中的 `WholePlanPredictor` 用于离线验证或显式抽样诊断：整请求配置保留流体网络影子重放，分块/合批配置复用 `execution.py` 的有界执行模型；在线候选评分不会调用 `WholePlanPredictor`。

默认 `recording.mode="essential"` 保留每个请求的实际绑定、绑定汇总和已开启的墙钟计时；下文的 `binding_selection`、`binding_prediction` 等详细诊断事件仅在 `recording.mode="debug"` 时写入轨迹。记录模式不改变候选选择或预测开关，详见 [实验记录规则](../RECORDING.md)。

## 开关与预算

旧配置保持原行为。联合选择需要同时设置 `joint_selection_enabled=true` 和 `policy=joint_lexicographic`；`policy=round_robin` 始终保留基线。配置示例：

```json
"binding": {
  "policy": "joint_lexicographic",
  "joint_selection_enabled": true,
  "window_enabled": true,
  "window_ms": 1.0,
  "early_trigger_margin_ms": 0.1,
  "max_wait_ms": 10.0,
  "decision_overhead_us": 5.0,
  "candidate_count": 4,
  "max_requests_per_round": 16,
  "max_pending_scan": 64,
  "max_pair_checks": 64,
  "max_score_work": 16384,
  "max_state_requests": 256,
  "max_state_objects": 4096,
  "starvation_ms": 10.0,
  "prediction_decode_steps": 8,
  "prediction_enabled": false,
  "prediction_sample_every": 0,
  "wall_clock_diagnostics": true
}
```

`joint_selection_enabled` 默认 false，其余新字段默认值如上。关闭窗口时，联合选择在到达时运行；暂缓请求仍通过窗口定时器重试。关闭联合选择后，原有立即/窗口轮询、前缀缓存、KV 模式、观测开关和预测诊断行为继续有效。轮询的资源预留仍在 Prefill 开始时进行。

`max_pending_scan` 限制本轮查看的 FIFO 前段；在其中按 SLO 紧迫程度及等待老化排序，处理不超过 `max_requests_per_round` 个请求。超过 `starvation_ms` 的请求优先；达到老化阈值且不降低预测达标总数时，允许牺牲余量获得派发机会，避免因次级指标一直暂缓。资源始终不可行或会降低达标总数时仍可能继续等待。

`max_pair_checks` 限制轻量候选池，`candidate_count` 限制每请求增量评分次数。`max_score_work` 是确定性的逻辑工作量上限，计入状态对象、部署排序、布局、链路和请求评分；它不是微秒承诺。过大的部署、活动状态或对象数量会安全暂缓，不能静默忽略已有请求。工作预算耗尽时保留此前选出的可行结果，以及当前请求已完成评分的最佳结果，其余下轮继续。没有局部搜索、墙钟截断或随机退化路径。固定输入、seed 和开关可复现绑定与 token 事件，诊断计时单独保存。

## 延迟快照与可见信息

联合选择每轮只读取一份一致快照。窗口定时器在决策前 `observation_delay_us` 安排采样，采样后达到可见时间才发布；提前触发来不及取得新样本时使用上一份可见样本。过期窗口的采样回调不做复制。关闭收集窗口时在到达时采样，仍遵守可见延迟。此路径避免在每个 token、层或网络事件上完整复制账本。

快照复用现有实例队列、profile、物理账本、前缀索引及最近采集的网络样本，与状态文件导出开关独立。网络样本保留自身时间，不叠加第二次延迟。快照不包含尚未到达请求、未来传输完成时间或隐藏真实 EOS。控制器自己已经提交的绑定、资源对象及声明派发时间可合并到旧遥测；序号区分同一仿真时刻采样前后的提交，合并不读取隐藏执行进度。

输出使用请求的 `max_output_tokens`，缺省用 `reservations.reserve_max_output_tokens`。联合模式运行前验证该上限覆盖模拟输出；精确 `output_tokens` 仅供执行器生成 EOS，不进入候选评分或增长预留。只有采样时已就绪的 P/D 缓存可以命中，两端独立；不假设本窗口或未来请求会预热缓存。候选试算不修改真实 LRU、账本、读 pin、队列或 ns-3 时间。

## 剪枝、评分与影子更新

静态路径复用 `NetworkResources` 的有向 ECMP 最短路并集，预先缓存每 P/D 对的路径和实例 ring 资源。候选池交错覆盖低 P 排队/D 负载、P 缓存优势、D 缓存优势和短网络路径；随后保留综合排队、传输成本和显存可行性的候选，同时为缓存及网络优势保留位置。候选数小于四时按综合负载、P 命中、D 命中、网络的顺序截断。剪枝是启发式，不能保证找到全局最优端点。

整请求 FIFO、单请求 Decode 配置继续使用有界 memo 聚合与队列摘要。启用 Chunked Prefill、Prefill 合批或 Decode 连续批处理时，`execution.py` 使用受同一 `max_score_work` 预算约束的局部队列预测；在线路径仍不调用 `WholePlanPredictor`。

分块/合批预测读取已处理 token 游标、活动 chunk、准入顺序和活动批次的全部成员。Prefill 与执行器复用 running 优先/等待 FCFS/token 预算规则，单请求 chunk 按块边界轮转；Decode 按就绪队列和批量上限逐轮组批。计算查询相同的 chunk/history、Prefill 共享线性层+独立 attention、Decode 最大上下文批量曲线。共享计算与 ring 通信只按批次计一次，每候选重新评估全部已有及暂选请求，不能把单请求成本简单相加。

候选剪枝阶段只用首尾完整 chunk 与尾块样本构造有界成本代理，不遍历长输入的全部 chunk；这是启发式剪枝，可能漏掉更优端点。保留下来的候选评分才按实际 query/history 与队列规则逐块预测。相同 ring 消息形状的各层在网络工作量中合并，避免重复展开路径。

活动批次从延迟可见的层/计算段进度保守续算。缓存只查询该快照，命中初始化计算游标，P/D 独立；不预热未来缓存，不读取精确 EOS。每个请求只报告 `prediction_decode_steps` 限制内的 token；较快到达预测范围末端的请求仍作为竞争者保留到可见输出上限，其后上下文冻结在预测范围末端，避免提前移除未知 EOS 请求或做范围外推。此处理仍是有限范围近似。

该路径按 chunk 聚合计算及各层 ring 工作，在 chunk 结束处估计 KV 就绪，网络使用有向 ECMP 资源的保守排队预留，未重放层间重叠、RDMA 或 KV 门控。已完成 chunk、活动共享批次及历史 token 不重复计费。快照缺少覆盖、事件/仿真时间上限不足或工作预算耗尽时不把不完整预测当成达标；联合选择保留已评分成功的结果，其余暂缓。完整诊断在分块/合批配置下复用该执行模型，并明确输出 `model=bounded_chunk_batch_resource_forecast`。此路径成本随 chunk/批次增多，原有 50 μs 目标仍待独立实测。KV 等待计入 handoff，不将整份 KV 时间重复加入 TTFT。

候选分数按以下顺序比较：

1. 已有、此前已选和当前新增请求的有限范围预测达标总数。
2. 最小归一化 SLO 余量，再比较余量之和；TTFT、handoff、后续最大 ITL 均纳入。
3. 网络工作量，再比较新增物理显存承诺。

每次接纳后立即更新影子 P 队列、D 周期、链路负载、物理对象大小和共享引用。新增请求会改变已有请求的预测；未达到老化阈值且无法改善字典序目标时可暂缓，老化请求保留达标总数优先的约束。真实物理占用、已有预留与共享对象只计一次。

## 原子预留与执行

选定批次先验证采样命中对象仍驻留、已完全物化且身份未变，然后调用 `CapacityLedger.try_commit_batch`。所有计划和物理对象合并验证成功后才发布；容量不足、无效对象或冲突计划 ID 不留下部分预留。重放相同计划幂等，不重复占用。

预留覆盖 P 输入、D 缺失输入、共享前缀引用和**可见输出上限**的私有增长空间。真实 Prefill admission 复用已提交的固定布局，不重新查询新命中、不重复预留；实际使用时才更新真实 LRU。已有源端传输 pin、ACK 后物化、源引用释放和结束回收沿用原路径。延迟期间被淘汰的采样命中使整批暂缓，不复活旧对象。试算和提交不主动淘汰缓存，因此闲置缓存导致的容量压力可能使选择偏保守。

## 预测范围与近似

`RequestPrediction.output_token_limit` 是可见输出上限，`horizon_output_tokens` 是本次有限范围，范围受 `prediction_decode_steps` 限制。`complete` 仅表示 profile 覆盖这段范围，不能保证未知输出阶段全部 token 的 SLO。`binding_selection` 明确记录 `prediction_scope=finite_visible_output_horizon`。

网络模型用共享有向链路的保守字节负债近似，包含采样队列和背景流量；Prefill/Decode 集合通信工作与 KV 竞争。它不复刻 RDMA/DCQCN、不逐块重放 KV 令牌/动态在途额度或集合通信保护窗口，也不精确重放层间传输重叠。D 负载包含已绑定但尚未就绪的请求，可能高估竞争；正在执行步骤只使用采样就绪层和历史 token 的摘要。完整预测器更细致，仍不是 ns-3 的替代品。

## 诊断、实测与仿真开销

`observation/binding_summary.json` 保存候选评分次数、预算耗尽次数、预留失败数、绑定等待、有限范围声明和窗口统计。`binding_selection` 记录每轮确定性工作量、快照时间及实际提交计划。

`observation/binding_timing.json` 用 `perf_counter_ns` 记录独立墙钟诊断：

- `round_us`：实际绑定回调，包含状态处理、候选筛选、评分、原子预留、事件记录及派发/定时器提交。
- `snapshot_capture_us`：从上轮结束到本轮开始之间，为绑定采样实际花费的时间。
- `inclusive_us`：上述两者之和，防止把延迟快照的构造成本移出计时；回调内采样不会重复计入。
- `state_processing_us`、`selection_us`、`reservation_us` 和单次快照的 P50/P95/P99，以及原始逐轮记录。

这些计时不进入事件轨迹、分数、预算或仿真时钟。仿真派发等待只使用显式 `decision_overhead_us`；示例 5 μs 是配置假设，绝非实测值。初始目标为 1 ms 窗口、整轮 P99 ≤50 μs，**没有达成保证**。历史验证报告和实验输出已清理；整轮控制成本、均匀/异构负载收益和开销敏感性待重新验证。

联合模式下，完整诊断要求 `prediction_enabled=true` 且 `prediction_sample_every>0`；默认零抽样，不阻塞在线选择。显式抽样会在候选选择之后同步运行一次完整预测并计入该轮墙钟，适合调试，不应计入默认在线性能。轮询模式保留原 `prediction_enabled` 诊断行为。完整预测器仍可直接用于离线 `WholePlanPredictor.predict(plan, state)`。

## 可运行验证

```bash
./inference --config joint_scheduling/configs/joint_heterogeneous.json run-baseline --output joint_scheduling/artifacts/joint-demo
./inference run-experiments --spec joint_scheduling/configs/joint_heterogeneous_comparison.json --output joint_scheduling/artifacts/joint-heterogeneous
./inference run-experiments --spec joint_scheduling/configs/joint_uniform_comparison.json --output joint_scheduling/artifacts/joint-uniform
./scripts/simai-python -m joint_scheduling.binding.benchmark --config joint_scheduling/configs/joint_heterogeneous.json --output joint_scheduling/artifacts/joint-timing --repetitions 20
./joint_scheduling/check.sh
```

两种对照均固定 workload/profile/seed/功能开关，扫描轮询/联合 × 0/5/50/200/1000/5000 μs 声明开销 × seed 7/19；全部数据明确标为 synthetic。benchmark 使用实际 ns-3 runner 测量冷状态 1/8/16 请求窗口，保留完整原始运行目录；它不代表热队列或真实 GPU 吞吐。原有 `binding_smoke.json` 和 `binding_comparison_example.json` 继续验证轮询窗口和完整预测诊断。
