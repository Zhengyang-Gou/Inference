# T003-r02 — 指挥审核意见

- 审核 agent：指挥 agent（Codex）
- 审核时间：2026-11-08T09:41:47+08:00
- 补充报告：/home/s223/m0nesy/Inference/outputs/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/REPORT.md
- 唯一原始研究需求：/home/s223/m0nesy/Inference/outputs/agents/2026-11-07/T003-kv-ep-contention-attribution/TASK.md
- 本轮修复范围：同目录 SCOPE.md 中用户明确授权的静态修复与复核。
- **本轮结论：accepted，仅指 r02 修复范围。**
- **父任务结论：原 T003 的 A6=failed、all_passed=false 保留；不认定原执行完全符合任务书。**
- 数据可采性：接受经独立复算的第二次 live 及三类重放数据作为**带流程偏离的有限历史诊断证据**，不提升为无偏离的正式基准或服务器新实验。

## 核心判断

本轮纠正了实际启动次数、失败停止条件、研究表述及证据来源；已完成用户要求的修复。指挥在服务器做了37项独立静态检查及4项失败/有效运行身份比较，合计41项均通过，没有启动 live 或 network replay。

原 T003 首轮确已推进到472621ns，提交12条native命令并登记1条Probe跨节点逻辑流；后续有效live和三次重放违反原任务的启动/停止规则。修复不能抹去这件事，墙钟未超预算也不能替代合规。本审核确认 A6 failed。

精度说明：上述1条是Probe的逻辑流登记，不能单凭它证明SEND已被后端接收或首包已发出。裁决依据是live已启动、后端已有native提交并推进仿真时间，不以首包是否发出作为“静态准备”的排除标准。

另一方面，第一次失败没有混入第二条完成轨迹；失败/有效两次的配置、profile/负载字节、绑定、源码和后端身份一致，完整轨迹的 both 重放又逐流/逐阶段复现为0ns误差。现有证据可以支持有限的历史机制诊断，不必为了改写历史合规状态而重复同一实验。将来若需要服务器本轮正式实验或算法基准，应另立明确任务与预算；本审核未授权启动。

## 独立核对记录

| 核对项 | 原始证据与方法 | 判断 |
| --- | --- | --- |
| 完整归档 | 重算完整包SHA256；323件独立解包文件和262件跟踪副本逐文件比对；原T003目录84件文件与执行前树清单逐项相同 | 通过 |
| 输入/完成轨迹 | 8条原始请求前缀、父绑定投影、profile身份；8请求/550token，编号与长度，失败/未完成0；32显存域释放，32074次EP调用无活动残留 | 通过 |
| 重放身份 | 原始flows.csv与三份completions.csv逐行重算，检查索引唯一性、完整性和对应类别；201677条跨节点流的both ACK与live完全相同 | 通过 |
| EP阶段 | 按max(冻结local_ack,远端ACK)重建64148个阶段门，逐阶段与live误差0ns | 通过 |
| 有符号互扰 | 从原始ACK重算全部流和阶段差值，并与原flow-comparison CSV核对 | 通过，详细数值见下文 |
| 请求边界 | 流式读取原始event-links，核对transfer↔flow、stage↔call/layer/batch；用真实batch成员和context推导token身份；重建8个KV准入门、8个首token和542个Decode token最终combine | 通过，指定边界暴露为0 |
| native日历 | 675175条注册：298164 TIMER、201677 SEND、175334 LOCAL_PORTS；所有跨节点flow均唯一对应SEND | 通过 |
| 启动与门禁 | 从console日志标记重新提取2次live；首轮RPC/失败产物证实已进入网络阶段；核对修订件A6 failed、all_passed=false及V1–V4 | 通过修正，原执行不合规 |
| 失败/有效运行身份 | 两次portable config、profile/workload字节、binding_plan、source-tree与backend元信息逐项比较 | 4项均相同 |
| 历史记录未改 | 审核期间原目录树保持52fe4a5f4a57fa7874bf57fad00294b26e99bfb26e73dd19faccdf1974ddafe5；本审核只新增r02审核件并更新其状态及指挥进度索引 | 通过 |

