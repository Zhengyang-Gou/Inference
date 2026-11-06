# Inference

Inference 是面向大模型 Prefill–Decode 分离推理的调度研究仓库，研究 P/D 实例联合绑定、KV 传输与集合通信竞争，以及请求级 SLO 达标完成吞吐。

**本仓库只做仿真。** 请求、计算、容量和通信事件均在 CPU 上推进；H800 等设备名称用于标识离线计算代价，节点与网络由显式参数建模。

当前主研究入口是 [DeepSeek-V3 的 EP/KV 对照](joint_scheduling/configs/research/README.md)：32 GPU、P:D＝16:16、TP1，两侧均为 Attention DP16 / Expert EP16，比较 Immediate / FIFO / MLU。研究输入范围限定为 Azure 2023/2024；[trace 调研](joint_scheduling/workload/TRACE_RESEARCH.md)保留为历史候选记录；160 GPU 布局保留为显式参考。计算使用 **公开数据推导的仿真计算 profile**，网络由 ns-3 分组模型执行，研究范围限定为离散事件仿真。该入口关闭绑定预测、联合选择和前缀缓存，相关功能与测试仍保留。其他配置的用途见 [配置目录](joint_scheduling/configs/README.md)，整理前快照与复现方法见 [冻结基线](joint_scheduling/RESEARCH_BASELINE.md)。

运行时组件、允许的功能组合、输入/源码归档和两类实验的边界，统一见 [研究系统说明](joint_scheduling/RESEARCH_SYSTEM.md)。

研究负载仅保留 [Azure 2023/2024](data/azure/README.md)：2023 原始文件及完整对话输入已保留，2024 尚待下载与覆盖审计；已移除其他研究负载和固定派生压力样本。通用导入器、模型曲线与基础功能测试保留。

请求负载、模型、节点/网络拓扑、P/D 比例、TP/EP、带宽、SLO 和实验矩阵的当前口径见 [主研究配置](joint_scheduling/configs/research/README.md)及对应 JSON 配置。

当前可运行的是 **CPU 上的 ns-3 分组网络仿真基线**，支持整份 KV、Prefill 后分块、逐层立即，以及动态观测下的集合通信保护和 SLO/FIFO KV 放行模式。已实现显式逐层计算/通信依赖、物理对象容量账本、统一时间与网络反馈、统计和绘图；已实现按共享资源补充带宽令牌、在途额度、集合通信保护及请求级 SLO 余量排序；**已实现可选的候选剪枝＋增量贪心联合绑定**。逐层模式需要显式 schema 2 计算性能文件，示例为 synthetic，使用显式仿真代价。

这是独立维护的 Inference 项目。第三方源码以普通文件保存在 `third_party/`，构建不依赖嵌套 Git 仓库或 Git 子模块。第三方代码的来源、版本和各组件许可证路径见 [组件清单](third_party/manifest.json)。

## 开发进度

以下按 11 个功能模块记录当前开发进度。“已实现”指当前简单基线范围；`binding` 内已实现窗口、完整预测器和有界联合选择；性能目标与算法收益需要通过实验验证。

2026-10-29 已清理历史实验输出、验证报告和基于旧结果的评审记录，保留实现、测试、输入样例、实验配置及后端构建。后续从小规模基线逐步重新验证，当前不以旧实验数值作为性能依据。

