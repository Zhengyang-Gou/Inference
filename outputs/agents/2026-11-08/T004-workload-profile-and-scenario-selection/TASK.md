# T004：真实负载画像、固定选窗与有界实验协议

- 执行角色：执行 agent；指挥 agent 负责后续审核。
- 仓库：/home/s223/m0nesy/Inference，main。
- 唯一任务目录：/home/s223/m0nesy/Inference/outputs/agents/2026-11-08/T004-workload-profile-and-scenario-selection
- 草稿目录：/home/s223/m0nesy/Inference/work/agents/2026-11-08/T004-workload-profile-and-scenario-selection
- 前置：T003-r02 修复范围 accepted。原 T003 的 A6 failed、all_passed=false 保留，本轮不要求或允许改写该历史事实。
- 前置审核：/home/s223/m0nesy/Inference/outputs/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/REVIEW.md
- 审核归档检查点：6a1e85f0035ec30f38f08b155d029f603f859361（后续任务发布提交仅新增任务记录）。
- 本任务**只做静态分析、生成输入与协议草案，不启动仿真**。

## 原始需求与唯一问题

用户要求：“先 commit/push，然后给我提示词并下发 t004 任务书”。

本轮对应已讨论的下一步：先分析完整真实负载，按事先固定的规则选择典型与高负载有界样本，保持现有物理配置和机制，再为后续竞争验证形成协议。

唯一问题：在冻结的真实轨迹中，典型和高到达密度样本的工作量、KV规模、资源覆盖分别是什么；能否据此准备来源清楚、选择规则固定、后续策略可公平复用的有界验证输入？

T003已复算出极弱网络互扰，指定请求边界暴露为0。8请求有限样本不代表整条轨迹，本轮不以“必须找到明显竞争”为目标。不得根据仿真输出改选样本，也不据此关闭研究主线。

## 执行环境与允许范围

全部开发、静态验证、文件更新及结果归档在服务器执行。本机 Windows PowerShell 只通过 ssh m0nesy 操作/传输/阅读；不要调用 wsl、旧 UNC 路径或本机 Python。

非交互命令前缀：
cd /home/s223/m0nesy/Inference
export CONDA_EXE=/home/s223/miniconda3/bin/conda
使用 ./scripts/simai-python；simai/Python3.12.16。

允许写入仅本任务目录和指定 work 草稿。原始输入、T001～T003历史件、正式源码、正式配置及后端均不修改。不安装依赖、不重建后端、不提交/推送、不启动其他agent，不修改指挥维护的 PROGRESS.md。

可读取 config/workload/profile/binding 的既有纯静态 API，包括 load_config、load_workload、binding_plan_document。**禁止调用 run_baseline、Ns3Backend、replay/run_suite、backend doctor/smoke，或以任何名称启动网络后端、GPU/队列调度模拟、离线事件仿真。** 不跑完整check.sh。本任务不需要使用实际运行来估计成本。

静态脚本实际运行墙钟合计上限15分钟，记录逐条命令/退出码/耗时；静态代码错误可修正重算并记录，不把写代码的人工时间算为计算预算。

## 冻结来源

只用以下T001冻结件及其manifest审计原数据：
/home/s223/m0nesy/Inference/outputs/agents/2026-11-07/T001-baseline-freeze/results/frozen/

- workload.json：19366条Azure2023对话请求，SHA256=0ce0c8cdc4b31d07587014debf7272209d4bd6efb23f03b84ff17fce3282c346。
- compute_profile.json：SHA256=374595adf46d7b65bf34eb47e693b41a06dddb9fb1890f093afb013f66f93554。
- config.json、binding_plan.json，以及manifest.json。
- manifest SHA256=811cc29616c0e1539a43109ae78d5bce14f23fde6b97a7e157c63ef956b0af78。

逐件复核哈希，manifest中的历史Windows分隔符规范化后再定位。无需重复解包/复算T003的大体积原始证据。

保持：32GPU（4×8）、P16/D16、TP1、独立EP16、61层估算profile/确定性合成路由、原每GPU400Gbps/节点内端口/拓扑、chunk512/token预算2048/最多16请求、Decode上限64、seed7、TTFT1秒/ITL100ms。

保留 layer_ready_immediate / ready_immediate。FIFO/MLU门控、SLO分级、collective保护、联合绑定/预测/窗口、prefix cache、Decode准入与原生网络优先级继续关闭；网络观测1ms、可见延迟100μs、state observation开启、essential记录保持冻结值。

不下载2024数据，不更换数据集，不压缩到达间隔，不复制请求，不截断/删除长请求，不改变带宽/profile/SLO寻找效果。

## 一、完整轨迹静态画像

时间统一用整数纳秒，按项目输入时钟规则 round(arrival_seconds×1e9) 转换并记录。保留请求ID、原始行索引、到达和配对输入/输出长度；先检查唯一性、排序、合法长度和profile覆盖。

至少输出：

