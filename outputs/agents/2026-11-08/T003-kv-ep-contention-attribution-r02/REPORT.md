# T003-r02：T003 修复（验收逻辑、研究结论、证据来源）— 补充报告

- 执行 agent：WorkBuddy（执行 agent），全部工作在远端 Linux 服务器完成
- 本轮复核：2026-11-08T09:17:10+08:00 — 2026-11-08T09:17:30+08:00（只读静态复核，20 s 墙钟）
- 状态：**submitted（待审）**；不自行设置 `accepted`，不修改 `coordination/PROGRESS.md`
- 任务目录：`/home/s223/m0nesy/Inference/outputs/agents/2026-11-08/T003-kv-ep-contention-attribution-r02`
- 关联原任务：`/home/s223/m0nesy/Inference/outputs/agents/2026-11-07/T003-kv-ep-contention-attribution`（`parent_task`）
- 服务器：`/home/s223/m0nesy/Inference`，分支 `main`，HEAD `df0248a1327c41645b00110ff6d260d6f6884587`，`git status --short --untracked-files=all` 为 0 行
- 环境：`CONDA_EXE=/home/s223/miniconda3/bin/conda`，`./scripts/simai-python`，Python 3.12.16
- 上轮审核：仅基于 GitHub 提交 `df0248a` 静态预审，服务器无正式 `REVIEW.md`

## 结论

**修复完成，但 T003 不再是「A1–A6 全部通过」：A6 判定为 `failed`，整体 `all_passed=false`，4 项流程偏离已登记并交指挥裁决。**

研究结论收窄为唯一表述：

> **检出极弱网络互扰；冻结条件下指定请求边界暴露为零；端到端反事实性能变化未测。**

三层证据的边界：① 资源共享与极弱互扰（已测，冻结日历移除对照，量级 10¹–10² ns）；② 指定请求必要边界的条件性暴露（已测，为 0）；③ 端到端 SLO/goodput 反事实（`layer3.status=not_measured`，重放不重新执行请求/计算调度）。

## 一、启动次数与 A6 判定

### 1.1 事实核对（与任务书逐条对照）

`TASK.md` 第 224 行：「启动次数上限=1次live+3次network replay。前置失败、live机制失败/未完成、both不保真、输入身份冲突、超时或资源保护触发，保留现场并停止；不补跑……」

`TASK.md` 第 225 行：「普通静态脚本错误可在网络开跑前修正并记录……已经启动的网络阶段不得借“脚本修复”静默重启；需要返工则先报告。」

由 `logs/*console*.log` 的启动标记推导（不是照抄报告结论）：live 实际启动 **2 次**

| 启动 | 时间 | 退出码 | 到达仿真时刻 | native 注册 | 跨节点流 | hook 事件 | 生命周期事件 | EP 调用 | 墙钟 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 第 1 次 | 2026-11-07T19:35:40+08:00 | **1** | **472621 ns** | **12** | **1** | 12 | 4 | 0 | 0.835 s |
| 第 2 次 | 2026-11-07T19:42:41+08:00 | 0 | 30 s 窗口跑完 | 675175 | 201677 | — | — | — | 911.906 s |

第 1 次证据：`results/attempt1-failed/run/network/backend_rpc_summary.json`（`time_ns=472621`、`submission_commands=12`、`submission_batches=5`、`synchronous_exchanges=10`）、`results/attempt1-failed/live-stage.json`（`native_registrations=12`、`cross_node_flows=1`、`hook_events=12`）、`results/attempt1-failed/run/events.jsonl`（4 条生命周期事件）、`results/attempt1-failed/run/expert_parallel_summary.json`（`started_calls=0`）。

**判定**：ns-3 后端已提交 12 条 native 命令、完成 5 个批次和 10 次同步交换、推进到 472621 ns，属于**已经启动的网络阶段**，不属于「网络开跑前」的静态错误；`0 请求完成`只能说明失败发生在早期，不能作为排除理由。

### 1.2 原验收逻辑缺陷

