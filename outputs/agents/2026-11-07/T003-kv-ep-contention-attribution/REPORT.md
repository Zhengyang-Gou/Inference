# T003：KV/EP 竞争及请求关键边界影响归因 — 执行报告

- 执行 agent：WorkBuddy（执行 agent）
- 开始/结束：2026-11-07T19:21:50+08:00 / 2026-11-07T20:05:10+08:00
- 状态：**submitted**
- 任务目录：`/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T003-kv-ep-contention-attribution`
- 前置任务：T001 accepted、T002-r03 accepted（r02 的 changes_requested 为历史状态，未改写）

## 结论

**研究判断：证据层级为「共享资源存在，但未检出可测量的 KV↔EP 互扰，也未暴露到请求必要边界」；本轮不支持在同等条件下进入 KV 调度算法对照。**

在 8 个冻结请求、30 s 窗口、seed 7、固定 P/D 绑定、真实 ns-3 包级网络的一次端到端运行中：

- **资源共享成立但很窄**：仅 2 个有方向 NIC RX 端点（`rx:12`、`rx:25`）出现两类流同时在途，合计重叠 `16.388 μs`；1 ms 采样中 24 个 egress 端口、1032 个区间同时有 KV 与 EP 字节。但混合端口 PFC 暂停为 0、峰值队列仅 56096 B、峰值利用率 0.244；**全部 29860 次 PFC 暂停与 5346528 B 峰值队列都落在 56 个 EP-only 端口**（`kv_bytes=0`），即拥塞主要是 EP 自竞争，不是跨类竞争。
- **移除对照几乎为零**：EP→KV（移除跨节点 EP）影响 671 条 KV 流中的 3 条，最大 `+0.144 μs`、均值 `0.000221 μs`；KV→EP（移除跨节点 KV）影响 201006 条 EP 流中的 5 条，最大 `+0.136 μs`、均值 `0.000002 μs`，另有 1 条改善 `-0.127 μs`（保留负值）。**两个方向均无任一流的正向拖延超过 1 μs**（计数门槛，不是显著性判断）。64148 个 EP 阶段里只有 1 个 `decode.ep_combine`（layer 42）出现 `+0.009 μs`，其余全为 0。
- **请求必要边界暴露为 0**：8 个请求的 P→D 准入门 `ΔG` 全为 0 ns；8 个首 token 的最终 Prefill combine、542 个 Decode token 的最终层 60 combine（共 550 个必要边界）暴露全为 0 μs。8 条非零流差值和那 1 个阶段差值都被「不是最晚到达的 KV 流」「不在最终层 60」「阶段门取 `max(local_ack, 远端 ACK)` 后被更早完成覆盖」等依赖遮蔽，没有落到任何请求等待边界。
- **实际请求指标**（一层）：8/8 完成、550 token、失败/未完成 0、SLO 违约 0；completion-window goodput `0.2667 rps`，TTFT 均值 `117.209 ms`、handoff `17.594 ms`、later_max_itl `18.782 ms`。这是本样本的实际表现，**不是**移除互扰后的反事实结果。
- **第三层未测**：重放冻结提交日历，不重新执行请求/计算调度，因此没有移除互扰后的 goodput / TTFT / ITL 轨迹，也未计算「去掉竞争后的 SLO goodput」。本轮最多证明「资源共享」与「条件性边界暴露（为 0）」两层，不能推出端到端 SLO 因果改善。

据此，**当前样本不足以支持进入 KV 调度算法对照**：互扰量级 `≤0.144 μs` 相对 TTFT `117.209 ms` 约为 `1.2×10⁻⁶`，所有请求边界暴露为 0，门控/优先级类算法在本样本里没有可作用的机制；若此时做算法对照，任何差异都不可能归因于 KV/EP 竞争。局限是 8 请求、全部 batch=1、单 seed、估算 profile、合成路由与人工 SLO；若研究目标是 KV 调度，需先由指挥明确一个能形成实际跨类瓶颈的负载/拓扑范围（本轮没有加压授权）。

