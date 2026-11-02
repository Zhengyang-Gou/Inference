# Inference 的指标统计与离线绘图

本模块可独立使用，不需要 GPU 或 ns-3。统计与 CSV/JSON 导出仅使用 Python 标准库；绘图需要 `requirements-plot.txt` 中的 Matplotlib。`backend/runner.py` 的真实 ns-3 简单基线已在返回 token 和请求结束时调用 recorder；其他执行器也可沿用相同接口。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `recorder.py` | 按仿真时间写入原始 JSONL 请求、token 及辅助事件 |
| `metrics.py` | 校验事件生命周期，计算请求指标、分位数、SLO 与吞吐 |
| `pressure.py` | 持续负载下的积压、token 吞吐、队列积分及未完成请求的 SLO 上下界 |
| `export.py` | 导出可离线复核的完整结果目录 |
| `plots.py` | 只读导出数据，生成单次实验或多策略比较图 |
| `config.py` | `evaluation` 配置段与参数检查 |
| `demo.py` | 可重复的合成轨迹生成器，仅用于演示管线 |

## 运行

在 Inference 仓库根目录执行。先创建 Conda 环境 `simai`，再用统一入口生成和统计轨迹；本模块不要求第三方 Git 元数据或子模块：

```bash
./joint_scheduling/bootstrap.sh
./inference record-demo --output joint_scheduling/artifacts/demo/events.jsonl
./inference evaluate --trace joint_scheduling/artifacts/demo/events.jsonl --output joint_scheduling/artifacts/demo/result --plot
```

bootstrap 会在 `simai`（Python 3.12）中安装绘图和构建依赖。若只使用统计与 CSV/JSON 导出，也可直接以 Python 3.8+ 运行 `python3 -m joint_scheduling`，无需 Matplotlib、GPU 或 ns-3。示例轨迹及图例显式标记为 `synthetic`，其数据不能用于说明调度策略收益。轨迹文件、结果目录与同名图文件都拒绝覆盖。

仅重新绘图或比较两个已经统计完成的实验：

```bash
./inference plot --results joint_scheduling/artifacts/demo/result --output joint_scheduling/artifacts/demo/replot
./inference plot --results path/to/baseline/result path/to/joint/result --output joint_scheduling/artifacts/comparison
```

比较图要求测量窗口相同，策略与种子组合不重复。每次运行独立展示，不自动合并不同种子，也不生成未经估计的置信区间。统计 SLO 时使用每请求阈值；未提供时使用 evaluation 的默认值。

## 接入仿真器

```python
from joint_scheduling.evaluation import TraceRecorder

with TraceRecorder("new_events.jsonl", strategy="joint", seed=7,
                   metadata={"workload": "trace_name", "config": "experiment_name"}) as recorder:
    recorder.request_arrived("r0", 0.0, ttft_slo_seconds=0.2,
                             itl_slo_seconds=0.1, expected_output_tokens=3)
    recorder.token_returned("r0", 0.10, token_index=1)  # P 返回首 token
    recorder.token_returned("r0", 0.18, token_index=2)  # D 返回第二个 token
    recorder.token_returned("r0", 0.23, token_index=3)
    recorder.request_completed("r0", 0.23)
    recorder.finish(10.0)  # 实际观测到的仿真时间，包括空闲时间
```

时间单位始终是秒，token 编号从 1 开始且必须连续。记录用户可见的返回时间，不能用预测时间或内部计算完成时间代替。`expected_output_tokens` 是已知的精确输出数，可省略；它不是最大输出 token 上限，提前 EOS 的工作负载不要把上限传进来。

失败/取消请求用 `request_failed(request_id, at_seconds, reason)`。待完成请求保留为 incomplete。上下文退出只关闭文件，不会凭空生成完成或观测终止事件；缺少 `run_end` 的截断轨迹不能被当作完整实验统计。

辅助事件可用 `record_event("layer_ready", time, request_id="r0", layer_id=0)` 等形式记录，供后续传输与资源分析复用。当前指标由请求和 token 生命周期推导，辅助事件不会改变 SLO。

## 指标口径

设请求到达时间为 a，第 j 个返回 token 的时间为 t_j，完成时间为 c：

| 指标 | 定义 |
| --- | --- |
| TTFT | t_1 - a，包含窗口等待、派发和排队 |
| handoff | t_2 - t_1，交接间隔 |
| later_max_itl | max(t_j - t_(j-1))，j >= 3 |
| max_itl | 所有相邻 token 间隔的最大值，包含 handoff |
| mean_tpot | (t_N - t_1) / (N-1)，仅作为辅助平均值 |
| E2E | c - a |
| SLO | TTFT 不超过 TTFT 阈值，handoff 与 later_max_itl 分别不超过同一个 ITL 阈值 |

