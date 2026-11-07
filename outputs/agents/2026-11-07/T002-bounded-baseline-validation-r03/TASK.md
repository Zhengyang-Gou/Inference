# T002-r03：补齐三处时序边界审计（返工）

- 来源：用户/指挥本轮直接指派（无新编号，按返工使用 `-r03` 目录，`parent_task=T002-r02`）。
- 仓库：`/home/m0nesy/Postgraduate/Inference`
- 任务目录：`/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T002-bounded-baseline-validation-r03`
- 父任务书原文：`TASK-parent.md`（逐字复制自 r02 的 `TASK.md`，未改写）
- 父任务（r02）已 `submitted`，其 `results/run-a/`、`results/run-b/` 与所有结论**完整保留、不覆盖、不重跑**。

## 本轮任务原文（逐字保留）

> 完整验收还缺三处时序证据：
> - KV 数据就绪 → 发送；
> - 完整 Prefill 结束 → 首次 Decode 计算；
> - 61 层完成 → 对应 token 返回。
> 返工范围仅补边界审计，原运行结果保留。

## 本轮唯一问题

能否用只读方式给出上述三处逐事件时序证据，证明每个 KV 块在就绪后才发送、每个请求在完整 Prefill 结束后才进入首次 Decode 计算、每个 Decode token 在对应第 61 层完成后才返回？

## 范围与约束

- **仅补边界审计**：不修改正式源码、正式配置、原始输入、T001 冻结件；不改 r02 的任何文件；不改变配置、负载、绑定、seed、窗口、记录模式或任何开关。
- 因 r02 使用 `recording.mode="essential"`，边界事件（`prefill_chunk_layer_ready` 244、`transfer_started` 142212、`compute_step_*` 12749、`decode_batch_*` 205 等）在 `result/summary.json → recording.omitted_event_counts` 中被明确记录为「已产生但省略写盘」，`events.jsonl` 只有 10 类必要事件。因此逐事件时序**无法从原产物静态恢复**；本轮通过项目现有 `RuntimeHooks(event=...)` 在事件写盘前捕获**只读副本**，新增一次采集运行，输出到本任务独立目录。
- 该采集运行使用与 r02 完全相同的 `inputs/`（逐字节哈希核对）、同一 `run_baseline` 调用方式与 `EPFlowAudit`；hooks 只保存与三处边界相关的事件字段及计数，不修改算法、时钟、事件顺序或网络，不新增仿真事件或高频采样。
- 不重复两次可复现性运行（r02 已证明两次语义一致）；不开展算法对比、KV/EP 干扰归因、持续加压、全量轨迹、扩容、MLU 计时或完整 `check.sh`。
- 不提交、不推送、不启动其他 agent；完成后写 `REPORT.md` 并更新 `STATUS.json`，停止等待审核。

## 交付与验收

1. `results/audit-run/`：本次采集运行的完整、独立运行目录（由 `run_baseline` 自行创建，拒绝覆盖）。
2. `results/boundary-events.json`：hooks 捕获的边界事件时间线（只含必要字段）与全量事件计数。
3. `results/boundary-audit.json`：三处时序逐条配对结果、违规数、最小/最大间隔与统计口径；并核对采集运行与 r02 的语义一致性（token 时刻、请求状态、EP/KV 逻辑统计、最终资源）。
4. `logs/`：命令、退出码、原始输出、执行前后 Git 状态。
5. `REPORT.md`：按模板回答三处时序证据是否成立，明确本轮新增运行的必要性与边界。
