# T002-r03：补齐三处时序边界审计 — 执行报告

- 执行 agent：WorkBuddy
- 开始/结束：2026-11-07T18:54:24+08:00 / 2026-11-07T19:03:04+08:00
- 状态：submitted
- 任务目录：`/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T002-bounded-baseline-validation-r03`
- 前置任务：T002-r02（`submitted`，其 `results/run-a/`、`results/run-b/` 与全部结论**完整保留、未改动**）；父任务书原文 `TASK-parent.md`

## 结论

指挥指出的三处时序证据**全部补齐，违规数均为 0**：

1. **KV 数据就绪 → 发送**：244 个 chunk-layer 就绪事件与 244 条 KV 逻辑传输一一配对，发送时刻与 `block.ready_at_seconds` **同刻**（差值恒为 0），无提前发送；逻辑 payload 合计 115878528 字节（=1649×70272）。
2. **完整 Prefill 结束 → 首次 Decode 计算**：3 个请求均满足 `last prefill_chunk_completed ≤ kv_all_received ≤ first decode_batch_started`，间隔 12.671–17.424 μs（均值 14.310 μs）。
3. **61 层完成 → 对应 token 返回**：205 个 `compute_step_completed(phase=decode, layer_id=60)` 与 205 个 `token_returned(token_index≥2)` 逐请求按时间配对（43/108/54），层完成与 token 返回**同刻**（差值恒为 0），decoded token 不在第 61 层完成前返回。

采集运行与 r02 `run-a` 的 9 个语义字段（token 时刻、请求状态、completion window、latencies、EP 汇总、逻辑通信字节、最终资源、绑定计划、hooks 事件计数）**完全一致**，证明本轮只多保留了事件字段、没有改变任何行为或结论。原运行结果完整保留，不覆盖、不重跑、不修改。

## 逐项验收

| 编号 | passed / failed / not_run | 原始证据路径与解释 |
| --- | --- | --- |
| B1 KV 就绪 → 发送 | passed | `results/boundary-audit.json → boundaries.kv_ready_to_send`：244 个 `prefill_chunk_layer_ready` 对 244 条 kv `transfer_started`（按 `request_id + layer_id` 分组、组内按时间排序配对），`send − ready = 0.0`（min/max/mean），`violations=0`；`ready_at_seconds` 字段缺失 0 条；逻辑 payload 115878528 字节与预期一致。明细时间线在 `results/boundary-events.json`。 |
| B2 完整 Prefill 结束 → 首次 Decode | passed | `results/boundary-audit.json → boundaries.full_prefill_end_to_first_decode`：3 行逐请求证据，最后一个 `prefill_chunk_completed`（879-token 请求为 chunk_index=1）≤ `kv_all_received` ≤ 首个含该请求的 `decode_batch_started`，`violations=0`；间隔 12.671–17.424 μs。 |
| B3 61 层完成 → token 返回 | passed | `results/boundary-audit.json → boundaries.sixty_one_layers_to_token`：205 对，按请求 43/108/54（恰为各请求 `output_tokens−1`），`token − layer60 = 0.0`（min/max/mean），`violations=0`；decode 计算步总数 12505 = 205×61，逐层 ID 0–60 各 205 条。 |
| D1 独立采集运行目录 | passed | `results/audit-run/`（27 个文件，由 `run_baseline` 以 `exist_ok=False` 创建），含事件、CSV、后端身份、`reproducibility.json` 与源码归档，未拆散。 |
| D2 边界事件时间线 | passed | `results/boundary-events.json`：13844 行只含白名单字段的 hooks 事件副本 + 全量事件计数 814151、`EPFlowAudit=passed`、输入 SHA256、墙钟 282.634 s。 |
| D3 审计结果与等价性 | passed | `results/boundary-audit.json`：三处配对、违规数、间隔统计，以及 `equivalence_with_preserved_r02_run_a.fields` 的 9 项全部 `true`。 |
| D4 命令与原始证据 | passed | `logs/01-capture.log`、`logs/02-analyze.log`、`logs/commands.md`、`logs/baseline-status.txt`、`logs/final-status.txt`。 |
| D5 原运行结果保留 | passed | r02 `run-a`/`run-b` 各 27 个文件未变；`REPORT.md` SHA256 `6409a54f839ed25be4831ad6287f1591f0ef45a0f8233023b7f481f2dcab87f7`、`acceptance-summary.json` SHA256 `12d28faef8c8b2b2f373440a9a9b3f2d6e358429ff1403e4889610f1e25f6f2d`；本轮对 r02 只有只读访问。执行前后 `git status` 均为 71 行且逐行一致。 |