原 `work/agents/2026-11-07/T003.../analyze_attribution.py` 的 A6 只比较 4 个有效阶段的墙钟（1142.005 s < 70 min）便判 `passed`，未把「启动 2 次」「失败后未停止」「已启动阶段借修复重启」「错误分类」纳入判定。原 `logs/commands.md` 因此写作「1 次有效 live + 3 次 replay，无补跑」，并称首次为「静态草稿失败启动」，与同一节记录的 472621 ns / 12 条注册自相矛盾。

本轮以独立修订脚本重建判定，**原脚本与原始结果未改动**。门禁结果：

| 项 | 值 | 判定 |
| --- | --- | --- |
| live 启动上限 / 实际 | 1 / **2** | 违反（V1） |
| 失败后是否停止 | 未停止（后续 1 次 live + 3 次 replay + 分析） | 违反（V2） |
| 已启动阶段是否先报告再重启 | 否（无 REPORT/REVIEW 先报告，间隔约 7 min） | 违反（V3） |
| 首次失败分类 | 原记「pre-network static script error」，实为 started network stage | 违反（V4） |
| 单阶段墙钟上限 25/15/15/15 min | 15.20 / 1.61 / 1.44 / 0.78 min | 未超 |
| 合计墙钟（含失败启动 1142.840 s） | 19.047 min < 70 min | 未超，但**不能据此清关门禁** |

`process_gate.status=failed`，`violation_count=4`。A6 因此判 `failed`，A2–A5 因依赖第 2 次 live 产物改判 `passed_with_deviation`（可达性由指挥裁决），A1 仍为 `passed`（纯静态核对）。

### 1.3 失败现场的保存与登记

第 1 次启动的完整现场（`run/` 全量自动产物、`event-links.jsonl`、`port-intervals.csv`、`live-baseline.log`、`live-stage.json`、`README.md`，共 24 个文件）保存在历史完整包内；本轮已解包到独立审计目录并逐文件核对哈希：

`work/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/archive-audit/outputs/agents/2026-11-07/T003-kv-ep-contention-attribution/results/attempt1-failed/`

现场未被本轮修改（审计目录只读比对）。**注意**：该目录不在 Git 快照、也不在服务器任务目录中，见第四节 D1。

## 二、研究结论收窄

从历史原始 `flows.csv`（201677 行）、`stages.json`（64148 阶段）、三份 `completions.csv` 逐条重算，结果（`results/static-recomputation.json`，退出码 0）：

| 方向 / 阶段 | 有符号差值 | 影响数量 | 说明 |
| --- | ---: | --- | --- |
| EP→KV（孤立 = replay-kv，移除跨节点 EP） | 最大 `+144 ns`，均值 `0.221 ns`，p50/p95/p99 全 0 | 671 条 KV 流中 3 条正向 | `gpu2→gpu15` 各 +2 ns（请求 …002）、`gpu6→gpu12` +144 ns（请求 …006） |
| KV→EP（孤立 = replay-collective，移除跨节点 KV） | 最大 `+136 ns`，最小 `−127 ns`，均值 `0.0016 ns` | 201006 条 EP 流中 5 正 1 负 | +64/+136 ns（…001，阶段 6269/7059）、+53 ns（…001，阶段 7673）、+77/+127 ns（…005，阶段 37402/37473）、−127 ns（…005，阶段 37473） |
| EP 阶段门 | 唯一非零 `+9 ns`（`decode.ep_combine`，阶段 7059，layer 42） | 64148 中 1 个非零 | 与 +136 ns 流同阶段，但阶段门取 `max(local_ack, 远端 ACK)`，被更晚完成项遮蔽 |
| P→D 准入门 ΔG | 8/8 全 `0 ns` | 8 请求 | 决定门的 KV 流本身差值为 0 |
| 最终层 60 combine 暴露 | 550/550 全 `0 μs` | 8 首 token + 542 Decode token | 指定冻结边界 |

要点：