1. 全量请求数、起止/持续时间、平均到达率；输入/输出长度及到达间隔的mean/p50/p95/p99/min/max，长度总量。
2. 固定原点0、半开区间的1ms/10ms/100ms/1s/5s/30s到达集中度。分别交代完整区间、零到达区间和末尾不完整区间的处理。细粒度可用稀疏计数与直方图，不生成数百万行全零CSV。
3. 所有完整5秒与30秒非重叠窗口的表：起止、请求数、输入/输出token总量及长度分位、逻辑KV字节、后续Decode token工作单元、名义Prefill分块量。
4. 按冻结父绑定计划，给出各窗P/D实例与GPU/节点/叶覆盖、KV源TX/目的RX需求分布、跨节点/跨叶KV量。推荐场景提供完整逐资源表。

口径：
- KV逻辑payload取 output_tokens>1 请求的input_tokens×profile总KV字节/token；复核冻结profile对应70272B/token。不拿含头/重传端口字节当payload。
- 后续Decode token单元=sum(max(output_tokens−1,0))。
- 名义单请求chunk量=sum(ceil(input_tokens/512))，仅作体量参照；打包token预算可能改变实际分块/批次，不能冒称真实执行次数。
- 若给出MoE工作代理，可列输入token或Decode token×58层×top8 assignment。它不等于EP网络字节、实际EP调用/批次数或GPU时间。
- 不将不同单位的KV字节和token工作量比值称作网络负载比。
- 轨迹没有真实完成时刻：**不能静态“算出”实际在途并发、batch大小、KV/EP时间重叠、排队、拥塞或请求时延**。多尺度到达集中度、绑定覆盖与单位时间需求是潜在压力代理，必须这样标注。SLO阈值也不能当实际服务时间。
- 推理请求来源是真实的；计算profile估算、路由合成、SLO人工假设分别说明。

## 二、预先固定的样本选择规则

以下规则在读取网络结果之前已固定，不得事后调整：

- 候选集：从0开始、步长5秒的**完整非空5秒窗口**；每个窗口包含[A,A+5)内全部原始请求，按原顺序保留。末尾不足5秒的尾段单列统计，不参加选择。
- 典型场景 typical：候选窗按(请求数升序,窗口起点升序)排序，取索引 floor((N−1)/2)，即固定下中位位置。
- 高到达密度场景 high-arrival：请求数最大的候选窗；并列取起点最早。
- 若二者恰好同窗，如实保留同一选择并解释，不能强行另挑窗口。
- 另列全候选中KV输入量最大、Decode token工作量最大的窗口作为诊断对照，提供排名和指标；不替换两个主场景，不为这些备选另生成运行矩阵。
- 同时报告原T003“前8条请求”在上述分布中的覆盖与局限。它不是[0,30)的完整到达集，不把其前缀截取当作完整30秒负载。
- 选中窗口内请求全部保留。若体量偏大，报告体量和成本风险，不自行改窗长、删请求、抽样或缩短输出。

必须保存全候选表、排序结果、选中索引/并列处理与选择规则文本，使指挥可独立重算，不能只给“选了两个窗口”。

## 三、冻结候选输入与P/D规则

本轮产出的是**新的独立有界cohort**，不是整条原轨迹系统状态的复现。

每个主场景至少准备：
- source-requests.json：完整选中源请求字段、原始时间和行索引的审计件。
- workload.json：只对到达时间作统一平移 t'=t−A，落入[0,5)，其余请求字段、长度、顺序不变；不缩放间隔。
- binding_plan.json：使用现有 balanced_random、seed7，对该场景**一次性生成并冻结**合法计划。
- parent-binding-projection.json：相同请求在T001全量计划中的原端点审计对照。
- config.json：T001副本，仅允许窗口起点60→0、终点3501.721937001→30、time_limit4101.721937001→30，以及输入/profile/output路径调整。
- sample-manifest.json：源件与派生件哈希、选窗、原始行索引、时间平移量、逐字段/长度校验、绑定身份和完整config差异。

**固定P/D的含义**：同一场景今后的Immediate/FIFO/MLU等任何对照必须原封不动复用该场景的同一计划，不能按策略重新绑定。本轮允许在网络输出出现之前为新cohort按原政策/seed一次冻结；不能宣称因此复用了T001全轨迹中每个相同ID的端点。两个计划的差异须逐条报告，T001父计划不修改。

理由与兼容性需静态核对：现有run_baseline会将传入计划与“当前workload+topology+binding配置”生成的完整期望文档比较；任意时间切片的父投影未必符合该要求。不得修改正式runner，也不得把投影件冒充合法计划。本轮用纯静态生成/比较确认新计划符合现有API约束。

派生provenance正确区分：原全量来源、选中请求数、原时间窗、统一平移量、冷启动cohort与输入哈希。不把父provenance的19366请求数/全量注入时长冒充新样本，也不把复制的历史结果作为服务器新运行证据。

无carry-in、无预热：每个场景从空系统注入这5秒cohort，后续候选运行拟观察[0,30)、截止30秒。窗口前已在服务的请求未恢复，不能称为原轨迹自然高峰时刻的完整系统状态或稳态承载率。若要研究carry-in/稳态，应另行提出方案，不能在本轮暗中补请求。

