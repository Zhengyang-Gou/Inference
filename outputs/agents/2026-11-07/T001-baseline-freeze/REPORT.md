# T001：冻结第一阶段唯一基线条件 — 执行报告

- 执行 agent：WorkBuddy
- 开始/结束：2026-11-07T16:21:01+08:00 / 2026-11-07T16:30:44+08:00
- 状态：submitted
- 任务目录：`/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T001-baseline-freeze`
- 前置任务：无（TASK.md 明确不继承或恢复已撤销的旧任务）

## 结论

已回答 TASK.md 的唯一问题：后续基础验收与算法对照应共同使用**本任务目录 `results/frozen/` 中这一套冻结条件**——以 `joint_scheduling/configs/research/base.json` 为唯一来源的 `config.json`（32 GPU / P16:D16 / TP1 / EP16-16 / ns-3）、逐字节复制的 Azure 2023 完整输入与 DeepSeek-V3/H800 估算 profile、覆盖全部 19366 请求的确定性 `binding_plan.json`（`balanced_random`, seed 7）、人工 SLO 与评估窗口、以及基础验收模式开关；完整条件表见 `results/baseline.md`，机器可核对身份见 `results/frozen/manifest.json`。

A1–A6 全部 passed，**无阻塞、未触发停止条件**：输入身份与 `data/azure/manifest.json` 一致、profile 曲线覆盖充足、请求级 SLO 覆盖与规定无冲突、冻结配置通过项目 CLI 的结构/约束校验。本轮是条件冻结与静态预检，**没有运行任何端到端仿真、算法对比或扩容实验**，因此不宣称链路已跑通或任何性能收益。

## 逐项验收

| 编号 | passed / failed / not_run | 原始证据路径与解释 |
| --- | --- | --- |
| A1 | passed | `results/baseline.md`：完整列出配置（拓扑/网络/Prefill/Decode/EP/显存）、负载、固定绑定、SLO、统计窗口与半开区间语义、指标口径（completion window 与 arrival cohort、失败/未完成/排空）、基础开关生效范围、模型与 profile 边界、9 项差异、冻结哈希；`results/input-audit.json` 为机器可读补充。 |
| A2 | passed | `results/frozen/config.json` 经 `python -m joint_scheduling --config ... validate` 退出码 0（`logs/02-validate.log`）；`logs/03-describe.log` 给出有效配置摘要。与 base 的实际差异经递归比对恰为 9 项，全部属于任务指定的基础开关（5 项）、观测（1 项）与可搬移路径（3 项），另有 22 个已符合要求的开关显式核对并保持原值；见 `frozen/manifest.json` 的 `config_field_diff_from_base` / `explicitly_verified_unchanged_switches` 与 `results/verification.json`（47/47 通过）。 |
| A3 | passed | `frozen/compute_profile.json`、`frozen/workload.json` 与原件 SHA256 相同（逐字节复制）；负载哈希 `0ce0c8cd…c346` 与 `data/azure/manifest.json` 的 `prepared[0].sha256` 一致。`results/input-audit.json`：19366 请求、到达 0.0–3501.721937 s、输入 2–14050、输出 7–1000、最大 Decode 上下文 14088、重复 ID 0；`ttft_slo_seconds`/`itl_slo_seconds` 覆盖均为 0，`prefix_blocks`/`max_output_tokens` 均为 0；4 项 profile/EP 覆盖检查全部通过，`gaps` 为空。 |
| A4 | passed | `frozen/binding_plan.json` 含 19366 条绑定、`bindings_sha256=6fd36888…d2aa5`、seed 7、`prefill_counts`/`decode_counts` 各合计 19366。冻结脚本两次生成结果完全相同；`verify.py` 用项目 `binding_plan_document` 重新生成逐字段一致，并在替换 `kv_scheduler.policy`、`runtime.kv_transfer_mode`、`runtime.seed` 后哈希不变，证明不按 KV 策略或网络状态重绑。`logs/01-freeze.log`、`logs/05-verify.log`、`results/verification.json`。 |
| A5 | passed | `results/baseline.md` 第 3 节与 `frozen/manifest.json` 的 `parameter_scope` 明确区分生效字段（逐层就绪即发、`max_block_bytes`、EP、显存准入、源读保护、完成依赖、评估与观测）与关闭后无效字段（KV 额度/门控/MLU/集合通信保护/Decode 准入参数、`feedback.scheduling_overhead_us`、绑定窗口与预测参数、前缀缓存参数）；`logs/03-describe.log` 显示实际有效开关。EP、Chunked Prefill、连续 Decode、容量与完成依赖均未修改（A6 的 Git 核对）。 |
| A6 | passed | `logs/baseline-status.txt`（执行前 71 条）与 `logs/final-status.txt` 经 `diff` 完全一致，无新增源码/正式配置/原始数据改动；`logs/commands.md` 记录所有命令与退出码，并明确列出 TASK 禁止项均未运行；本 `REPORT.md` 与 `STATUS.json` 完整，任务原文 `TASK.md` 与 `FORWARD.txt` 保留未改。未提交、未推送、未启动其他 agent、未恢复旧任务。 |

