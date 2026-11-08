# 研究推进记录

最后整理：2026-11-08。维护人：指挥 agent。这里记录研究状态；任务需求和证据仍以各任务目录原文为准。

**当前：T003-r02 静态修复已审核接受；原 T003 的 A6=failed、all_passed=false 保留。历史数据仅按带偏离的诊断证据采纳。T004 尚未发布。** r02 accepted 不表示原执行全部通过。

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
| [T003 KV/EP竞争归因](../outputs/agents/2026-11-07/T003-kv-ep-contention-attribution/TASK.md) | 原A6 failed，历史原文保留 | 原[报告](../outputs/agents/2026-11-07/T003-kv-ep-contention-attribution/REPORT.md)和submitted状态未改写；原“全部passed”与过度结论被[r02指挥审核](../outputs/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/REVIEW.md)及修订件替代 |
| [T003-r02静态修复](../outputs/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/REPORT.md) | **accepted（仅修复范围）** | 服务器独立41项检查通过；极弱互扰最大144ns/136ns，指定8个准入门及550个token最终边界暴露0，端到端反事实未测；启动/停止偏离保留；[证据定位](../outputs/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/EVIDENCE_LOCATIONS.md) |

T003的修复审核已完成。经复算确有极弱网络互扰，冻结条件下指定请求边界暴露为零，端到端反事实变化未测。原执行违规不能通过改脚本消除；现数据仅用于有限历史机制诊断，不据此关闭主线或宣称算法收益。当前没有授权重跑、扩大样本或改变冻结条件。

## 执行环境已迁移

默认服务器：/home/s223/m0nesy/Inference，main；Windows PowerShell 使用 ssh m0nesy，CONDA_EXE=/home/s223/miniconda3/bin/conda，simai/Python3.12.16。流程见[远端执行](REMOTE_EXECUTION.md)。

代码迁移检查点 e9b447d 已推送 GitHub。[服务器迁移报告](../outputs/migrations/2026-11-07/server-migration-201527/REPORT.md)和[本轮服务器验证](../outputs/migrations/2026-11-07/server-migration-201527/validation-summary.json)记录18个定向测试通过、12个synthetic smoke请求完成并满足配置SLO；未做完整回归或服务器研究负载实验。

上述 T001～T003 原运行是 **WSL历史研究记录**；T003-r02是服务器静态审计，不是新仿真。原任务文件与输入保持字节不变；复制到服务器/Git不会把它们变为服务器实验。服务器迁移验证是另一组证据。原始报告内旧绝对路径仅用于历史来源说明；新任务使用服务器实际路径和本轮实测。

## 如何找到完整证据

Git 保存唯一任务目录中的任务书、状态、报告、审核意见、冻结输入、关键汇总/请求CSV、紧凑日志和分析脚本。大体积日历、逐流明细及源码压缩包在服务器完整历史包：

/home/s223/m0nesy/Inference/artifacts/history/wsl-2026-11-07-task-records/records-full.tar.gz

[ARCHIVE.json](history/2026-11-07/ARCHIVE.json)逐文件记录原位置、大小、SHA256、是否进入Git，并记录完整包的SHA256。文件选择与后续提交方法见[版本归档规则](ARCHIVING.md)。完整包是历史证据备份，不是新的运行结果。
