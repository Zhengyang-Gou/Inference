# T002：有界样本的基础链路验收

- 执行角色：执行 agent；当前聊天为指挥 agent，负责后续审核。
- 仓库：`/home/m0nesy/Postgraduate/Inference`
- Windows 入口：`\\wsl.localhost\Ubuntu\home\m0nesy\Postgraduate\Inference`
- 唯一任务目录：`/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T002-bounded-baseline-validation`
- 草稿目录：`/home/m0nesy/Postgraduate/Inference/work/agents/2026-11-07/T002-bounded-baseline-validation`
- 前置任务：T001，状态必须为 accepted。
- 前置报告：`/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T001-baseline-freeze/REPORT.md`
- 前置审核：`/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T001-baseline-freeze/REVIEW.md`
- 唯一基线来源：上述 T001 目录内的 `results/frozen/`。

## 原始需求与本轮问题

用户要求下发“有界样本的基础链路验收”。第一阶段研究目标仍为固定P/D绑定下的KV与EP竞争及KV调度对请求性能的影响。

本轮只回答：沿用T001冻结的模型、拓扑、执行规则和基础开关，少量真实来源请求能否实际完成Prefill→逐层KV→Decode→资源释放，并且结果可复现？

这是实际端到端功能验收，不是吞吐承载率实验，不验证MLU收益，不调优网络或扩大规模。

## 固定条件与允许差异

完整继承T001：32GPU、P16/D16、TP1、两侧EP16、ns-3、每GPU400Gbps、节点内gpu_ports、同一61层估算profile与合成路由、Prefill chunk512/token预算2048/最多16请求、Decode batch上限64、seed7、固定P/D计划、TTFT1秒/ITL100ms。

KV仍为layer_ready_immediate，关闭FIFO/MLU门控、软件collective保护、联合绑定/预测/窗口、前缀缓存、Decode末层准入与原生网络优先级。保留网络和状态观测，周期/可见延迟保持T001值，recording.mode保持essential。保留显存账本、源读保护和通信完成依赖。

允许在本任务临时输入副本中作以下有界验证差异：

| 项目 | 本轮值或规则 |
| --- | --- |
| 负载内容 | T001冻结workload中按原顺序取前3个请求；仅提取，不改变请求字段 |
| 到达时间 | 保留原始相对到达时间，不压缩、不对齐、不重复注入 |
| 评估窗口 | [0.0, 30.0)秒；取消正式实验的60秒预热，仅用于本轮功能核对 |
| 仿真截止 | runtime.time_limit_seconds=30.0；截止时必须如实保留未完成状态 |
| 配置相对路径 | profile、workload、evaluation输出只调整到本任务输入副本所需的可搬移路径 |
| 记录与审计 | essential保持；通过现有只读RuntimeHooks和EPFlowAudit补充机制检查，不新增仿真事件或高频采样 |

除此之外不得改变冻结条件。profile复制内容与T001逐字节一致，不使用backend/fixtures中的旧少层模型。上述窗口产生的吞吐仅是功能样本统计，不能解释为系统承载能力。

## 唯一样本与绑定

| request_id | arrival_seconds | input_tokens | output_tokens | P | D |
| --- | ---: | ---: | ---: | --- | --- |
| azure-conv-000000000 | 0.0 | 374 | 44 | p0 | d3 |
| azure-conv-000000001 | 4.314579 | 396 | 109 | p1 | d14 |
| azure-conv-000000002 | 4.541877 | 879 | 55 | p2 | d7 |

合计：3请求、1649输入token、208输出token。第三个请求需要512+367两块Prefill；三请求均需要后续Decode。