## 实际修改

| 文件 | 本轮修改及目的 |
| --- | --- |
| `TASK.md` | 保存本轮用户原文与范围说明；`TASK-parent.md` 逐字保留父任务书。 |
| `STATUS.json` | 新建，`in_progress → submitted`，`parent_task="T002-r02"`。 |
| `inputs/*.json` | 从 r02 `inputs/` 逐字节复制并核对哈希（5 个文件全部 `OK`），内容零修改。 |
| `results/audit-run/` | 一次只读采集运行的完整独立目录（未覆盖任何已有目录）。 |
| `results/boundary-events.json`、`results/boundary-audit.json` | 边界事件时间线与三处时序配对结果。 |
| `logs/*` | 执行前/后 Git 状态、Git HEAD、两次命令原始输出、命令与退出码记录。 |
| `work/agents/2026-11-07/T002-bounded-baseline-validation-r03/{capture_boundary.py,analyze_boundary.py}` | 本轮草稿脚本，不属正式源码。 |

**未修改**：正式源码、正式配置、原始输入、T001 冻结件、r02 的任何文件（含 `REPORT.md`、`STATUS.json`、`results/run-a/`、`results/run-b/`）、ns-3 后端源码/二进制。执行前 71 条脏状态全部保留；未提交、未推送、未启动其他 agent。

## 验证证据

| 实际命令 | 运行目录 | 退出码/结果 | 日志或结果路径 |
| --- | --- | --- | --- |
| `git -c safe.directory='*' status --short --untracked-files=all > logs/baseline-status.txt` | 仓库根 | 0，71 行 | `logs/baseline-status.txt` |
| `git -c safe.directory='*' rev-parse HEAD > logs/git-head.txt` | 仓库根 | 0，`91cf1e2a16b8…` | `logs/git-head.txt` |
| 复制 r02 `inputs/*.json` 并逐个 SHA256 比对 | 仓库根 | 0，5 个 `OK` | `inputs/` |
| `timeout 900 env CONDA_EXE=... PYTHONPATH=. ./scripts/simai-python work/.../capture_boundary.py` | 仓库根（WSL） | **0**，282.634 s，`EPFlowAudit=passed`，814151 hooks 事件，13844 行边界记录 | `logs/01-capture.log` |
| `env CONDA_EXE=... PYTHONPATH=. ./scripts/simai-python work/.../analyze_boundary.py` | 仓库根（WSL） | **0**，244/3/205 对全部通过、违规 0、9 个语义字段一致 | `logs/02-analyze.log` |
| 只读核对 r02 文件数与哈希 | 仓库根 | 0，未改动 | 见 D5 |
| 最终 `git status ...` + `diff baseline-status.txt final-status.txt` | 仓库根 | 0，`IDENTICAL` | `logs/final-status.txt` |

## 结果与解释

