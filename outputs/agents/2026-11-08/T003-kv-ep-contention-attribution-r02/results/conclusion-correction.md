# T003 研究结论修正（补充文件，不改写历史原件）

- 性质：WSL 历史证据的服务器端只读静态复核；**不是新实验**，不提升证据等级。
- 关联原任务：`/home/s223/m0nesy/Inference/outputs/agents/2026-11-07/T003-kv-ep-contention-attribution`
- 原 `REPORT.md`、`TASK.md`、`STATUS.json`、`results/*.json`、`logs/*` 全部保持字节不变；本文件只作为审核时的结论修正与撤回清单。
- 数值依据：`results/static-recomputation.json`（从历史原始 `flows.csv`、`completions.csv`、`stages.json` 重新计算）、原 `results/contention-summary.json`、`results/request-impact-summary.json`。

## 修正后的唯一结论

**检出极弱网络互扰；冻结条件下指定请求边界暴露为零；端到端反事实性能变化未测。**

英文对应：Detected extremely weak network interference; exposure at the specified frozen request-necessary boundaries is zero; end-to-end counterfactual performance change was not measured.

支撑数值（静态复算，退出码 0，201677 条跨节点流 / 64148 个 EP 阶段全部重算）：

| 方向 / 阶段 | 有符号差值 | 影响数量 |
| --- | ---: | --- |
| EP→KV（移除跨节点 EP，replay-kv） | 最大 `+144 ns`（0.144 μs），均值 `0.221 ns` | 671 条 KV 流中 3 条正向，其余 0 |
| KV→EP（移除跨节点 KV，replay-collective） | 最大 `+136 ns`（0.136 μs），最小 `-127 ns`（反向改善） | 201006 条 EP 流中 5 条正向、1 条负向 |
| EP 阶段门 | 1 个 `decode.ep_combine`（layer 42）`+9 ns` | 64148 个阶段中仅 1 个非零 |
| P→D 准入门 ΔG | 全部 `0 ns` | 8/8 请求 |
| 最终层 60 combine 暴露 | 全部 `0 μs` | 550 个必要边界（8 首 token + 542 Decode token） |

- **1 μs 仅是描述性计数门槛**，不是统计显著性判断；`>1 μs` 计数为 0 不等于互扰为零。上述 144/136/−127/9 ns 是实际检出的非零差值，必须如实报告。
- 三层证据：① 资源共享与极弱网络互扰（已测，冻结日历移除对照）；② 指定请求必要边界的条件性暴露（已测，为 0）；③ 端到端反事实 SLO/goodput 变化（**未测**，重放不重新执行请求/计算调度，`layer3.status=not_measured`）。
- 本结论**不**支持：互扰不存在；任何算法差异都不可能来自竞争；据此关闭整个研究主线；当前样本可直接判定算法对照无价值。是否进入 KV 调度算法对照、是否需要换负载/拓扑重跑，均由指挥裁决。

## 撤回的过度推断（原 REPORT.md，逐条登记）

| 原文件行 | 原表述（摘录） | 问题 | 修正后表述 |
| --- | --- | --- | --- |
| 11 | 「未检出可测量的 KV↔EP 互扰」；「不支持在同等条件下进入 KV 调度算法对照」 | 与非零差值矛盾；把样本判断升级为研究方向判断 | 改为上述「修正后的唯一结论」；算法对照价值交指挥裁决 |
| 16 | 「移除对照几乎为零」 | 定性词掩盖 144/136/−127/9 ns 的实际检出 | 改为「互扰极弱但非零」，并列出具符号差值与影响数量 |
| 21 | 「任何差异都不可能归因于 KV/EP 竞争」 | 无反事实请求轨迹，不能作全称否定 | 删除；改为「本轮未测端到端反事实变化，不能判断算法差异来源」 |
| 23 | 「A1–A6 全部 passed」 | A6 只检查有效阶段墙钟，漏掉启动次数与停止条件 | 以 `results/acceptance-summary.corrected.json` 为准：`all_passed=false`、A6=`failed` |
| 62 / commands.md 21 | 「A1–A6 全 passed」 | 同上；且来自旧分析脚本 | 登记为历史记录，不再作为有效判定 |
| 90 | 「未检出互扰」 | 同上 | 改为「检出极弱互扰，仅对本样本与当前拓扑/负载成立」 |
| 97 | 「KV/EP 竞争在冻结基线样本中不成立」；「据此关闭竞争归因主线」 | 事实错误 + 越权关闭研究主线 | 删除；是否关闭主线由指挥决定 |
| 101 | 「未检出可测量互扰…判定当前样本不进入算法对照」 | 同上 | 改为请指挥裁决证据可采性与下一步范围 |

