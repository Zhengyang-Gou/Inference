# 实验记录保留规则

默认使用 `recording.mode="essential"`。旧配置省略 `recording` 时也采用这个默认值。

```json
"recording": {"mode": "essential"}
```

## 默认保留

| 记录 | 用途 |
| --- | --- |
| `resolved_config.json`、`workload.json`、`compute_profile.json` | 固定实际配置、种子、负载与计算性能输入 |
| `config.json`、`reproducibility.json`、`runtime_components.json` | 可移动复跑的配置、输入/源码/后端身份及组件状态所有者 |
| `source/`，或 suite 的 `inputs/source/` | 未提交源码的固定归档与清单，独立于 essential/debug；对照实验共用一次归档 |
| `network/network.topo`、`backend_metadata.json`、`backend.log` | 保留网络拓扑、构建指纹及后端错误诊断 |
| `network/backend_rpc_summary.json` | 后端命令数与同步交换批次数，区分执行器开销和模型中的通信工作量 |
| 根目录 `events.jsonl` | 每个请求的到达、全部 token 返回、完成/失败，以及绑定、Prefill 开始、KV 就绪和首次 Decode 开始等关键时刻 |
| `result/summary.json` | SLO、TTFT、交接、ITL、吞吐、失败/未完成数，以及积压和达标率上下界 |
| `result/requests.csv`、`tokens.csv`、`timeseries.csv` | 按请求和 token 核对结果、重新绘制指标图 |
| `observation/summary.json`、`instances.csv` | 完整运行的利用率、队列、通信、显存汇总；`measurement_window` 保存配置测量窗口内的队列/在途量平均值和峰值 |
| `observation/network_summary.json` | 动态采样数量、最后采样时刻、各端口累计计数和采样峰值；不含逐流历史 |
| 缓存、KV 调度、绑定汇总与绑定开销计时 | 解释命中、阻塞、暂缓和控制成本；按相关模块开关输出 |
| `final_resources.json` | 核对最终显存占用与预留，保留截止时未完成请求的状态 |
| 实验矩阵的计划、输入、对照表、失败记录 | 保留成功、失败和跳过的每次运行；失败不当作零值成功 |

请求级失败原因始终进入轨迹。矩阵执行异常仍写入 `error.json`；被中断的轨迹缺少 `run_end` 时不能按完整实验统计。

`run-baseline` 只保存一份原始事件文件。`result/summary.json` 的 `raw_trace_file` 指向 `../events.jsonl`；移动结果时保留整个运行目录。独立 `evaluate` 导出仍复制输入轨迹，便于单独交付结果包。

队列和在途量仍逐事件在线积分，默认不将全部事件状态写盘。因此配置窗口的平均值和峰值保留精确口径，但不能在事后任意重新划分状态统计窗口；需要重划窗口或绘制完整状态时间线时使用详细模式。请求/token 原始时刻始终保留，可以重新计算请求指标。

## 需要排查时开启详细模式

```json
"recording": {"mode": "debug"}
```

详细模式额外保留：

- 每层计算、每条传输、资源变化、候选评分和 KV 放行明细事件。
- `observation/state.jsonl`、`state.csv` 和结束时的延迟可见快照。
- `observation/network_samples.jsonl` 的逐次端口/流/ACK 快照。
- 配合 `--plot` 生成状态时间线；两种模式都可生成请求指标图。

详细模式会随事件数量和在途传输数量产生大量文件，适合短时、小规模排查。它仍遵守各观测功能开关：关闭状态观测时不产生状态文件，关闭动态网络观测时不产生网络采样文件。

## 观测与写盘分开

`recording.mode` 只控制记录保留方式，不改变到达、调度、传输、网络采样周期和可见延迟。`feedback.network_observation_enabled=false` 会改变受控 KV 的输入，不应当用作省日志开关。

必要模式与详细模式的原始墙钟耗时可能不同；仿真时间仍使用显式开销配置。`test_recording.py` 用相同输入对照两种模式，核对绑定、token 时间、SLO、缓存、最终资源和状态积分，并检查截止时的未完成请求。

源码归档有固定存储成本，小型 smoke 中可能比事件记录更大。体积核对分别报告共用复现材料和各 case 记录目录，不将源码大小解释为日志压缩效果；`test_recording.py` 按批量实验相同的共享归档规则执行，仍要求 essential 的 case 记录体积小于 debug 的五分之一，并验证两组引用同一源码。独立运行若自带完整源码归档，不承诺整个目录也缩小五倍。