## 实际修改

| 文件 | 本轮修改及目的 |
| --- | --- |
| `STATUS.json` | `ready` → `in_progress`（填写 agent / started_at）→ `submitted`；保留指挥写入的 `created_at`、`task_id` 等字段。 |
| `results/frozen/config.json` | base.json 的完整嵌套副本，仅按 TASK 修改 9 个字段；profile/workload/evaluation 输出改为相对冻结配置目录的路径。 |
| `results/frozen/compute_profile.json`、`workload.json` | 原件逐字节复制，内容零修改。 |
| `results/frozen/binding_plan.json` | 用项目 `binding.static.binding_plan_document` 为全部请求生成确定性 P/D 计划。 |
| `results/frozen/manifest.json` | 原文件与冻结文件 SHA256、字段差异、Git HEAD 与执行前脏状态、模型假设、参数生效范围、明确的未运行声明。 |
| `results/baseline.md`、`results/input-audit.json`、`results/verification.json` | 唯一条件表、输入审计、47 项静态一致性核对结果。 |
| `logs/*` | 执行前/后 Git 状态、Git HEAD、命令与退出码记录、各命令原始输出、失败日志。 |
| `work/agents/2026-11-07/T001-baseline-freeze/{freeze.py,verify.py}` | 本轮草稿脚本，仅用于生成与核对，不属正式源码。 |

**未修改正式源码、正式配置、profile 或原始输入。** 执行前工作区已有 71 条脏状态（含上一轮 flowSim 删除、其他会话的改动，以及 `AGENTS.md` / `coordination/` 模板更新），全部属于他人已有修改；本轮未回退、未清理、未纳入自己的改动，也未提交。执行前后 `git status --short --untracked-files=all` 逐行一致。

## 验证证据

| 实际命令 | 运行目录 | 退出码/结果 | 日志或结果路径 |
| --- | --- | --- | --- |
| `git -c safe.directory='*' status --short --untracked-files=all > logs/baseline-status.txt` | 仓库根 | 0，71 行 | `logs/baseline-status.txt` |
| `git -c safe.directory='*' rev-parse HEAD > logs/git-head.txt` | 仓库根 | 0，`91cf1e2a16b8…` | `logs/git-head.txt` |
| `python work/.../freeze.py` | 仓库根 | 0，4 个冻结文件 + 审计 + 清单；4 项覆盖检查通过、gaps 空 | `logs/01-freeze.log` |
| `./inference --config results/frozen/config.json validate` | 仓库根 | **2**，`Conda is unavailable`（包装入口需 Linux Conda） | `logs/02a-inference-wrapper.log` |
| `python -m joint_scheduling --config results/frozen/config.json validate` | 仓库根 | 0，`配置有效：基础模块与 evaluation 的结构和约束检查通过。` | `logs/02-validate.log` |
| `python -m joint_scheduling --config results/frozen/config.json describe` | 仓库根 | 0，有效配置摘要 | `logs/03-describe.log` |
| `python work/.../verify.py`（第一次） | 仓库根 | **1**，脚本路径笔误 `FileNotFoundError` | `logs/04-verify-attempt1-failed.log` |
| `python work/.../verify.py`（修正后） | 仓库根 | 0，**47/47 通过**，失败 0 | `logs/05-verify.log`、`results/verification.json` |
| `git status ... > logs/final-status.txt` + `diff baseline-status.txt final-status.txt` | 仓库根 | 0，`IDENTICAL` | `logs/final-status.txt` |

失败、跳过与未运行项均如实列在 `logs/commands.md`：两次失败分别是环境（Conda 不可用）与本轮草稿脚本笔误，后者已修正并保留原始失败日志；TASK 禁止的 `run-baseline`、`backend smoke/build/doctor`、`run-experiments`、完整 `check.sh`、后端构建、MLU 计时、策略矩阵与扩容实验**全部未运行**。