- **证据类型**：一次真实 ns-3 端到端运行中的**只读 hooks 事件副本** + essential 生命周期事件；不是算法对比、不是性能实验。
- **为什么必须新增一次采集运行**：r02 的 `recording.mode="essential"` 把三处边界事件全部标为「已产生但省略写盘」（`result/summary.json → recording.omitted_event_counts`：`prefill_chunk_layer_ready` 244、`transfer_started` 142212、`compute_step_completed` 12749、`decode_batch_started/completed` 各 205 等）；`events.jsonl` 只有 10 类必要事件、`instances.csv` 只有聚合值。逐事件时序无法从原产物静态恢复，只能通过项目现有 `RuntimeHooks` 在写盘前捕获副本。本轮**没有改变记录模式或任何冻结条件**，只是 hooks 回调多保留了字段。
- **输入与配置身份**：`inputs/` 与 r02 逐字节一致（config `c4db2484…`、workload `625bf0a5…`、profile `374595ad…`、binding_plan `461ba02d…`）；同一 `run_baseline` 调用、`EPFlowAudit`、seed 7、窗口 `[0,30)`、`time_limit=30`、后端 `simai_ns3_rdma` / fingerprint `23ec96146135d0706e39`。Git HEAD `91cf1e2a16b8ff59b595619a7d0c02c4dd5df8dc`。
- **样本与窗口**：3 请求、1649 输入 / 208 输出 token；墙钟 282.634 s（仅仿真运行成本，不是 GPU 计算时间或控制器决策延迟）。
- **数值依据**（`results/boundary-audit.json`）：B1 244 对、`send − ready = 0.0 s`、payload 115878528 字节；B2 3 对、`decode − prefill_done` 最小 12.671 μs / 最大 17.424 μs / 均值 14.310 μs；B3 205 对、`token − layer60 = 0.0 s`，逐请求 43/108/54，decode 计算步 12505。所有配对的 `violations=0`。
- **等价性**：与 r02 `run-a` 的 token 时刻、arrival cohort、completion window、latencies、EP 汇总、逻辑通信字节、最终资源、绑定计划、hooks 事件计数 9 项全部相同，说明采集运行与原运行语义一致，可直接作为 r02 验收的补充证据链。
- **结论边界**：本轮只补齐三处时序证据，不改变 r02 的 A1–A6 结论与数值；仍是 3 请求、单成员批次的功能样本，未覆盖 batch>1、满载、持续负载、KV/EP 竞争强度或算法收益。计算 profile 仍是估算、专家路由仍是合成、SLO 仍是人工假设。

## 风险、未完成项与阻塞

1. 无阻塞项：采集运行退出码 0、`EPFlowAudit=passed`、三处配对零违规；未超时、未修改输入身份。
2. **新增一次端到端采集运行**是本轮为补齐证据所必需（原因见上），输出到全新目录，r02 的两次运行与结论完全不动；其运行预算属于本轮返工任务，未触碰 r02 的 run-a/run-b。若指挥认为不应新增运行，可直接删除本轮 `results/audit-run/`，其余证据仍可说明原产物缺少边界事件。
3. 边界事件来自 hooks 只读副本，因 `essential` 模式仍不会写入 `events.jsonl`；审核时应同时看 `boundary-events.json`（原始时间线）与 `boundary-audit.json`（配对结论）。
4. `token_returned` 由 `TraceRecorder` 直接写盘、不经过 hooks，因此 B3 的 token 时刻取自 `audit-run/events.jsonl`，层完成时刻取自 hooks；两者时间单位已统一为秒（hooks 的 `now_ns / 1e9`）。
5. 未运行第二次采集（r02 已证明两次语义一致）、未开展算法对比/归因/扩容/全量轨迹/完整 `check.sh`，未提交或推送。

## 下一步建议

1. 指挥在审核 r02 时把 `T002-r03/results/boundary-audit.json` 与 `boundary-events.json` 作为 A3 时序证据链一并查阅；r02 的报告与状态按「原结果保留」保持不动。
2. 若需要把三处证据也写进正式验收件，另发任务明确是否允许更新 r02 的 `acceptance-summary.json`（本轮未改）。
3. 下一阶段若要进入策略对照或竞争归因，另发任务明确范围与样本规模；本轮不自动启动。

## 需要指挥决定

1. 是否接受本轮三处时序证据作为 T002 完整验收的补充，并据此结束 T002 系列。
2. r02 的 `STATUS.json` / `REPORT.md` / `acceptance-summary.json` 是否保持原样（本轮未改动），还是需要一份合并后的验收汇总。
3. 其余无。执行完成不等于审核通过，等待指挥写 `REVIEW.md`；不自行开始下一轮。
