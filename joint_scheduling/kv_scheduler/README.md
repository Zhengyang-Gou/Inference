# 动态网络观测、集合通信保护与 SLO KV 调度

新增 `policy="slo_mlu"`：KV 按 MLU 晋升，EP 分阶段保护，并可启用真实主机/交换机优先级。配置及与原策略的区别见 [MLU 说明](MLU.md)。下文描述原 `slo_slack` 路径。

默认 `recording.mode="essential"` 保留请求轨迹、KV 调度汇总和网络汇总。下文的 `network_samples.jsonl`、`kv_scheduling_decision`、逐条传输和集合通信明细只在 `recording.mode="debug"` 时写盘；在线采样、可见延迟和 KV 放行不变，详见 [实验记录规则](../RECORDING.md)。

`runtime.kv_transfer_mode=layer_ready_slo` 启用完整 KV 控制；`layer_ready_fifo` 使用相同观测、保护、额度但按请求到达顺序排列，供消融对照。`layer_ready_immediate` 就绪即提交。三者要求同一 schema 2 层模型；P/D 绑定保持轮询，本地计算保持 FIFO/逐 token。

```bash
./inference --config joint_scheduling/configs/dynamic_window_smoke.json run-baseline --output joint_scheduling/artifacts/dynamic-window-new --plot
./inference run-experiments --spec joint_scheduling/configs/dynamic_window_comparison_example.json --output joint_scheduling/artifacts/dynamic-window-comparison-new
```

## 请求时间预算

请求实际到达时间为 a，TTFT 阈值为 F，ITL 阈值为 I。TTFT 的绝对截止为 a+F。首 token 返回前，用当前层剩余计算、已知后续层样本和 ring 通信工作量估计首 token 时刻 t1；只读取已到达的请求和正在执行的队列，不读取未来到达。首 token 返回后使用实际 t1。

KV 最晚交付时刻为 t1+I，扣除预计 D 排队和首个 Decode 步骤时间。返回首 token 之前还将 t1 限制到 TTFT 截止，避免对已经可能错过 TTFT 的请求不断后移交接目标。

未完成 KV 字节包含尚未就绪、待放行及在途数据；已完整 ACK 的块立即扣除，延迟可见网络快照中的在途部分 ACK 进一步减少估计。按剩余数据在各共享资源上的工作量/估计服务速率，估计剩余传输时间 T。完整 KV 预计到齐时间近似为 max(预计 t1, now+T)，允许传输与后续计算重叠。

slack = KV 最晚交付时刻 - KV 预计到齐时刻；normalized_slack = slack/I。

预测 TTFT 仍可达标且 slack>=0 的请求优先，其中 normalized_slack 较小者优先。预测已不可达标的请求保留、按到达顺序尽力完成，不因超期丢弃。每次层就绪、块完成、集合通信计划变化、网络观测可见或额度唤醒都重新计算；一个被瓶颈阻塞的高优先级请求不会阻止其他独立路径的请求放行。块内的层、rank、offset 使用稳定顺序。

该预测是启发式：当前集合通信可能按完整剩余轮次估计，D 正在执行的步骤按整步估计，并未解决多个请求未来排队的联合最优预测。SLO 优先级不构成达标保证，也不等价于请求到达的 FIFO。

## 共享网络资源额度

ns-3 STATS 导出每个有向端口的容量、分类包字节、队列、PFC 暂停计数及已提交 QP 的 ACK 进度。字节在 PhyTxBegin 计入，含包头；短采样窗的利用率可能略大于 1。节点内局部 FIFO 的字节/队列按其已建模服务过程计算，不能解释为真实 NVLink 测量。

NetworkObserver 按 network_observation_interval_us 采样，使用 network_ewma_alpha 对速率平滑；所有数据在 observation_delay_us 后才可见。读取 STATS 不推进时间；周期采样和控制唤醒是显式 ns-3 事件。普通 StateObserver 的在途逻辑字节继续保留原口径。

每个有向端口以及每服务器的局部链路各维护一个字节令牌桶。按容量扣除安全余量、观测集合通信和其他流量，再按队列压力降低补充速率。已经占用的 KV 不作为其他流量重复扣除。

令牌按上一段时间已经确定的速率补充，单次启动扣除该块在各资源上的字节；完成只释放在途额度，不退还令牌。同一服务器多个 rank 共享发送/接收方向额度。ECMP 不假设固定路径，而是对所有最短路径的有向端口并集保守扣费；会对多路径产生额外保守性。

启动必须同时满足全局并发、全局在途字节、各资源在途字节和所有相关令牌。就绪但未启动的 KV 仍保留源端引用；启动后加读 pin，未发送完不能释放源端，未 ACK 完不能解除读依赖。

## 集合通信保护

本地执行器根据正在执行的计算段，登记下一次集合通信的预计开始时刻和资源需求。保护前瞻包括 collective_guard_us、现有 KV 在途字节、新块和观测队列的排空估计；不使用未来真实完成时刻。

集合通信进行中或即将开始时，为其保留 collective_reserve_fraction 的资源容量，取“保留容量”和“观测集合通信速率”的较大者，KV 只能使用扣除安全余量和其他流量后的剩余速率。队列达到 queue_target_bytes 或出现新的 PFC 暂停时，暂停相关资源上的新 KV。