负结果按任务书合法通过，A1–A6 全部 passed（验收的是证据链完整性，不要求正结论）。

## 逐项验收

| 编号 | 状态 | 原始证据路径与解释 |
| --- | --- | --- |
| A1 | passed | `results/identity-check.json`、`results/environment-check.json`、`inputs/sample-manifest.json`：冻结 manifest `811cc296…af78` 与 4 件冻结文件哈希全匹配；8 请求逐字段等于冻结负载原顺序前 8 条、到达/长度不变、无平移重复；绑定计划由 `binding_plan_document` 生成且 8 条端点与父计划投影逐字段一致（父计划哈希 `9a48e35f…`）；相对 T001 冻结 config 恰 3 项许可差异（窗口起/止、time_limit），诊断根 config 另加 2 项可搬移路径；源码树 `427d8527…`、155 个核心 Python 文件与后端 fingerprint/二进制哈希均与 accepted 的 T002-r03 相同。 |
| A2 | passed | `results/mechanism-checks.json`（31/31）、`results/live-stage.json`、`results/metrics-summary.json`、`results/diagnostic/run/`：8 请求完成、550 token、11 chunk / 3913 query token / 542 Decode 步 × 61 层 / 32074 EP 调用、KV 逻辑 payload `274974336 B`、671 条消息收发一致、32 显存域 occupied/reserved 全 0、无活动流/EP/批次、`EPFlowAudit=passed`；完整 CSV/事件/绑定/源码/后端身份留在同一 `run/`。 |
| A3 | passed | `results/replay-fidelity.json`（15/15）、`results/diagnostic/native_schedule.jsonl`、`replay-both/`：675175 条日历（TIMER 298164 / SEND 201677 / LOCAL_PORTS 175334）全部提交；201677/201677 排空、ID 无漏失；**每条跨节点流 ACK 与 live 最大绝对误差 0 ns**；冻结 local_ack + 远端 ACK 重建的 64148 个 EP 阶段完成时刻与 live 最大误差 0 ns；separate-by-class 端点对检查为真、port cooldown 检查保留（本样本触发 0 次）、后端身份与共享源码归档一致。 |
| A4 | passed | `results/contention-summary.json`、`results/flow-comparison.csv`（201677 行有符号差值）、`results/stage-comparison.csv`（64148 行）、`results/port-intervals.csv`（243186 行 1 ms 差分 + 省略规则）、`results/diagnostic/replay-collective|kv/`：三类重放各完成一次，双向、四类 EP 阶段（prefill/decode × dispatch/combine）与最终层分组齐全，负值保留；共享资源含 2 个有方向 NIC 重叠、24 个混合端口、1032 个混合区间、队列/利用率/PFC 分解。 |
| A5 | passed | `results/event-links.jsonl`（transfer_id↔flow_index、EP stage↔call/phase/layer/batch、batch↔成员/context、完整 Prefill/全 KV/D 准入/各 Decode 边界）、`results/kv-transfer-links.json`、`results/request-impact.csv`、`results/request-impact-summary.json`：8/8 请求 KV 门与 542/542 Decode token 最终层精确映射，首 token 与 550 个必要边界身份误差 0；`T_KV_all^both` 与 live `kv_all_received` 逐请求一致；端到端反事实明确标为 `not_measured`。 |
| A6 | passed | 本 REPORT、`results/acceptance-summary.json`、`logs/commands.md`：4 阶段墙钟合计 19.03 min（<70 min），单 seed、无算法比较/扩容/全量轨迹/完整 check.sh；结论给出方向、位置、量级与不确定项；负结果明确写出。 |

## 实际修改