1. 先核对T001冻结输入和绑定文件，实际记录必须与上表相符。保存源文件哈希、提取索引/ID、原始字段及样本哈希到本任务输入清单。
2. 从T001全量binding_plan按request_id投影，保持每个请求原端点。不得把任意子集重新分配后宣称复用了原计划。
3. 本任务特意选择原始前缀：可调用项目binding_plan_document生成候选子计划，但必须逐条证明其端点等于全量计划的投影，元信息用真实子集重新统计；父计划哈希另放sample-manifest，不给runner期望的计划对象增加自定义字段。
4. 实际run_baseline应传入核对后的binding_plan；运行导出的binding_plan.json必须再次与该子计划及父计划投影一致。
5. 若源版本、样本字段或端点不符，提交差异并停止，不更换样本或改变长度来绕过问题。

## 执行步骤

1. 阅读根AGENTS.md、本TASK、T001报告和REVIEW。保留需求原文，更新STATUS.json为in_progress，保存新的logs/baseline-status.txt。
2. 核对T001冻结文件SHA256。使用Linux/WSL的simai环境，推荐命令前缀 `env CONDA_EXE=/home/m0nesy/miniconda3/bin/conda`，项目入口为 ./inference 和 ./scripts/simai-python。静态预检不等于本轮端到端验收。
3. 在本任务inputs/准备config.json、workload.json、compute_profile.json、binding_plan.json和sample-manifest.json。仅作上表许可差异，给出相对T001的完整字段差异与输入身份；不改T001文件或正式输入。
4. 执行冻结副本的配置validate与backend doctor。后端已就绪时直接使用；仅当既有ns-3构建缺失/失效时，允许按仓库既有命令重建ns-3（jobs=2，构建墙钟上限10分钟），保留日志和新后端身份。不得修改后端源码、安装或升级依赖、切换后端。
5. 编写本任务运行/检查脚本，通过 ./scripts/simai-python 调用现有backend.runner.run_baseline，传入上述配置与绑定计划。允许组合现有RuntimeHooks(event=...)和experiments.ep_audit.EPFlowAudit，回调仅观察数据副本、统计或写检查证据，不修改算法、时钟、事件顺序或网络。
6. 首次运行输出到results/run-a，最多30秒仿真时间、15分钟墙钟。首轮正常完成且无机制检查错误后，以相同配置、输入、绑定、seed和审计方式再运行一次，输出到results/run-b，采用同样上限。只预建results/父目录，run-a/run-b必须由运行工具创建且拒绝覆盖。
7. 两次均完成下列核验，输出results/mechanism-checks.json和results/reproducibility-check.json；保留每次运行自动生成的配置、profile、workload、源码归档、后端身份、事件与CSV，不能拆散运行目录。
8. 按模板写REPORT.md，状态设为submitted或按实际为failed/blocked。未运行第二次、超时、失败、未覆盖机制全部如实列明，提交后停止。

## 必须核验的行为

### 请求、token与Chunked Prefill

- 3个请求全部到达并正常完成，failed=0、incomplete=0；不能只依据命令退出0或SLO达标率判通过。
- 每个请求分别返回44/109/55个token，编号连续、时刻不倒退，无丢失和重复，总计208。
- 每个请求首token由完整Prefill结束产生；879-token输入确实处理512+367两块。此样本应合计完成4个Prefill chunk、1649个query token。
- Decode每轮每成员生成一个token；样本的D端点各不相同，应共完成205个Decode token步/单成员batch。连续重新入队和完整61层执行必须有实际记录支持。
- 本轮不要求形成batch>1。报告实际P/D batch大小，明确未覆盖大批合批和满载行为，不改变绑定或到达时间凑批。

### EP与KV依赖

- 使用现有EPFlowAudit与必要的只读边界检查，核对当前逐层路由/assignment、GPU级激活去重、dispatch完成→专家计算→本地合并/必要TP→combine完成→后继层的依赖。
- Decode每步按当前profile完成全部61层，前3层无routed EP、后58层各有对应EP；token不能在该步层依赖尚未完成时提前返回。
- 核对源batch的EP开始/完成调用数、未完成调用/流/计算步骤与Decode批次均无残留。按本样本的固定分块和绑定，预期Prefill EP调用4×58=232、Decode EP调用205×58=11890，合计12122；实际不符须解释或判为缺口，不重写预期掩盖差异。
- KV逐chunk/层就绪后发送；发送不能早于对应数据就绪。D的第一次Decode计算不得早于该请求Prefill完成和全部输入KV接收完成。
- 无缓存、TP1时，P→D逻辑KV总payload应为1649×70272=115878528字节；核对逻辑消息payload，不能拿含包头/重传的端口字节直接比较。
- 保存足够的边界事件/只读检查结果，支撑以上依赖；不要求另存全部网络采样或逐包事件。

