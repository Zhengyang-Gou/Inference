# 研究推进记录

最后整理：2026-11-07。维护人：指挥 agent。这里记录研究状态；任务需求和证据仍以各任务目录原文为准。

**当前停在 T003 已提交、等待指挥审核。T004 尚未发布。** 不能因为执行报告声称 passed 就把 submitted 改成 accepted。

## 已确定的研究范围

第一阶段固定每个请求的 P/D 绑定，关注 KV 与 EP 的竞争及 KV 调度对请求性能的影响。主指标是 SLO 达标完成吞吐，辅看 TTFT、handoff=t2−t1、后续 ITL、失败和未完成。沿用一套冻结条件，尚未开始算法收益对照。

冻结基线：32 GPU（4×8）、P16/D16、TP1、两侧 EP16、61层估算计算profile、确定性合成路由、seed7；TTFT1秒/ITL100ms为人工实验假设。

## 任务状态

| 任务 | 状态 | 已完成内容与当前位置 |
| --- | --- | --- |
| [T001 基线冻结](../outputs/agents/2026-11-07/T001-baseline-freeze/TASK.md) | accepted | [审核](../outputs/agents/2026-11-07/T001-baseline-freeze/REVIEW.md)：配置、profile、真实来源负载、父绑定计划已冻结；[冻结输入](../outputs/agents/2026-11-07/T001-baseline-freeze/results/frozen/manifest.json) |
| [T002 原轮次](../outputs/agents/2026-11-07/T002-bounded-baseline-validation/TASK.md) | blocked（历史） | 工具环境阻塞；证据保留，后由 r02/r03继续 |
| [T002-r02](../outputs/agents/2026-11-07/T002-bounded-baseline-validation-r02/REPORT.md) | changes_requested（历史） | 两轮3请求/208token实际跑通；[审核](../outputs/agents/2026-11-07/T002-bounded-baseline-validation-r02/REVIEW.md)要求补三处边界证据，现已由r03补齐 |
| [T002-r03](../outputs/agents/2026-11-07/T002-bounded-baseline-validation-r03/REPORT.md) | accepted | [审核](../outputs/agents/2026-11-07/T002-bounded-baseline-validation-r03/REVIEW.md)与[合并验收](../outputs/agents/2026-11-07/T002-bounded-baseline-validation-r03/results/acceptance-summary.json)：有界基础链路验收完成；244条KV、3个交接边界、205个Decode token边界复核通过 |
| [T003 KV/EP竞争归因](../outputs/agents/2026-11-07/T003-kv-ep-contention-attribution/TASK.md) | **submitted，待审** | [执行报告](../outputs/agents/2026-11-07/T003-kv-ep-contention-attribution/REPORT.md)、[状态](../outputs/agents/2026-11-07/T003-kv-ep-contention-attribution/STATUS.json)、[结果汇总](../outputs/agents/2026-11-07/T003-kv-ep-contention-attribution/results/contention-summary.json)：报告称8请求样本互扰极小、必要边界暴露为0；尚未经指挥独立审核，失败启动与重跑记录也待核对 |

T003 的负结果只是执行方提交的有限样本判断，不能据此关闭整个研究主线，也不构成调度算法收益证据。下一动作是审核 T003；本次归档没有开展审核、重跑或新实验。

## 执行环境已迁移

默认服务器：/home/s223/m0nesy/Inference，main；Windows PowerShell 使用 ssh m0nesy，CONDA_EXE=/home/s223/miniconda3/bin/conda，simai/Python3.12.16。流程见[远端执行](REMOTE_EXECUTION.md)。

代码迁移检查点 e9b447d 已推送 GitHub。[服务器迁移报告](../outputs/migrations/2026-11-07/server-migration-201527/REPORT.md)和[本轮服务器验证](../outputs/migrations/2026-11-07/server-migration-201527/validation-summary.json)记录18个定向测试通过、12个synthetic smoke请求完成并满足配置SLO；未做完整回归或服务器研究负载实验。

上述 T001～T003 是 **WSL历史研究记录**，原文及输入保持字节不变；复制到服务器/Git不会把它们变为服务器实验。服务器迁移验证是另一组证据。原始报告内旧绝对路径仅用于历史来源说明；新任务使用服务器实际路径和本轮实测。

## 如何找到完整证据

Git 保存唯一任务目录中的任务书、状态、报告、审核意见、冻结输入、关键汇总/请求CSV、紧凑日志和分析脚本。大体积日历、逐流明细及源码压缩包在服务器完整历史包：

/home/s223/m0nesy/Inference/artifacts/history/wsl-2026-11-07-task-records/records-full.tar.gz

[ARCHIVE.json](history/2026-11-07/ARCHIVE.json)逐文件记录原位置、大小、SHA256、是否进入Git，并记录完整包的SHA256。文件选择与后续提交方法见[版本归档规则](ARCHIVING.md)。完整包是历史证据备份，不是新的运行结果。
