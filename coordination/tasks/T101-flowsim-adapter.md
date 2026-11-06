# T101-flowsim-adapter：接入 flowSim，复用现有推理与调度机制

- 执行角色：执行 agent；当前聊天为指挥 agent，负责后续审核。
- 仓库：`/home/m0nesy/Postgraduate/Inference`
- Windows 入口：`\\wsl.localhost\Ubuntu-26.04\home\m0nesy\Postgraduate\Inference`
- 正式任务文件：`/home/m0nesy/Postgraduate/Inference/coordination/tasks/T101-flowsim-adapter.md`
- **执行结果目录**：`/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-06/T101-flowsim-adapter/`
- 草稿目录：`/home/m0nesy/Postgraduate/Inference/work/agents/2026-11-06/T101-flowsim-adapter/`
- 前置报告：
  - `/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-05/M-092017-layer-routing-dedup/REPORT.md`
  - `/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-05/M-094452-layer-decode-azure/REPORT.md`
  - `/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-06/M-164119-decode-kv-overlap/REPORT.md`
  - `/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-06/M-182557-flow-model-assessment/REPORT.md`

## 执行要求

你是本轮任务的执行 agent。先完整阅读根目录 `AGENTS.md`、本文件和前置报告，以及修改范围内的子目录约定。按照仓库模板建立上述执行结果目录，保存本任务原文为 `TASK.md`，创建 `STATUS.json`，先保存执行前的 `git status --short --untracked-files=all`。若执行日期跨午夜，继续使用明确指定的目录。

本任务授权你完成适配、必要构建、定向验证、完整回归和下述有界实验。普通实现选择自行决定并记录，不把一般技术细节反复交给用户选择。只完成本任务，不自行启动其它 agent，不自动进行下一轮优化，不提交或推送。当前工作区大量未提交修改属于已完成的前置工作，必须保留；包括路由/去重/逐层 Decode、研究 profile/spec 更新及用户要求删除旧实验报告。不能 checkout/reset/clean 掉这些改动。

## 背景与目标

当前 `joint_scheduling/backend/runner.py` 直接创建 `Ns3Backend`，`run_baseline()` 开头还无条件调用 `require_backend()`。现有网络是实际 ns-3 包级模型，包级运行成本已限制大规模策略扫描。最近 5 请求的运行有 288167 条远端逻辑流、91408 个 EP 阶段，端到端 baseline 墙钟约 178.68 s；截止 30 s，实际网络活跃约 4.36 s。历史耗时仅作背景，不代替本轮性能测量。

目标：**接入真正的 flowSim 流级网络后端，以配置选择 ns-3 或 flowSim；共用当前整套推理、调度和评估运行时。** flowSim 用于快速端到端策略筛选，ns-3 保留为包级复核与默认后端。

“机制一样”指上层算法、数据口径、资源约束和依赖关系一致，不要求两种网络模型具有相同的通信时延或包级拥塞细节。网络时延不同可以合法改变组批和调度轨迹，不能为了逐时刻对齐而冻结端到端调度。不能把一个 `size / bandwidth` 计时器改名为 flowSim，也不能另写一套简化的 Prefill/Decode 模拟器。

## 一、核实并固定 flowSim 来源

1. 优先检查 `https://github.com/netiken/m4` 公共产物中的 `flowsim/` 及其 SimAI 集成。上游 README 明确区分 UNISON(ns-3)、flowSim 和 m4；本轮目标是 **flowSim**，不是训练或接入 m4 学习模型。
2. 实际核对源码、许可证、接口、动态流到达能力、时钟推进、完成回调、拓扑、带宽分配、优先级和观测能力。不能只看 README 就宣称适配成功。
3. 固定上游仓库、commit、必要子模块 commit、内容哈希、许可证和本地补丁；按仓库第三方组织方式纳入最小必要源码与 manifest。构建放隔离产物目录，不在第三方源码目录内构建，不依赖会变化的远程分支。
4. 只获取适配必要的代码与依赖，不默认克隆训练数据、checkpoints 或所有子模块，不引入 GPU/模型训练要求。已有参考 `third_party/astra_sim` 的 Analytical 不等于 flowSim，不得拿它替代本任务后端。
5. 如果源码确实无法获取，或只能离线处理全量 trace、无法支撑在线请求/完成回调，需要记录实际尝试、限制和适配判断。可以补充适配层或明确记录必要补丁；若核心能力仍不满足，则如实提交缺口，不能用自制后端冒充完成。

## 二、后端选择、统一时钟与生命周期

优先扩展已有 `feedback.network_backend` / `feedback.time_authority`，不要无故增加第二套含义重复的选择字段。当前默认分别为 `ns3_alibabacloud` / `ns3`，须保持旧配置兼容；新增 flowSim 合法组合及严格校验，文档给出完整可运行配置。可以在核对接口后选择更合适的命名，但必须只有一处清晰的后端选择入口。