| 模块 | 职责 | 当前状态 |
| --- | --- | --- |
| **1. 拓扑与部署 `topology`** | 描述 GPU、服务器、Leaf/Spine、链路以及 P/D 实例部署位置 | 已实现 |
| **2. P/D 联合绑定 `binding`** | 收集待绑定请求，预测整体方案，选择 P/D 对并决定是否暂缓 | 已实现候选剪枝＋增量贪心、有限快速预测与暂缓；保留轮询，性能待重新验证 |
| **3. KV 调度 `kv_scheduler`** | 逐层 KV 分块、SLO 排序、带宽令牌、在途额度和集合通信保护 | 已实现 |
| **4. 资源预留 `reservations`** | 管理显存容量、预留转占用、对象引用和传输读依赖，控制释放时机 | 已实现物理对象试算与整批绑定原子预留；轮询保留原预留时机 |
| **5. 时间与反馈 `feedback`** | 定义统一仿真时钟、网络提交、传输完成及依赖解除接口 | 已实现接口，实际适配在 backend |
| **6. 执行与网络后端 `backend`** | 驱动请求生命周期、计算、集合通信和 KV 传输，对接 ns-3 | 已接入窗口、联合选择、原子预留与独立墙钟诊断 |
| **7. 负载生成 `workload`** | 生成请求到达、输入输出长度和请求级 SLO | 已实现 |
| **8. 状态观测 `observation`** | 观测实例队列、计算状态、显存、网络流量和拥塞，落实观测延迟 | 已实现；预测器已使用延迟可见的请求、队列、缓存和资源快照 |
| **9. 指标评估 `evaluation`** | 记录 token 轨迹，统计 TTFT、交接、ITL、达标吞吐及压力指标 | 已实现 |
| **10. 实验管理 `experiments`** | 参数扫描、多 seed 对照、输入冻结、失败记录和结果汇总 | 已支持轮询/联合、均匀/异构及声明开销敏感性对照 |
| **11. 前缀缓存 `prefix_cache`** | 匹配前缀，管理实例内共享对象、就绪状态和 LRU 淘汰，支持 P/D 独立复用 | 已接入五种 KV 模式、方案预测及 P/D 独立命中的联合选择 |

## 目录与职责

```text
Inference/
├── inference                     # 项目统一命令入口
├── joint_scheduling/              # 自有配置、调度接口、后端、资源与评估代码
│   ├── configs/                   # 默认配置与可运行的 smoke 配置
│   ├── binding/                   # 绑定窗口、整体方案预测与预留策略接口
│   ├── kv_scheduler/              # KV 块与启动许可接口
│   ├── topology/                  # 物理拓扑和 P/D 到 GPU 的部署映射
│   ├── reservations/              # 物理对象、共享引用和读依赖账本
│   ├── prefix_cache/              # 前缀匹配、P/D 复用与有界 LRU 缓存
│   ├── feedback/                  # 统一时钟与依赖完成接口
│   ├── backend/                   # ns-3 适配器和简单请求基线
│   ├── evaluation/                # 请求/token 轨迹、统计与绘图
│   ├── workload/                  # 到达过程、请求长度和 SLO 负载生成
│   ├── observation/               # 运行状态、延迟观测和状态时间线
│   └── experiments/               # 参数矩阵、多 seed 实验与汇总
├── third_party/                   # 保留来源及许可的第三方源码
│   ├── ns3/                       # 当前基线使用的网络后端
│   ├── vidur/                     # 保留的推理仿真参考实现
│   ├── aicb/                      # 保留的计算/通信 workload 生成工具
│   ├── simccl/                    # 保留的集合通信流量模型
│   ├── astra_sim/                 # 保留的计算/通信仿真框架
│   └── manifest.json              # 第三方组件来源与版本记录
├── scripts/check_repository.py     # 独立仓库结构与网络源码完整性检查
├── joint_scheduling_explanation.md # 研究方案与目标
├── assets/                        # 方案图
└── .github/workflows/inference.yml # 基线回归工作流
```

`third_party/vidur` 等保留组件尚未接入当前请求基线。当前执行器使用可选联合/轮询绑定和 FIFO/按 token 块轮转/多请求token预算合批的 Prefill 规则，不复刻完整 Vidur/Sarathi 批处理，支持可选 Decode 连续批处理（需提供批量时延曲线），尚不支持 PP。实验性 [EP 路径](joint_scheduling/backend/EXPERT_PARALLEL.md) 支持节点内 TP、跨节点 dispatch/专家计算/combine 与 KV 共享网络；当前 EP 支持静态轮询和均衡随机绑定，不支持联合选择或绑定预测。前缀复用由自有 `prefix_cache` 模块模拟，使用显式前缀身份和缓存命中计算样本，不保存真实模型 KV 张量。