| 文件 | 本轮修改及目的 |
| --- | --- |
| `outputs/agents/2026-11-07/T003-kv-ep-contention-attribution/` | 新增 TASK 原文保留、STATUS/REPORT、`inputs/`（5 文件）、`logs/`（原始命令与全部阶段日志）、`results/`（诊断根、比较 CSV、汇总 JSON、首轮失败现场）。 |
| `work/agents/2026-11-07/T003-kv-ep-contention-attribution/` | 新增 7 个草稿脚本：输入准备、环境核对、live 采集、静态钩子复现、分阶段重放、保真门槛、只读分析。 |
| 正式源码 / 正式配置 / 原始输入 / 后端 / T001 / T002-r03 文件 | **未修改**；`git status --short --untracked-files=all` 执行前后逐行一致（71 行），全部新增内容位于 `.gitignore` 覆盖的 `outputs/`、`work/`。 |

执行前已有改动（上轮删除 flowSim 遗留的 59 个 `M` / 2 个空目录等）全部保留，未 reset/checkout/clean。本轮草稿唯一一次代码修正是把 `T3Probe` 自有事件计数从官方 `Probe.counts` 改名为 `hook_counts`（静态命名冲突，见「风险」），不涉及正式源码或仿真参数。

## 验证证据

| 实际命令 | 运行目录 | 退出码/结果 | 日志或结果路径 |
| --- | --- | --- | --- |
| `./scripts/simai-python work/.../prepare_inputs.py` | 仓库根 | 0，8 请求 / 3913 输入 / 550 输出 / 542 Decode 步 | `logs/01-prepare-inputs.log` |
| `./inference --config <T003 inputs>/config.json validate` | 仓库根 | 0，配置有效 | `logs/02-validate-doctor.log` |
| `./inference --config <diagnostic>/config.json validate` | 仓库根 | 0，配置有效 | 同上 |
| `./inference backend doctor` | 仓库根 | 0，`backend_ready=true`，fingerprint `23ec96146135d0706e39` | 同上 |
| `./scripts/simai-python work/.../check_environment.py`（首轮） | 仓库根 | **1**，草稿键名笔误 | `logs/02a-identity-attempt1-failed.log` |
| 同上（修正后） | 仓库根 | 0，`all_passed=true` | `logs/03-identity-check.log` |
| `./scripts/simai-python work/.../live_baseline.py`（第 1 次） | 仓库根 | **1**，静态钩子命名冲突，0.835 s，0 请求完成 | `logs/04-live-baseline.log`、`results/attempt1-failed/` |
| `timeout -k 20 1500 … live_baseline.py`（有效） | 仓库根 | 0，911.906 s，31/31 机制检查 | `logs/07-live-baseline.log` |
| `timeout -k 20 900 … replay_stage.py --stage both` | 仓库根 | 0，96.516 s，201677/201677 | `logs/08-replay-both.log` |
| `./scripts/simai-python work/.../check_replay_both.py` | 仓库根 | 0，15/15，最大误差 0 ns | `logs/09-fidelity.log` |
| `timeout -k 20 900 … replay_stage.py --stage collective` | 仓库根 | 0，86.651 s，201006/201006 | `logs/10-replay-collective.log` |
| `timeout -k 20 900 … replay_stage.py --stage kv` | 仓库根 | 0，46.932 s，671/671 | `logs/11-replay-kv.log` |
| `./scripts/simai-python work/.../analyze_attribution.py` | 仓库根 | 0，A1–A6 全 passed，`errors=[]` | `logs/12-analyze.log` |

失败、跳过、未运行项见 `logs/commands.md`「预算与次数」「未运行」两节。两处失败（身份核对键名、live 第 1 次静态钩子冲突）均在网络重放启动前修正，原始失败日志与首轮运行产物完整保留；**没有任何网络阶段被静默重启**，三类重放各只启动一次。

## 结果与解释

**证据类型**：有限样本功能仿真 + 冻结日历网络移除对照（不是持续负载、不是算法收益实验）。WSL `simai` Python 3.12.16，ns-3 后端 fingerprint `23ec96146135d0706e39`，protocol 4，源码树 `427d8527…`。

