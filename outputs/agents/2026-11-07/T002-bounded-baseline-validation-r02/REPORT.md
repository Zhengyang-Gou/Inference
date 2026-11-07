# T002-r02：有界样本的基础链路验收（返工） — 执行报告

- 执行 agent：WorkBuddy
- 开始/结束：2026-11-07T17:02:21+08:00 / 2026-11-07T17:18:45+08:00
- 状态：submitted
- 任务目录：`/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T002-bounded-baseline-validation-r02`
- 前置任务：T001（`accepted`）；`parent_task` = T002（原轮次因 WSL 工具策略拒绝而 `blocked`，其目录与证据完整保留，本轮为 `-r02` 返工）

## 结论

**已回答 TASK.md 的唯一问题**：沿用 T001 冻结的模型、拓扑、执行规则和基础开关，3 个真实来源请求在 WSL `simai` + 真实 ns-3 包级网络中**完整跑通 Prefill → 逐层 KV → Decode → 资源释放，并且两次运行的语义结果完全一致**。

A1–A6 全部 passed，38/38 机制检查通过，13 个语义字段两次一致，无失败、无未完成、无资源残留、无机制错误。两次运行合计墙钟 568.338 s（约 9.5 分钟），在任务书 30 分钟合计预算内；仿真时间上限 30 s。

**边界**：这是 3 请求、208 token 的**功能样本验收**，所有 Prefill/Decode 批次大小均为 1，未触及 batch 上限 16/64。本轮不证明系统承载率、KV/EP 竞争强度、SLO 在真实负载下的表现或任何算法收益；「基础链路验收通过」不等于研究问题或算法收益已成立。

## 逐项验收

| 编号 | passed / failed / not_run | 原始证据路径与解释 |
| --- | --- | --- |
| A1 | passed | `inputs/sample-manifest.json` 与 5 个输入文件哈希（与 blocked 轮次逐字节一致）；T001 4 个冻结件 SHA256 复核未变；样本 3 请求字段、到达时间、1649 输入 / 208 输出 token 与 TASK 表格一致；子计划端点逐条等于父计划投影；相对 T001 配置差异恰为 3 项（窗口起点 `60→0`、终点 `3501.721937001→30`、`time_limit 4101.721937001→30`）；两次运行导出的 `binding_plan.json` 内容哈希 `459d8b39…f32c3` 与输入及父计划投影一致（`results/run-a/b/binding_plan.json`）。 |
| A2 | passed | `logs/04-run-a.log`、`logs/05-run-b.log` 均退出码 0；`results/run-a/`、`results/run-b/` 完整。两次均 3 请求到达并完成、208 token（44/109/55）、`failed_count=0`、`incomplete_count=0`；`results/run-*/result/requests.csv`、`tokens.csv` 逐行核对，token 编号连续、时刻不倒退。 |
| A3 | passed | `results/mechanism-checks.json`、`results/run-*-hooks.json`：4 个 Prefill chunk / 1649 query token，879-token 输入确为 512+367 两块；205 个 Decode 步 × 61 层（`layers_per_step=61`、无活动层/批次残留）；`EPFlowAudit` 两次 `passed`，逐层路由/assignment、GPU 级激活去重、dispatch→专家→本地合并/TP→combine→后继层依赖均通过；EP 调用 12122 = Prefill 232（4×58）+ Decode 11890（205×58）；`compute_step_started` 12749 = 244 chunk-layer + 205×61 层；`prefill_chunk_layer_ready=244` 与 244 条逻辑 KV 消息对应；事件时间顺序证明 `prefill_started ≤ kv_all_received ≤ decode_started`，首 token 在 Prefill 开始之后。 |
| A4 | passed | `results/run-*/observation/summary.json → communication.kv`：`started_bytes = completed_bytes = 115878528`（=1649×70272），`started_count = completed_count = 244`，用的是**逻辑消息 payload**，不是含包头的端口字节；`final_resources.json` 32 个显存域 `occupied/reserved` 全为 0；`expert_parallel_summary.json` 的 `active_calls_at_end`、`active_gpu_tasks_at_end`、`queued_gpu_tasks_at_end`、`planned_phase_demands_at_end` 全空；`EPFlowAudit` 的 `incomplete_calls / active_logical_flows / active_compute_steps / active_decode_batches` 全为 0。 |
| A5 | passed | `results/reproducibility-check.json`：13 个语义字段（tokens.csv、requests.csv、事件中的 token 时刻、请求状态/SLO、completion window、latencies、绑定计划、EP 汇总、KV/collective 逻辑字节、Prefill/Decode 汇总、最终资源、EPFlowAudit 结果、hook 事件计数）两次全部相同；路径、墙钟、源码归档等非语义元数据按任务书排除。 |
| A6 | passed | `results/metrics-summary.json` 与每轮 `result/summary.json`、`requests.csv`、`tokens.csv`、`timeseries.csv`，以及 `network/backend_metadata.json`、`compute_profile.json`、`expert_parallel_summary.json`、`prefill_{chunk,batch}_summary.json`、`decode_batch_summary.json`、`final_resources.json`、`reproducibility.json`、`source/` 归档齐备。报告见下方「结果与解释」：本样本 SLO 违约为 0，功能样本统计不能解释为系统承载能力；未覆盖 batch>1、满载与持续负载。 |

