# 研究系统整理：冻结与统一入口

2026-11-02 完成整理计划的第 1、2 项：冻结当前可运行状态，确定唯一的 EP/KV 研究入口。沿用已有仿真执行器与 EP 事件语义。绑定预测、联合选择、前缀缓存的实现与测试全部保留，在新的主研究配置中关闭。

本文件保留第 1、2 项的历史身份。随后第 3、4、5 项已在此基线上进行运行时组件迁移、正式诊断入口和文档/测试整理；当前结构与复现方法见 [研究系统说明](RESEARCH_SYSTEM.md)，原快照保持不变。

## 整理前快照

快照位于本机：

```text
/home/m0nesy/Documents/Codex/2026-11-01/home-m0nesy-postgraduate-inference/outputs/research-system-stage12/frozen-before/
```

它包含全部已跟踪及未忽略的未跟踪源文件、Git 历史 bundle、暂存区/工作区差异、文件哈希清单，以及选定的历史结果、公开计算 trace、输入与当前网络后端。构建缓存和其他被忽略的输出没有全部复制；具体范围在 `source_manifest.json` / `archive_manifest.json` 中列明。原仓库未提交、未重置。

| 身份 | 值 |
| --- | --- |
| Git HEAD | `9dbe9b415b103f79bc7450d100fda77b17d3a066`，工作区修改另存于源码归档 |
| 源文件 | 1833 个，`working-tree.tar.gz`；包含当时未提交和未跟踪内容 |
| 源清单摘要 | `8d077e2a1074d15234d0d2778a0e003ee6b22f9874737cbfac9346f7d4777742` |
| 网络后端 | fingerprint `4c65a81002d1b4ccbb2f`，protocol 3 |
| 后端二进制 SHA-256 | `f31f2dfef2e2328daa2ed3622697bb7c330d23b18e872a90b1a914abd6c09caa` |
| MoE profile SHA-256 | `e108d6a2e50f4e623c4893ac776df70a06b021c7c56a989bf13d434723e0008d`，历史冻结的估算仿真参数 |

147 个归档文件已逐一校验。另在全新临时目录实际执行了 bundle 克隆、源码恢复和检查：1833 个文件的内容及权限一致，恢复后的 Git 工作区状态也与冻结时一致；报告为快照上一级的 `restore_verification.json`。

恢复时使用**新目录**，不要覆盖正在使用的仓库：

```bash
task_snapshot=/home/m0nesy/Documents/Codex/2026-11-01/home-m0nesy-postgraduate-inference/outputs/research-system-stage12/frozen-before
git clone "$task_snapshot/history.bundle" /tmp/Inference-frozen-20261102
git -C /tmp/Inference-frozen-20261102 checkout --detach 9dbe9b415b103f79bc7450d100fda77b17d3a066
tar -xzf "$task_snapshot/working-tree.tar.gz" -C /tmp/Inference-frozen-20261102
if [ -s "$task_snapshot/staged.patch" ]; then
    git -C /tmp/Inference-frozen-20261102 apply --cached "$task_snapshot/staged.patch"
fi
```

此次快照没有缺失/删除的源路径。归档的二进制适用于当前环境，迁移机器后按 [后端说明](backend/README.md) 重新构建；构建 fingerprint 包含路径和工具链信息，异地构建不要求 fingerprint 字符串相同。

## 行为保持核对

整理前已重新运行 201 项测试及 12 请求ns-3 仿真后端 smoke，全部通过。整理前后分别执行以下配置，29 个确定性产物逐字节一致（token 时间、请求结果、输入、最终资源及批处理/EP 统计）；墙钟诊断与输出目录路径不参与逐字节比较。

| 场景 | 配置 | 请求 / token |
| --- | --- | --- |
| Dense 组合 | `configs/composed_execution_smoke.json` | 4 / 12 |
| 小型 MoE | `configs/ep_mlu_smoke.json` | 6 / 24 |
| Azure 首请求 | 冻结的 `references/moe-profile/azure_first_request_smoke_config.json` | 1 / 44 |

对比文件为 `references-before/`、`references-after/` 和 `reference_equivalence.json`，均在快照上一级目录。历史四请求竞争诊断也原样保存在 `frozen-before/references/azure-competition/`，保留其网络回放验证与解释边界，没有将其改写成全量实验结果。

整理后的完整检查通过：209 项测试（20 评估、115 后端、30 绑定、14 负载、6 观测、24 实验）及 12 请求ns-3 仿真后端 smoke。新增的 8 项实验测试覆盖命名策略、隐藏变量拒绝、固定开关、输入身份及冻结时变化、优先级消融、公共窗口与ns-3 仿真后端三策略执行。原有 201 项全部保留，日志为 `post-change-check.log`。

## 主研究入口

[research/README.md](configs/research/README.md) 规定输入、模型、拓扑、并行、批量、包大小、SLO 和统计窗口。主策略对照为 Immediate / FIFO / MLU，原生网络优先级共同关闭；其效果由另一套仅改变优先级的消融衡量。两套完整 Azure 实验通过预检；本次执行的短程三策略验证仅包含 Azure 前 1 秒的 1 个请求。

变化集中于实验规划/预检、配置、数据归档与说明，没有修改运行时和原生网络计算逻辑。后续若调整执行语义或替换 profile，应建立新的基线身份，不能沿用本次参考结果证明性能。

## 第 3、4、5 项的迁移核对

运行时从四算法混入的 `_Baseline` 拆成按职责组合的 `ExecutionCoordinator`，保留 `run_baseline`、`backend.runtime` 导入和旧私有名称的兼容端口。请求输入与分区进度分离，组件字段只有一个登记所有者；绑定、EP 与合批算法保留。

Dense、MoE、Azure 首请求的 29 个确定性产物与冻结基线逐字节一致。原四请求诊断也通过正式 `run-experiments` 入口重新执行：627,620 条跨节点流，both 重放 ACK 误差 0 ns；流表、EP stage、token、请求、最终资源和干扰统计均与原结果一致。profile、原生网络源码/后端和 EP 执行语义保持原版本。

该轮的源码快照、原始日志和验收材料位于本机 `outputs/research-system-stage345/`（与上文 stage12 同一父目录）。完整 Azure 19,366 请求矩阵仍未执行，短程复现只用于迁移验收。
