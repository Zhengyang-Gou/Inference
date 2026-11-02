# 运行状态观测

每次 `run-baseline` 自动在结果中创建 `observation/`，不增加 ns-3 事件，也不改变请求、计算或通信完成时间。采集采用事件驱动：状态在相邻事件之间按上一条记录保持。

默认 `recording.mode="essential"` 只写统计汇总。队列与在途量仍逐事件在线积分，在 `summary.json` 的 `measurement_window` 中保存配置窗口的平均值和峰值。以下完整状态文件与时间线需要 `recording.mode="debug"`；详细规则见 [实验记录规则](../RECORDING.md)。

`feedback.state_observation_enabled=false` 可关闭上述状态采集、状态文件和时间线；请求/token 轨迹、SLO 评估及独立缓存/调度汇总继续输出。`feedback.network_observation_enabled` 独立控制动态网络采样；关闭时不产生周期采样事件或 network_samples.jsonl，受控 KV 使用静态拓扑容量。初始化仍读取端口拓扑与容量，之后不读取队列/ACK 状态。

```text
observation/
├── state.jsonl          # debug：带版本序号、采集时间和可见时间的完整状态
├── state.csv            # debug：队列、忙闲、通信和内存的全局时间序列
├── observed_state.json  # debug：仿真结束时已经可见的最新快照，可能为 null
├── instances.csv        # 每实例统计
├── summary.json         # 统计口径、实例/通信/显存汇总
└── timeline.png         # debug 且 run-baseline --plot 时生成
```

状态包括：

- 已到达、已完成、失败和未完成请求数；不暴露尚未到达的请求。
- 每 P/D 实例的队列长度、队首请求、当前最长排队时间、执行忙闲和计算忙闲。
- 每 GPU 的 KV 可用容量、占用、预留，以及峰值承诺；这些容量已扣除模型权重与安全余量。
- KV / collective 在途逻辑消息的数量与字节数、提交/完成计数，及每条传输的 local/inter_node 范围。

`compute_busy` 仅覆盖性能数据驱动的计算时间；`execution_busy` 还包括该实例计算步骤内的集合通信等待。利用率是各自忙碌时间除以完整仿真时长，是仿真指标，不是实际 GPU 硬件测量。

队列平均长度按时间积分，不按事件条数平均。平均排队等待按已经出队的 Prefill 请求或 Decode token 步骤统计；尚未出队的请求保留在状态与队列长度积分中，不伪造已完成等待样本。

## 延迟可见性

`feedback.observation_delay_us` 现在实际控制观测的可见时间。快照的 `time_seconds` 是采集时刻，`visible_at_seconds` 是最早可见时刻。程序接口 `StateObserver.latest_observed()` 只读取当前仿真时钟，返回已经可见的最新快照的副本；没有可见快照时返回 None，不能指定未来查询时间。

`state.jsonl` 和 state.csv 是离线真值记录，不能直接当作在线策略状态。在线策略应使用 latest_observed，离线重放应按 visible_at_seconds 筛选。立即传输基线不根据观测决策；layer_ready_fifo/slo 使用延迟可见的网络快照，观测延迟会影响 KV 放行。scheduling_overhead_us 在受控模式中计入 KV 决策唤醒时间；它尚不表示 P/D 绑定计算开销。

在途字节数保留整条消息的提交大小，直到 ACK 完成后移除；它不是链路队列积压或剩余在线字节数。包含节点内通信，并非真实 NIC 利用率或逐链路遥测。仿真截止时保留未完成请求、在途消息与其资源，不把它们清零伪装为完成。

可以离线重新绘制状态时间线：

```bash
./scripts/simai-python -c 'from joint_scheduling.observation.plots import plot_observation; plot_observation("joint_scheduling/artifacts/generated-run/observation")'
```

图文件同样拒绝覆盖；重新绘制前应选择新的结果目录或处理旧图文件。

## 动态网络观测

feedback.network_observation_enabled=true 时增加显式周期采样，两种记录模式都向调度器提供相同的延迟可见快照。默认只输出 `network_summary.json`，包含最后采样的端口累计计数及采样峰值；debug 模式额外输出 `network_samples.jsonl`。采样数据包括端口分类发送字节、队列、PFC 暂停、平滑后的速率和 QP ACK 进度；节点内链路为已有 FIFO 模型的服务进度。NetworkObserver.latest_observed() 同样仅返回延迟后可见的最新快照。

字节在包开始发送时计入，含包头，极短窗口利用率可略高于 1；它与普通 StateObserver 的整条逻辑消息在途字节是两种口径。动态模式的 KV 额度、请求 SLO 余量和阻塞原因见 kv_scheduling_decision 事件，调度汇总见 kv_scheduler_summary.json。具体算法见 ../kv_scheduler/README.md。