建议增加小型后端工厂/适配层，让运行器只依赖公共契约，避免在 Prefill、Decode、KV、EP、MLU 内散布后端分支。

需要覆盖的现有入口包括：

- `joint_scheduling/feedback/__init__.py`：`SimulationClock`、`NetworkBackend`、`NetworkTransfer`、`TransferProgress`；
- `backend/client.py`：`now_ns`、`now_seconds()`、`call_at()`、`submit()`、`network_snapshot()`、`advance()`、`run_until()`、`submission_batch()`、上下文管理与关闭；实际调用到的其它方法也要核查；
- `backend/runner.py`：构建/身份解析、实例创建、metadata、strategy 命名、错误处理；
- `backend/execution/network.py`、`compute.py`、`kv.py`、`expert_parallel.py`：网络完成与计算依赖；
- `observation/network.py`、`feedback/`、`experiments/provenance.py`、实验执行与独立 EP 核验入口。

约束：

1. 一次运行只有一个仿真时间权威。flowSim 的网络事件、计算 timer、请求到达、调度开销、采样 timer 必须在同一时间轴推进，不允许 Python/flowSim 两个时钟各自前进。
2. 完成回调恰好一次、时间单调、不可提前释放依赖；零延迟/同时间戳事件采用确定且记录的顺序。新增流导致速率变化时，应先结算已传字节、更新已有流的完成计划并取消/失效旧事件，不能留下幽灵完成回调。
3. 逻辑传输标识、字节数、源/目标 GPU、kind、priority 与资源映射保持原契约；非法输入、重复活跃 ID、未知后端/不兼容设置必须明确报错。
4. flowSim 不应无条件调用 ns-3 的 `require_backend()`，也不应为了读取后端身份而强迫构建 ns-3。后端缺失不静默回退到另一种模型。
5. ns-3 默认路径及已有包级原生日历重放保持原行为。flowSim 无需伪装执行 ns-3 的原始 SEND/TIMER RPC 或声称产生真实 ACK；若复用竞争诊断工具，须增加诚实的后端适配/能力声明，不能修改其 1 ns 包级自身重放门槛来容纳跨模型差异。

## 三、必须共用和保留的上层机制

不能修改以下行为来降低 flowSim 工作量或制造速度/收益：

| 范围 | 保留内容 |
| --- | --- |
| 请求与 P/D | 真实输入/输出长度配对、到达事件、可见输出上限与当前容量口径；原 P/D 绑定策略、seed、冻结绑定计划；队列、admission、显存预留与释放 |
| Prefill | 现有逐层执行、chunk、packed 多请求 batch、query/history 成本、就绪事件；主配置 chunk 512、总预算 2048、最多 16 请求 |
| Decode | 61 层顺序，前 3 Dense、后 58 MoE；每层非专家计算与对应 MoE 交错；同一 token 步成员/context 固定，最终层完成后才返回 token；连续组批及 EOS |
| EP | 逐真实层编号的确定性合成路由；按 token/目标 GPU 去重激活，expert assignment 计算不去重；dispatch 全就绪→专家 FIFO→本地输出合并→现有 TP 依赖→combine 完成；共享专家 GPU 的计算争用 |
| KV | 现有 whole/layer 就绪、chunk/放行、接收和来源读取完成语义；先用整请求 `whole_ready_immediate` 验证，再覆盖现有受控策略；不能让请求在 KV 未齐时提前 Decode |
| 策略 | Immediate/FIFO/MLU 的同一套实现，SLO 分级、集合通信保护、已有预算与当前/未来层剩余计算估计；不为 flowSim 另写简化策略 |
| 观测评估 | 采样间隔、可见延迟、EWMA、调度开销；token 时间、TTFT/ITL/SLO、统计窗口、完成与未完成请求口径；源码/配置/profile/workload/后端身份冻结 |

沿用已声明的模型限制：计算 profile 为估算，路由为合成；本地多专家输出合并额外成本当前显式为 0；没有新增实机采样、双微批重叠或 DeepEP 节点聚合。不要在本任务顺手变更这些假设，也不要改 profile/输入长度来使结果更好看。

对相同的冻结逻辑 batch/token-span/layer 输入，应验证路由、assignment、激活字节、目标 GPU 与依赖关系一致。端到端两后端的实际 batch 轨迹不必逐条相同，因为网络时延会影响合法调度；不能把合法差异当作必须抹平的误差。

## 四、流级资源和反馈模型

必须实际存在 KV 与 EP 的共享资源竞争，而不是给每条流独享带宽：

