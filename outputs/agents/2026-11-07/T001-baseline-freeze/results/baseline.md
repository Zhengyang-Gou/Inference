# T001 第一阶段唯一基线条件表（静态冻结，非仿真验收）

- 任务：`outputs/agents/2026-11-07/T001-baseline-freeze/`
- 冻结来源：`joint_scheduling/configs/research/base.json`（唯一配置来源）、`data/azure/conv_original.json`、`joint_scheduling/profiles/deepseek_v3_h800_tp1_ep16_ep16_estimated.json`
- 冻结产物：`results/frozen/config.json`、`compute_profile.json`、`workload.json`、`binding_plan.json`、`manifest.json`
- 本轮性质：**条件核对、冻结与静态预检**。没有运行 `run-baseline`、后端 smoke、`run-experiments`、完整 `check.sh`，没有构建后端，没有算法对比、策略矩阵或扩容实验。下表所有数值来自静态文件与项目自身的加载/校验代码，不是测量结果。

## 1. 冻结文件与身份

| 角色 | 相对仓库根路径 | SHA256 |
| --- | --- | --- |
| 原 base 配置 | `joint_scheduling/configs/research/base.json` | `1c645b97768ec120e5c47229eb114f63e4a1fbed5f2d9fc2625583f10e23d0bd` |
| 原始负载 | `data/azure/conv_original.json` | `0ce0c8cdc4b31d07587014debf7272209d4bd6efb23f03b84ff17fce3282c346` |
| 原始 profile | `joint_scheduling/profiles/deepseek_v3_h800_tp1_ep16_ep16_estimated.json` | `374595adf46d7b65bf34eb47e693b41a06dddb9fb1890f093afb013f66f93554` |
| Azure manifest | `data/azure/manifest.json` | `fd065c6c899fb1424bf07855face6782b86830c15a9270d8cdc02381d29bd196` |
| 冻结配置 | `outputs/.../results/frozen/config.json` | `565572ab4d2dde71bd265bfc8a240f7b433bf5987886505c231ea9a8f049a8f1` |
| 冻结 profile | `outputs/.../results/frozen/compute_profile.json` | `374595adf46d7b65bf34eb47e693b41a06dddb9fb1890f093afb013f66f93554` |
| 冻结负载 | `outputs/.../results/frozen/workload.json` | `0ce0c8cdc4b31d07587014debf7272209d4bd6efb23f03b84ff17fce3282c346` |
| 冻结绑定计划 | `outputs/.../results/frozen/binding_plan.json` | `9a48e35f53a4e8df2c9c09312a1fbf402bf8e79c2d47f083ba12e9416d37b091` |

`compute_profile.json` 与 `workload.json` 为**逐字节复制**，哈希与原件相同；负载哈希与 `data/azure/manifest.json` 中 `prepared[0].sha256` 一致。冻结 `config.json` 的 `feedback.compute_profile_path`、`runtime.workload_path`、`evaluation.output_dir` 均改为相对冻结配置目录的路径（`compute_profile.json`、`workload.json`、`evaluation`），不引用旧任务目录或正式输入目录。

Git HEAD：`91cf1e2a16b8ff59b595619a7d0c02c4dd5df8dc`；执行前脏状态 71 条（`logs/baseline-status.txt`），属他人已有修改，本轮保留、未回退、未提交。执行前后 `git status --short --untracked-files=all` 逐行一致。

## 2. 配置条件（冻结 `config.json` 实际值）

### 规模与部署

| 项 | 值 |
| --- | --- |
| 节点与 GPU | `num_leaves=2`、`num_spines=4`、`nodes_per_leaf=2`、`gpus_per_node=8` → 4 节点 × 8 GPU = **32 GPU** |
| P/D | `placements` 共 32 条：`p0–p15`（prefill，GPU 0–7、16–23）、`d0–d15`（decode，GPU 8–15、24–31）→ **P16 / D16，TP1** |
| EP 组 | `expert_parallel.enabled=true`，`groups` 为两个各含 16 个实例的组（P 组、D 组），`rank_unit="gpu"` → P/D 各一个 EP16 组；Attention DP16 |
| 显存 | `gpu_memory_gib=80.0`；`reservations.reserve_max_output_tokens=2048`、`capacity_safety_fraction=0.05`、`shared_prefix_accounting="physical_object"`、`source_release="after_dependencies"` |
| placement | 完整沿用 base.json 的 32 条映射，未改动 |