## 四、下一轮实验协议草案（只写、不执行）

写 results/EXPERIMENT_PROTOCOL.md，包含：

1. 两个冻结场景各自要回答的问题、样本/配置/计划哈希；全物理配置一致，场景间不同输入不能算作算法收益。
2. 固定P/D、seed、[0,5)注入、[0,30)观察/截止及冷启动限制；未来失败/未完成如实计入，不能延时凑完成。
3. 竞争验证拟采用每场景一次live与both/仅EP/仅KV三类冻结网络重放，both保真≤1ns先通过；后续重放保持日历与路由身份。协议不能直接跳到算法比较。
4. 主指标为live的SLO达标完成吞吐；辅看TTFT、handoff=t2−t1、later ITL、失败/未完成。静态阶段不生成这些结果值。
5. 区分共享资源、网络互扰、指定依赖门暴露、端到端反事实。1μs只作描述门槛；负值保留、无明显暴露是合法结果，不能外推整条研究主线。
6. 可提出沿用单live25分钟、每重放15分钟、总70分钟及既有事件保护上限作为待审预算草案；列出两个场景相对T003的输入/KV/Decode/名义分块工作量。**结构比值不等于服务器墙钟预测**；不能凭旧WSL耗时线性外推保证服务器预算内完成。
7. 说明未知运行成本、可能的内存/产物规模风险、单次启动与失败即停止规则；输入/静态API可加载不等于实际网络阶段可在预算内完成。若建议先做成本探针或改预算，只列待指挥决定事项，不执行。
8. 下一轮启动前置条件：本轮静态材料经指挥接受并另发明确执行任务。协议不是执行授权。

## 交付与验证

建议目录：
inputs/ 保存经哈希核对的源冻结件或无歧义引用；
results/ 至少包括：
- source-audit.json、trace-summary.json；
- arrival-concentration.csv/json、windows-5s.csv、windows-30s.csv；
- selection-manifest.json（固定规则、全排序/并列处理、两场景与备选排名）；
- scenarios/typical/ 与 scenarios/high-arrival/ 的上述输入/计划/manifest；
- resource-demand.csv（标明parent投影/新场景计划，避免混用）；
- binding-compatibility.json；
- EXPERIMENT_PROTOCOL.md；
- static-validation.json、acceptance-summary.json；
logs/ 保存环境、Git状态、原始命令/退出码与耗时。

验证至少覆盖：
- 全量哈希、19366唯一请求、时钟/长度合法、窗口半开边界与尾段计数守恒；
- 全候选重算并严格复现固定选窗规则；
- 两场景ID/长度/原序与源行一致；平移前后所有相邻到达间隔一致；
- 绑定由同一政策/seed确定且符合当前静态期望文档；报告父投影差异；
- config完整差异仅许可项、profile不变、所有依赖路径在服务器可解析；
- JSON/CSV可读取、指标单位/代理含义清楚，未填造实际运行结果；
- 错误条件进入检查结果与非零退出码，不能把passed写成常量。

只读取已完成的历史研究结论作背景，不把T003的网络结果用于选窗。选择规则和输入冻结不以出现SLO违约或正收益为通过条件。

## 验收条件

| 编号 | 通过标准 | 证据 |
| --- | --- | --- |
| A1 | 来源/哈希/19366条身份正确，冻结机制未变 | source-audit、环境与Git状态 |
| A2 | 全量、多尺度与5s/30s画像可重算，尾段/空窗口径正确 | CSV/JSON与统计脚本 |
| A3 | 两主场景严格按预定规则选择，全部源行保留，无按结果换窗 | selection-manifest、源行校验 |
| A4 | 源时间/派生时间、合法一次冻结绑定、父映射差异及允许config差异可核对 | scenarios、binding-compatibility、static-validation |
| A5 | 有界协议完整、明确冷启动/无carry-in与成本未知，不冒称并发/拥塞/性能或收益 | EXPERIMENT_PROTOCOL、REPORT |
| A6 | 无任何live/replay/后端启动、无正式代码/旧任务修改，静态预算与归档规范符合 | 命令/退出码/耗时、Git差异、acceptance-summary |

## 停止、回报与归档

源哈希/身份不符、依赖缺失、静态预算耗尽或发现需要改正式机制时，保存证据并报告，不换数据或启动仿真绕过。选择恰同窗、需求不足以支持竞争猜想、或成本无法静态保证，均可作为明确结论提交。

按模板写同目录REPORT.md，STATUS设submitted或实际failed/blocked；执行agent不设置accepted、不改PROGRESS。列出精确待Git归档文件，不把大型生成目录整体强制加入，不提交/推送。

最终回复报告绝对路径、结论、实际静态命令/验证与待指挥决定事项：
/home/s223/m0nesy/Inference/outputs/agents/2026-11-08/T004-workload-profile-and-scenario-selection/REPORT.md

提交后停止，等待审核。本任务未授权下一轮网络运行。
