# T002-r02：有界样本的基础链路验收 — 审核意见

- 审核 agent：指挥 agent（Codex）
- 审核时间：2026-11-07T18:50:58+08:00
- 执行报告：/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T002-bounded-baseline-validation-r02/REPORT.md
- 结论：**changes_requested（补齐 A3 边界审计证据）**

## 核心判断

**3 个冻结来源请求已在实际 ns-3 后端中完成两次端到端运行，基础链路“能跑通”已经证实。** 两轮各完成 208 个 token，失败和未完成均为 0，token 时刻、请求结果、绑定、EP/KV 汇总及最终资源完全一致。

但本轮还不能判为 A1–A6 全部通过：A3 要求保存 KV 数据就绪、完整 Prefill 完成、首次 Decode 计算以及层完成与 token 返回之间的边界检查。当前执行脚本只保留事件计数，部分检查名称比实际条件更强。缺口是验收证据覆盖不足，**没有发现由本次证据证实的运行时序错误**。

指挥未重新启动仿真。独立只读核验 76 项均通过，并单独记录 3 类未覆盖要求；这 76 项不包含缺失的边界断言，不能据此替代 A3。

## 检查记录

以下证据均位于本任务目录，路径基准为：
/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T002-bounded-baseline-validation-r02/

| 验收项 | 独立核对的原始结果 | 判断 |
| --- | --- | --- |
| A1 | 重算 T001 的 config/profile/workload/binding/manifest 五件哈希；本轮五个输入与原 blocked 轮次逐字节一致；3 请求逐字段等于父负载前缀，绑定逐字段等于父计划投影；直接比较配置，只有窗口起点、终点、time_limit 三处许可差异；两轮实际导出配置等于输入配置加载并规范化的结果 | 通过 |
| A2 | 两轮 requests.csv / tokens.csv / events.jsonl；各 3 请求全部完成，44/109/55 个 token、合计 208，编号连续、时刻不倒退；真实后端身份 simai_ns3_rdma / protocol 4 / fingerprint 23ec96146135d0706e39；后端结束时间均为 30 秒；日志与墙钟一致 | 通过 |
| A3 | 4 个 chunk，token 直方图 374/396/512/367；205 个 Decode 步，每步 61 层；EPFlowAudit 核对 12122 次调用、141968 条逻辑流及 EP 内部路由/ACK/层依赖。下面列出的 KV 与 token 边界未被实际保存和断言 | **证据不足，需返工** |
| A4 | 两轮逻辑 KV started/completed 均为 115878528 字节、244 条消息；32 个显存域 occupied/reserved 均为 0；EP 调用、计算、队列和需求为空，EPFlowAudit 无活动流/步骤/批次 | 通过；KV 逐消息就绪与 ACK 断言归入 A3 补证据 |
| A5 | 独立比较全部 events、tokens、requests、EP 汇总（包含执行脚本排除的 compute_queue_wait_seconds）、通信统计、绑定、P/D 摘要、资源、EPFlowAudit 和 hook 计数；均完全一致。源码树哈希也相同 | 通过 |
| A6 | 指标由原始 token CSV 重新计算并与请求 CSV 对照；完成窗口 3/3 达标，goodput=3/30=0.1 rps；归档、各输入哈希与完整 tar 文件清单核验通过；当前核心源码与运行源码快照一致；报告明确 batch 全为 1、profile 估算、路由合成、SLO 人工假设 | 通过 |

两轮墙钟合计 568.338 秒，处于任务预算内。本次功能样本的 0.1 rps 不表示系统承载率或算法收益。原 T002 的 STATUS.json 仍为 blocked，原证据完整保留；本轮只更新 r02 状态。

独立审核证据：

- results/review-verification.json：76 项核验、逐请求重算指标、实际事件清单、3 类缺口。
- logs/review-artifacts.txt：WSL simai 下只读核验输出。
- 审核脚本：/home/m0nesy/Postgraduate/Inference/work/agents/2026-11-07/T002-bounded-baseline-validation-r02/review_artifacts.py。

## 问题与返工要求

### 1. KV 就绪到发送：计数不能证明时序

执行检查脚本的 layer_ready_precedes_transfers 只比较 244 次 ready 与传输计数；run_validation.py 的 EventLog 只保存总数和整个运行的首尾时刻。EPFlowAudit 对 KV 消息不作逐条审计，它的 transfer 分支只处理 ep_dispatch / ep_combine。

