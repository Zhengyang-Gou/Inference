# Agent 协作约定

用户本轮明确要求优先于本文件；子目录有 AGENTS.md 时一并阅读。仓库根目录为 `/home/s223/m0nesy/Inference`。

## 默认执行位置

- 开发验证、测试、仿真和实验默认在远端 Linux 仓库 `/home/s223/m0nesy/Inference` 执行，分支为 `main`。本地 WSL 不再作为执行环境。
- 从本机 Windows PowerShell 使用 `ssh m0nesy '远端命令'`。别名 m0nesy 对应 `s223@121.48.163.223:22`；不要从 WSL 假定可用同一 SSH 配置/密钥。
- 非交互命令显式指定 Conda：`cd /home/s223/m0nesy/Inference && export CONDA_EXE=/home/s223/miniconda3/bin/conda && ./inference backend doctor`。环境为 simai / Python 3.12.16；已在服务器内的 agent 直接执行命令即可。
- 每轮先读服务器 AGENTS.md 并检查 git status。更新代码前保留已有修改，按任务需要使用 `git pull --ff-only`；不自动 stash/reset/覆盖文件，不自动提交或推送。
- 本地未提交代码、配置和必要输入须先列清单，再定向同步并核对哈希；不复制 WSL Conda、缓存、编译产物或旧实验结果。冻结输入可以按来源和哈希迁移，不能据此宣称已在服务器验证。
- 新任务、草稿、命令日志和结果归档都以服务器目录为准。本地 WSL 的历史验收/实验只作历史记录，不能替代服务器本轮实际验证。
- 未经明确任务授权不启动长时间全量实验；先运行必要的小规模检查。操作说明见 coordination/REMOTE_EXECUTION.md。

## 角色与任务边界

- 指挥 agent：澄清研究目标，分配编号，编写任务书，审核证据，决定下一轮；指挥角色不自动变成执行角色。
- 执行 agent：只完成指定 TASK.md，保存原始证据并提交 REPORT.md，不自行扩展目标、派发任务或启动其他 agent。
- 用户负责将指挥 agent 给出的任务路径转发给执行 agent，再将报告路径交回审核。
- 默认串行推进；上一项未审核通过，不派发下一项。并行必须由指挥 agent 明确编号、文件边界和依赖。
- 只读答疑、概念讲解和尚未确定范围的讨论不创建任务目录。只有正式实现、实验、验收和审查进入任务流程。
- 每项任务只回答一个主要问题。新增机制、扩大规模和调整研究假设须成为明确任务，不自动从讨论或报告建议转成执行工作。

## 唯一任务目录

正式任务从 T001 开始顺序编号。日期使用 Asia/Shanghai 的任务开始日期；跨午夜不换目录。

```text
outputs/agents/YYYY-MM-DD/T001-short-name/
├── TASK.md          # 指挥编写；唯一有效需求
├── STATUS.json      # 指挥初始化，执行与审核阶段按职责更新
├── REPORT.md        # 执行提交；必须有证据
├── REVIEW.md        # 指挥审核；必须给出结论
├── logs/            # 原始命令、退出码、工作区状态和错误
└── results/         # 输入快照、运行结果、分析和图表
```

