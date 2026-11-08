# T003 修复轮：范围记录（不是新任务书）

- 本目录为原 T003 的返工补充目录，按 `AGENTS.md` 采用 `-r02` 后缀并在 `STATUS.json` 记录 `parent_task`。
- **未创建新的 TASK.md**：指挥没有发布新任务书；唯一有效需求仍是原 T003 `TASK.md`，本文件只登记本轮授权范围与用户原文，不改写原任务书。
- 原 T003 目录：`/home/s223/m0nesy/Inference/outputs/agents/2026-11-07/T003-kv-ep-contention-attribution`
- 原 TASK.md SHA256：`60970ac5a33a8881738fda811710928f7b94f0e23d79dc286ce682cec5405b77`
- 原 REPORT.md SHA256：`964fd132cff554d2902adaca2227c8f38e03982f50bfe19eafb03e5843e5f0dc`
- 原 acceptance-summary.json SHA256：`2556977a9040e08793a7ff377359c8761921f27470b7833ce6468837f8ad9dae`
- `coordination/PROGRESS.md` SHA256（只读，未修改）：`e024cc6957b38a2f5b71dc12d467f90b4c37b585d281701ab990001fdc21367a`
- 服务器 HEAD（执行前）：`df0248a1327c41645b00110ff6d260d6f6884587`，分支 `main`，`git status --short --untracked-files=all` 为 0 行。
- 上轮审核仅基于 GitHub 提交 `df0248a` 静态预审，服务器无正式 `REVIEW.md`；本轮不自行设置 `accepted`。

## 用户原文（本轮唯一授权范围）

```
Inference 仓库已迁移到远端 Linux。你现在作为执行 agent 修复 T003，所有开发、验证、分析、文件更新和归档都在服务器执行。本机 WSL/Ubuntu 已卸载，禁止使用 `wsl` 命令或旧 UNC 路径。
连接：从 Windows PowerShell 执行 `ssh m0nesy`，非交互执行用 `ssh m0nesy '远端命令'`。
服务器仓库：`/home/s223/m0nesy/Inference`，分支 `main`。
Conda：`/home/s223/miniconda3`，环境 `simai`，Python 3.12.16；非交互命令显式设置 `export CONDA_EXE=/home/s223/miniconda3/bin/conda`，使用 `./inference`、`./scripts/simai-python`。
开始前先读取服务器 `AGENTS.md`，检查并保存 `git status --short --untracked-files=all`、HEAD，再读取 `coordination/PROGRESS.md`。保留所有已有修改，不 reset、stash、覆盖或自动拉取。T003 原任务目录为：
`/home/s223/m0nesy/Inference/outputs/agents/2026-11-07/T003-kv-ep-contention-attribution`
上轮审核仅基于 GitHub 提交 `df0248a` 静态预审，未进入服务器，也未写入正式 REVIEW。请先核对服务器 TASK、REPORT、原始日志、分析脚本及历史归档，再处理以下问题：
1. **启动次数与 A6 判定。** 记录显示 live 实际启动两次，首次已推进至仿真时间 472621 ns、产生 12 条 native 注册，不能直接按“网络开跑前的静态错误”排除。核对任务书的单次启动和失败停止要求，修复验收逻辑，使失败启动及停止条件违规进入判定，不能只检查有效阶段墙钟时间。保留失败现场，明确登记偏离，交指挥裁决；不能靠修改脚本消除既有违规，也不能继续声称 A1–A6 全部通过。
2. **收窄研究结论。** 汇总中存在 EP→KV 最大 144 ns、KV→EP 最大 136 ns、反向 −127 ns，以及一个 EP 阶段 9 ns 的差值。核实后将结论准确表述为“检出极弱网络互扰；冻结条件下指定请求边界暴露为零；端到端反事实性能变化未测”。1 μs 仅是描述计数门槛。删除“互扰不存在”“任何算法差异都不可能来自竞争”“据此关闭整个研究主线”等过度推断。
3. **保留证据来源。** T001～T003 原运行属于 WSL 历史证据。保持历史原始件不变，按仓库返工约定另存修订脚本、补充报告和本轮服务器静态复核记录，关联原 T003；不得把旧结果改写为服务器新实验。服务器历史完整包位置见 `coordination/history/2026-11-07/ARCHIVE.json`，读取前核对哈希，并在独立目录审计。
本轮只授权文档、分析及验收逻辑修复和必要的只读静态复算。**不启动 live、network replay、长时间实验，不扩大样本或改变冻结条件，不修改正式仿真机制，不提交、不推送、不发布下一研究任务。** 不改写原 TASK，不自行设置 accepted，不修改指挥维护的进度索引。
若已有本机修复，先列清单、比较服务器版本及哈希，再定向迁移必要修复文件；不得覆盖未知修改或复制旧结果冒充新运行。SSH 失败时保留错误并报告阻塞，不退回本机执行。
完成后提供服务器补充报告绝对路径、修改文件清单、实际复核命令及退出码、仍需指挥裁决的事项，保持待审。
```

## 本机修复清单（迁移前核对）

上一轮会话工作区 `C:/Users/33199/WorkBuddy/2026-11-07-15-08-25` 仅有 `.workbuddy/memory/2026-11-07.md`（会话笔记），**没有任何本机修复脚本或结果文件**。因此无需定向迁移。

## 本轮会话（2026-11-08 08:47 起）的现状核对与迁移记录

1. **服务器已有同日草稿，先核对再改**：进入时发现 `work/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/` 已存在 3 个脚本（`archive_audit.py`、`interference_recompute.py`、`acceptance_revision.py`）与 `archive-audit/` 解包副本，`outputs/agents/2026-11-08/...-r02/` 已有 REPORT/SCOPE/results。未直接采信，先逐文件读取源码、核对硬编码路径与证据文件是否存在，再决定改动范围。
2. **只覆盖一个自有文件并留存 v1**：仅重写 `acceptance_revision.py`；覆盖前将其原样复制为 `legacy/acceptance_revision.v1.py`（SHA256 `7965047b529712eb4131b6a410424e9850744fe311cbded0d214cfde54647ad8`）。
3. **本机无产出**：本机工作区 `C:/Users/33199/WorkBuddy/2026-11-08-08-47-19` 仅有驱动脚本副本（`_driver/`，即上传到服务器的同一批脚本）与阅读用的临时镜像（核对完成后已删除）；**所有脚本在服务器执行，所有结果写在服务器**，本机未运行 Python、未产生任何结果文件，也未使用 `wsl` 或旧 UNC 路径读写仓库。
4. **传输方式**：脚本以 `scp` 上传，命令以 `ssh m0nesy` 执行；PowerShell 管道读取中文会乱码，故核对材料改用 `scp` 取回后只读查看，避免以乱码内容作判断。
5. **未覆盖未知修改**：`git status --short --untracked-files=all` 在进入与结束时均为 0 行；原 T003 目录 84 文件树哈希前后一致（`52fe4a5f…afe5`）。
