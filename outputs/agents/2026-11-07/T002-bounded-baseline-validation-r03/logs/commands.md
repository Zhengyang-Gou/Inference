# T002-r03 实际命令、运行目录与退出码

- 运行目录（所有命令）：`/home/m0nesy/Postgraduate/Inference`
- WSL 调用统一前缀：`MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' wsl.exe -e /bin/bash -lc '...'`
- 项目环境：`env CONDA_EXE=/home/m0nesy/miniconda3/bin/conda PYTHONPATH=. ./scripts/simai-python ...`（WSL `simai`，Python 3.12.16）
- 时间：2026-11-07 18:54–19:03 +08:00。父任务书原文 `TASK-parent.md` 与本轮 `TASK.md` 均保留。

| # | 命令 | 退出码 / 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | `git -c safe.directory='*' status --short --untracked-files=all > logs/baseline-status.txt` | 0，71 行 | `logs/baseline-status.txt` |
| 2 | `git -c safe.directory='*' rev-parse HEAD > logs/git-head.txt` | 0，`91cf1e2a16b8…` | `logs/git-head.txt` |
| 3 | 复制 r02 `inputs/*.json` 到本轮 `inputs/` 并逐个 SHA256 比对 | 0，5 个文件全部 `OK` | `inputs/` |
| 4 | `timeout 900 env CONDA_EXE=... PYTHONPATH=. ./scripts/simai-python work/.../capture_boundary.py` | **0**，墙钟 282.634 s，`EPFlowAudit=passed`，hooks 事件 814151，捕获边界行 13844 | `logs/01-capture.log`、`results/audit-run/`、`results/boundary-events.json` |
| 5 | `env CONDA_EXE=... PYTHONPATH=. ./scripts/simai-python work/.../analyze_boundary.py` | **0**，KV 244 对 / Prefill→Decode 3 对 / 61 层→token 205 对，违规 0；与 r02 的 9 个语义字段全一致 | `logs/02-analyze.log`、`results/boundary-audit.json` |
| 6 | 只读核对 r02 运行目录文件数（run-a/run-b 各 27）与 `REPORT.md`、`acceptance-summary.json` 的 SHA256 | 0，未改动 | 见 `REPORT.md` |
| 7 | 最终 `git status --short --untracked-files=all` 与 `diff baseline-status.txt final-status.txt` | 0，`IDENTICAL`（71 行） | `logs/final-status.txt` |

## 脚本说明

- `capture_boundary.py`：一次 `run_baseline` 调用，配置/负载/profile/绑定计划/seed 与 r02 完全相同；`RuntimeHooks(event=dispatch)` 把事件副本分发给 `EPFlowAudit.event` 与一个只读 `BoundaryCapture`，后者只保留三处边界所需字段（字段白名单写在 `boundary-events.json → field_whitelist`）。`recording.mode` 仍为 `essential`，未改配置、未新增仿真事件、未改时钟或事件顺序、未修改网络。
- `analyze_boundary.py`：只读 `boundary-events.json`、`audit-run/events.jsonl` 及 r02 产物，生成三处逐事件配对结果、违规数、间隔统计与语义等价性核对。
- 两者均为本轮 `work/agents/2026-11-07/T002-bounded-baseline-validation-r03/` 草稿，不属正式源码。

## 为什么需要一次新的采集运行

r02 使用 `recording.mode="essential"`；`results/run-a/result/summary.json → recording.omitted_event_counts` 明确记录 `prefill_chunk_layer_ready` 244、`transfer_started` 142212、`compute_step_completed` 12749、`decode_batch_started/completed` 各 205 等边界事件**都已产生但被省略写盘**，`events.jsonl` 只有 10 类生命周期事件，`observation/instances.csv` 只有聚合值。因此三处逐事件时序无法从原产物静态恢复，只能通过项目现有 hooks 在写盘前捕获只读副本。该运行输出到全新的 `results/audit-run/`，r02 的 `run-a/`、`run-b/` 及全部结论未被读取以外的任何操作，也未重跑。

## 明确未运行

- 算法对比、KV/EP 干扰归因、持续加压、全量 19366 请求轨迹、扩容、DeepEP 适配、MLU 计时、完整 `check.sh`
- 第二次采集运行（r02 已证明两次语义一致，本轮只补边界证据）
- ns-3 重建、后端源码修改、依赖安装/升级、后端切换
- 修改 r02 的 `REPORT.md`、`STATUS.json`、验收汇总或任何运行文件；修改正式源码/配置/原始输入/T001 冻结件
- `git commit` / `git push` / `git checkout` / `git reset` / `git clean`，以及启动其他 agent
