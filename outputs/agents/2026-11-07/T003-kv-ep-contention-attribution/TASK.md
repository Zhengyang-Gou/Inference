# T003：KV/EP 竞争及请求关键边界影响归因

- 执行角色：执行 agent；当前聊天为指挥 agent，负责审核。
- 仓库：/home/m0nesy/Postgraduate/Inference
- 唯一任务目录：/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T003-kv-ep-contention-attribution
- 草稿目录：/home/m0nesy/Postgraduate/Inference/work/agents/2026-11-07/T003-kv-ep-contention-attribution
- 前置条件：T001 accepted，T002-r03 accepted；r02 的 changes_requested 是已由 r03 补齐的历史状态，不要求改写旧轮次。
- 前置审核：
  - /home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T001-baseline-freeze/REVIEW.md
  - /home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T002-bounded-baseline-validation-r03/REVIEW.md
  - /home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T002-bounded-baseline-validation-r03/results/acceptance-summary.json
- 唯一基线来源：/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T001-baseline-freeze/results/frozen/

## 原始需求与本轮问题

用户原文：“确认 **KV 与 EP 的实际竞争及其对请求性能的影响**，发布任务”。

本轮只回答：在固定 P/D 绑定、同一冻结基线的有界真实请求样本中，KV 与 EP 是否共享网络资源并互相拖慢；这些延迟是否暴露到 Prefill 首 token、P→D 准入以及 Decode token 完成的必要依赖边界上？

先给出竞争是否成立、强度和请求侧暴露的证据，再决定算法对照的价值。**不要求得出存在明显竞争或存在性能收益的正结论。**

本轮的“请求影响”分两层报告：
1. 一次真实端到端基线中的实际 SLO goodput、TTFT、handoff、later ITL、失败与未完成。
2. 固定网络提交日历下，互扰对请求依赖边界的条件性拖延量。

网络重放不重新执行请求/计算调度，因此不产生移除互扰后的端到端请求指标。不得把第 2 层当成 goodput、TTFT 或 ITL 的端到端因果改善；若证据只能支持网络互扰，应明确指出请求端结论的限度。

## 交付目标

形成可复查的一条证据链：

实际基线 → 有方向的资源重叠 → 同日历网络移除对照 → 每条流/EP 阶段差值 → 请求必要边界暴露 → 实际请求指标与研究判断。

报告必须回答：
- 竞争发生在什么方向、哪些 GPU NIC/交换机端口和哪些阶段？
- EP 对 KV、KV 对 EP 分别增加多少延迟，影响多少流/阶段？
- 有多少延迟落在 D 准入或最终层 combine 等请求等待边界；有多少被 Prefill、局部完成或其他依赖遮蔽？
- 当前样本是否足以支持进入 KV 调度算法对照？依据与局限是什么？

## 固定条件与唯一输入

继承 T001：32 GPU（4 节点×8），P16/D16、TP1、独立 EP16 组、61 层（前3 Dense/后58 MoE）、原估算 profile/合成路由、同一 ns-3 后端、每 GPU 400Gbps、原节点内模型与链路参数。Prefill chunk512/token预算2048/最多16请求，Decode batch上限64，seed7。

沿用 layer_ready_immediate / ready_immediate。FIFO/MLU 门控、SLO 分级、collective 保护、Decode 准入限制、联合绑定/预测/窗口、prefix cache、原生网络优先级保持关闭。保留账本、源读保护、ACK/层依赖。网络采样仍为1ms、可见延迟100μs，state observation 开，recording.mode=essential；只读 hook 留存必要诊断字段，不改采样频率。

本轮使用 T001 冻结负载**原顺序前8个请求**，保留到达、长度和每条请求全部字段；不压缩/平移时间、不重复注入、不挑“更容易出效果”的窗口。增加的是有限样本覆盖，GPU/节点规模不变。