- `+127 ns` 与 `−127 ns` 出现在**同一阶段 37473**（`gpu12→gpu27` 与 `gpu13→gpu27`），方向相反、相互抵消，因此阶段级净差为 0；这解释了「最大流差值 136 ns 但阶段最大仅 9 ns」，不是统计错误。
- **`1 μs` 仅是描述性计数门槛**，不是显著性检验；`>1 μs` 计数为 0 **不等于**互扰为零。144/136/−127/9 ns 是实际检出的非零差值，必须如实报告。
- 已删除的过度推断（逐行登记于 `results/conclusion-correction.md`）：「互扰不存在」「任何算法差异都不可能来自竞争」「据此关闭整个研究主线」「当前样本可直接判定算法对照无价值」。是否进入算法对照、是否关闭主线，均由指挥裁决。

## 三、证据来源与可解析性

- T001～T003 原运行均为**本机 WSL 历史证据**；本轮的服务器操作只是解包审计 + 只读复算，**不把旧结果改写为服务器新实验**。
- 完整包 `artifacts/history/wsl-2026-11-07-task-records/records-full.tar.gz`：SHA256 `5ea2d7c640e060c32b259a0d8a94f97b758f05b29bf562c11611f8704fc18a8a`，58791704 B，与 `coordination/history/2026-11-07/ARCHIVE.json` 记录一致（读取前核对）。
- 解包到独立目录 `work/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/archive-audit/`（383 MB，323 文件），**逐文件核对**：323/323 哈希与字节数匹配；262 个服务器跟踪副本哈希不变；0 处不符。`results/archive-audit.json`。
- 原 T003 任务目录在执行前后字节不变：84 文件，树哈希 `52fe4a5f4a57fa7874bf57fad00294b26e99bfb26e73dd19faccdf1974ddafe5`（`results/t003-original-tree-before.json` / `-after.json`）。`TASK.md` `60970ac5…`、`REPORT.md` `964fd132…`、`STATUS.json` `7c73b504…`、`results/acceptance-summary.json` `2556977a…` 与 `SCOPE.md` 记录一致；`coordination/PROGRESS.md` 仍为 `e024cc69…`（未改）。

### 引用可解析性缺陷（本轮新发现，已登记 D1–D3）

原 `REPORT.md` 把若干原始证据写成任务目录内相对路径，但这些文件**只存在于完整历史包**（`ARCHIVE.json` 记 `tracked_snapshot=false`），在服务器任务目录中不存在：

| 引用路径 | 任务目录内 | 历史包内 | Git 快照 |
| --- | --- | --- | --- |
| `results/attempt1-failed/`（24 文件） | 否 | 是 | 否 |
| `results/diagnostic/flows.csv`（14.96 MB） | 否 | 是 | 否 |
| `results/diagnostic/stages.json`（8.22 MB） | 否 | 是 | 否 |
| `results/diagnostic/native_schedule.jsonl`（87.35 MB） | 否 | 是 | 否 |
| `results/flow-comparison.csv`、`stage-comparison.csv`、`port-intervals.csv`、`event-links.jsonl` | 否 | 是 | 否 |

这属于**引用可解析性问题，不是证据丢失**：文件已在 `ARCHIVE.json` 逐条登记大小与 SHA256，本轮已解包核对。但它使原报告「现场完整保留于 `results/attempt1-failed/`」在服务器上无法直接解析，需在审核或后续引用时以包内路径为准。是否要求对原 REPORT 出具勘误（erratum），交指挥决定。

## 四、逐项验收（修正后，以 `results/acceptance-summary.corrected.json` 为准）

| 编号 | 状态 | 证据与说明 |
| --- | --- | --- |
| A1 | `passed` | 冻结身份、8 请求字段、父绑定投影、许可配置差异均为静态核对，不依赖 live；归档审计另证历史原件字节不变。 |
| A2 | `passed_with_deviation` | 8 请求 / 550 token、31/31 机制检查、EPFlowAudit 满足条目，但全部来自违反启动上限与停止条件的第 2 次 live，可采性需指挥裁决。 |
| A3 | `passed_with_deviation` | both 重放 201677/201677 排空、逐流与阶段误差 0 ns，但日历同源同上。 |
| A4 | `passed_with_deviation` | 双向有符号流/阶段差值与共享资源证据完整、已从原始文件重算，仍是 WSL 历史证据而非服务器实验。 |
| A5 | `passed_with_deviation` | 8 个 KV 门与 550 个 token 最终层依赖精确映射、条件性暴露为 0；端到端反事实明确未测。 |
| A6 | **`failed`** | 支出与范围不符合：live 启动 2 次（上限 1）、失败后未停止、已启动阶段借脚本修复重启且未先报告。仅检查有效阶段墙钟不足以判定。 |