### 网络

| 项 | 值 |
| --- | --- |
| 后端 | `feedback.time_authority="ns3"`、`network_backend="ns3_alibabacloud"`（包级 ns-3，默认且唯一） |
| 拓扑 | `routing="ecmp"`，2 Leaf / 4 Spine |
| NIC | `nic_model="per_gpu"`、`nic_bandwidth_gbps=3200`（节点合计 3.2 Tbps，8 GPU 均分 → **每 GPU 400 Gbps 独立 RDMA 端点**） |
| 上联 | `uplink_bandwidth_gbps=1600`（单 Leaf 上联 1.6 Tbps；4 Spine 逻辑聚合 6.4 Tbps） |
| 链路延迟 | `link_latency_us=1.0` |
| 节点内 | `intra_node_model="gpu_ports"`、`intra_node_bandwidth_gbps=1280`（= 每 GPU TX / RX 端口各 **160 GB/s**）、`intra_node_latency_us=0.5` |
| 拥塞与包 | `congestion_control="dcqcn"`、`packet_payload_bytes=8000`、`switch_buffer_bytes=16777216`（16 MiB）、`rpc_timeout_seconds=60.0` |
| 优先级 | `runtime.network_priority_enabled=false`（关闭原生数据优先出队） |

### Prefill / Decode

| 项 | 值 |
| --- | --- |
| Prefill | `runtime.prefill_chunk_tokens=512`（chunk 上限）、`prefill_batch_token_budget=2048`、`prefill_batch_size=16` |
| Decode | `runtime.decode_batch_size=64`；profile 为 **显式 61 层**，前 3 Dense / 后 58 MoE，逐层非专家计算与对应 EP 交错，全部层完成才返回 token；不是旧 aggregate fixture |
| KV 模式 | `runtime.kv_transfer_mode="layer_ready_immediate"`：每层完成后立即提交该层 KV 块并继续计算，**不是整份 KV 等到 Prefill 结束才发**，也不启用 FIFO/MLU 门控 |

### 固定绑定

| 项 | 值 |
| --- | --- |
| 策略 | `binding.policy="balanced_random"`、`binding.seed=7`；`window_enabled=false`、`prediction_enabled=false`、`joint_selection_enabled=false`、`decision_overhead_us=0` |
| 规则 | P 按 `(arrival_seconds, request_id)` 顺序轮询 16 个 P；D 每完整一轮用独立 `Random(7)` 重洗 16 个 D 实例，末轮可不完整；只读取请求身份/顺序、placement 和绑定种子，不读取长度、EOS、运行状态或网络种子 |
| 计划 | `binding_plan.json` 含全部 **19366** 条绑定，`bindings_sha256=6fd36888a9e6529550efbd870af8099478ec608a5a6b0250bebf4e13f08d2aa5`，P/D 分配计数各合计 19366，跨 Leaf 请求 9663（49.8967%） |
| 复用 | 后续所有策略共用这一份计划；已用替换 `kv_scheduler.policy` / `runtime.kv_transfer_mode` / `runtime.seed` 的配置重新生成，计划哈希不变 |

### 请求负载