- 指挥先建立目录并填写 TASK.md、STATUS.json；派发前状态为 ready。只建 logs/、results/ 父目录，不预建仿真工具将创建的运行子目录。
- 需求、状态、执行报告和审核意见放在同一目录。不再在 coordination/tasks 或其他目录维护同一任务的第二份需求。
- 转发时只提供唯一 TASK.md 的绝对路径和角色说明，不要求用户拼接路径或补占位字段。
- 执行 agent 保留原始 TASK.md；遇到范围冲突或需扩大目标时写入报告，交指挥处理，不自行改写验收条件。
- 临时草稿放 `work/agents/YYYY-MM-DD/T001-short-name/`。正式源码、配置和输入按任务边界写入所属模块；后端构建继续用 `joint_scheduling/artifacts/backend/`。
- 不覆盖已有任务、日志或结果。返工使用新编号或 -r02 目录，并在 parent_task 记录来源。
- 不维护多个 agent 共写的 latest 文件或全局结果汇总。coordination/PROGRESS.md 由指挥独占维护，执行 agent 不共写总索引。
- 任务书、状态、报告、审核意见、必要输入/身份清单、关键结果与分析脚本须按 coordination/ARCHIVING.md 纳入版本记录。outputs/work 默认忽略，新增记录需逐文件显式跟踪；不把整棵生成目录加入 Git。用户未授权提交时说明未提交清单，不自动 commit/push。大体积原始证据另存服务器并记录位置与 SHA256。

## 状态与所有权

STATUS.json 使用 coordination/templates/STATUS.json。指挥创建时填 created_at、updated_at；执行开始时填 agent、started_at。时间统一为带 +08:00 的 ISO 8601。

| 状态 | 含义 | 谁更新 |
| --- | --- | --- |
| ready | 需求完整，等待执行 | 指挥 |
| in_progress | 正在执行 | 执行 |
| submitted | 报告已提交，等待审核 | 执行 |
| blocked | 缺少执行所必需的条件 | 执行 |
| failed | 本轮执行失败 | 执行 |
| accepted | 验收通过，REVIEW.md 已写 | 指挥 |
| changes_requested | 需要补证据或返工，REVIEW.md 已写 | 指挥 |

执行 agent 不自行设置 accepted。失败、阻塞也须写 REPORT.md，保留已完成工作与恢复条件。

## 执行前与验证

1. 阅读 AGENTS.md、唯一 TASK.md，以及任务明确列出的前置报告和相关模块文档。旧报告不代替本轮证据。
2. 保存执行前 `git status --short --untracked-files=all` 到 logs/baseline-status.txt；保留他人的未提交、未跟踪源码、配置和输入，不 reset/checkout/clean 他人修改。
3. 将状态改为 in_progress。核对目标、允许修改范围、冻结条件、实验规模和验收标准；一般实现细节自行合理决定并记录。
4. 使用 `./inference`、`./scripts/simai-python` 进入项目环境。先做直接相关验证；任务要求完整回归时执行 `./joint_scheduling/check.sh`。
5. 记录实际命令、运行目录、退出码、输入与配置身份。未运行、失败、超时和跳过如实标记；预检通过不能写成仿真成功，小样例成功不能写成性能收益。
6. 定量结论指向原始 JSON/CSV/日志；保留工具生成的配置/profile/workload/源码/后端身份与复现文件，不拆散运行目录。
7. 当前研究条件以 `joint_scheduling/configs/research/README.md`、实际配置和 profile 为准。明确区分真实请求来源、估算计算、合成路由和人工 SLO。
8. 仓库在 CPU 上做离散事件仿真。调度决策的真实 CPU 耗时、配置的仿真延迟和模拟请求时延分别报告。
9. 未经任务明确指派，不启动长时间全量矩阵，不引入新后端或新通信机制，不提交、推送或清理已有产物。用户明确要求清理时，以其给定范围为准。

## 提交与审核

- 执行 agent 按 REPORT.md 模板逐项回答验收条件，区分自己的修改与执行前已有修改，更新 STATUS.json 为 submitted/blocked/failed，然后停止。
- 最后回复任务目录、REPORT.md 的绝对路径、一句话结论、实际验证结果与需决定事项。
- 指挥读取报告、原始结果及必要代码，按 REVIEW.md 模板写审核意见，再设置 accepted 或 changes_requested。
- 只有审核通过后才规划下一项。报告中的下一步建议不是新授权，不能自动继续。

流程与模板见 coordination/README.md，研究当前位置见 coordination/PROGRESS.md。迁移或归档不重置编号，不自动发布任务或恢复已撤销的任务。