**输入与配置身份**：`inputs/sample-manifest.json` 记录 T001 五件冻结文件、8 请求逐字段提取、父绑定投影与全部输入哈希；`results/diagnostic/config.json` 为唯一有效运行配置，`run/reproducibility.json` 与 `replay-*/reproducibility.json` 共享 `inputs/source/` 归档；`results/replay-inputs-manifest.json` 给出日历、流、阶段、配置、源码、后端与映射哈希，**重放可脱离 live 进程从文件恢复**。配置仅窗口起止 `60→0 / 3501.72→30`、time_limit `4101.72→30` 与两项可搬移路径；SLO 仍 TTFT 1 s、ITL 100 ms（人工假设），`recording.mode=essential`、1 ms 网络采样、可见延迟 100 μs 均未改。

**样本与窗口**：冻结负载原顺序前 8 请求，到达 0–8.251431 s 不平移、不重复注入；3913 输入 / 550 输出 token；测量窗口 `[0,30)`，无预热；seed 7；所有 Prefill/Decode 批次大小均为 1（各 D 不同），不覆盖 batch>1 或满载。

**数值依据（全部指向原始文件）**：

1. 实际基线：`results/diagnostic/run/result/{summary.json,requests.csv,tokens.csv,timeseries.csv}`、`results/metrics-summary.json`。8/8 完成、8/8 SLO 达标，goodput `0.2667 rps`、完成吞吐 `0.2667 rps`、输出 `18.33 token/s`、峰值在途 3；TTFT mean/min/max `117.209 / 56.723 / 280.010 ms`，handoff `17.594 ms`，later_max_itl `18.782 ms`，E2E mean `1.312 s`。
2. 共享资源：`results/diagnostic/baseline_summary.json`、`results/port-intervals.csv`、`results/contention-summary.json → shared_resource`。有方向端点在途重叠 `rx:12 = 11.229 μs`、`rx:25 = 5.159 μs`；混合端口 24 个、混合 1 ms 区间 1032 个（30001 个采样、30000 个有效区间、243186 行写入、2156814 行按全零且无排队/PFC 规则省略）；混合端口 PFC 0、峰值队列 56096 B、峰值利用率 0.244；全部 29860 次 PFC 与 5346528 B 峰值队列位于 56 个 EP-only 端口。两类证据时间粒度不同：端点重叠是事件级精确在途时长（含 pacing/ACK 等待），1 ms 端口计数是累计字节差分，均非逐包服务时间。
3. 双向移除差值：`results/flow-comparison.csv`、`results/stage-comparison.csv`、`results/contention-summary.json → traffic_classes / ep_stage_groups`。
   - EP→KV（`replay-kv` 为孤立）：KV 流 671 条、逻辑 payload `274974336 B`；全量时延 mean `13.893948 μs` / p95 `17.424 μs`，孤立 mean `13.893727 μs`；有符号 added mean `0.000221 μs`、p50/p95/p99 全 0、max `0.144 μs`；正向 3 条（最大 `gpu6→gpu12`，请求 `…006`），`>1μs` 0 条，负向 0 条。
   - KV→EP（`replay-collective` 为孤立）：EP 流 201006 条、逻辑 payload `16977795072 B`；全量 mean `19.983813 μs` / p99 `334.965 μs` / max `468.709 μs`，孤立几乎相同；added mean `0.000002 μs`、max `0.136 μs`、min `-0.127 μs`；正向 5 条（集中于 `gpu15→gpu30`、`gpu30→gpu8`、`gpu12/13→gpu27`，请求 `…001`/`…005`，阶段 `decode.ep_combine` 3 / `decode.ep_dispatch` 2），改善 1 条，`>1μs` 双向均 0。
   - 四类 EP 阶段：prefill.dispatch 638、prefill.combine 638、decode.dispatch 31436、decode.combine 31436；added 最大值仅 `decode.ep_combine` 的 `0.009 μs`（1 个 stage 7059，layer 42），其余全 0；final-layer-60 combine（prefill 11 / decode 542）added 全 0。EP/KV 逻辑 payload 与端口字节分开报告：端口 `tx_bytes` 含头/重传，1 ms 混合字节里 EP 占 `68359888080 B`、KV 仅 `689228264 B`（约 1%）。