`results/acceptance-summary.json` 逐项列出上述状态与证据路径。

## 实际修改

| 文件 | 本轮修改及目的 |
| --- | --- |
| `STATUS.json` | 新建，`ready/in_progress → submitted`，`parent_task="T002"`。 |
| `TASK.md`、`FORWARD.txt` | 从原 T002 目录**逐字复制**任务原文，未改写。 |
| `inputs/*.json` | 从原 T002 `inputs/` 逐字节复制并核对哈希，未改动内容。 |
| `results/run-a/`、`results/run-b/` | 由 `run_baseline` 自行创建（`exist_ok=False`）的完整运行目录，未拆散、未覆盖。 |
| `results/run-a-hooks.json`、`run-b-hooks.json` | 每轮 `EPFlowAudit.finish` 结果、只读事件计数与墙钟。 |
| `results/mechanism-checks.json`、`reproducibility-check.json`、`metrics-summary.json`、`acceptance-summary.json` | 由 `check_results.py` 只读两次运行产物生成。 |
| `logs/*` | 执行前/后 Git 状态、Git HEAD、两次运行与核验的原始输出、命令与退出码记录。 |
| `work/agents/2026-11-07/T002-bounded-baseline-validation-r02/{run_validation.py,check_results.py}` | 本轮草稿脚本，不属正式源码。 |

**未修改**：正式源码、正式配置、原始输入、T001 冻结件、ns-3 后端源码/二进制、原 T002 blocked 轮次的任何文件。执行前工作区 71 条脏状态全部保留；执行前后 `git status --short --untracked-files=all` 逐行一致（`logs/final-status.txt`）。未提交、未推送、未启动其他 agent。

## 验证证据

| 实际命令 | 运行目录 | 退出码/结果 | 日志或结果路径 |
| --- | --- | --- | --- |
| `git -c safe.directory='*' status --short --untracked-files=all > logs/baseline-status.txt` | 仓库根 | 0，71 行 | `logs/baseline-status.txt` |
| `git -c safe.directory='*' rev-parse HEAD > logs/git-head.txt` | 仓库根 | 0，`91cf1e2a16b8…` | `logs/git-head.txt` |
| `MSYS_NO_PATHCONV=1 ... wsl.exe -e /bin/bash -lc 'echo WSL_OK; uname -sr; .../simai/bin/python -V'` | 仓库根 | 0，`WSL_OK` / Linux 6.18.54 WSL2 / Python 3.12.16 | `logs/commands.md` |
| `env CONDA_EXE=... ./inference --config <inputs>/config.json validate` | 仓库根（WSL） | 0，配置有效 | `logs/commands.md` |
| `env CONDA_EXE=... ./inference backend doctor` | 仓库根（WSL） | 0，`backend_ready=true` | `logs/commands.md` |
| `timeout 900 env CONDA_EXE=... PYTHONPATH=. ./scripts/simai-python work/.../run_validation.py --run run-a` | 仓库根（WSL） | 0，280.673 s，`EPFlowAudit=passed` | `logs/04-run-a.log`、`results/run-a/` |
| 同一命令 `--run run-b` | 仓库根（WSL） | 0，287.665 s，`EPFlowAudit=passed` | `logs/05-run-b.log`、`results/run-b/` |
| `env CONDA_EXE=... PYTHONPATH=. ./scripts/simai-python work/.../check_results.py` | 仓库根（WSL） | 0，38/38 通过、13 字段一致、A1–A6 passed | `logs/06-check-results.log` |
| `git status ... > logs/final-status.txt` + `diff baseline-status.txt final-status.txt` | 仓库根 | 0，`IDENTICAL` | `logs/final-status.txt` |

所有命令、环境变量前缀、退出码与未运行项详见 `logs/commands.md`。两次运行均使用同一配置、输入、绑定计划、seed 与审计方式；`RuntimeHooks(event=...)` 只接收事件副本，未新增仿真事件或高频采样。

## 结果与解释