`all_passed=false`；原 `results/acceptance-summary.json`（`all_passed=true`）**保持字节不变**，仅被本修正件标注为 `superseded_by_this_correction`。

## 五、本轮实际复核命令与退出码

工作目录 `/home/s223/m0nesy/Inference`；每条命令前 `export CONDA_EXE=/home/s223/miniconda3/bin/conda`。

| # | 命令 | 退出码 | 输出摘要 | 日志 |
| --- | --- | ---: | --- | --- |
| 0 | `./scripts/simai-python -c 'import sys;print(sys.version.split()[0])'` 及 `git rev-parse HEAD`、`git status --short --untracked-files=all` | 0 | 3.12.16 / `df0248a…` / 0 行 | `logs/r02-00-environment.log` |
| 1 | `./scripts/simai-python work/.../archive_audit.py` | **0** | 323/323 匹配，262 副本不变，0 不符 | `logs/r02-01-archive-audit.log` |
| 2 | `./scripts/simai-python work/.../interference_recompute.py` | **0** | 144 / 136 / −127 / 9 ns；ΔG 全 0；`layer3=not_measured` | `logs/r02-02-static-recompute.log` |
| 3 | `./scripts/simai-python work/.../acceptance_revision.py` | **3** | `gate=failed`、`violations=4`、`live_starts=2`、`stop_observed=false`、`a6=failed`、`total=19.047 min` | `logs/r02-03-acceptance-revision.log` |
| 4 | `./scripts/simai-python work/.../t3_tree_hash.py <after>` | **0** | 84 文件，树哈希与 before 相同 | `logs/r02-04-tree-hash-after.log` |
| 5 | `./scripts/simai-python work/.../record_static_review.py` | **0** | `all_passed=true`（审计+复算+树不变+退出码齐全） | `logs/r02-06-command-record.log` |
| 6 | `./work/.../run_static_recheck.sh`（串行驱动 #1–#5） | 0 | 各步退出码见 `logs/r02-05-exit-codes.txt` | 同上 |
| 7 | `./scripts/simai-python work/.../verify_round.py`（文档更新后的一致性校验，单独执行） | **0** | `problems=[]`、`all_ok=true`、`git_status_lines=0`、原目录 84 文件不变 | `logs/r02-07-verify.log` |

退出码约定：`0` = 完成且无流程偏离；`3` = **复核完成且记录到流程偏离**（本例 A6 不通过），**不是崩溃**。`logs/r02-03-acceptance-revision.log` 末尾的 `ERROR conda.cli.main_run ... failed` 是 `conda run` 对非零退出码的转述，内容即上面 `gate=failed` 的 JSON，不代表脚本异常。

## 六、修改文件清单

本轮**未修改**：原 T003 的 `TASK.md`、`REPORT.md`、`STATUS.json`、`FORWARD.txt`、`inputs/`、`logs/`、`results/` 全部文件；T001/T002 任一文件；正式源码、正式配置、原始数据、ns-3 后端；`coordination/PROGRESS.md`、`coordination/history/2026-11-07/ARCHIVE.json`。

新增或修改均在 `outputs/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/` 与 `work/agents/2026-11-08/T003-kv-ep-contention-attribution-r02/`：