| request_id 后缀（完整前缀 azure-conv-） | arrival_seconds | input | output | P | D |
| --- | ---: | ---: | ---: | --- | --- |
| 000000000 | 0.0 | 374 | 44 | p0 | d3 |
| 000000001 | 4.314579 | 396 | 109 | p1 | d14 |
| 000000002 | 4.541877 | 879 | 55 | p2 | d7 |
| 000000003 | 4.710427 | 91 | 16 | p3 | d9 |
| 000000004 | 5.892655 | 91 | 16 | p4 | d13 |
| 000000005 | 6.311529 | 381 | 84 | p5 | d11 |
| 000000006 | 7.745497 | 1313 | 142 | p6 | d4 |
| 000000007 | 8.251431 | 388 | 84 | p7 | d5 |

合计8请求、3913输入token、550输出token；预期11个 Prefill chunk、542个 Decode token步。各 D 不同，不能据此宣称验证 batch>1。

绑定从 T001 全量计划按 request_id 投影。可用 joint_scheduling.binding.static.binding_plan_document 生成合法子计划，但必须逐条证明端点等于父计划投影；元信息按8请求统计，父计划哈希放 sample-manifest，不向 runner 的计划对象加自定义字段。实际 run_baseline 显式传入核对后的 binding_plan。

临时 config 相对 T001 只允许：
- evaluation.window_start_seconds：60.0 → 0.0；
- evaluation.window_end_seconds：3501.721937001 → 30.0；
- runtime.time_limit_seconds：4101.721937001 → 30.0；
- profile/workload/output 的可搬移路径调整。

其余配置不变。SLO 仍 TTFT1秒、ITL100ms；测量窗口 [0,30)，无预热，仅作本样本功能与竞争诊断。

## 对照设计与运行预算

**只运行以下4个阶段，默认串行，每个阶段最多启动一次；不增加 seed、重复轮次或负载档位。**

| 顺序 | 阶段 | 含义 | 硬墙钟上限 |
| --- | --- | --- | ---: |
| 1 | live-baseline | 一次真实 ns-3 端到端执行，采集逻辑流、native 日历和请求映射 | 25分钟 |
| 2 | replay-both | 原日历全部网络流量，验证重放保真 | 15分钟 |
| 3 | replay-collective | 保留跨节点 EP，移除跨节点 KV，测 KV→EP 互扰 | 15分钟 |
| 4 | replay-kv | 保留跨节点 KV，移除跨节点 EP，测 EP→KV 互扰 | 15分钟 |

网络阶段合计最多70分钟；超时停止并保留现场。实际 live 仿真上限30秒。重放允许按现有工具规则排空至 max(30秒, 最后一个基线ACK+1秒)，不得通过延长 live 截止让未完成请求变成成功。

重放必须保留选中流的提交时刻、payload、方向、优先级、端点/每对端点的流顺序，以及用于保真重放的 native TIMER / LOCAL / LOCAL_PORTS 提交。只过滤对应跨节点 SEND；节点内工作和原计算/计时骨架保持冻结。“仅EP/仅KV”是网络移除对照，不是关闭 EP 计算或修改端到端执行器。

replay-both 必须先完成并验证：
- 所有选中流排空，流ID/映射无漏失；
- 每条跨节点逻辑流的 ACK 与 live 对应，最大绝对误差 ≤1ns；
- 根据远端 ACK 和冻结 local_ack 重建的 EP 阶段完成时刻，也与 live 对应，误差≤1ns；
- 每对端点移除流量不改变另一类的端口/路由身份；保留既有 separate-by-class 和 port cooldown 检查。

任一保真条件不成立，就停止，后两类移除重放不启动，不把差值当竞争结果。不要放宽误差门槛。

单轮 seed7 只作确定性有限样本归因，不给统计置信区间或全负载结论。

## 允许修改与复用方式

允许写入仅：
- 本任务目录及其 inputs/、logs/、results/；
- 指定 work/agents/2026-11-07/T003-kv-ep-contention-attribution/ 草稿。

不改正式源码、正式配置、原始数据、T001/T002 任一文件或既有 ns-3 后端；不安装/升级依赖、不重建/切换后端、不提交推送、不启动其他 agent。

可复用：
- joint_scheduling.backend.runner.run_baseline；
- joint_scheduling.backend.execution.RuntimeHooks；
- joint_scheduling.experiments.ep_audit.EPFlowAudit；
- joint_scheduling.experiments.contention 的 Probe、replay、analyze 及导出逻辑；
- 既有 freeze_source / record_run 等输入与源码身份工具。

