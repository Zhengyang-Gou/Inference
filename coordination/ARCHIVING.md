# 任务记录的版本归档

用户已明确要求将任务进度与结果纳入Git，避免换机器或换agent后失去上下文。记录保留原任务目录，不在其他位置复制第二份有效TASK。

## 纳入Git的内容

每轮至少保留TASK、STATUS、REPORT、已有REVIEW/转发文本、输入与源码/后端身份清单、关键结果汇总、必要请求CSV、检查/分析/复现脚本。必要冻结输入也可以提交；较大而重复的输入优先引用已有跟踪文件及哈希。

本次2026-11-07历史归档还保留有界边界审计JSON、选定live导出、源码manifest和紧凑原始日志，具体文件由 ARCHIVE.json 的 tracked_snapshot 字段给出。原状态、失败、blocked和返工记录不删改，submitted不等于accepted。

开发、验证和实验按AGENTS在服务器执行。归档时区分WSL历史研究、服务器当前验证；不得因为移动文件或提交Git而提升证据等级。

## 大体积原始产物

逐流/逐端口明细、native日历和源码压缩包等完整保存在服务器历史/任务存储中，Git内记录实际位置、字节数及SHA256。提供逐文件清单，使审核人可核对或解包原证据。不得只留报告却丢失原始产物，也不得把生成的全部outputs、Conda或构建产物整体加入Git。

历史完整包可按仓库相对路径解包到独立目录做只读审计；不要覆盖正在运行的任务。新实验必须另建输出目录、使用服务器本轮环境，不复用旧完成结果冒充运行。

## Git忽略规则

outputs/、work/仍默认忽略，保护大型生成物。已经明确跟踪的文件后续修改会正常显示；新增任务的版本记录须逐文件显式加入，例如：

```bash
git add -f -- outputs/agents/YYYY-MM-DD/Tnnn-name/TASK.md outputs/agents/YYYY-MM-DD/Tnnn-name/STATUS.json
```

按实际任务列出REPORT、REVIEW、输入清单、关键汇总和脚本的精确路径；不得用 git add -f outputs 或 git add -f work 全量放行。提交前核对暂存文件清单、体积、JSON/状态与证据身份。

归档是交付要求；commit/push仍须有用户当前授权，未授权时明确列出尚未提交的记录，不自动提交或推送。服务器目前缺少本仓库的GitHub推送认证，可在明确授权下将服务器commit以bundle带回Windows，使用已有Windows凭据推送；不复制其他账号的密钥。

## 进度索引

coordination/PROGRESS.md 由指挥独占维护，负责串联状态与链接。执行agent只更新自己的任务目录并报告状态，不共写总索引或latest文件。新任务编号续接既有序列，迁移服务器不重置编号。审核决定必须有REVIEW，执行摘要中的passed不能替代accepted。