| 项 | 值 |
| --- | --- |
| 来源 | `data/azure/conv_original.json`，完整 Azure 2023 conversation 输入；`provenance.synthetic=false`、`source_synthetic=false` |
| 规模 | **19366** 请求，保留请求 ID、配对长度、源顺序和原始相对到达时间；未筛选、截短、循环、压缩时间或改变到达率 |
| 时间 | 到达 `0.0 – 3501.721937 s`；`manifest.duration_seconds=3501.721937001 s`；平均约 5.530421989 请求/秒 |
| 长度 | 输入 `2 – 14050` token，输出 `7 – 1000` token，最大 Decode 上下文 `14088` token |
| 完整性 | 重复 ID 0；请求字段只有 `request_id / arrival_seconds / input_tokens / output_tokens` 四项；无 `prefix_blocks`、无 `max_output_tokens` |
| 处理 | 本轮**不生成新压力样本**；冻结完整输入不等于获准全量运行（约 58 分钟仿真时间）；有界功能样本需后续任务明确规则；Azure 2024 不在范围 |

### SLO 与统计窗口

| 项 | 值 |
| --- | --- |
| SLO | `evaluation.default_ttft_slo_seconds=1.0`、`default_itl_slo_seconds=0.1`；**人工实验假设**，Azure trace 不提供业务 SLO（`provenance.slo_origin="not supplied by source"`） |
| 请求级覆盖 | 19366 个请求中 `ttft_slo_seconds` 覆盖 0 个、`itl_slo_seconds` 覆盖 0 个 → 全部使用默认值，无字段与规定冲突 |
| 随机性 | `runtime.seed=7`（与 `binding.seed=7` 独立） |
| 预热 | `evaluation.window_start_seconds=60.0`，即 0–60 s 为预热 |
| 测量窗口 | `evaluation.window_end_seconds=3501.721937001`；窗口为**半开区间 `[60.0, 3501.721937001)`**，长度 3441.721937001 s |
| 排空 | `runtime.time_limit_seconds=4101.721937001`，即窗口结束后还有 600.0 s 排空时限 |
| 分桶 / 分位数 | `time_bin_seconds=1.0`；`percentiles=[0.5, 0.95, 0.99]` |
| 本轮约束 | 不调窗口找收益 |

### 观测与记录

| 项 | 值 |
| --- | --- |
| 网络观测 | `feedback.network_observation_enabled=true`、`network_observation_interval_us=1000`（**1 ms**）、`observation_delay_us=100`（**100 μs** 可见延迟） |
| 状态观测 | `feedback.state_observation_enabled=true`（本轮由 base 的 `false` 改为 `true`，属任务指定的观测开关），用于后续排队与资源分析 |
| 记录 | `recording.mode="essential"`：保留请求/token 轨迹、指标与资源汇总；高频状态、网络采样明细仅 debug 写盘 |
| 边界 | 观测**不是优化策略**；记录模式不改变在线调度与仿真时钟；状态/网络采样在 `observation_delay_us` 后可见 |

### 模拟调度开销

- `feedback.scheduling_overhead_us=10.0` 是**仿真配置假设**，不是任何实测 CPU 耗时。
- `runtime.kv_transfer_mode="layer_ready_immediate"` 属于立即发送路径，**不经过受控 KV 决策**，因此该 10 μs 在基础验收模式下**不产生实际延迟**，不能写成已生效的调度开销。
- `binding.decision_overhead_us=0`，且绑定窗口/联合选择均关闭，不产生派发等待。
- 真实 CPU 决策耗时、配置中的仿真延迟、模拟请求时延是三件不同的事，后续若测量须分别报告。

## 3. 基础验收开关（冻结配置实际值）

| 字段路径 | 冻结值 | 基础模式下 |
| --- | --- | --- |
| `binding.window_enabled` | `false` | 关闭 |
| `binding.prediction_enabled` | `false` | 关闭 |
| `binding.joint_selection_enabled` | `false` | 关闭 |
| `binding.decision_overhead_us` | `0` | 无派发开销 |
| `prefix_cache.enabled` | `false` | 关闭（其余缓存字段不生效） |
| `runtime.kv_transfer_mode` | `layer_ready_immediate` | **生效**：逐层/逐 chunk KV 生产，就绪即发 |
| `kv_scheduler.policy` | `ready_immediate` | **生效**：选择无 FIFO/MLU 门控的路径 |
| `kv_scheduler.slo_priority_enabled` | `false` | 关闭 |
| `kv_scheduler.collective_protection_enabled` | `false` | 关闭 |
| `kv_scheduler.decode_admission_limit` | `0` | 关闭（base 无此字段，冻结时显式置 0） |
| `runtime.network_priority_enabled` | `false` | 关闭 |