先阅读这些 API 的实际实现。现有 contention.baseline 没有 binding_plan 参数且未组合 EPFlowAudit；本轮用草稿组合只读 hooks 后显式调用 run_baseline，并按既有格式导出，不修改/monkeypatch 正式函数。现有 analyze 的聚合不能替代本任务要求的请求映射与有符号完整统计，可在草稿中补充分析。

不要直接启动默认不带本任务边界/预算的完整实验套件。每阶段执行前确认上一阶段的实际结果通过；统一脚本也须实现硬时限、失败退出与停止条件。

## 执行步骤

1. 阅读根 AGENTS.md、本 TASK、前置审核与上述工具代码。保存新的 Git status / HEAD；STATUS 改为 in_progress。确认 T001、T002-r03 accepted。
2. 复核 T001 manifest 与冻结件哈希。本轮预期 frozen manifest SHA256：
   811cc29616c0e1539a43109ae78d5bce14f23fde6b97a7e157c63ef956b0af78。
   清单中的路径若含 Windows 分隔符，先规范化再解析。读取 r03 的源码/后端身份，若研究核心源码或后端已变更，报告差异并停止，不静默换版本。
3. 在 inputs/ 准备 config.json、workload.json、compute_profile.json、binding_plan.json、sample-manifest.json。保存父文件哈希、逐请求提取/投影、实际8请求统计、完整配置差异与全部输入哈希。原 workload provenance 可原样保留，但需明确它描述原始全量来源，不能冒充本样本请求数。
4. 使用 WSL simai 环境执行配置 validate 与 backend doctor：
   env CONDA_EXE=/home/m0nesy/miniconda3/bin/conda
   入口 ./inference 与 ./scripts/simai-python（脚本需要时 PYTHONPATH=.）。
   若 doctor 不就绪则 blocked，不修改后端修复。
5. 编写本任务采集/分阶段运行/分析脚本。必要资源保护：跨节点逻辑流超过100万条，或 native 注册超过200万条时安全停止并保留产物；不得删字段后继续以假装满足预算。不要保留全部原始 network_snapshot.flows 副本，流明细由 Probe/逻辑事件保存即可。
6. 只预建 results/ 和必要诊断父目录；live run 目录由 run_baseline 自行 mkdir(exist_ok=False)，拒绝覆盖。按下方数据约定完成一次 live-baseline。必须确认全部8请求正常完成、EPFlowAudit passed、通信/资源无残留，才进入 replay-both。
7. 保存所有可复用重放输入及哈希，使重放可从文件恢复，不因 Python 进程退出丢失日历。依序完成三类重放及保真检查。
8. 计算网络差值、请求依赖边界暴露和实际指标，写结论及限制；只读静态重算无需再次启动 ns-3。
9. 按模板提交 REPORT，STATUS 设 submitted / failed / blocked；提交后停止。

## 必须留存的数据

建议完整诊断根目录为 results/diagnostic/，按既有 replay() 的相对路径约定组织：

- config.json，inputs/{compute_profile.json,workload.json,binding_plan.json,source/}；
- run/：完整 live 自动产物，含 requests/tokens/timeseries、summary、绑定、EP/P/D/资源、后端身份与源码引用；共享源码归档保留在同一诊断根目录，不能拆散；
- flows.csv：既有跨节点逻辑流字段（start_ns/ack_ns/端点/payload/kind/priority/stage_index/request_id）；
- stages.json、native_schedule.jsonl、baseline_summary.json；
- replay-both/、replay-collective/、replay-kv/：各自完整后端、复现身份、completions.csv、summary；
- replay-inputs-manifest.json：上述日历、流、阶段、配置、源码、后端与映射哈希。

在 results/ 另存下列可核查补充文件：

1. event-links.jsonl（或等价 CSV/JSON）：
   - transfer_id ↔ flow_index，KV 的 request/layer/offset/ready/start/ACK/size；
   - EP stage_index ↔ ep_call_id/phase/source instance/layer/batch；
   - batch ↔ 请求成员/Prefill chunk token区间或 Decode context；
   - 完整 Prefill、首 token、全部 KV、D 准入、各 Decode batch 完成等必要边界。
   只读回调接收副本；token 时刻直接来自 recorder 输出的 CSV/事件，不能假定 token_returned 经过 hooks。