Dense 路径中的前缀缓存、Chunked/合批 Prefill、Decode 连续批处理现可与绑定预测及联合选择同时开启。
执行按 token 区间更新共享 KV，预测按实际本地组批规则重新评估已有与新增请求，受确定性工作预算约束。
分块/合批预测使用聚合网络近似，未逐层重放 ns-3 或 KV 门控，不能当作 SLO 保证。
可运行的小型组合示例（全部计算与负载为 synthetic）：

```bash
./inference --config joint_scheduling/configs/composed_execution_smoke.json run-baseline --output joint_scheduling/artifacts/composed-example
./inference run-experiments --spec joint_scheduling/configs/composed_execution_comparison.json --output joint_scheduling/artifacts/composed-comparison
```

组合示例已开启缓存、P token 预算合批、D 连续批处理、有界联合绑定和抽样诊断；对照规格固定输入，比较轮询/联合 × D 批量 1/4 × 两个 seed。
时延模型与预测范围见 [绑定说明](joint_scheduling/binding/README.md) 和 [Prefill 合批说明](joint_scheduling/backend/PREFILL_BATCHING.md)。

## 功能开关与前缀复用

实验记录默认采用 `recording.mode="essential"`：保留输入、请求/token 轨迹、指标和资源汇总，高频状态、网络采样和调度明细仅在 `"debug"` 模式写盘。网络观测和在线调度不因记录模式改变。完整保留清单见 [实验记录规则](joint_scheduling/RECORDING.md)。

默认配置 `joint_scheduling/configs/default.json` 列出开关。旧配置省略新字段时使用下表默认值；前缀缓存默认关闭。实际生效设置保存在每次运行的 `resolved_config.json` 与轨迹元信息中，`./inference describe` 也会显示开关。

| 功能 | 配置项 | 开启 / 关闭 | 默认值 |
| --- | --- | --- | --- |
| 联合选择 | `binding.joint_selection_enabled` + `binding.policy` | `true` + `joint_lexicographic` 启用有界贪心；`round_robin` 保留基线 | `false` |
| 绑定窗口 | `binding.window_enabled` | `true` 收集后按窗口派发；`false` 立即派发，P/D 都按轮询选择 | `false` |
| 方案预测诊断 | `binding.prediction_enabled` | `true` / `false`；联合模式还需 `prediction_sample_every>0` 开启显式抽样，默认不运行完整重放 | `false` |
| 绑定决策开销 | `binding.decision_overhead_us` | `0` 不计开销；正数计入派发等待与 TTFT | `0` |
| 前缀缓存 | `prefix_cache.enabled` | `true` / `false`；关闭后完整 Prefill、完整输入 KV 传输，不保留缓存 | `false` |
| P 侧前缀复用 | `prefix_cache.prefill_reuse_enabled` | `true` / `false`，控制 P 侧命中及相应计算复用 | `true` |
| D 侧前缀复用 | `prefix_cache.decode_reuse_enabled` | `true` / `false`，控制 D 侧命中及缺失 KV 补传 | `true` |
| 空闲缓存淘汰 | `prefix_cache.eviction_enabled` | `true` 使用 LRU；`false` 保留已有缓存，新条目放不下时不缓存 | `true` |
| KV SLO 优先级 | `kv_scheduler.slo_priority_enabled` | `layer_ready_slo` 下 `true` 按 SLO 余量；`false` 按 FIFO | `true` |
| 集合通信保护 | `kv_scheduler.collective_protection_enabled` | `true` / `false`，控制集合通信预留、前瞻保护和保护期排空额度 | `true` |
| KV MLU 分级 | `kv_scheduler.policy` | `slo_mlu` 启用期限/MLU 晋升与 EP 分阶段保护；`slo_slack` 保留原策略 | `slo_slack` |
| 网络数据优先级 | `runtime.network_priority_enabled` | `true` 启用主机和交换机优先出队；`false` 保留轮询 | `false` |
| 动态在途额度 | `kv_scheduler.inflight_budget_mode` | `adaptive` / `fixed`，切换带宽与 ACK 反馈窗口或固定额度 | `adaptive` |
| 动态网络观测 | `feedback.network_observation_enabled` | `true` 运行期采样；`false` 不采样，受控 KV 使用静态拓扑容量估计 | `false` |
| 状态观测 | `feedback.state_observation_enabled` | `true` / `false`，控制队列、忙闲、显存统计；完整轨迹另需 `recording.mode="debug"` | `true` |