一个 token 的请求没有任何 ITL；两个 token 的请求只有 handoff。缺少的间隔在 JSON 中是 null、CSV 中为空，不按 0 进入分位数。阈值比较包含极小浮点容差（相对 1e-9，绝对 1e-12 秒），统计分位数使用 q*(n-1) 的线性插值。

### 测量窗口与未完成请求

- 所有窗口采用半开区间 `[start, end)`，结束时间必须不超过 `run_end`。
- `completion_window`：窗口内实际完成的全部请求，包含窗口开始之前到达的 carry-in 请求。完成吞吐 = 完成数 / 窗口长度；达标完成吞吐 = 完成且所有适用 SLO 达标的请求数 / 窗口长度。
- `completion_window.slo_attainment` 的分母仅是窗口内完成的请求数；没有完成时为 null，吞吐为 0。
- `arrival_cohort`：窗口内到达的全部请求；统计直到观测终止时的 completed、failed 和 incomplete。排空阶段在窗口之后完成的请求属于该 cohort，但不增加窗口内完成吞吐。
- `completed_and_slo_fraction` 用 cohort 全部到达数作分母，保留失败与未完成的影响；存在未完成请求时，它不是最终达标率。
- `slo_attainment_among_completed` 只对该 cohort 已完成的请求计算，必须与 incomplete 数一起解释。
- 延迟分布与分位数使用 arrival cohort 中已完成的请求，避免把部分输出请求的“当前最大 ITL”误当成最终值。completed-only 延迟本身可能有选择偏差，summary 会显式保留未完成数。
- 时间序列按完成事件分桶，最后一个不完整时间桶使用其实际宽度计算速率。

因此图中的延迟总体与 completion-window 吞吐总体可能不同；图标题和 summary 均明确标注，不能直接混用。

## 导出内容

`run-baseline` 默认保留必要请求事件及每个 token 返回时间；高频辅助事件只在 `recording.mode="debug"` 时保留。两种模式使用同一套 SLO 和吞吐统计，规则见 [实验记录规则](../RECORDING.md)。

下面是独立 `evaluate` 的自包含导出。`run-baseline` 的 `result/` 不再复制 `events.jsonl`，其 summary 中 `raw_trace_file` 指向运行根目录的 `../events.jsonl`。默认运行还在 summary 的 `pressure` 中保存积压、token 吞吐和含未完成请求的 SLO 上下界。

```text
result/
├── events.jsonl           # 原始轨迹，包含辅助事件和 run 元信息
├── config_snapshot.json  # 实验配置快照（CLI）或 evaluation 配置（直接 API）
├── summary.json          # 指标、分位数、时间窗口、单位和样本数量
├── requests.csv          # 各请求状态、延迟、SLO 判定和所属总体
├── tokens.csv            # 每个 token 的返回时间、间隔和阶段
├── timeseries.csv        # 每个时间桶的实际完成数、达标数和速率
└── figures/              # evaluate --plot 时生成
```

图表包括 TTFT/交接/后续最大 ITL/E2E 的 CDF 与分位数、完成窗口 SLO 达标率与吞吐对比、完成吞吐时间序列，格式由 `plot_formats` 配置为 PNG/PDF/SVG。

## 配置

`evaluation` 段配置测量起止时间、时间桶、默认 SLO、分位数、输出目录、图表和格式。输出目录相对于配置文件解析；CLI 显式传入的路径相对于当前工作目录。为兼容已有五模块配置，省略整个 evaluation 段时采用默认值。

测试：

```bash
./scripts/simai-python -m unittest discover -s joint_scheduling/evaluation/tests -v
```

没有 Matplotlib 时只跳过绘图测试，统计、导出和 CLI 测试仍会运行。

真实网络基线的依赖来源与完整性由根目录 `third_party/manifest.json`、`third_party/ns3/SOURCE_MANIFEST.json` 及后端内容锁记录。评估模块只消费明确记录的事件与配置，不从上游 Git 仓库读取指标；保留这些清单可追溯运行所用网络源码。

持续加压时，`summarize_pressure` 以 [start,end) 统计到达/完成/token，并在两端事件发生前取积压，验证“积压变化 = 到达 − 完成 − 失败”。到达 cohort 包括未完成请求：已观察到的 TTFT/ITL 超限和截止时仍在延迟的返回可判定失败，其他未完成结果保持未知，不推断为成功。`summarize_pressure_states` 对事件保持的队列和在途量积分，避免按高频事件条数求均值引入偏差。有限窗口的队列增长和达标率只能用于扫描负载范围，不能证明无限时长稳定性。