2. port-intervals.csv：
   - 原1ms样本的时间区间、resource_id、容量、Δtx/Δkv/Δcollective 字节、queue、PFC增量；
   - 使用累计计数差分得到区间速率，避免把 EWMA 当即时服务量；
   - 全零且无排队/PFC区间可省略，记录完整样本数和省略规则。端点 outstanding 重叠可由 flows.csv 精确重算，不要求额外采样。
3. flow-comparison.csv / stage-comparison.csv：
   - 保留原身份/请求映射、both与相应移除对照的ACK/阶段完成、延迟和有符号差值，便于独立重算。
4. request-impact.csv、request-impact-summary.json：
   - 下文定义的实际请求指标和条件性边界暴露；不伪造移除流量后的请求指标。
5. mechanism-checks.json、replay-fidelity.json、contention-summary.json、acceptance-summary.json；
   - 各项条件由原文件重算，不把 passed 写成常量；输入不符/映射缺失/检查失败必须反映到总结果和非零退出码。

允许文件名按工具需要调整，但 REPORT 必须提供完整映射，不能用汇总表替代原始流/阶段/映射证据。

## 分析口径

### 1. 实际端到端基线

- 8请求、550 token，逐请求完整输出，failed=0、incomplete=0。
- 预期11 chunk、3913 query token、542 Decode步、每步61层；EP调用 (11+542)×58=32074。
- 无缓存/TP1，逻辑KV payload=3913×70272=274974336字节；每chunk/层一个消息，预期11×61=671条。统计逻辑payload，不用含头/重传的端口字节替代。
- 最终32显存域 occupied/reserved=0、EP/流/步骤/批次无残留。
- 报告 completion-window SLO goodput（主指标）和完成吞吐，及 arrival cohort 的失败/未完成；TTFT、handoff=t2−t1、later_max_itl（从t3−t2起）、E2E 分布和每请求值；实际P/D batch分布。
- SLO违约本身不使功能检查失败。保持阈值，不要求出现违约。

### 2. 共享资源与网络互扰

同时提供两类证据，不能仅凭“同时在途”断言实质拖慢：
- 资源共享：同一**有方向** NIC TX/RX 或交换机 egress 端口的两类流量。不同GPU、不同方向、同节点名称均不能直接视作同一瓶颈。给出端点在途重叠时长，以及1ms端口混合字节/队列/利用率/PFC概况；分别说明它们的时间粒度和含义。
- 移除对照：对保留的同一流或EP阶段，比较 both 与另一类跨节点流量被移除后的完成时刻。

分别统计 EP→KV、KV→EP：
- 流数、逻辑payload、mean/p50/p95/p99/max 的完整与孤立流时延及有符号 added latency；
- ΔACK=ACK_both−ACK_isolated，保留负值并报告负值数量/幅度，不截断为0；
- >1μs 正向拖延的流数/比例，反向改善的流数/比例（1μs仅描述计数门槛，不是统计显著性判断）；
- Prefill/Decode × dispatch/combine 四类阶段，阶段完成=max(冻结local_ack, 对应远端流ACK)，统计同样的差值并核对 live 阶段保真；
- 按NIC/叶交换机端口、请求、阶段定位主要贡献；EP/KV payload与端口字节分开解释。

### 3. 请求必要边界的条件性暴露

按实际身份连接，不能只按时间排序 zip，不能把全部流延迟加总为请求损失。

A. P→D就绪门：
- 对每请求，从对应KV流ACK重建 T_KV_all^both 与 T_KV_all^KV-only，核对前者等于live kv_all_received。
- 固定实际完整Prefill结束 T_P，计算 G^both=max(T_P,T_KV_all^both)、G^isolated=max(T_P,T_KV_all^KV-only)，报告 ΔG。
- ΔG 是冻结上游时刻下的准入门暴露；实际 D batch-start 还可能有队列等待，单独报告。
- 若 KV 提前于 Prefill 结束，则被遮蔽的差值不算作 D 准入拖延。