- 远端：源 GPU NIC 发送容量、目标 GPU NIC 接收容量、实际拓扑路径/共享上联容量，区分方向；互不共享资源的流能并行；
- 本地：保留主配置 `gpu_ports` 的单 GPU TX/RX 端口、带宽/延迟及现有本地传输服务顺序，不把所有本地通信合并为一个全节点锁；
- 路由/ECMP 与 seed 可复现，记录路径映射与相对 ns-3 的近似，不能把所有跨节点流压成一条虚构共享链路；
- 活跃流的速率分配、容量守恒、到达/完成时更新，明确 flowSim 的公平性、队列或优先级规则；需要的扩展必须记录为本地补丁；
- 网络优先级开关及分类 0/1/2/3 的处理须声明并测试。关优先级时不得暗中优待 EP；开优先级时不得忽略配置却声称保持语义。

`64 MiB` 是现有可靠传输实现的内部消息上限，不是软件层 KV chunk。流级如何聚合/保留这些底层部分要明确记录；不得因此多次放行 KV、重复计字节/回调，或无意给同一 KV 赋予多份公平带宽权重。

提交 `capability_and_observation_mapping`：逐项列出当前策略读取的 counter、flow progress、resource ID、queue、PFC、priority、容量和延迟字段在 flowSim 中的来源、单位和定义。字节/利用率要随实际服务进展更新，不能直到结束才补记全部字节。

流级不能原样复现 DCQCN/ECN/PFC 和真实包队列。没有的指标明确标为不支持/模型不产生，并提供兼容消费者的明确语义；不能将所有反馈硬置零掩盖竞争，不能把全路径未传完字节直接冒充真实交换机队列。若需要流级 backlog/压力代理，必须定义、标注并校准，与包队列作区分；不通过暗改 MLU 阈值/算法来造收益。关键策略输入映射缺失必须列为验收缺口，不能宣称所有策略已等价支持。上层控制机制相同与网络反馈近似的边界要写进文档和结果 metadata。

## 五、允许修改的范围

可修改：后端公共契约/配置校验、后端工厂与 flowSim 适配、最小必要的第三方源码和隔离构建/锁定、运行器和身份归档、观测映射、受影响的实验/核验接线、相关测试与文档、新的显式 flowSim 示例/研究规格。

核心推理与调度逻辑原则上不修改；确有必要的接口解耦应最小化，并在报告中解释。不得重写 EP 路由、计算曲线、SLO、调度策略、绑定、原始工作负载；不得更改主 32 GPU 带宽、批量参数或现有 ns-3 研究规格默认后端。新增 flowSim 规格明确指定后端、模型能力与输入哈希，不污染已有结果身份。

主研究条件保持：32 GPU（P16/D16）、TP1、P/D Expert EP16、Attention DP16、每 GPU 400 Gbps、节点内每 GPU TX/RX 各 160 GB/s，具体以当前 `configs/research/README.md`、base 配置和冻结 profile 为准。旧布局或功能确有未支持项时必须在运行前明确拒绝并报告；不能静默改变拓扑。

## 六、必须执行的验证

### A. 机制与接口测试

至少覆盖：

1. 单流已知带宽/延迟的完成时间；字节守恒与完成回调恰好一次。
2. 流中途加入/完成导致带宽重新分配，旧完成事件失效；同时间戳 timer/回调；截止时活跃流状态。
3. 共享 TX、共享 RX、共享上联的竞争，独立端口/独立链路并行；双方向容量与可复现路径。
4. 节点内 GPU 端口串行/并发、TP>1 的小型 allreduce、Dense 与 MoE 的本地/远端路径。
5. 优先级开关、实际进度计数、观测延迟、快照副本、MLU 可见预算和反馈映射。
6. 固定 batch 输入下逐层路由/GPU 去重字节一致；dispatch→expert→merge/TP→combine 与 token 返回的独立依赖核验，覆盖 61 层顺序及截止场景。
7. 同一 flowSim 输入+seed 可复现；旧配置默认 ns-3；未知/不支持设置 fail-fast；flowSim 不依赖 ns-3 可执行文件。

利用现有独立 `ep_reference.py` / `ep_audit.py` 和测试体系，不用生产公式复制一遍当独立证据。旧 Dense/MoE 行为冻结测试必须继续通过；不能重新生成预期哈希来掩盖 ns-3 行为变化。

### B. 流级/包级小型校准

在本轮分别实际运行 flowSim 与 ns-3：单流、共享 TX、共享 RX、共享上联/incast、小 EP+大 KV。相同拓扑、带宽、路由条件、流大小、开始时间和优先级。固定路径的校准与 ECMP 场景分开，防止把路径差异误认为模型误差。

