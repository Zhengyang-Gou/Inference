# T002：有界样本的基础链路验收 — 执行报告

- 执行 agent：WorkBuddy
- 开始/结束：2026-11-07T16:46:17+08:00 / 2026-11-07T16:50:49+08:00
- 状态：**blocked**
- 任务目录：`/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T002-bounded-baseline-validation`
- 前置任务：T001（`STATUS.json` = `accepted`；已阅读 `REPORT.md` 与 `REVIEW.md`）

## 结论

**本轮未能回答 TASK.md 的唯一问题**：没有在 WSL `simai` 环境中实际运行 ns-3，因此无法判定「少量真实来源请求能否完成 Prefill→逐层 KV→Decode→资源释放且结果可复现」。

阻塞原因不是输入或配置：T001 冻结件哈希全部匹配、前 3 个请求与 TASK 表格逐字段一致、绑定端点逐条等于父计划投影、许可配置差异恰为 3 项。阻塞原因是**工具策略拒绝 WSL 执行**：`wsl.exe` 位于 Command Security 的 Program Blacklist，安全策略明确禁止重试该程序、通过其他 shell/脚本启动或等价绕过；本会话的 Bash 为 Git Bash（Windows）、PowerShell 为 Windows PowerShell 5.1，均无法执行 WSL 内的 Linux ELF（`simai` Python、ns-3 后端）。项目入口 `./inference` 与 `./scripts/simai-python` 在本会话均返回退出码 2「Conda is unavailable」。

TASK.md 第 112 行正是对此情形的规定：「若工具策略拒绝WSL或后端执行，保存拒绝信息，以blocked报告；不能用Windows静态校验、计时器或synthetic fixture替代实际ns-3运行」。本轮据此保存拒绝信息并如实提交 `blocked`；**A2–A6 全部 `not_run`，没有任何端到端验收、机制证据或性能结论**。

## 逐项验收

| 编号 | passed / failed / not_run | 原始证据路径与解释 |
| --- | --- | --- |
| A1 | passed（仅静态部分） | `inputs/sample-manifest.json`、`results/input-identity-check.json`、`logs/01-prepare.log`：T001 4 个冻结文件 SHA256 与其 manifest 全部匹配；T001 冻结 workload 前 3 请求与 TASK 表格逐字段一致（`0.0/374/44/p0/d3`、`4.314579/396/109/p1/d14`、`4.541877/879/55/p2/d7`，合计 1649 输入 / 208 输出 token），到达时间未改动；`inputs/workload.json` 仅提取、请求字段不变；`inputs/compute_profile.json` 与 T001 逐字节相同；`inputs/binding_plan.json` 由项目 `binding_plan_document` 对真实子集生成、元信息为子集真实统计、未加自定义字段，3 条端点逐条等于父计划投影；相对 T001 冻结 config 的差异恰为 `evaluation.window_start_seconds 60→0`、`evaluation.window_end_seconds 3501.721937001→30`、`runtime.time_limit_seconds 4101.721937001→30` 三项。T001 冻结件在本轮结束后再次核对哈希未变。**边界**：runner 对计划的实际接受与运行导出的 `binding_plan.json` 一致性需要真实运行，未验证。 |
| A2 | not_run | 两次 ns-3 端到端运行（run-a / run-b）未启动，`results/run-a/`、`results/run-b/` 不存在；没有 3 请求 / 208 token 完成证据，也没有失败/未完成统计。 |
| A3 | not_run | `results/mechanism-checks.json` 未生成：多 chunk Prefill、205 个 Decode 步、61 层 EP 依赖、KV 就绪与 D 准入、EPFlowAudit 都需要运行期只读钩子与事件。 |
| A4 | not_run | KV 逻辑 payload 守恒（预期 1649×70272=115878528 字节）与最终显存/活动依赖无残留，均需运行汇总、网络/EP/资源导出，本轮无这些数据。 |
| A5 | not_run | `results/reproducibility-check.json` 未生成：两次语义结果比较需要两次运行。 |
| A6 | not_run | 无 `result/summary.json`、`requests.csv`、`tokens.csv`、`timeseries.csv`；无法从测量报告 TTFT / handoff / later_max_itl / 达标完成吞吐 / SLO 违约。本 REPORT 只说明功能样本范围与未覆盖项。 |