| 文件 | 类型 | 本轮动作 |
| --- | --- | --- |
| `REPORT.md` | 报告 | 重写，补齐结论、偏离、复核命令/退出码、修改清单、待裁决事项 |
| `STATUS.json` | 状态 | 新增，`status=submitted`、`parent_task` 指向原 T003 |
| `SCOPE.md` | 范围记录 | 保持，补充 STATUS 落地说明 |
| `results/acceptance-summary.corrected.json` | 验收判定 | 重生成（schema v2），新增 `budget`、`evidence_location`、`documentation_findings` |
| `results/deviation-register.json` | 偏离登记 | 重生成（schema v2），新增启动标记列表、预算门与失败现场解析位置 |
| `results/archive-audit.json` | 归档审计 | 重生成（本轮重新执行） |
| `results/static-recomputation.json` | 静态复算 | 重生成（本轮重新执行） |
| `results/server-static-review-2026-11-08.json` | 复核记录 | 新增：环境、脚本哈希、逐条命令与退出码、审计/复算/验收/树哈希结果 |
| `results/t003-original-tree-before.json` / `-after.json` | 完整性 | 新增：原 T003 目录 84 文件逐文件哈希与树哈希 |
| `results/conclusion-correction.md` | 结论修正 | 补充证据位置与退出码说明 |
| `logs/r02-00…r02-07-*.log` | 原始日志 | 新增：本轮全部命令输出、退出码与终端校验结果 |
| `work/.../acceptance_revision.py` | 验收脚本 | 重写为 v2（启动次数由日志推导、停止条件、预算门、证据位置） |
| `work/.../legacy/acceptance_revision.v1.py` | 历史脚本 | 新增：v2 覆盖前的 v1 原样留存（`7965047b…`） |
| `work/.../archive_audit.py`、`interference_recompute.py` | 复算脚本 | 沿用，未改逻辑，本轮实际执行 |
| `work/.../record_static_review.py`、`run_static_recheck.sh`、`verify_round.py` | 编排/校验 | 新增 |
| `work/.../t3_tree_hash.py`、`inspect_archive.py`、`inspect_t3_tracking.py` | 只读辅助 | 新增（树哈希与归档条目核对） |

## 七、未执行与边界

- **未启动** live baseline、network replay 或任何长时间实验；未扩大样本、未改冻结条件、未改正式仿真机制、未重建后端。
- **未提交、未推送**；所有新增内容位于 `.gitignore` 覆盖的 `outputs/`、`work/`，`git status --short --untracked-files=all` 执行前后均为 0 行。
- **未修改** `coordination/PROGRESS.md`（仍 `e024cc69…`），未新建/改写 `REVIEW.md`，未自行设置 `accepted`。
- 本轮不产生任何新的实验结论；所有数值仍属 WSL 历史证据的服务器只读复算。

## 八、仍需指挥裁决

1. **可采性**：在第 1 次 live 已进入网络阶段（472621 ns、12 条 native 注册）后未停止、并在修复草稿后重启的情况下，第 2 次 live 与三类重放的数据是否可作为 T003 证据链采纳。
2. **A6 结论**：是否确认 A6 `failed`、整体 `all_passed=false`，并在研究记录中保留该流程偏离（而非仅保留负结果结论）。
3. **是否返工**：是否需要重新发派一个预算合规的 `-r03`（或新编号）任务，在服务器以单次 live + 三次 replay 重跑；本轮严格未自行补跑。
4. **结论收窄的落地**：是否接受「检出极弱网络互扰；冻结条件下指定请求边界暴露为零；端到端反事实性能变化未测」，以及是否因此在 `PROGRESS.md` 调整表述（该索引由指挥维护），并决定是否进入 KV 调度算法对照。
5. **引用勘误**：原 `REPORT.md` 对 `results/attempt1-failed/`、`flows.csv`、`stages.json`、`native_schedule.jsonl` 的路径引用需先解包历史包才可解析，是否需要出具勘误或补充说明。

## 九、审核建议入口

- 门禁与偏离：`results/acceptance-summary.corrected.json`、`results/deviation-register.json`
- 数值复算：`results/static-recomputation.json`（含 10 个源文件 SHA256）
- 归档与完整性：`results/archive-audit.json`、`results/t003-original-tree-before.json` / `-after.json`
- 本轮命令与退出码：`results/server-static-review-2026-11-08.json`、`logs/r02-0*.log`
- 结论修正与撤回清单：`results/conclusion-correction.md`
