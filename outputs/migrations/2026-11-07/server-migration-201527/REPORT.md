# Inference 提交与远端执行迁移记录

- 用户直接要求：先提交 commit，不发布新任务；后续开发验证、仿真和实验默认在远端 Linux。
- 源仓库：Windows 读取的 \\wsl.localhost\Ubuntu\home\m0nesy\Postgraduate\Inference
- 目标仓库：/home/s223/m0nesy/Inference
- SSH：Windows PowerShell → ssh m0nesy（s223@121.48.163.223:22）
- 服务器归档：/home/s223/m0nesy/Inference/outputs/migrations/2026-11-07/server-migration-201527
- 结论：**提交、定向同步和服务器小规模验证完成；未推送 GitHub。**

## Commit 与同步

Commit：e9b447df7f118661b9b0e4ab2ce13483e7dd9b2a

标题：chore: finalize ns3-only baseline and move execution to Linux server

43 个文件，288 行新增、3367 行删除。包含此前工作区已有的 flowSim 移除/配置与入口收口，以及任务协作模板、审核流程和本次远端执行说明。已有删除与代码修改在提交前逐项检查并保留，不将其冒称为本次新实现。

AGENTS.md、coordination/README.md、TASK/REPORT 模板及新增 coordination/REMOTE_EXECUTION.md 已写入远端仓库路径、Windows SSH 入口、Conda 路径和服务器证据口径。后续编号续接既有任务；本次未创建或发布新任务书。

服务器开始前工作区干净，HEAD=91cf1e2a16b8ff59b595619a7d0c02c4dd5df8dc；已执行 git fetch origin 和 git pull --ff-only，结果为已是最新。随后使用只含本次提交的 Git bundle fetch，并 merge --ff-only，使两端 main 持有同一 commit；没有覆盖未提交修改，没有推送 GitHub。

提交保留 Git 索引中的 Linux 可执行位，未夹带 Windows/UNC 显示的 mode-only 差异。本地未提交内容差异已归零；Windows Git 仍能显示的权限位差异原地保留，未 chmod/reset 清理。服务器工作区干净。

## 定向迁移输入

只迁移 T001 的五个冻结输入文件及新的 TRANSFER.json，目标：

/home/s223/m0nesy/Inference/artifacts/inputs/T001-baseline-freeze/

- config.json：565572ab4d2dde71bd265bfc8a240f7b433bf5987886505c231ea9a8f049a8f1
- compute_profile.json：374595adf46d7b65bf34eb47e693b41a06dddb9fb1890f093afb013f66f93554
- workload.json：0ce0c8cdc4b31d07587014debf7272209d4bd6efb23f03b84ff17fce3282c346
- binding_plan.json：9a48e35f53a4e8df2c9c09312a1fbf402bf8e79c2d47f083ba12e9416d37b091
- manifest.json：811cc29616c0e1539a43109ae78d5bce14f23fde6b97a7e157c63ef956b0af78

传输前后逐文件 SHA256 均匹配。目标原先不存在，未覆盖服务器文件。旧manifest保持原字节，其中的WSL路径仅作来源记录；TRANSFER.json 明确标为 frozen_inputs_only_not_server_run_evidence。

输入压缩包 SHA256：9d83ab392d2b112a45153f76da4a98171f31cd1ff647f7df52fb7ccfba8172a5
代码 bundle SHA256：f8af5d9df1799a1d403e51567f0ee79398548b46f221b6c6e6bd8280c0081bf4

未迁移本地 Conda、构建目录/二进制、缓存、旧实验运行产物或 work 草稿。旧任务和 T003 原始结果仍留在原位置，没有复制或重新标记为服务器结果。

## 服务器本轮验证

执行主机 s223，Python 3.12.16，Conda /home/s223/miniconda3，环境 simai。命令由 Windows PowerShell SSH 发起；验证脚本与原始输出均在上述服务器归档内。

| 检查 | 结果 | 原始证据 |
| --- | --- | --- |
| 项目 Python / pip check | 通过 | logs/01-python.log、02-dependencies.log |
| backend doctor | backend_ready=true，服务器 fingerprint=f1a48f36b6a4ebb9cf8c | logs/03-backend-doctor.log |
| baseline_smoke.json 配置 validate | 通过 | logs/04-smoke-config.log |
| 迁移的冻结 config validate | 通过；仅静态检查，未启动全量轨迹 | logs/05-frozen-config.log |
| module_boundaries / rpc_batching / vendored_source 定向测试 | **18 测试通过** | logs/06-targeted-tests.log |
| 新运行 baseline_smoke | **12/12完成、12/12 SLO达标、失败0、未完成0，最终资源归零** | logs/07-smoke.log、results/smoke/ |
| 最终 Git 状态 | 干净，HEAD 与本次 commit 一致 | logs/final-status.txt、validation-summary.json |

本轮 smoke 为 CPU 离散事件功能验证，不是实际 GPU 推理性能。服务器后端为本机已有构建，经 doctor 实际检查通过；未复制或重建 WSL 后端。

全部检查与 smoke 均由服务器本轮执行；未将用户先前的验证或本地实验作为本轮通过依据。实际 argv、退出码和墙钟见 commands.json；总汇总见 validation-summary.json。

## 未完成及后续边界

- 未执行完整回归、61层研究负载实验、全量长实验或GPU性能测试。
- T003 仍为原本地 submitted、等待指挥审核；本次没有进行 T003 研究结论验收，也没有把其结论作为服务器实验结果。
- 后续正式任务和实际命令默认使用服务器目录，依据服务器新运行的输入、源码和后端身份；不继续依赖 WSL 环境与旧实验数值。
- 只提交本次明确要求的 commit；未推送 GitHub。服务器 main 因此比 origin/main 前进一个提交，后续更新前仍须先检查状态并按需快进。
