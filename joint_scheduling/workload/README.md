# 请求负载生成

## 公开轨迹导入与规模映射

`import-trace` 支持 Azure 的 `TIMESTAMP,ContextTokens,GeneratedTokens` CSV、Mooncake 的毫秒 JSONL，以及 FreeInference 原始会话 JSONL / JSONL.ZST。
保留请求长度配对、来源行与时间顺序；不会启用缓存、推断会话依赖、循环数据或截短长请求。

```bash
./inference import-trace --trace /absolute/path/AzureLLMInferenceTrace_conv.csv --format azure \
  --source-url https://github.com/Azure/AzurePublicDataset/blob/master/data/AzureLLMInferenceTrace_conv.csv \
  --target-rps 2 --profile /absolute/path/profile.json --output /absolute/path/replay.json
```

不提供 `--target-rps` 时使用原相对时间。提供后，以完整轨迹的半开观测区间为基准统一缩放时间；
最后一个请求仍严格处于结束时间之前。`provenance` 记录原文件 SHA-256、实际到达率、时间缩放倍数、
长度配对哈希及来源行顺序哈希。加速回放会改变绝对突发时长，不能等同于原时间回放。
输入本身是合成数据时用 `--source-synthetic` 声明。

`--duration-seconds` 只能显式选取缩放后时间前缀，并报告排除的请求数；超过数据长度会拒绝，
不会自动循环补足。`--sha256` 验证冻结来源。`--profile` 同时检查 Prefill 和最后一次 Decode 上下文，
越界时不写文件。请求 SLO 用 `--ttft-slo-seconds` / `--itl-slo-seconds` 明确指定，来源轨迹没有的目标不能当成原始业务承诺。

### FreeInference 适配

```bash
./inference import-trace \
  --trace data/freeinference/raw/2026-05-17.jsonl.zst --format freeinference \
  --sha256 f768c3a44d6c7eb02340adcb7bceaf5a451e20dc568d7a96d69f78bfb70e09a8 \
  --invalid-requests skip --request-prefix freeinference \
  --profile joint_scheduling/profiles/deepseek_v3_h800_tp1_ep16_ep16_160k_estimated.json \
  --ttft-slo-seconds 1 --itl-slo-seconds 0.1 \
  --output joint_scheduling/artifacts/freeinference/replay.json
```

FreeInference 的 `--trace` 可为单个原始文件，或仅含原始会话文件的目录；目录按相对路径排序、递归读取 `.jsonl` / `.jsonl.zst`。不读取 `processed/*.parquet`。Zstandard 依赖随 `backend/requirements.txt` 安装，仅在解压 `.zst` 时加载。文件按块解压、逐行解析，最终的请求与精简元数据仍保存在内存；大数据建议按周导入。

- 展开会话及工具调用中的嵌套子 agent；按请求自己的 `start_ms` 排序，以纳秒记录时间。长度取 `provider_tokens.prompt` / `completion`，不扣除 cached tokens。
- 默认 `--invalid-requests error`：遇到无效时间或非正/缺失长度时拒绝。显式 `skip` 才排除这些请求，所有排除项和原因写入 `replay.audit.json`；来源计数、原因汇总、`dropped_requests` 也进入负载 provenance。原始合法时间戳仍决定完整时间区间，排除请求不会悄悄挤掉时间间隔。
- 目录身份是排序后的相对路径、字节数、SHA-256 清单的确定性哈希；单文件身份仍为原文件字节 SHA-256。同一周重复出现的 session ID 会拒绝，避免重叠输入导致重复计数。
- `trace_metadata.requests[request_id]` 保留来源文件/行、会话、step、turn、父会话/工具位置、模型和原始到达时间。它不进入执行队列字段，也不把 source `end_ms` / TTFT 当成仿真服务时间。
- 不导入工具参数、文本或 `block_ids` 作为可执行内容/缓存命中。源块采用另一 tokenizer；当前以完整 provider token 数重放请求形状。
- FreeInference 默认同时输出审计；`--audit-output` 可指定位置。质量拒绝和 profile 越界也写审计，负载文件不会写出；两类输出均拒绝覆盖。

已提供固定版本的单分片输入、32 GPU 三策略连通性验证和 1 请求/秒对照规格，见 [样本及复现](../../data/freeinference/README.md)。163,840-token profile 是以原 4K H800 锚点外推的估算曲线，保留 `estimated_simulation_profile`。它覆盖当前样本，不代表所有 FreeInference 数据都已覆盖。现有 16K profile 不变。

当前为固定外部时间戳的请求重放，记录会话关系但不模拟 agent 因上一步完成、工具等待或用户等待产生的后续到达。闭环 agent 完成时间不在此适配范围内。

可用 `joint_scheduling.experiments.capacity.estimate_capacity` 做离线资源需求核算。
其参考率是计算需求与平均链路载荷给出的乐观起点，`sustainable_capacity_rps` 保留为 `null`；
必须用基线长时间回放和冻结的 SLO/积压判据另行进行仿真承载率评估。