B. 首 token / 各 Decode token 的最终层 combine：
- 按 source batch、phase、真实layer_id=60、chunk/context 确定请求的最终Prefill chunk及每个 Decode token 对应阶段。
- 核对实际首 token/后续token 与完整最终层依赖完成的对应关系。
- 比较该最终combine的 both 与 no-KV 阶段门，报告冻结阶段开始及local完成条件下的边界暴露值。
- 其他层dispatch/combine差值作为必要层依赖诊断分列；不简单求和、不重复计算，也不宣称每个必要前置条件都在完整请求的最长关键路径上。
- 若无法识别精确映射，标缺证据并停止该项因果解释，不靠猜测配对。

C. 汇总：
- 每请求并列实际 TTFT/handoff/later ITL/SLO 状态，与 ΔG、最终P-combine暴露、首次D/following D token最终combine暴露统计。
- 给出受影响请求/token数、相对于实际毫秒级指标的量级。
- 明确区分“存在网络拖慢”“暴露到请求必要边界”“已量化端到端SLO变化”。本轮最多证明前两层；第三层没有重新执行的反事实请求轨迹，必须标为未测。
- 不计算“去掉竞争后的SLO goodput”，不把1ms混合采样、总EP占比或网络流差值直接当作请求性能因果证据。

## 验收条件

| 编号 | 通过标准 | 必须证据 |
| --- | --- | --- |
| A1 | 冻结身份、8请求原始字段/时间、父绑定投影及配置许可差异正确 | inputs/清单、哈希与逐字段比较 |
| A2 | 真实live完整完成8请求/550token，机制/逻辑KV/资源守恒通过；导出主辅指标 | 完整run、EPFlowAudit、机制检查、CSV/summary |
| A3 | 日历/身份完整，both重放排空且每流ACK与阶段门误差≤1ns；端口/路由身份不因移除改变 | native日历、flow/stage、replay-fidelity、后端身份 |
| A4 | 两个移除对照同日历完成，提供双向、有符号的流与四类EP阶段差值及共享资源证据 | completions、比较CSV、port区间、contention-summary |
| A5 | 所有请求的KV准入门与首/后续token最终阶段精确映射；提供条件性暴露和实际指标，未冒充端到端反事实收益 | event-links、request-impact及核对脚本 |
| A6 | 结论明确强度/位置/量级/不确定项及是否值得进入算法对照；负结果同样可通过；预算与范围符合 | REPORT、acceptance-summary、命令/时限日志 |

## 失败、停止与非目标

- 启动次数上限=1次live+3次network replay。前置失败、live机制失败/未完成、both不保真、输入身份冲突、超时或资源保护触发，保留现场并停止；不补跑、不扩大样本、不延长预算、不修改网络/profile/SLO寻找正结果。
- 普通静态脚本错误可在网络开跑前修正并记录；分析脚本可只读纠错重算。已经启动的网络阶段不得借“脚本修复”静默重启；需要返工则先报告。
- 低重叠、拖延很小、全部请求SLO达标，都是合法研究结果，不构成自动加压授权。
- 不开展Immediate/FIFO/MLU算法比较，不实现新的调度器或隔离网络，不关闭真实计算/EP来冒充公平请求对照；不扩容、不做DeepEP/节点聚合/MLU计时、不运行全量19366请求或完整check.sh。
- 若现有重放工具与本冻结条件确有兼容性问题，给出定位和最小证据，报告blocked/failed，不在本轮修改正式工具。
- 不把墙钟仿真成本解释为GPU时间或调度器决策时间。

## 交付与回报

REPORT 按模板逐项回答 A1–A6。首段给出研究判断及证据层级：未检出/存在但边界暴露很小/存在且暴露到请求必要边界/证据不足；用数字说明，不使用“显著”替代统计依据。

明确列出运行阶段/次数/墙钟/仿真时限、失败与未完成、SLO违约、未测端到端反事实及其他覆盖限制。若建议下一步，说明证据支持何种范围，不能自行启动。

最终回复：
/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T003-kv-ep-contention-attribution/REPORT.md

提交后停止，等待指挥审核。
