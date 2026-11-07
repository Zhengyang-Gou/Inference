# T002 实际命令、运行目录与退出码

- 运行目录（所有命令）：`/home/m0nesy/Postgraduate/Inference`
  （本会话 Windows 侧入口 `\\wsl.localhost\Ubuntu\home\m0nesy\Postgraduate\Inference`，Git Bash 执行）
- 本会话解释器：Windows Python 3.13.14；**无法使用** WSL `simai`（Python 3.12.16）与 ns-3 Linux 二进制，原因见 `logs/wsl-tool-policy-block.txt`。
- 时间：2026-11-07 16:46–16:5x +08:00。
- 前置条件：T001 `STATUS.json` 为 `accepted`，已阅读 `AGENTS.md`、T001 `REPORT.md` 与 `REVIEW.md`。

| # | 实际命令 | 退出码 | 关键输出 / 证据 |
| --- | --- | --- | --- |
| 1 | `git -c safe.directory='*' status --short --untracked-files=all > logs/baseline-status.txt` | 0 | 71 行执行前脏状态（含上一轮及其他会话改动），已保存 |
| 2 | `git -c safe.directory='*' rev-parse HEAD > logs/git-head.txt` | 0 | `91cf1e2a16b8ff59b595619a7d0c02c4dd5df8dc` |
| 3 | `ls -l "//wsl.localhost/Ubuntu/home/m0nesy/miniconda3/envs/simai/bin/python"` 等只读路径检查 | 非 0 | `Input/output error`（UNC 无法解析 Linux 符号链接）；未调用 `wsl.exe` |
| 4 | `python work/agents/2026-11-07/T002-bounded-baseline-validation/prepare.py > logs/01-prepare.log` | 0 | T001 4 个冻结件哈希全匹配；样本与 TASK 表格一致（1649 输入 / 208 输出）；子计划端点逐条等于父计划投影；配置差异恰 3 项；5 项静态检查通过；`ns-3 runs: 0` |
| 5 | `./inference --config outputs/.../inputs/config.json validate > logs/02a-inference-wrapper.log` | **2** | `Conda is unavailable. Initialize Conda and run ./joint_scheduling/bootstrap.sh.` |
| 6 | `./scripts/simai-python -c 'import sys; print(sys.executable)' > logs/02b-simai-python.log` | **2** | 同上；项目环境入口在本会话不可用 |
| 7 | `python -m joint_scheduling --config outputs/.../inputs/config.json validate > logs/02c-static-validate.log` | 0 | `配置有效：…` —— **仅 Windows 侧静态结构校验，不是 simai 环境验证，也未启动 ns-3**；不用于替代端到端运行 |
| 8 | `find joint_scheduling/artifacts/backend/current ... > logs/03-backend-artifacts.txt` | 0 | 存在 `build_manifest.json`（Linux g++ 15.2.0 / cmake 3.31.10 / `simai` 路径），说明后端曾在 WSL 构建，但本会话无法执行 |
| 9 | 重新计算 T001 `results/frozen/` 四个文件 SHA256 并与 T001 `manifest.json` 比对（内联 Python） | 0 | 4 个文件全部 `OK`，T001 冻结件未被修改 |
| 10 | `git -c safe.directory='*' status --short --untracked-files=all`（最终） | 0 | 仍为 71 行，与执行前一致；无新增正式源码/配置/输入改动 |

## 材料阅读（只读）

`AGENTS.md`、`outputs/.../T001-baseline-freeze/REPORT.md`、`REVIEW.md`、`STATUS.json`、`results/baseline.md`、`results/frozen/manifest.json`；T002 的 `TASK.md`、`FORWARD.txt`、`STATUS.json`。另只读核对 `joint_scheduling/backend/runner.py` 中 `binding_plan` 契约、`joint_scheduling/binding/static.py` 与 T001 冻结输入/绑定前三条记录。

## 明确未运行（TASK.md 禁止项 + blocked 停止条件）

- `env CONDA_EXE=/home/m0nesy/miniconda3/bin/conda ./inference ... validate`（WSL `simai` 环境）
- `./scripts/simai-python` 调用 `backend.runner.run_baseline`（两次 run-a / run-b）
- `./inference backend doctor`、ns-3 重建（jobs=2）
- `./joint_scheduling/check.sh`、算法对比、KV/EP 干扰归因、持续加压、全量 Azure 轨迹、扩容、DeepEP 适配、MLU 计时
- `git commit` / `git push` / `git checkout` / `git reset` / `git clean`
- **未重试 `wsl.exe`**：安全策略明确禁止重试该程序、通过其他 shell/脚本启动或等价绕过；本轮也没有使用 `dangerouslyDisableSandbox`（该升级路径仅适用于 sandbox policy failure，本次为 Program Blacklist 拒绝）。

## 说明

TASK.md 第 112 行规定「若工具策略拒绝WSL或后端执行，保存拒绝信息，以blocked报告；不能用Windows静态校验、计时器或synthetic fixture替代实际ns-3运行」。本轮所有 Windows 侧工作仅用于：保存拒绝信息、核对输入身份、准备可复用的 `inputs/`；`results/acceptance-summary.json` 中 A2–A6 一律为 `not_run`，没有任何端到端验收或性能结论。