`assess-calibration --plan ... --results-root ... --output ...` 只读取已经完成的基线结果，检查输入哈希、部署、
可选模块、测量长度和种子齐全性；缺失运行保留 pending。只有较低档通过、较高档拒绝且种子齐全时，
才给出该有限轨迹下的参考区间。它不会启动仿真，也不会把 1 秒结果当成长时间容量结论。

本模块生成供真实基线读取的请求 JSON，不生成 token 返回时间或模拟结果。所有生成负载明确标记为 synthetic；实际模型计算数据仍由独立的 compute profile 提供。

从 Inference 根目录执行：

```bash
./inference generate-workload --spec joint_scheduling/configs/workload_example.json --output joint_scheduling/artifacts/generated-workload.json
./inference --config joint_scheduling/configs/baseline_smoke.json run-baseline --workload joint_scheduling/artifacts/generated-workload.json --output joint_scheduling/artifacts/generated-run --plot
```

规格 JSON 使用 `schema_version: 1`，结构见 `configs/workload_example.json`：

| 字段 | 含义 |
| --- | --- |
| `seed` | 非负整数，小于 2147483647；CLI 的 `--seed` 可覆盖 |
| `arrival.pattern` | `periodic`、`poisson` 或 `burst` |
| `arrival.count` | 精确生成的请求数，可为 0，最多 100 万；与 duration_seconds 二选一 |
| `arrival.duration_seconds` | periodic/poisson 在固定时长内持续到达，区间为 [start,start+duration)，最多 100 万请求 |
| `arrival.start_seconds` | 到达过程起点，默认 0 |
| `arrival.rate_rps` | periodic/poisson 的到达率，必须为正 |
| `arrival.burst_size` | 每个突发的请求数；最后一组可不足 |
| `arrival.burst_interval_seconds` | 突发起点之间的间隔 |
| `arrival.spread_seconds` | 每组请求在该时间范围内均匀散布；默认 0，必须小于组间隔 |
| `classes` | 正权重的请求类别，联合指定输入、输出长度与可选 SLO |

periodic 的第一条请求在 start 到达；poisson 的第一条请求在 start 之后经过一次指数间隔到达。burst 不使用 rate_rps。例如把 arrival 替换为：

```json
{"pattern": "burst", "count": 12, "burst_size": 4, "burst_interval_seconds": 0.02, "spread_seconds": 0.001}
```

持续加压使用固定时长而非固定请求数，例如 `{"pattern":"periodic","duration_seconds":1.0,"rate_rps":150}`。扫描到达率时，各组持续投喂同样时长，请求数随负载变化；同一 seed 的对应请求仍使用相同长度和 SLO 随机流。时间先按原生时钟取纳秒，再排除右端点。

`sustained_pressure_workload.json` 使用 64/128/256 token 输出，`sustained_pressure_comparison_example.json` 扫描 10/40/200 Gbps、三种 KV 模式、25/75/150/300 请求每秒及两个 seed。每次持续到达 1 秒，预热 0.2 秒，在 [0.2,0.9) 秒测量，末尾仍有请求到达。需同时报告完成吞吐、达标吞吐、token 吞吐和未完成请求的积压，不能将截止时未完成请求从容量判断中隐去。

每个类别需要唯一 `name`、正 `weight`、`input_tokens` 和 `output_tokens`。长度可以是固定正整数，或以下分布：

```json
{"type": "uniform", "min": 64, "max": 512}
```

```json
{"type": "choice", "values": [2, 4, 8], "weights": [6, 3, 1]}
```

类别中的 `ttft_slo_seconds`、`itl_slo_seconds` 为可选正数，缺省时采用评估配置。类别允许关联长短请求与不同 SLO；权重表示抽样概率，不保证每个类别的精确数量。

类别还可配置有序 `prefix_blocks`，例如 `[{"id":"system-v1","tokens":64}]`；该类别生成的请求复制同一前缀身份。前缀长度不得超过任何生成请求的输入长度，同 ID 长度必须一致，错误负载不会写入输出。缓存查询和命中发生在运行时，不由生成器预先指定；详细格式见 [前缀缓存说明](../prefix_cache/README.md)。

到达过程与请求类别/长度使用独立随机流。同一 seed 下只改到达率，会保留对应请求的长度和 SLO，便于公平比较。输出记录完整规格、seed 和实际类别数量，到达时间精确到纳秒，结果拒绝覆盖。

文件中的 `output_tokens` 是仿真所需的实际请求长度，不应当作在线调度器预知的真实 EOS。生成器不检查目标模型的采样覆盖范围；执行器会检查输入和 Decode 上下文长度、TP 与 profile 是否匹配。

请求和类别可声明 `max_output_tokens` 作为调用方可见的输出上限，必须覆盖实际模拟的 output_tokens。绑定预测器仅读取上限，缺省使用配置的 reserve_max_output_tokens，不读取精确 EOS；其未来 Decode 范围还受 prediction_decode_steps 限制。
