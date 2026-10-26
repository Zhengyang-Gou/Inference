# 请求负载生成

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
| `arrival.count` | 精确生成的请求数，可为 0，最多 100 万 |
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

每个类别需要唯一 `name`、正 `weight`、`input_tokens` 和 `output_tokens`。长度可以是固定正整数，或以下分布：

```json
{"type": "uniform", "min": 64, "max": 512}
```

```json
{"type": "choice", "values": [2, 4, 8], "weights": [6, 3, 1]}
```

类别中的 `ttft_slo_seconds`、`itl_slo_seconds` 为可选正数，缺省时采用评估配置。类别允许关联长短请求与不同 SLO；权重表示抽样概率，不保证每个类别的精确数量。

到达过程与请求类别/长度使用独立随机流。同一 seed 下只改到达率，会保留对应请求的长度和 SLO，便于公平比较。输出记录完整规格、seed 和实际类别数量，到达时间精确到纳秒，结果拒绝覆盖。

文件中的 `output_tokens` 是仿真所需的实际请求长度，不应当作在线调度器预知的真实 EOS。生成器不检查目标模型的采样覆盖范围；执行器会检查输入和 Decode 上下文长度、TP 与 profile 是否匹配。