**生效且保留**：逐层/逐 chunk KV 生产与就绪即发、`kv_scheduler.max_block_bytes=1048576`（层/rank/offset 分块上限）、EP 计算与 dispatch/combine、显存容量准入与物理对象账本、源端读 pin 与通信完成依赖、请求/token 轨迹与 SLO 评估、上述观测与记录开关、评估窗口与 SLO 阈值。

**存在但在无门控模式下不生效**：`max_concurrent_transfers`、`max_inflight_bytes`、`max_resource_inflight_bytes`、`safety_margin_fraction`、`control_interval_us`、`collective_guard_us`、`collective_reserve_fraction`、`pause_on_collective`、`queue_target_bytes`、`inflight_budget_mode`、`collective_max_blocking_us`、`completion_feedback_alpha`、`mlu_*`、`slo_priority_enabled`、`collective_protection_enabled`、`decode_admission_limit`、`feedback.scheduling_overhead_us`；以及 `binding.*` 中窗口/候选/预测预算参数、`prefix_cache.*` 全部参数。集合通信流量本身仍在同一 ns-3 网络中真实执行，只是没有应用层保护与优先级门控。

未改动：EP 路由与逐层执行、Chunked/合批 Prefill、连续 Decode、容量模型、完成依赖。不同策略后续仅可改变明确指定的策略项；本轮未创建或运行任何策略矩阵。

## 4. 指标口径（`joint_scheduling/evaluation/README.md`）

- **主指标**：测量窗口 `[start, end)` 内「完成且所有适用 SLO 通过」的请求数 ÷ 窗口长度 = **达标完成吞吐**。不得用「已完成请求中的达标率」代替主指标。
- 指标定义（a = 到达，t_j = 第 j 个 token 返回时刻，c = 完成时刻）：TTFT = `t1 - a`（包含窗口等待、派发、排队）；handoff = `t2 - t1`；later_max_itl = `max(t_j - t_{j-1}), j>=3`；max_itl 含 handoff；E2E = `c - a`。
- SLO 判定：TTFT ≤ TTFT 阈值；handoff 与 later_max_itl **分别** ≤ 同一个 ITL 阈值。单 token 请求没有任何 ITL；两个 token 只有 handoff。缺失间隔在 JSON 为 `null`、CSV 为空，**不补 0**、不进入分位数。
- **completion_window**：窗口内实际完成的全部请求，包含窗口前到达的 carry-in；完成吞吐 = 完成数 / 窗口长度；`completion_window.slo_attainment` 分母仅为窗口内完成数，没有完成时为 `null`。
- **arrival_cohort**：窗口内到达的全部请求；统计到观测终止时的 completed / failed / incomplete。排空阶段完成的请求属于该 cohort，但**不增加窗口内完成吞吐**。
- `completed_and_slo_fraction` 以 cohort 全部到达数作分母，保留失败与未完成的影响；存在未完成时不是最终达标率。`slo_attainment_among_completed` 只看已完成请求，必须与 incomplete 数一起解释。
- 延迟分布/分位数使用 cohort 中已完成请求，可能有选择偏差；时间序列按完成事件分桶，末个不完整桶按实际宽度算速率。
- 失败/取消请求记为 failed，未完成保留 incomplete，均**不当成完成样本**；缺少 `run_end` 的截断轨迹不能当完整实验统计。

## 5. 模型与 profile 边界