默认 inflight_budget_mode=adaptive，不再固定限制为一块。对每个共享资源，以可用 KV 速率 B、已完成 KV 从提交到 ACK 的时延 EWMA L、控制间隔 I 和调度开销 D 计算流水额度 W=max(max_block_bytes, ceil(B×(max(I,L)+D)))，再受 max_resource_inflight_bytes 硬上限约束。尚无完成样本时使用控制间隔启动；完成反馈只更新 L 并释放在途占用，不产生带宽令牌。端到端完成时延保守用于该块经过的所有资源；队列反馈仍会降低 B。

对于已登记的下一次集合通信，额外计算排空额度 G=max(0, floor(C_safe×(max(0,t_collective-now)+collective_max_blocking_us/1e6)-observed_queue_bytes))，C_safe 为扣除安全余量的容量。实际额度取 min(W,G,硬上限)。如果一个完整块无法放入，就等待；不会把 G 向上取整成一块。额度缩小时不取消已经启动的块，停止新增直到占用回落；观察到的排队和未 ACK 字节可能重叠，因此排空估计偏保守。这个额度是基于延迟观测和已知计算段的排空模型，不能保证真实集合通信额外延迟一定小于配置值。

inflight_budget_mode=fixed 保留旧对照：普通资源使用固定硬上限，保护资源最多一块。dynamic_window_smoke.json 使用 adaptive、50 μs 阻塞预算、每资源 1 MiB 硬上限，全局 8 MiB / 128 块；硬上限仅为安全边界，实际放行量由动态公式决定。dynamic_window_ablation_example.json 在相同硬上限下只切换 fixed/adaptive，可区分放宽旧硬上限与动态窗口的影响。

pause_on_collective=true 可切换为严格暂停新 KV 的对照。保护期清除严格暂停资源的令牌，避免解除保护后倾倒积累预算。默认保留剩余服务，避免集合通信频繁时 KV 长时间等不到额度。

这是应用层启动保护，没有修改 RDMA/NIC 的实际速率或抢占在途消息；保留比例是放行预算目标，不能作为硬件瞬时带宽保证。应同时报告集合通信时延、TTFT、交接、后续 ITL 和达标完成吞吐，不能仅靠某一项下降认定策略更好。

## Decode 并发感知的尾层准入

`decode_admission_limit` 默认为 `0`，关闭本保护；非零值只用于受控的逐层 KV 模式。
它按目标 Decode 实例限制已接入请求数。在首个最后一层 KV 块开始发送前预占一个名额，
其余层仍可与 Prefill 计算重叠。一个请求的多个 rank 和尾块共用同一个名额。
KV 收齐不会释放名额；必须等该请求全部输出完成才释放，并唤醒待传尾块。
因此同时完成的网络 ACK 不会导致重复准入，保护只使用已发生的本地状态。

这个变体通过 KV 完成时机影响 Decode 接入，包含下游并发控制，超出了纯网络集合通信保护。
它保持原有 P/D 绑定和 Decode 逐 token 轮询规则，不抢占在途传输，也不丢弃请求。
被延迟接入的请求可能违反首次交接 ITL；这些超时必须计入原来的全请求 SLO 分母。
应同时报告达标数、后续 ITL、交接尾延迟、端到端延迟和最终完成/资源释放，不能仅报告受保护请求。
并发上限需要与模型逐 token 服务时间和 ITL 阈值一起核对，配置值不构成硬实时保证。

`kv_scheduler_summary.json` 记录 `decode_slots_acquired/released`、`peak_decode_owners` 和
`decode_owners_remaining`；`blocked_checks.decode_admission` 是阻塞候选检查次数，不是等待时长。

## 配置与证据

- kv_scheduler：max_block_bytes、max_concurrent_transfers、max_inflight_bytes、max_resource_inflight_bytes、safety_margin_fraction、control_interval_us、collective_guard_us、collective_reserve_fraction、pause_on_collective、queue_target_bytes、inflight_budget_mode、collective_max_blocking_us、completion_feedback_alpha、decode_admission_limit。
- feedback：network_observation_enabled、network_observation_interval_us、network_ewma_alpha、observation_delay_us、scheduling_overhead_us。网络观测按开关显式启用；关闭时受控模式使用已知拓扑容量，停止运行期采样，不使用队列、分类速率或部分 ACK 反馈。调度开销只计 KV 决策延迟。

`slo_priority_enabled=false` 将 `layer_ready_slo` 排序切换为 FIFO；`collective_protection_enabled=false` 关闭集合通信预留、前瞻保护和集合通信排空约束，但集合通信仍真实执行。`inflight_budget_mode=fixed` 关闭动态带宽/ACK 流水额度，使用固定额度。全局和每资源硬上限、容量和传输依赖继续执行。
- observation/network_samples.jsonl：带采集/可见时间的网络真值样本。
- events.jsonl 中 kv_scheduling_decision：各请求的到达、TTFT 截止、预计首 token、KV 最晚交付、剩余字节/时间、余量，以及选中的块、阻塞原因和资源额度。资源记录包含 inflight_limit_bytes、pipeline_limit_bytes、collective_limit_bytes、next_collective_seconds、completion_latency_seconds 和 completion_samples，可审计公式及每次新放行。
- collective_started/completed：逻辑 ring 集合通信组及实际完成时延。
- observation/kv_scheduler_summary.json：放行数、字节、峰值、未完成待传/在途数和阻塞检查计数。阻塞检查数不是阻塞时长。

示例比较采用不规则到达和不同 TTFT/ITL 阈值。所有计算文件显式标为 synthetic，尚不能用于真实模型的性能结论。
