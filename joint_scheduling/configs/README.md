# 配置用途

主研究统一从 [research/README.md](research/README.md) 进入。旧配置保持原路径，避免破坏测试和复现；它们不自动成为主研究实验。

| 配置范围 | 用途与边界 |
| --- | --- |
| `research/base.json`、`research/kv_policy.json`、`research/network_priority.json` | 32 GPU、P16/D16、TP1、两侧 DP16/EP16；固定 Azure 回归输入与 DeepSeek-V3 估算 profile 的 EP/KV 研究主线；策略对照与网络优先级消融分开 |
| `research/base_160gpu.json`、`research/*_160gpu.json` | 原 P32/D128 布局参考；早期命名的 `kv_step1_160gpu*` 等规格仍显式使用该布局 |
| `research/base_32gpu.json`、`research/base_48gpu.json` 及对应规格 | 显式规模入口；32 GPU 与当前主布局一致，48 GPU 为 P16/D32 对照 |
| `research/validation.json` | 同模型/拓扑的短程集成检查，仅 1 个真实来源请求，不评估负载压力 |
| `research/contention_diagnostic.json` | 四个真实 Azure 请求的端到端 + both/EP/KV 固定流量重放；只归因网络干扰，不作持续负载结论 |
| `research/freeinference_validation.json`、`research/freeinference_kv_policy_1rps.json` | 32 GPU、P16/D16、163840-token 估算 profile；FreeInference 单请求连通性及明确缩放到 1 rps 的 1192 请求对照；来源与排除规则见 [输入说明](../../data/freeinference/README.md) |
| `default.json`、`baseline_smoke.json`、`baseline_all_off.json`、`layered_smoke.json` | 基础执行和接口功能示例，不是大模型部署参数 |
| `ep_smoke.json`、`ep_mlu_smoke.json`、`ep_mlu_pressure.json`、`ep_mlu*_comparison.json` | 小型 synthetic EP/KV 机制和压力检查，不能据此判断 Azure 或主研究集群的竞争强度 |
| `composed_execution_*.json`、`prefix_cache_*.json` | 小型 synthetic Dense 组合功能检查；覆盖前缀复用、P 分块/合批、D 连续批处理与联合选择 |
| `binding_*.json`、`dynamic_window_*.json`、`joint_*.json` | Dense 绑定预测、窗口与联合选择的独立实验；本次 EP/KV 主线关闭这些功能 |
| `adaptive_*.json`、`kv_comparison_example.json` | 小型 synthetic KV 立即/FIFO/SLO 放行示例 |
| `experiments_example.json`、`workload_example.json` | 实验矩阵与负载生成接口示例 |
| `sustained_pressure_*.json` | 固定 synthetic profile 下的到达率/带宽扫描；只能支持对应假设下的结论 |
| `cluster_128gpu_*.json` | 历史拓扑/模型探索模板；说明见 [CLUSTER_128GPU.md](CLUSTER_128GPU.md)，正式配对实验使用 `research/` |

`backend/fixtures/` 中的小 token、少层模型保留用于回归测试。扩大 GPU 数、重命名模型或提高到达率，不会把这些 fixture 转换成真实模型 profile。主线关闭的功能保留实现和测试，当前没有删除或重写运行时模块。