| 项 | 值 / 说明 |
| --- | --- |
| profile | `estimated_simulation_profile`，DeepSeek-V3 / NVIDIA H800，`provenance.synthetic=true`（公开可归因的离线算子锚点估算），**非实机部署保证** |
| 结构 | schema 2，TP1；61 层，decode 前 3 Dense / 后 58 MoE；`kv_bytes_per_token=70272`；`weights_bytes_per_gpu=59748544176`（另有 by_phase）；`network_communication_excluded=true` |
| 曲线覆盖 | prefill 61 层各 15 个点覆盖 `1–16384` token；chunked groups 覆盖 `1,2,...,512`；linear_points 覆盖到 `2048`；decode points 覆盖 `1–16384`，batch 曲线覆盖 `1,2,4,8,16,32,64` |
| 静态覆盖检查 | `validate_prefill_batch_coverage(14050, 512, 2048, 1)`、`lookup_decode_batch(64 × 14088)`、`lookup_decode_batch(1 × 14088)`、`expert_profile.validate_profile(冻结配置, 冻结 profile)` 全部通过，无外推错误 |
| 路由 | 确定性合成 `layerwise_periodic`（`routing_seed=7`，256 experts，top_k=8），**没有真实专家激活 trace**；本地多专家输出合并额外成本显式为 0；无双微批重叠、无 DeepEP 节点聚合 |
| 数据缺失 | Azure trace 不提供 prompt 文本、业务 SLO、DeepSeek-V3 专家路由；2024 尚未下载审计 |
| 执行环境 | CPU 上的离散事件仿真；计算时间来自离线估算曲线，通信由 ns-3 分组网络单独给出 |

## 6. 从 base.json 到冻结 config 的实际差异（共 9 项）

| 字段 | base | 冻结 | 类别 |
| --- | --- | --- | --- |
| `kv_scheduler.policy` | `slo_mlu` | `ready_immediate` | 基础开关 |
| `kv_scheduler.slo_priority_enabled` | `true` | `false` | 基础开关 |
| `kv_scheduler.collective_protection_enabled` | `true` | `false` | 基础开关 |
| `kv_scheduler.decode_admission_limit` | 字段缺失（默认 0） | `0` | 基础开关（显式化） |
| `runtime.kv_transfer_mode` | `layer_ready_slo` | `layer_ready_immediate` | 基础开关 |
| `feedback.state_observation_enabled` | `false` | `true` | 观测 |
| `feedback.compute_profile_path` | `../../profiles/...json` | `compute_profile.json` | 可搬移路径 |
| `runtime.workload_path` | `../../../data/azure/conv_original.json` | `workload.json` | 可搬移路径 |
| `evaluation.output_dir` | `../../artifacts/evaluation` | `evaluation` | 可搬移路径 |

另有 22 个开关在 base 中已符合任务要求，冻结时显式核对并保持原值（详见 `manifest.json` 的 `explicitly_verified_unchanged_switches`），未产生额外差异。机器可读的差异集合与哈希见 `manifest.json`；`results/verification.json` 记录了 47 项静态一致性核对。

## 7. 本轮结论边界

- 已证明：冻结配置可由项目 CLI 加载并通过结构/约束校验；profile 与 workload 逐字节保持来源身份；19366 请求的输入范围、SLO 覆盖、曲线覆盖与绑定完整性/确定性均静态核对通过；绑定不随 KV 策略或网络 seed 变化。
- **未证明**：任何端到端链路可运行、任何策略效果、SLO 达标吞吐数值、网络利用率、调度开销实测值、源码/二进制运行归档。冻结完整输入**不代表**获准启动约 58 分钟仿真时间的全量包级运行。
- 环境说明：`./inference` 包装入口需要 Linux Conda `simai` 环境，本会话通过 Windows UNC 访问仓库且 `wsl.exe` 被安全策略禁止，故该包装入口返回 2（`Conda is unavailable`），已如实记录；改用同一 CLI 模块入口 `python -m joint_scheduling` 与项目自身 Python 模块完成静态校验（本机 Python 3.13.14，项目 CLI 的纯 Python 部分兼容 3.8+）。正式运行须在 WSL 的 `simai` 环境中执行。