因此，“244 条就绪事件、244 条 KV 消息”不能证明每条发送对应正确请求/层/chunk，也不能证明发送晚于对应数据就绪或 KV 完成具有 ACK/readiness 条件。

要求：使用现有只读 hook，按 request/layer/chunk 或 token/字节区间匹配 KV；保存就绪、发送、完成的必要时间和标识，断言发送不早于实际就绪，逐消息检查完成的 source_reads_complete / destination_ready。继续核对总字节、消息数与无残留。无需保存所有 81 万条回调。

### 2. 完整 Prefill 到首次 Decode 计算：当前检查用了 Prefill 开始

decode_admission_after_prefill_and_all_kv 实际验证的是：
prefill_started ≤ kv_all_received ≤ decode_started，以及首 token 晚于 prefill_started。

这没有验证“首 token 来自完整 Prefill 结束”，也没有记录每个请求第一次 compute_step_started(phase=decode) 相对于完整 Prefill/KV 完成的时刻。

要求：对每请求保存最终 chunk / 全部 Prefill 完成边界，核对完整输入已处理、首 token 的对应时刻；保存首次 Decode layer 0 计算开始边界，明确检查它同时晚于完整 Prefill 与全部输入 KV 接收完成。879-token 请求必须对应两个 chunk。

### 3. 61 层到 token 返回：EP 层审计未连接 token CSV

EPFlowAudit 已证明每个 decode_batch_completed 前完成 61 层及对应 MoE 依赖。但 token_returned 由 recorder 直接记录，不经过当前 RuntimeHooks.event；本轮没有保存 batch/最终层完成时刻来与 token CSV 逐项连接。

要求：保留每个 Decode batch 的成员、context/token 身份、最后一层完成时间和 batch 完成时间；将 token 2..N 逐一与对应 batch 对齐，断言返回不早于该步最后一层和 EP 依赖完成，无漏项/重复。继续复用现有 EPFlowAudit，不修改正式执行器。

### 4. 修正检查脚本的假阳性路径

/home/m0nesy/Postgraduate/Inference/work/agents/2026-11-07/T002-bounded-baseline-validation-r02/check_results.py 的 A1 状态直接写为 passed；t001_ok、projection_ok 只进入说明文字，配置差异也直接信任 sample-manifest。即使这些条件失败，它仍可能报告 A1 passed。本次指挥重算证实输入正确，但该脚本本身不能作为自动验收依据。

返工检查应从实际文件计算条件，缺证据标 not_run 或 failed，并让总结果与退出码正确反映失败。上述三个 A3 检查改为实际边界断言。A5 同时保留 compute_queue_wait_seconds 比较：它是仿真时序量；本次独立核对该项两轮确实相同。

代码定位：

- /home/m0nesy/Postgraduate/Inference/work/agents/2026-11-07/T002-bounded-baseline-validation-r02/run_validation.py:26：EventLog 仅计数。
- /home/m0nesy/Postgraduate/Inference/work/agents/2026-11-07/T002-bounded-baseline-validation-r02/check_results.py:151：计数冒充先后关系。
- /home/m0nesy/Postgraduate/Inference/work/agents/2026-11-07/T002-bounded-baseline-validation-r02/check_results.py:177：未检查 Prefill 完成/首次 Decode 计算。
- /home/m0nesy/Postgraduate/Inference/work/agents/2026-11-07/T002-bounded-baseline-validation-r02/check_results.py:228：排除仿真队列等待时间。
- /home/m0nesy/Postgraduate/Inference/work/agents/2026-11-07/T002-bounded-baseline-validation-r02/check_results.py:298：A1 无条件 passed。

## 下一轮条件

本轮原始结果有效且必须保留，返工只补上述边界审计和检查脚本条件，沿用相同 3 请求、绑定、配置及机制。暂不进入算法对比或竞争归因实验。

现有 essential 事件不包含所缺的 chunk/逐层/逐消息边界，不能事后从事件总数还原这些时刻。需要补运行时，应由指挥另发返工任务书，在新目录明确运行次数与预算；不能覆盖 r02，不能把原任务最多两次启动的额度延续为无限补跑。本审核不启动额外仿真或自动创建下一项任务。