### 资源、指标和可复现性

- 最终所有显存域occupied/reserved为0；必要读依赖、EP在途调用和逻辑传输无残留。计数验证需要结合运行汇总和现有审计，不能仅看单个空队列。
- 正常导出summary、requests.csv、tokens.csv、timeseries.csv，以及后端、计算、EP、Prefill、Decode和资源摘要；报告实际TTFT、handoff、later_max_itl、SLO达标完成吞吐、失败和未完成数。
- SLO违约本身不等于基础链路错误。不得为通过验收调整SLO、带宽、计算profile、输出长度或窗口。
- 两次语义结果应一致：token时刻、请求状态及SLO、绑定端点、EP/KV逻辑统计和最终资源。可直接比较对应CSV/JSON或规范化后比较语义字段；不要求日志路径、源码归档路径、独立墙钟计时等非语义元数据字节相同，也不得把真正时序差异当元数据忽略。
- 墙钟只记录为仿真运行成本，不作为GPU计算时间、控制器决策延迟或算法收益。

## 验收条件与交付

| 编号 | 通过标准 | 证据 |
| --- | --- | --- |
| A1 | 样本、绑定、哈希及许可配置差异正确，T001冻结件未修改 | inputs/sample-manifest.json、输入与绑定文件、差异清单 |
| A2 | 两次真实ns-3端到端运行完成，3请求/208token，0失败/未完成 | results/run-a、results/run-b、命令与退出码日志 |
| A3 | 多chunk Prefill、205个Decode步及61层EP依赖、KV就绪与D准入检查通过 | results/mechanism-checks.json、EPFlowAudit结果和必要边界记录 |
| A4 | KV逻辑payload守恒，最终显存和活动依赖无残留 | 网络/EP/资源汇总及检查结果 |
| A5 | 两次语义结果可复现 | results/reproducibility-check.json与原始CSV/JSON |
| A6 | 指标导出完整，明确功能样本、SLO违约与未覆盖范围 | REPORT.md、每轮result/summary.json |

此外提供results/acceptance-summary.json，逐项列A1–A6的passed/failed/not_run及证据路径。状态与最终报告按仓库模板填写；执行agent不得设置accepted。

## 范围限制与停止条件

- 允许新增/修改仅本任务目录和指定work/草稿；必要的既有ns-3重建仅写原构建目录。不得修改正式源码、正式配置、原始数据或T001冻结文件。
- 本轮最多两次端到端启动；首轮异常、超时、机制失败或输入身份冲突时保留现场并停止，不自动补跑、延长预算、换样本或修算法。静态准备脚本的普通错误可在开跑前修正并记录。
- 两次运行墙钟预算合计最多30分钟；如需重建，另限10分钟。超时按失败/阻塞如实记录，不当成功或性能结果。
- 若工具策略拒绝WSL或后端执行，保存拒绝信息，以blocked报告；不能用Windows静态校验、计时器或synthetic fixture替代实际ns-3运行。
- 不开展算法对比、KV/EP干扰归因实验、持续加压、全量Azure轨迹、扩容、DeepEP适配、MLU计时或完整check.sh；不提交/推送、不启动其他agent，不恢复旧任务。
- 发现源码问题，给出最小复现与定位，交指挥安排后续修复；不在本轮悄悄改代码或验收条件。

## 回报

完成后回复以下绝对路径、结论、实际运行/验证结果和需指挥决定事项：
`/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T002-bounded-baseline-validation/REPORT.md`

提交后停止，等待指挥审核；不能把“基础链路验收通过”写成研究问题或算法收益已成立。