`results/acceptance-summary.json` 逐项列出上述状态与证据路径。

## 实际修改

| 文件 | 本轮修改及目的 |
| --- | --- |
| `STATUS.json` | `ready` → `in_progress`（填 agent / started_at）→ `blocked`；保留指挥写入的 `created_at`、`parent_task` 等。 |
| `inputs/config.json` | T001 冻结 config 副本，仅按 TASK 允许差异改评估窗口起点/终点与仿真截止三项；其余冻结条件不变。 |
| `inputs/workload.json` | T001 冻结 workload 前 3 个请求，请求字段与到达时间逐字保留，provenance 逐字保留。 |
| `inputs/compute_profile.json` | T001 冻结 profile 逐字节复制，未改内容。 |
| `inputs/binding_plan.json` | 项目静态绑定实现对真实 3 请求生成的子计划（无自定义字段），端点逐条核对等于父计划投影。 |
| `inputs/sample-manifest.json` | 源文件哈希、提取索引/ID、原始字段、样本哈希、父计划哈希、配置差异清单、不变条件列表。 |
| `results/input-identity-check.json`、`results/acceptance-summary.json` | 静态身份核对结果与 A1–A6 逐项状态。 |
| `logs/*` | 执行前 Git 状态、Git HEAD、拒绝信息、命令与退出码记录、各命令原始输出、后端构建目录只读清单。 |
| `work/agents/2026-11-07/T002-bounded-baseline-validation/prepare.py` | 本轮草稿脚本，仅做静态准备与核对，不属正式源码。 |

**未修改**：正式源码、正式配置、原始输入、T001 冻结件（已用 SHA256 前后核对）、ns-3 后端源码或构建。执行前工作区已有 71 条脏状态，全部属于他人已有修改，本轮未回退、未清理、未提交；执行前后 `git status --short --untracked-files=all` 均为 71 行。`TASK.md` 与 `FORWARD.txt` 原文保留未改。

## 验证证据

| 实际命令 | 运行目录 | 退出码/结果 | 日志或结果路径 |
| --- | --- | --- | --- |
| `git -c safe.directory='*' status --short --untracked-files=all > logs/baseline-status.txt` | 仓库根 | 0，71 行 | `logs/baseline-status.txt` |
| `git -c safe.directory='*' rev-parse HEAD > logs/git-head.txt` | 仓库根 | 0，`91cf1e2a16b8…` | `logs/git-head.txt` |
| `python work/.../T002.../prepare.py > logs/01-prepare.log` | 仓库根 | 0；T001 哈希全匹配、样本/绑定/差异全通过、5 项静态检查通过、`ns-3 runs: 0` | `logs/01-prepare.log` |
| `./inference --config inputs/config.json validate` | 仓库根 | **2**，`Conda is unavailable` | `logs/02a-inference-wrapper.log` |
| `./scripts/simai-python -c 'import sys; print(sys.executable)'` | 仓库根 | **2**，`Conda is unavailable` | `logs/02b-simai-python.log` |
| `python -m joint_scheduling --config inputs/config.json validate` | 仓库根 | 0（**仅 Windows 静态结构校验，非 simai、非 ns-3 运行**） | `logs/02c-static-validate.log` |
| `find joint_scheduling/artifacts/backend/current ...` | 仓库根 | 0，存在 `build_manifest.json`（Linux g++ 15.2.0 / cmake 3.31.10 / simai 路径） | `logs/03-backend-artifacts.txt` |
| T001 `results/frozen/` 四文件 SHA256 重新核对 | 仓库根 | 0，4 个 `OK` | 见 `inputs/sample-manifest.json` 的 `t001_frozen_files` |
| 最终 `git status --short --untracked-files=all` | 仓库根 | 0，71 行，与执行前一致 | `logs/commands.md` |

安全策略原始拒绝信息（逐字保留）与「本轮未重试」说明见 `logs/wsl-tool-policy-block.txt`。完整命令清单与明确未运行项见 `logs/commands.md`。

## 结果与解释

