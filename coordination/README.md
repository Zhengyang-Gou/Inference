# 指挥与执行工作流

仓库根目录的 `AGENTS.md` 是共同约定。指挥 agent 给任务，用户转发给执行 agent；执行 agent 将证据归档，指挥 agent 读取后给出审核结论和下一轮任务。

## 一轮任务

1. 指挥 agent 使用 `templates/TASK.md` 明确目标、任务编号、可修改范围、验收条件、前置报告和本轮结果目录。
2. 用户把任务提示词转发给执行 agent。执行 agent 先读 `AGENTS.md`，建立任务目录并保存原始任务。
3. 执行 agent 完成任务，使用 `templates/REPORT.md` 写报告，更新状态为 `submitted`（或如实记录阻塞、失败）。
4. 用户只需把报告路径发回指挥 agent，例如：“请审核 `/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-04/T001-example/REPORT.md`。”
5. 指挥 agent 读取报告、日志和相关代码，按需写 `REVIEW.md` 并更新状态。审核通过后再安排新任务；返工用关联的新目录，保留前一轮证据。

日期目录使用任务开始时的上海日期。任务编号由指挥 agent 分配；每次给提示词时填写实际日期和准确路径，不让用户手工拼接。首次建立本约定的归档为 `outputs/agents/2026-11-04/T000-collaboration-setup/`。

## 通用转发提示词

下面是模板。指挥 agent 在转发前补齐所有尖括号字段，并附上完整任务内容；没有确定任务时不让执行 agent自行找任务修改。

```text
你是 Inference 仓库本轮任务的执行 agent。

工作仓库：/home/m0nesy/Postgraduate/Inference
请先完整阅读 /home/m0nesy/Postgraduate/Inference/AGENTS.md。

任务编号：<T001>
任务名称：<名称>
前置报告：<绝对路径；无则写“无”>
结果目录：/home/m0nesy/Postgraduate/Inference/outputs/agents/<YYYY-MM-DD>/<T001-short-name>/

请先建立结果目录，按约定保存 TASK.md、STATUS.json 和执行前工作区状态。
只完成下述任务，保留已有修改。所有验证日志和本轮产物写入结果目录。
完成或阻塞时均须写 REPORT.md；不要自动开展下一项建议。
最后回复 REPORT.md 的绝对路径、结论和验证情况，供指挥 agent 审核。

<按 templates/TASK.md 补齐的本轮目标、范围、约束和验收条件>
```

## 指挥 agent 如何查阅

优先打开用户回传或上轮提示词指定的 `REPORT.md`，再查看 `STATUS.json`、`logs/`、`results/` 和实际修改。任务产物在忽略目录下，默认文件搜索可能不显示它们；可用：

```bash
rg --files --hidden --no-ignore outputs/agents -g REPORT.md -g STATUS.json -g REVIEW.md
```

`REVIEW.md` 建议包含：审核结论（通过/需返工/待补证据）、检查了哪些代码或结果、发现的问题、下一轮任务及其前置条件。报告中的下一步建议是输入材料，不自动成为已授权的新任务。
