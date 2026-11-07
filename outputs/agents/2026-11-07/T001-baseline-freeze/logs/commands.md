# T001 实际命令、运行目录与退出码

- 运行目录（所有命令）：`/home/m0nesy/Postgraduate/Inference`
  （本会话 Windows 侧入口 `\\wsl.localhost\Ubuntu\home\m0nesy\Postgraduate\Inference`，通过 Git Bash 执行）
- Python 解释器：本机 `C:\Users\33199\.workbuddy\binaries\python\versions\3.13.12\python.exe`（Python 3.13.14）；**不是** WSL 的 `simai` Conda 环境，原因见下方第 5 项。
- 时间：2026-11-07 16:21–16:4x +08:00。
- 本轮严格遵守 TASK.md 禁止项：未运行 `run-baseline`、`backend smoke`、`run-experiments`、完整 `check.sh`；未构建后端、未安装依赖、未做 MLU 计时或收益评估、未提交/推送。

| # | 实际命令 | 退出码 | 关键输出 / 证据 |
| --- | --- | --- | --- |
| 1 | `git -c safe.directory='*' status --short --untracked-files=all > outputs/agents/2026-11-07/T001-baseline-freeze/logs/baseline-status.txt` | 0 | 71 行执行前脏状态，已保存；与指挥派发的 `dispatch-baseline-status.txt` 并存 |
| 2 | `git -c safe.directory='*' rev-parse HEAD > outputs/agents/2026-11-07/T001-baseline-freeze/logs/git-head.txt` | 0 | `91cf1e2a16b8ff59b595619a7d0c02c4dd5df8dc` |
| 3 | `mkdir -p work/agents/2026-11-07/T001-baseline-freeze outputs/agents/2026-11-07/T001-baseline-freeze/results/frozen` | 0 | 建立草稿与冻结父目录；未预建仿真运行子目录 |
| 4 | `python work/agents/2026-11-07/T001-baseline-freeze/freeze.py > .../logs/01-freeze.log` | 0 | 生成 4 个冻结文件 + `results/input-audit.json` + `frozen/manifest.json`；9 项字段差异；22 个开关显式核对保持原值；绑定计划两次生成一致；输入哈希与 manifest 一致；4 项 profile/EP 覆盖检查通过；gaps 为空 |
| 5 | `./inference --config outputs/agents/2026-11-07/T001-baseline-freeze/results/frozen/config.json validate > .../logs/02a-inference-wrapper.log` | **2** | `Conda is unavailable. Initialize Conda and run ./joint_scheduling/bootstrap.sh.` —— 包装入口依赖 Linux Conda `simai`，本会话不可用；如实记录，未绕过 |
| 6 | `python -m joint_scheduling --config outputs/agents/2026-11-07/T001-baseline-freeze/results/frozen/config.json validate > .../logs/02-validate.log` | 0 | `配置有效：基础模块与 evaluation 的结构和约束检查通过。`；`input_notes` 确认前缀缓存关闭、绑定窗口/预测关闭、P 轮询 + D 固定种子均衡随机、记录模式 essential、EP 开启 |
| 7 | `python -m joint_scheduling --config outputs/agents/2026-11-07/T001-baseline-freeze/results/frozen/config.json describe > .../logs/03-describe.log` | 0 | 32 GPU / P16 D16 / `ready_immediate` / `layer_ready_immediate` / `state_observation_enabled=true` / `network_observation_enabled=true` / 窗口 60–3501.721937001 / SLO 1.0 & 0.1 / 全部基础开关关闭 |
| 8 | `python work/agents/2026-11-07/T001-baseline-freeze/verify.py > .../logs/04-verify.log` | **1** | 脚本自身路径笔误：`frozen_files` 路径应相对任务目录而非仓库根，`FileNotFoundError`；原日志另存为 `04-verify-attempt1-failed.log` 保留 |
| 9 | `cp .../logs/04-verify.log .../logs/04-verify-attempt1-failed.log` | 0 | 保留失败证据，不覆盖 |
| 10 | 修正 `work/.../verify.py` 中 `ROOT / entry["path"]` → `TASK / entry["path"]` | — | 仅改本轮草稿脚本，不涉及正式源码 |
| 11 | `python work/agents/2026-11-07/T001-baseline-freeze/verify.py > .../logs/05-verify.log` | 0 | **47 / 47 项静态一致性核对通过**，失败 0；绑定计划重新生成一致；替换 KV 策略/网络 seed 后绑定哈希不变；差异集合与声明完全一致 |
| 12 | `git -c safe.directory='*' status --short --untracked-files=all > .../logs/final-status.txt` 及 `diff .../logs/baseline-status.txt .../logs/final-status.txt` | 0 | 输出 `IDENTICAL`：执行前后工作区状态逐行一致，没有新增源码/正式配置/原始输入改动 |

## 材料阅读（TASK.md 步骤 1，只读）

通过文件读取完成，未产生写操作：`AGENTS.md`、`TASK.md`、`FORWARD.txt`、`coordination/templates/STATUS.json`、`coordination/templates/REPORT.md`、`joint_scheduling/configs/research/README.md`、`joint_scheduling/configs/research/base.json`、`data/azure/README.md`、`data/azure/manifest.json`、`joint_scheduling/evaluation/README.md`、`joint_scheduling/config.py`、`joint_scheduling/binding/static.py`。

为完成静态覆盖与开关核对，另读取：`joint_scheduling/kv_scheduler/__init__.py`、`joint_scheduling/backend/settings.py`、`joint_scheduling/validation.py`、`joint_scheduling/workload/inputs.py`、`joint_scheduling/backend/compute_profile.py`、`joint_scheduling/backend/expert_profile.py`、`joint_scheduling/cli/configuration.py`、`joint_scheduling/cli/__init__.py`，以及 profile 与 workload 的原始 JSON。

## 明确未运行（TASK.md 禁止项）

- `./inference run-baseline ...`（含任何端到端仿真）
- `./inference backend build / doctor / smoke`
- `./inference run-experiments ...`（含 `--dry-run` 之外的任何实验矩阵）
- 完整 `./joint_scheduling/check.sh` 未运行（TASK.md 明确禁止）
- 后端构建、依赖安装、MLU 计时、策略收益评估、扩容实验、新机制接入
- `git commit` / `git push` / `git checkout` / `git reset` / `git clean`

## 说明

本会话通过 Windows UNC 访问 WSL 仓库，`wsl.exe` 被安全策略禁止，无法进入 WSL 的 `simai` Conda 环境；因此 `./inference` 包装入口返回 2。项目 CLI 的纯 Python 部分（`joint_scheduling/__main__.py` → `cli/`）与 evaluation 文档说明「统计与配置代码可用 Python 3.8+ 直接运行」，故改用 `python -m joint_scheduling` 这一同一 CLI 模块入口完成 `validate` / `describe`，并用项目自身模块完成 profile/workload/绑定核对。此环境差异与失败均已如实记录，没有把静态预检写成仿真成功。