4. 请求边界：`results/event-links.jsonl`、`results/request-impact.csv`、`results/request-impact-summary.json`。按真实身份（KV 流 ACK、block/layer/offset、EP call→source/phase/layer/batch、batch 成员与 context、token_index=context−input+2）配对，8 个 `T_KV_all^both` 与 live `kv_all_received` 逐请求一致，542 个 Decode token 与首 token 8 个与最终层完成时刻误差 0；`ΔG` 8 个请求全 0 ns（无请求被 Prefill 遮蔽，但决定门的最晚 KV 流本身未受互扰），550 个必要最终 combine 边界 added 全 0 μs；`ΔG` 与边界暴露最大 `0 μs`，相对 TTFT `117 ms` 无可观量级。实际 D 首批启动相对门的等待另列在 `request-impact.csv` 的 `decode_queue_wait_after_gate_ns`，未混入 `ΔG`。

**结论边界**：单 seed 有限样本，不给置信区间；无 batch>1、无持续负载/承载能力结论；无算法对照收益；无端到端反事实 SLO/goodput 变化；估算计算曲线、合成专家路由、人工 SLO 阈值均继承冻结输入。墙钟 19.03 min 是 CPU 仿真成本，不是 GPU 时间或调度器决策时间。

## 风险、未完成项与阻塞

- 无阻塞，4 个阶段全部完成，保真门槛与预算均未触发停止条件。
- 已如实记录并处理两处失败：①身份核对草稿键名笔误（退出 1，日志保留，修正后通过）；②live 第 1 次启动因草稿将官方 `Probe.counts` 覆盖为 `Counter` 在 0.835 s 处中止，0 请求完成、无任何指标；按任务书静态脚本错误条款修正并于重放启动前重跑，首轮现场完整保留于 `results/attempt1-failed/`。这使 live 进程实际被启动 2 次（第 1 次为失败的静态草稿启动），已在 `logs/commands.md` 与本报告中显式登记，未静默重启任何网络阶段。
- 覆盖范围限制：8 请求、全部 batch=1、单 seed、30 s 窗口；混合端口只有 2 个有方向 NIC 重叠且总时长 16.388 μs，因此「未检出互扰」只对本样本与当前拓扑/负载成立，不能外推到持续高压或 batch>1。
- 流程记录：会话中途被用户中断并重启，重启后 `STATUS.json` 的 `in_progress` 写入未落盘（文件 mtime 仍是派发时刻 19:19:41）；本轮最终状态直接置 `submitted`，`agent`/`started_at`/`updated_at` 已补齐，实际开始时间以本报告开头为准。执行前的 Git 基线与全部证据均在中断前后保持一致。

## 下一步建议

1. 若研究主线仍是 KV 调度，先由指挥单独立项确定能形成实际跨类瓶颈的有界场景（更高并发/batch>1/不同 P:D 分布或更紧的链路），并在该场景下重跑本轮同构的保真+移除对照；本轮数据不能作为算法收益先验。
2. 可复用本轮 `results/diagnostic/` 的完整日历与 `results/replay-inputs-manifest.json`，在不启动 live 的前提下复核两类移除差值，或针对 EP 自竞争（29860 次 PFC、5.3 MB 队列）单独立项研究——但那是 EP 内部拥塞议题，不属于本任务范围。
3. 若指挥接受本轮负结果，可将「KV/EP 竞争在冻结基线样本中不成立」写入研究记录，并据此关闭竞争归因主线；本轮不自动规划或执行下一项。

## 需要指挥决定

1. 是否接受「未检出可测量互扰、请求必要边界暴露为 0」作为 T003 结论，并据此判定**当前样本不进入 KV 调度算法对照**。
2. 若不接受，请明确返工范围（是否允许加压、扩大样本或改拓扑）——本轮严格遵守「不延长预算、不修改网络/profile/SLO 寻找正结果」，未自行扩大范围。
3. 首轮静态草稿失败启动（2 次 live 进程 / 1 次有效运行）记录是否满足审计口径，或需另发 `-r02` 返工目录；本轮按任务书条款保留证据并如实登记，未改动任何历史轮次。