## 结果与解释

- **证据类型**：静态分析（配置加载与约束校验、SHA256 身份核对、请求级输入统计、profile 曲线覆盖检查、绑定确定性核对）。不是功能验证、不是有限仿真、不是持续负载实验。
- **输入与配置身份**：原件哈希见 `results/baseline.md` 第 1 节与 `frozen/manifest.json`；Git HEAD `91cf1e2a16b8ff59b595619a7d0c02c4dd5df8dc`，执行前脏状态 71 条并已保留。按 TASK 要求，未生成源码归档、未归档后端二进制，`manifest.json` 的 `claims` 明确标为 false。
- **样本与窗口**：完整 Azure 2023 输入 19366 请求仅作为**来源冻结**，本轮没有筛选、截短、循环、压缩时间或改到达率，也没有运行任何仿真样本；统计窗口 `[60.0, 3501.721937001)`（半开，长度 3441.721937001 s）、排空 600 s、`runtime.seed=7` / `binding.seed=7`。窗口数值是配置值，不是测量结果。
- **数值依据**：`results/input-audit.json`（请求/长度/SLO 覆盖/曲线覆盖）、`results/frozen/manifest.json`（哈希与差异）、`logs/03-describe.log`（有效配置）、`results/verification.json`（47 项核对）。profile 为 `estimated_simulation_profile`（公开锚点估算）、专家路由为确定性合成、SLO 为人工假设——三者都不是实测或真实业务数据；`feedback.scheduling_overhead_us=10.0` 是仿真配置假设且立即发送路径下不生效，不是 CPU 实测耗时。
- **结论边界**：本轮不能证明端到端链路可运行、任何 SLO 达标吞吐数值、KV/EP 竞争强度、策略收益、网络利用率或调度开销实测值；冻结完整输入不代表获准启动约 58 分钟仿真时间的全量包级运行。`./inference` 包装入口的失败也意味着正式运行仍需在 WSL 的 `simai` 环境中验证。

## 风险、未完成项与阻塞

1. **环境差异**：本会话通过 Windows UNC 访问 WSL 仓库，`wsl.exe` 被安全策略禁止，`./inference` 包装入口因缺 Linux Conda 返回 2。改用同一 CLI 模块入口 `python -m joint_scheduling`（纯 Python 部分兼容 3.8+，本机 Python 3.13.14，非项目 `simai` 3.12）完成静态校验。**正式运行前应在 WSL `simai` 环境用 `./inference --config <frozen>/config.json validate` 复核一次**，本轮未做。
2. **草稿脚本一次失败**：`verify.py` 首轮把冻结文件路径相对仓库根解析，退出码 1；失败日志保留，修正后 47/47 通过。冻结文件由 `freeze.py` 生成，未受该笔误影响。
3. **冻结完整输入的使用边界**：`results/frozen/workload.json` 是全量输入副本，后续基础链路验收必须在另发任务中明确有界样本规则，不能据此直接启动全量运行。
4. **无停止条件触发**：输入身份一致、profile 覆盖充足（含 batch 64 × 14088 token）、SLO 覆盖无冲突、配置校验通过；`input-audit.json` 的 `gaps` 为空。
5. 无其他已知遗留项；本轮未提交或推送代码。

## 下一步建议

1. 指挥审核后，在 WSL `simai` 环境用 `./inference --config <frozen>/config.json validate` 做一次纯静态复核（不构建后端）。
2. 下一轮基础链路验收应另发任务，明确从冻结 Azure 输入生成有界样本的时长/请求数/筛选规则与临时规格，复用本轮 `binding_plan.json`。
3. 后续算法对照仅改变明确指定的策略项，其余冻结条件、输入身份、seed、窗口与指标口径保持不变。

## 需要指挥决定

1. 是否确认 `results/frozen/` 这套冻结件（当前位于 `outputs/` 忽略目录，按 TASK 未纳入正式 `configs/`）为后续基础验收与算法对照的**唯一引用**。
2. 下一轮有界功能样本的生成规则（时长、请求数、筛选方式）与是否授权基础链路仿真，需由指挥明确；本轮未自行生成样本或启动运行。
3. 其余无。执行完成不等于审核通过，等待指挥写 `REVIEW.md`。