- **证据类型**：两次真实 ns-3 包级端到端有限仿真（功能样本），配合 `EPFlowAudit` 与只读事件计数。不是持续负载实验，不是算法对比。
- **输入与配置身份**：Git HEAD `91cf1e2a16b8ff59b595619a7d0c02c4dd5df8dc`；T001 冻结 config `565572ab…98a8f1`、profile `374595ad…f93554`、workload `0ce0c8cd…8c346`、父绑定计划 `9a48e35f…b091`；本轮子计划内容哈希 `459d8b39…f32c3`。后端 `simai_ns3_rdma`、protocol 4、fingerprint `23ec96146135d0706e39`、`per_gpu` 端点、DCQCN；每轮运行目录含 `reproducibility.json`、`source/source.tar.gz` 与 `network/backend_metadata.json`。
- **样本与窗口**：3 请求、1649 输入 / 208 输出 token，到达 `0.0 / 4.314579 / 4.541877 s`；测量窗口 `[0.0, 30.0)`（半开），`time_limit=30.0`，`runtime.seed=7`、`binding.seed=7`。墙钟：run-a 280.673 s、run-b 287.665 s，合计 568.338 s（<30 min 预算）；**墙钟只是仿真运行成本**，不是 GPU 计算时间、控制器决策延迟，也不是算法收益。
- **数值依据**（两轮相同，见 `results/metrics-summary.json` 与每轮 `result/summary.json`）：
  - 完成窗口 `[0,30)`：完成 3、达标 3、`slo_attainment=1.0`、完成吞吐 0.1 rps、**达标完成吞吐 0.1 rps**、carry-in 0；到达 cohort：到达 3、完成 3、失败 0、未完成 0。
  - TTFT：min 0.084032527 / mean 0.118626754 / max 0.185244063 s（P50 0.086603672、P95 0.175380024、P99 0.183271255）；handoff mean 0.0170037 s（0.016722584–0.017463745）；later_max_itl mean 0.01824015 s（0.016948988–0.019045819）；E2E mean 1.311978 s（0.805357903–1.976731080）。
  - SLO 阈值 TTFT 1.0 s / ITL 0.1 s 为**人工实验假设**；本样本 3 请求全部达标，**SLO 违约 0**，未为通过验收调整阈值、带宽、profile、输出长度或窗口。
  - Prefill：4 chunk、1649 query token、batch 大小全为 1（`{"1":4}`），峰值运行请求每实例 1；Decode：205 步、batch 大小全为 1（`{"1":205}`）、每步 61 层。
  - EP：12122 次调用（Prefill 232 + Decode 11890），`dispatch_bytes=4379203584`、`combine_bytes=8758407168`，`checked_logical_flows=141968`；KV 逻辑 payload 115878528 字节 / 244 条消息。
- **结论边界**：3 请求、单成员批次的功能样本没有形成 batch>1，未覆盖大批合批、满载或持续负载行为；不能据此推断 KV 与 EP 的竞争强度、系统承载率、SLO 在完整 Azure 轨迹上的表现或任何 KV 调度策略收益。计算 profile 仍是 `estimated_simulation_profile`（公开锚点估算）、专家路由是确定性合成、SLO 是人工假设，均非实机测量。本轮也不验证 MLU 收益、不调优网络、不扩容。

## 风险、未完成项与阻塞

1. 无阻塞项：两次运行均正常完成，无超时、无机制失败、无输入身份冲突；首轮正常后才启动第二轮，符合 TASK 停止条件。
2. 环境说明：原 T002 的 `blocked` 源于 `wsl.exe` 被工具策略黑名单拒绝；用户解除后本轮恢复执行。从 Git Bash 调用 WSL 必须加 `MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'`，否则 `/bin/bash` 会被转换为 Windows 路径而失败（首次尝试已记录）。原 blocked 轮次的 `REPORT.md`、拒绝证据与 `inputs/` 完整保留，未被覆盖或修改。
3. 未覆盖：batch>1 合批、满载、持续加压、KV/EP 干扰归因、算法对照、全量 19366 请求轨迹、扩容、DeepEP、MLU 计时、完整 `check.sh`；这些均不在本任务授权范围。
4. 未重建后端（`backend doctor` 已就绪）、未安装/升级依赖、未切换后端、未提交或推送。

## 下一步建议

1. 指挥按模板写 `REVIEW.md`，决定 `accepted` 或 `changes_requested`，并明确原 T002 `blocked` 轮次作为历史证据保留。
2. 若下一步要做 KV 调度策略对照或 KV/EP 竞争归因，需另发任务明确策略项、样本规模与统计口径；本轮冻结条件与绑定计划可直接复用。
3. 若需要覆盖 batch>1 或满载行为，另发任务并明确到达率/批量参数；本轮样本不构成该范围的证据。

## 需要指挥决定

1. 是否接受本轮 `-r02` 的基础链路验收结论（A1–A6 全 passed），以及如何处理原 T002 `blocked` 状态。
2. 下一阶段任务的范围与编号（策略对照 / 竞争归因 / 合批覆盖），本轮不自动启动。
3. 其余无。执行完成不等于审核通过，等待指挥写 `REVIEW.md`；不自行开始下一轮。