导出逐流/阶段延迟、相对误差、容量利用、争用方向、不能复现的机制及误差解释。ns-3 自身 frozen replay 的 1 ns 校验保持不变；**不得要求或声称 flowSim 与 ns-3 ACK 精确到 1 ns，也不能把完成时刻称为真实 ACK。** 不承诺复现历史 3.206 μs 数值，不为追这个数值反调模型。

### C. 实际推理与策略接线

1. 小型同输入样本在 flowSim 下实际运行现有 Immediate、FIFO、MLU，覆盖整请求和逐层 KV 放行；记录实际有效开关、门控/预算事件和资源释放，不能只验证配置能加载。
2. 【2026-11-06 用户范围更新】研究负载仅保留Azure2023/2024，固定派生输入及其规格已移除。改为从保留的Azure2023原始输入在本任务 outputs/inputs/ 内明确生成有界校准样本与临时规格，两后端共享同一长度配对、到达时刻、绑定计划、profile、seed及记录级别。不得重新往 data/ 添加固定派生负载；已完成的旧样本实验只按本任务历史归档解释。
3. 两次都验证请求完成、KV/EP 字节、独立完成依赖和最终显存释放；报告大 KV 区间实际 Decode 活跃请求/阶段/流数。若近似网络时序让原固定到达样本不再重叠，必须如实展示，另加共同的显式压力样本用于验证竞争，不能暗自只改某个后端的到达时间或输出长度。
4. 提交端到端 TTFT/ITL/SLO/完成吞吐的差异，并区分数值近似误差、组批轨迹变化与策略效果；不能用冻结流量重放代替端到端策略验证。

### D. 性能与回归

1. 给出上述同输入新运行的实际墙钟、CPU/内存（可测则测）、逻辑流/事件数、输出规模与加速比。编译/首次初始化与运行分开，记录模式、采样频率、计时范围一致。小型可重复基准至少 3 次，报告中位数；5 请求端到端配对至少各 1 次。历史 178.68 s 不作为本轮包级测量结果。
2. 不预设十倍/百倍收益。若未明显加速，记录性能剖析与瓶颈，不能宣称快速筛选目标已经完成；机制适配与性能目标分项提交审核。
3. 定向检查通过后执行 `./joint_scheduling/check.sh`，记录真实命令、退出码和完整日志；不得跳过 ns-3 回归或只运行新增测试。
4. 不启动全量 19366 请求、长时间压力矩阵、GPU 采样或 m4 训练。任何失败、跳过、超时按实际记录；所有运行拒绝覆盖旧输出。

## 七、交付物与验收清单

- [ ] 真实 flowSim 来源/许可证/commit/内容哈希、补丁和隔离构建可复现，CPU 环境可运行。
- [ ] 显式后端配置、公共工厂、统一时钟与完成语义；旧默认 ns-3 行为保留，无静默回退。
- [ ] P/D、Prefill/Decode、EP 路由去重和计算依赖、KV 放行、策略代码共用，未偷换为简化运行时。
- [ ] TX/RX/链路与节点内端口的资源模型明确，争用/独立并发/优先级测试通过。
- [ ] 能力与观测映射表、包级缺失机制和策略解释边界齐全，没有伪造 ACK/PFC/包队列。
- [ ] 本轮新执行的机制测试、独立 EP 核验、包级校准、三策略接线、Azure 配对端到端和性能对比证据齐全。
- [ ] 完整回归通过；若未通过，明确列为缺口，不能将任务标作全部验收满足。
- [ ] `README`/后端文档提供构建、切换、复现命令、字段含义、模型限制及当前支持范围。
- [ ] `REPORT.md` 按仓库模板填写，状态更新为 `submitted`（执行完成提交审核）或如实反映失败/阻塞；不自行设置 accepted。

除原始日志与运行目录外，至少交付：

- `results/acceptance-summary.json`：各验收项状态及证据路径；
- `results/backend-capabilities.json`：功能和观测字段映射、支持范围、完成语义；
- `results/calibration.csv`：流级/包级校准原始对比；
- `results/performance.csv`：新运行的计时、输入身份、记录模式与加速比；
- `results/mechanism-checks.json`：逐层、字节、依赖、截止及最终资源检查；
- 每次完整运行的配置/profile/输入/源码/后端身份快照和错误日志。

允许在实现中选择等价文件组织，但报告必须给出这五类材料的实际路径。结果必须明确区分“已实现”“有源码但未验证”“本轮实测”“模型不支持”；不能只给一张成功截图或聊天结论。

## 回报给指挥 agent

完成后按 `coordination/templates/REPORT.md` 写报告。最后回复以下绝对路径、结论、实际验证情况和需决定事项：

`/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-06/T101-flowsim-adapter/REPORT.md`

建议只写在报告里，等待指挥 agent 审核；不要自动做策略优化或继续扩大实验。