- **证据类型**：静态分析 + 环境拒绝记录。**没有**功能验证、有限仿真或持续负载实验。
- **输入与配置身份**：T001 冻结 config `565572ab…98a8f1`、profile `374595ad…f93554`、workload `0ce0c8cd…8c346`、父绑定计划 `9a48e35f…b091`（内容哈希 `6fd36888…d2aa5`）全部与本轮前一致；本轮子计划内容哈希 `459d8b39…f32c3`。Git HEAD `91cf1e2a16b8ff59b595619a7d0c02c4dd5df8dc`。后端 `build_manifest.json` 显示 Linux 工具链构建，但本会话无法执行该二进制。
- **样本与窗口**：静态准备的有界样本为 3 请求、1649 输入 / 208 输出 token，到达 `0.0 / 4.314579 / 4.541877 s`，窗口 `[0.0, 30.0)`、`time_limit=30.0`；这些是**配置值，不是测量结果**。未生成任何仿真样本输出。
- **数值依据**：`inputs/sample-manifest.json`、`results/input-identity-check.json`、`logs/01-prepare.log`。profile 仍为 `estimated_simulation_profile`（估算）、专家路由为合成、SLO 为人工假设，均非实测。
- **结论边界**：本轮不能证明端到端链路可运行、请求完成、token 数、EP/KV 机制、资源释放、结果可复现，也不能给出任何 TTFT/ITL/吞吐/SLO 违约数值。所有 Windows 侧静态核对只用于身份确认与输入准备，**不替代、不冒充** ns-3 运行。

## 风险、未完成项与阻塞

1. **阻塞项（主因）**：`wsl.exe` 被 Command Security Program Blacklist 拒绝，安全策略禁止重试或等价绕过；本轮未重试，也未使用 `dangerouslyDisableSandbox`（该路径仅适用于 sandbox policy failure）。因此 WSL `simai`（Python 3.12.16）与 ns-3 后端完全不可用，`./inference` / `./scripts/simai-python` 均返回 2。
2. **未完成**：TASK 步骤 4–8 全部未执行——`backend doctor`、可能需要的 ns-3 重建、运行/检查脚本、`results/run-a` 与 `results/run-b` 两次端到端运行、`mechanism-checks.json`、`reproducibility-check.json`、各轮 `result/` 导出。这些文件/目录均未生成，也没有用占位内容伪造。
3. **输入侧无冲突**：样本字段、父绑定端点、T001 冻结件哈希、许可配置差异全部核对通过，不属于「源版本/样本字段/端点不符」的停止情形，不需要换样本或改长度。
4. **恢复条件**（三选一，供指挥决定）：
   - 在 Security Center → Command Security → Program Blacklist 移除 `wsl.exe`，之后由执行 agent 重试；
   - 由具备 WSL 访问能力的 agent / 指挥侧直接执行（T001 `REVIEW.md` 已记录指挥曾用 `env CONDA_EXE=/home/m0nesy/miniconda3/bin/conda ./inference ... validate` 在 WSL `simai` 中成功，退出码 0）；
   - 重新指派有 WSL 权限的执行 agent 承接 T002，可直接复用本轮 `inputs/` 与 `sample-manifest.json`。
5. 恢复后仍需按 TASK 步骤 5–8 编写运行/检查脚本、执行两次运行（各 30 s 仿真 / 15 min 墙钟上限）、做机制与可复现性核对；本轮没有启动过任何运行，因此两次预算均未被消耗。
6. 未提交/推送，未启动其他 agent，未恢复旧任务，未开展算法对比、扩容或完整轨迹运行。

## 下一步建议

1. 指挥按上述恢复条件之一解除 WSL 执行限制，再决定是返工本任务（可用 `-r02` 新目录）还是由具备权限的 agent 直接续做；本轮 `inputs/` 可复用。
2. 恢复执行时先按 TASK 步骤 4 做 `validate` 与 `backend doctor`，后端就绪才进入两次运行；首轮异常即按停止条件保留现场，不自动补跑或调参。
3. 审核时以 `results/acceptance-summary.json` 的 A2–A6 `not_run` 为准，不把 A1 的静态通过理解为「基础链路验收通过」。

## 需要指挥决定

1. 选择上述哪一种恢复路径（解除黑名单 / 指挥侧代跑 / 改派有 WSL 权限的执行 agent），并明确是否使用新的返工目录。
2. 本轮 `inputs/` 是否作为 T002 后续运行的固定输入（当前位于任务目录，未改动 T001 冻结件与正式输入）。
3. 其余无。执行报告状态为 `blocked`，等待指挥写 `REVIEW.md`；不自行开始下一轮。
