# 运行状态观测

每次 `run-baseline` 自动在结果中创建 `observation/`，不增加 ns-3 事件，也不改变请求、计算或通信完成时间。采集采用事件驱动：状态在相邻事件之间按上一条记录保持。

```text
observation/
├── state.jsonl          # 带版本序号、采集时间和可见时间的完整状态
├── state.csv            # 队列、忙闲、通信和内存的全局时间序列
├── observed_state.json  # 仿真结束时已经可见的最新快照，可能为 null
├── instances.csv        # 每实例统计
├── summary.json         # 统计口径、实例/通信/显存汇总
└── timeline.png         # run-baseline --plot 时生成
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

`state.jsonl` 和 state.csv 是离线真值记录，不能直接当作在线策略状态。在线策略应使用 latest_observed，离线重放应按 visible_at_seconds 筛选。当前轮询基线不根据观测做决策，因此调整观测延迟不会改变其绑定和完成时刻。`scheduling_overhead_us` 仍是预留配置，未因本次观测功能而实现。

在途字节数保留整条消息的提交大小，直到 ACK 完成后移除；它不是链路队列积压或剩余在线字节数。包含节点内通信，并非真实 NIC 利用率或逐链路遥测。仿真截止时保留未完成请求、在途消息与其资源，不把它们清零伪装为完成。

可以离线重新绘制状态时间线：

```bash
./scripts/simai-python -c 'from joint_scheduling.observation.plots import plot_observation; plot_observation("joint_scheduling/artifacts/generated-run/observation")'
```

图文件同样拒绝覆盖；重新绘制前应选择新的结果目录或处理旧图文件。