KV 门控由 `runtime.kv_transfer_mode` 选择：`whole_ready_immediate`、`whole_ready_chunked`、`layer_ready_immediate` 不使用门控，`layer_ready_fifo`、`layer_ready_slo` 使用门控。SLO、保护和动态额度开关仅影响受控模式；关闭保护后，真实集合通信流量仍在同一 ns-3 网络中执行。关闭动态网络观测不会关闭网络仿真；初始化只读取拓扑端口容量，后续不读取队列、流量或 ACK 快照。状态观测、网络观测可独立开关。请求/token 事件与 SLO 统计继续记录，显存容量检查、读依赖、ACK 完成语义和统一时间轴始终保留。

前缀缓存示例及对照：

```bash
./inference --config joint_scheduling/configs/prefix_cache_smoke.json run-baseline --output joint_scheduling/artifacts/prefix-demo --plot
./inference run-experiments --spec joint_scheduling/configs/prefix_cache_comparison_example.json --output joint_scheduling/artifacts/prefix-comparison
```

请求用有序 `prefix_blocks`（每块包含 `id`、`tokens`）声明相同内容；键包含完整上文链及 profile 命名空间，输入长度相同不会自动命中。轮询在实际 Prefill 准入时查询缓存，联合选择在延迟可见快照上查询并在提交时验证、固定命中布局；只有已就绪并仍驻留的对象可命中。共享前缀按每 GPU 物理对象计数，P/D 各自保存的副本分别计数；活动引用与在途读 pin 阻止淘汰。缓存受 `max_bytes_per_instance` 限制，并可在轮询请求容量不足时回收空闲条目。

P 命中的整请求计算使用 schema 2 中显式的 `cached_points`，分块/合批计算使用显式 query/history 曲线；两者均禁止超范围外推或按命中比例自动扣时间。完整输入 KV 命中时仍重算最后一个查询以产生首 token；新增输出写入私有对象。D 命中只补传缺失 KV；P 命中而 D 未命中仍需传输完整输入 KV。正常结束后缓存可继续占用显存，`observation/prefix_cache_summary.json` 记录驻留字节、命中和节省传输量，不能把这部分保留量当成泄漏。详细输入格式、生命周期和模型边界见 [前缀缓存说明](joint_scheduling/prefix_cache/README.md)。示例数据全部为 synthetic。

## 绑定窗口与整体方案预测

窗口支持固定间隔、临近 TTFT 截止提前触发和最大等待，派发开销计入 TTFT。整体方案预测器读取延迟可见的请求、队列、计算进度、缓存、显存与网络状态，使用独立影子时间轴估计 TTFT、交接及后续 ITL；候选方案之间及其对已有请求的竞争一起计算。前缀命中影响预测计算和缺失 KV 字节，容量试算按物理对象去重，不改变真实状态。

联合选择使用候选剪枝和增量快速评分，按已有与新增请求的有限范围预测达标数、SLO 余量和资源开销排序；每次选择立即更新影子队列、显存共享引用和链路负载。选定批次统一验证并原子预留；默认每轮最多处理 16 个请求、每请求评分约 4 个候选，使用确定性工作预算，预算不足或不可行时暂缓。完整预测器保留作离线或关闭默认抽样的诊断。关闭联合选择保持轮询及其原预留时机。请求可声明 `max_output_tokens`，预测使用这个可见上限或配置默认上限，按 `prediction_decode_steps` 有限步预测，不使用未来真实 EOS。网络是共享资源流体近似，KV 门控不在影子中逐块重放，预测误差在 `observation/binding_summary.json` 中保留。详细接口和边界见 [绑定模块说明](joint_scheduling/binding/README.md)。

