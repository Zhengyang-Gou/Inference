# T003 实际命令、退出码与运行记录

所有 WSL 调用均带 `MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'` 前缀（Git Bash 会重写 `/bin/bash`，否则 WSL 报 `execvpe` 失败）。工作目录统一为 `/home/m0nesy/Postgraduate/Inference`。环境入口统一 `env CONDA_EXE=/home/m0nesy/miniconda3/bin/conda`，脚本另加 `PYTHONPATH=.`。

| # | 阶段 | 实际命令（要点） | 退出码 | 墙钟 / 时刻 | 日志 |
| --- | --- | --- | ---: | --- | --- |
| 1 | 输入准备 | `./scripts/simai-python work/.../prepare_inputs.py` | 0 | 19:30 | `logs/01-prepare-inputs.log` |
| 2 | 环境预检 | `./inference --config outputs/.../T003.../inputs/config.json validate` | 0 | 19:31 | `logs/02-validate-doctor.log` |
| 3 | 环境预检 | `./inference --config outputs/.../T003.../results/diagnostic/config.json validate` | 0 | 19:31 | 同上 |
| 4 | 环境预检 | `./inference backend doctor` → `backend_ready=true`，fingerprint `23ec96146135d0706e39` | 0 | 19:31 | 同上 |
| 5 | 身份核对（首轮） | `./scripts/simai-python work/.../check_environment.py`（草稿键名笔误 `ns3_revision`） | 1 | 19:31 | `logs/02a-identity-attempt1-failed.log`（原始失败保留） |
| 6 | 身份核对 | 修正草稿键名后重跑 → `all_passed=true`；源码树 `427d8527…`、后端二进制哈希与 r03 相同 | 0 | 19:32 | `logs/03-identity-check.log` |
| 7 | 静态钩子复现 | `./scripts/simai-python work/.../diag_hook.py`（不启动 ns-3） | 0（脚本内打印 traceback） | 19:38 | `logs/05-static-hook-diag.log` |
| 8 | 静态修复验证 | 重跑 `diag_hook.py` → `OK probe.event` / `OK probe.capture` | 0 | 19:40 | `logs/06-static-hook-diag-fixed.log` |
| 9 | live 第 1 次启动 | `timeout -k 20 1500 ./scripts/simai-python work/.../live_baseline.py` | **1** | 0.835 s，19:35:42–19:35:43 | `logs/04-live-baseline.log`、`results/attempt1-failed/` |
| 10 | **live-baseline（有效）** | 同上（修正 `hook_counts` 命名冲突后） | **0** | 911.906 s，19:42:41–19:57:58 | `logs/07-live-baseline.log` |
| 11 | replay-both | `timeout -k 20 900 ./scripts/simai-python work/.../replay_stage.py --stage both` | 0 | 96.516 s，19:58:43–20:00:23 | `logs/08-replay-both.log` |
| 12 | 保真门槛 | `./scripts/simai-python work/.../check_replay_both.py` → 15/15，最大误差 0 ns | 0 | 20:00 | `logs/09-fidelity.log` |
| 13 | replay-collective | `timeout -k 20 900 ./scripts/simai-python work/.../replay_stage.py --stage collective` | 0 | 86.651 s，20:00:54–20:02:25 | `logs/10-replay-collective.log` |
| 14 | replay-kv | `timeout -k 20 900 ./scripts/simai-python work/.../replay_stage.py --stage kv` | 0 | 46.932 s，20:02:36–20:03:27 | `logs/11-replay-kv.log` |
| 15 | 只读分析 | `./scripts/simai-python work/.../analyze_attribution.py` → A1–A6 全 passed，无错误 | 0 | 20:03 | `logs/12-analyze.log` |

## 预算与次数

- live 仿真 `time_limit_seconds=30`，测量窗口 `[0,30)`，无预热；重放排空上限沿用 `max(30 s, 最后基线 ACK + 1 s)`。
- 4 个有效阶段墙钟合计 `911.906 + 96.516 + 86.651 + 46.932 = 1142.005 s ≈ 19.03 min`，远低于 70 min 合计上限；各阶段分别低于 25/15/15/15 min 硬限；脚本内另设 `SIGALRM` 硬时限，外层 `timeout -k` 二次保护。
- 网络阶段启动次数：**1 次有效 live + 3 次 replay**（both / collective / kv），无补跑、无重复 seed、无额外负载档位。
- 另有 1 次 **静态草稿失败启动**（#9）：0.835 s、仿真时刻 472621 ns、12 条 native 注册、0 个请求完成、无 `result/` 导出、无指标；原因是草稿把官方 `Probe.counts` 覆盖为 `Counter`。按 TASK.md「普通静态脚本错误可在网络开跑前修正并记录」修正草稿属性命名，并在启动任何重放前完成；首轮全部产物保留在 `results/attempt1-failed/`，未用于任何结论。两次启动在 `REPORT.md` 中如实登记。
- 资源保护：`201677` 条跨节点逻辑流（< 1e6），`675175` 条 native 注册（< 2e6），未触发安全停止；`port-intervals.csv` 只保留 1 ms 资源计数差分，未复制 `network_snapshot.flows`。

## 未运行（按任务边界）

- 未运行 `Immediate/FIFO/MLU` 算法比较、未实现新调度器或隔离网络、未关闭真实计算/EP、未扩容、未跑 DeepEP/节点聚合/MLU 计时、未运行 19366 请求全量轨迹、未运行完整 `check.sh`、未提交或推送。
- 未重建/切换后端、未安装依赖、未修改正式源码、正式配置、原始输入或 T001/T002-r03 任一文件。
