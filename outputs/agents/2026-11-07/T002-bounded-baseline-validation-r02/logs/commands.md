# T002-r02 实际命令、运行目录与退出码

- 运行目录（所有命令）：`/home/m0nesy/Postgraduate/Inference`
- WSL 调用统一前缀：`MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' wsl.exe -e /bin/bash -lc '...'`
  （Git Bash 会把 `/bin/bash` 自动转换成 Windows 路径，导致 WSL 内 `execvpe` 失败；禁用 MSYS 路径转换后正常）
- 项目环境：`env CONDA_EXE=/home/m0nesy/miniconda3/bin/conda PYTHONPATH=. ./scripts/simai-python ...`（WSL `simai`，Python 3.12.16）
- 时间：2026-11-07 17:02–17:18 +08:00；`TASK.md` / `FORWARD.txt` 原文保留，原 blocked 轮次目录未改动。

## 环境前置验证（黑名单解除后）

| 命令 | 退出码 | 关键输出 |
| --- | --- | --- |
| `MSYS_NO_PATHCONV=1 ... wsl.exe -e /bin/bash -lc 'echo WSL_OK; uname -sr; /home/m0nesy/miniconda3/envs/simai/bin/python -V'` | 0 | `WSL_OK` / `Linux 6.18.54.1-microsoft-standard-WSL2` / `Python 3.12.16` |
| `env CONDA_EXE=... ./inference --config outputs/.../T002-bounded-baseline-validation/inputs/config.json validate` | 0 | `配置有效：基础模块与 evaluation 的结构和约束检查通过。` |
| `env CONDA_EXE=... ./inference backend doctor` | 0 | `backend_ready: true`，二进制 `.../current/source/build/bin/joint-scheduling-ns3`，fingerprint `23ec96146135d0706e39` |

注：首次不带上表前缀的 `wsl.exe -e /bin/bash -lc '...'` 返回 1，错误为 `CreateProcessCommon ... execvpe(C:/.../PortableGit/usr/bin/bash) failed`；这是路径转换问题，不是黑名单拒绝，已通过加前缀解决。

## 本轮命令

| # | 命令 | 退出码 / 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | `git -c safe.directory='*' status --short --untracked-files=all > logs/baseline-status.txt` | 0，71 行 | `logs/baseline-status.txt` |
| 2 | `git -c safe.directory='*' rev-parse HEAD > logs/git-head.txt` | 0，`91cf1e2a16b8…` | `logs/git-head.txt` |
| 3 | 复制 T001/原 T002 的 `TASK.md`、`FORWARD.txt` 与 `inputs/*.json` 到 r02，逐个 SHA256 比对 | 0，5 个输入文件哈希全部 `OK` | `inputs/sample-manifest.json` |
| 4 | `timeout 900 env CONDA_EXE=... PYTHONPATH=. ./scripts/simai-python work/.../run_validation.py --run run-a` | **0**，run-a 完成，墙钟 280.673 s，`EPFlowAudit=passed`，814151 条只读事件 | `logs/04-run-a.log`、`results/run-a/`、`results/run-a-hooks.json` |
| 5 | 同一命令 `--run run-b` | **0**，run-b 完成，墙钟 287.665 s，`EPFlowAudit=passed`，814151 条只读事件 | `logs/05-run-b.log`、`results/run-b/`、`results/run-b-hooks.json` |
| 6 | `env CONDA_EXE=... PYTHONPATH=. ./scripts/simai-python work/.../check_results.py` | **0**，38/38 机制检查通过、13 个语义字段全一致、A1–A6 全 passed | `logs/06-check-results.log`、`results/mechanism-checks.json`、`results/reproducibility-check.json`、`results/acceptance-summary.json` |
| 7 | 最终 `git status --short --untracked-files=all` 与 `diff baseline-status.txt final-status.txt` | 0，`IDENTICAL`（71 行） | `logs/final-status.txt` |

两次运行合计墙钟 **568.338 s（约 9.5 分钟）**，小于任务书 30 分钟合计上限；每次均小于 15 分钟单次上限；仿真时间上限 30 s 由 `runtime.time_limit_seconds=30.0` 与 `evaluation.window_end_seconds=30.0` 保证（后端 RPC 汇总 `time_ns=30000000000`）。`run-a` / `run-b` 由 `run_baseline` 自行 `mkdir(exist_ok=False)` 创建，脚本额外拒绝覆盖。

## 运行脚本说明

`work/agents/2026-11-07/T002-bounded-baseline-validation-r02/run_validation.py` 每次调用只跑一个 run（首轮失败不会自动补跑），通过 `RuntimeHooks(event=dispatch)` 把已发出事件的**副本**分发给 `EPFlowAudit.event` 和一个只读事件计数器；不修改算法、时钟、事件顺序或网络。`check_results.py` 只读两次运行产物，生成机制、可复现性、指标与验收汇总。两者均为本轮草稿，不属正式源码。

## 明确未运行

- 算法对比、KV/EP 干扰归因实验、持续加压、全量 19366 请求 Azure 轨迹、扩容、DeepEP 适配、MLU 计时、完整 `check.sh`
- ns-3 重建（`backend doctor` 已就绪，无需重建）、后端源码修改、依赖安装/升级、后端切换
- `git commit` / `git push` / `git checkout` / `git reset` / `git clean`，以及启动其他 agent
- 未改动正式源码、正式配置、原始数据、T001 冻结件或原 T002 blocked 轮次文件