## 证据来源声明

- T001～T003 原运行均为**本机 WSL 历史证据**；完整包 `artifacts/history/wsl-2026-11-07-task-records/records-full.tar.gz`，SHA256 `5ea2d7c640e060c32b259a0d8a94f97b758f05b29bf562c11611f8704fc18a8a`（读取前已核对，见 `results/archive-audit.json`）。
- 本轮在服务器 `main` / HEAD `df0248a` 上，把历史包解到独立目录 `work/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/archive-audit/` 做只读审计；323/323 文件哈希匹配，262 个服务器跟踪副本哈希不变。
- 未启动 live、network replay、长时间实验；未扩大样本、未改变冻结条件、未修改正式仿真机制；未改写旧结果冒充服务器新运行。

## 数值细节补充（复核时容易被误读的三处）

1. **`+127 ns` 与 `−127 ns` 同一阶段相互抵消**：二者都在阶段 37473，分别是 `gpu12→gpu27`（+127 ns）与 `gpu13→gpu27`（−127 ns），因此该阶段净差为 0；阶段级最大 +9 ns 出现在阶段 7059，而不是最大流差值所在的 23504。故「流最大 136 ns / 阶段最大 9 ns」并非矛盾。
2. **单位**：`contention-summary.json` 以 μs 记 `0.144 / 0.136 / −0.127 / 0.009`，换算为 144 / 136 / −127 / 9 ns；两种写法是同一数值，不应混用为「0.144 ns」。
3. **复算一致性**：`flow-comparison.csv` 201677 行的 `delta_ack_ns` 与 `both − isolated` 重算逐行一致（最大差 0）；`stage-comparison.csv` 64148 行与之最大差 0；由 `local_ack + 远端 ACK` 重建的阶段完成时刻与 live 最大误差 0 ns。

## 本轮退出码与日志位置

| 步骤 | 退出码 | 含义 |
| --- | ---: | --- |
| 归档审计 `archive_audit.py` | 0 | 323/323 匹配、262 副本不变 |
| 静态复算 `interference_recompute.py` | 0 | 全部交叉核对通过 |
| 修正验收 `acceptance_revision.py` | **3** | 复核完成且记录到流程偏离（A6 failed），非崩溃 |
| 原目录树哈希 `t3_tree_hash.py` | 0 | 84 文件，前后树哈希一致 |
| 复核记录 `record_static_review.py` | 0 | 汇总 `all_passed=true` |

日志：`logs/r02-00-environment.log`、`logs/r02-01-archive-audit.log`、`logs/r02-02-static-recompute.log`、`logs/r02-03-acceptance-revision.log`、`logs/r02-04-tree-hash-after.log`、`logs/r02-05-exit-codes.txt`、`logs/r02-06-command-record.log`。

## 引用可解析性说明

原 `REPORT.md` 引用的 `results/attempt1-failed/`（24 文件）、`results/diagnostic/flows.csv`、`stages.json`、`native_schedule.jsonl`，以及 `flow-comparison.csv`、`stage-comparison.csv`、`port-intervals.csv`、`event-links.jsonl`，在 `ARCHIVE.json` 中均为 `tracked_snapshot=false`，只存在于完整历史包，**不在服务器任务目录、也不在 Git 快照**。因此这些相对路径必须先解包才能解析；本轮已在 `archive-audit/` 解包并逐文件核对 SHA256，未回写原任务目录。是否对原报告出具勘误由指挥决定。