审核脚本：/home/s223/m0nesy/Inference/work/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/commander_review.py。
审核证据：results/review-verification.json、results/review-restart-comparison.json、logs/commander-review.log。

指挥脚本首次比较“目录树计算结果”与“多含tree标签的JSON文档”时出现结构性假失败；已改为比较相同的文件清单、数量、树哈希字段，重新静态核验通过。首版结果与日志原样保留为review-verification-initial.json、commander-review-initial.log；没有新增网络运行。

## 科学结论

**检出极弱网络互扰；冻结条件下指定请求边界暴露为零；端到端反事实性能变化未测。**

| 项目 | 独立重算结果 |
| --- | --- |
| EP→KV | 671条KV流，3条正差值，最大+144ns；平均+0.220566ns，无负差值 |
| KV→EP | 201006条EP流，5条正差值、1条负差值，最大+136ns、最小−127ns；平均+0.001642ns |
| EP阶段 | 64148阶段仅1个非零差值：stage7059、ep-3530、decode combine，+9ns |
| >1μs计数 | 两个方向均0；这只表示没有超过该描述阈值，不表示互扰不存在 |
| 指定请求边界 | 8个ΔG均0，8个首token与542个Decode token最终combine暴露均0；身份和实际token时刻逐项一致 |
| 端到端反事实 | 未测；没有重新执行移除互扰后的请求/计算调度，不能给出反事实goodput/TTFT/ITL |

合计是9条非零流差值（3条KV正差值＋5条EP正差值＋1条EP负差值），不能漏掉反向差值。它们与唯一非零阶段差值均未暴露到本次检查的请求边界。

这些结论限于8请求、单seed、固定提交日历及当前指定依赖门。它们不证明整个模型不存在竞争、不证明完整关键路径总影响严格为零、不证明其他负载下算法无效，也不支持关闭整个研究主线。数据可用于决定后续场景问题；当前没有已测得的请求级收益可用来支持算法优势。

## 流程偏离裁决

1. V1（live启动2次）、V2（失败后未停止）、V3（已启动阶段修复后重启且未先报告）、V4（错误排除为网络开跑前静态错误）均保留。V4是分类/记录问题，与前面三项执行偏离分别登记。
2. 确认原 A6 failed、原 all_passed=false。原REPORT与原acceptance-summary仍原字节保留，其中的“全部passed”和过度结论已被r02修订件及本审核替代。
3. 第二次live和重放数据仅按“有偏离的历史诊断”采纳。此决定不追认第一次失败后的补跑为合规，也不把父任务状态包装成完整验收通过。
4. r02状态设置accepted，表示静态修复要求已完成，不表示父任务A6通过。原T003 STATUS保留submitted的历史原文，当前裁决以本REVIEW及coordination/PROGRESS.md为准。
5. 无需继续为文案或历史启动次数补跑。是否开展服务器新实验或其他场景属于下一次明确任务；本轮不自动派发。

## 证据定位勘误

确认D1–D3属于迁移后的路径解析问题，**不是证据丢失**。原报告相对路径描述原执行目录；Git仅保留了选定快照，其余文件在已验证的完整包中。

本轮增加 EVIDENCE_LOCATIONS.md 和 results/evidence-locations.json，提供包内相对路径与当前独立审计副本路径，不修改原REPORT。后续读者可以仅在服务器恢复/审计原文件，无需已卸载的WSL。

完整包：
/home/s223/m0nesy/Inference/artifacts/history/wsl-2026-11-07-task-records/records-full.tar.gz

SHA256：5ea2d7c640e060c32b259a0d8a94f97b758f05b29bf562c11611f8704fc18a8a。

## 版本与后续状态

本轮没有修改正式仿真机制、冻结条件、原T003任务文件或既有实验结果。所有核验在服务器执行，本机仅传输/阅读。

审核与修订记录已按精确文件清单准备纳入版本记录；当前未commit、未push。进度索引由指挥更新，标明r02修复已接受、原A6失败和历史数据可采边界。没有发布T004或新研究任务。