```bash
./inference --config joint_scheduling/configs/binding_smoke.json run-baseline --output joint_scheduling/artifacts/binding-demo --plot
./inference run-experiments --spec joint_scheduling/configs/binding_comparison_example.json --output joint_scheduling/artifacts/binding-comparison
```

联合选择可运行配置为 `joint_scheduling/configs/joint_heterogeneous.json` 和 `joint_uniform.json`，对应 `*_comparison.json` 固定两类负载、profile、seed 和开关，对照轮询/联合及 0/5/50/200/1000/5000 μs 声明开销。整轮墙钟 P50/P95/P99 与采样构造成本单独诊断，**1 ms 窗口、P99 ≤50 μs 是初始目标，不能视作已达保证；5 μs 仅是仿真配置假设**。实现边界见 [绑定模块说明](joint_scheduling/binding/README.md)，性能与收益待重新实验验证。

这些参考组件保留用于后续批处理、性能采样和集合通信接入。ns-3 已裁去无关无线/移动网络及上游示例，保留当前依赖与后续拓扑、通信观测和并行仿真代码；保留依据见 [第三方目录说明](third_party/README.md)。

外层 Git 仓库由本项目独立管理；`SimAI/`、嵌套 Git、上游子模块和上游工作流已移除。保留的第三方源码随本仓库一起维护，原版权和许可保留在各组件中，路径见组件清单。AICB 已裁去训练入口、训练模型、安装包和历史示例结果。

## 快速运行

在本仓库根目录执行。需要 Linux、Conda 和 C++ 编译器；无需 GPU、MPI 或管理员安装。项目统一使用名为 `simai` 的 Conda 环境，Python 固定为 3.12，CMake 版本固定。首次创建环境需要联网下载依赖。

```bash
./joint_scheduling/bootstrap.sh
conda activate simai
./inference backend build --jobs 2
./inference backend doctor
./inference --config joint_scheduling/configs/baseline_smoke.json run-baseline --output joint_scheduling/artifacts/my-first-run --plot
./joint_scheduling/check.sh
```

`baseline_smoke.json` 使用明确标记为 `synthetic` 的计算数据，仅验证反馈、网络竞争与统计管线，数值不能代表真实模型性能。默认配置没有计算性能文件和负载，运行时会要求补齐，不会自动退回合成数据。结果目录拒绝覆盖。

`bootstrap.sh` 创建或更新 `simai`，显式使用 conda-forge 安装 Python 3.12，再从项目 requirements 安装运行依赖；根目录 `environment.yml` 同步记录环境定义。`./inference` 和 `check.sh` 会自动调用 `simai`；手动运行 Python 时先执行 `conda activate simai`。

当前网络模型使用服务器共享全双工 NIC、ECMP、RDMA 与拥塞控制；节点内通信采用共享局部 FIFO 链路。计算时间来自离线采样，计算文件不得重复包含本后端显式模拟的通信时间。详见 [后端说明](joint_scheduling/backend/README.md)。

## 负载生成、状态观测与批量实验

```bash
./inference generate-workload --spec joint_scheduling/configs/workload_example.json --output joint_scheduling/artifacts/generated-workload.json
./inference --config joint_scheduling/configs/baseline_smoke.json run-baseline --workload joint_scheduling/artifacts/generated-workload.json --output joint_scheduling/artifacts/generated-run --plot
./inference run-experiments --spec joint_scheduling/configs/experiments_example.json --output joint_scheduling/artifacts/suite-example --dry-run
./inference run-experiments --spec joint_scheduling/configs/experiments_example.json --output joint_scheduling/artifacts/suite-example --jobs 2
```

[负载生成](joint_scheduling/workload/README.md) 支持周期、Poisson 和突发到达，以及长短请求和 SLO 混合。到达与长度使用独立随机流，同 seed 下改变到达率仍保留对应请求长度。

每次基线运行自动导出 [状态观测](joint_scheduling/observation/README.md)：队列、计算/执行忙闲、在途通信、显存、时间积分统计和延迟后可见状态；`--plot` 另外生成状态时间线。普通状态观测不增加网络事件；动态网络观测会按配置采样并推进到采样事件，`feedback.observation_delay_us` 控制两者何时可见。在途字节是等待完成的整条逻辑消息，不是剩余在线字节或实际 NIC 利用率。

[批量实验](joint_scheduling/experiments/README.md) 示例运行 2 种 NIC 带宽 × 2 种到达率 × 2 个 seed，共 8 次，生成独立结果、失败记录及跨 seed 汇总。失败不会变成零值成功样本；存在失败时命令返回非零。运行器可对照轮询与联合绑定，并扫描已实现的 KV 模式、预算、块大小及网络参数，拒绝未知参数。

## 配置、评估与研究入口

```bash
./inference validate
./inference describe
./inference export-topology --output joint_scheduling/artifacts/topology.json
./inference record-demo --output joint_scheduling/artifacts/demo/events.jsonl
./inference evaluate --trace joint_scheduling/artifacts/demo/events.jsonl --output joint_scheduling/artifacts/demo/result --plot
```

配置字段与跨模块语义见 [模块说明](joint_scheduling/README.md)。统计分别保留 TTFT、P 到 D 的交接间隔、后续最大 ITL、端到端时延和达标完成吞吐；失败与未完成请求不会被当成完成样本。具体口径见 [评估说明](joint_scheduling/evaluation/README.md)。

建议先读 [研究方案](joint_scheduling_explanation.md)，再从 `joint_scheduling/backend/runner.py` 跟踪一条请求，结合 `reservations/ledger.py` 和 `evaluation/metrics.py` 理解资源生命周期与指标。联合选择需要同时设置 `binding.joint_selection_enabled=true` 和 `policy=joint_lexicographic`。`slo_slack` 仍由 KV 执行模式选择实际行为。实际 KV 策略由 `runtime.kv_transfer_mode` 或 CLI 的 `--kv-mode` 显式选择；`layer_ready_slo` 执行带集合通信保护的 SLO 余量调度。算法和配置见 [KV 调度说明](joint_scheduling/kv_scheduler/README.md)。

KV 在途额度现可按可用带宽、实际块完成反馈和集合通信可容忍阻塞时间动态计算；示例配置为 `joint_scheduling/configs/dynamic_window_smoke.json`，固定额度消融见 `dynamic_window_ablation_example.json`。

持续加压使用 `joint_scheduling/configs/sustained_pressure_comparison_example.json`：固定 1 秒到达时长，扫描到达率与带宽，输出长度为 64/128/256 token，预热后同时检查达标吞吐、token 吞吐、积压及未完成请求。负载生成器支持 `arrival.duration_seconds`；压力统计见 `joint_scheduling/evaluation/pressure.py`。

## 第三方版本与构建校验

`third_party/manifest.json` 记录第三方组件的来源和版本。当前网络构建读取 `third_party/ns3/SOURCE_MANIFEST.json`，核对 vendored 源码的 SHA256，并与 `joint_scheduling/backend/backend.lock.json` 的内容锁匹配；上游提交号仅作为来源记录，不要求本地存在相应 Git 元数据。

`./scripts/simai-python scripts/check_repository.py` 可以在未安装项目依赖、未构建后端时检查目录完整性、嵌套 Git 残留与网络源码内容。`check.sh` 和 CI 也会执行此检查。克隆本仓库后无需执行 `git submodule update`。

后端在 `joint_scheduling/artifacts/backend/` 中建立隔离副本，记录工具链、构建指纹及二进制 SHA256。变更第三方网络源码时，应同步审查来源清单、内容锁和回归结果。`check.sh` 检查统计、资源、分组网络仿真及端到端确定性；GitHub Actions 在推送或拉取请求中执行相同基线回归。
