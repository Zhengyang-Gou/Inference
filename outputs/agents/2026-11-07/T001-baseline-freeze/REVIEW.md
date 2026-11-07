# T001：冻结第一阶段唯一基线条件 — 审核意见

- 审核 agent：Codex（指挥 agent）
- 审核时间：2026-11-07T16:37:56.5974355+08:00
- 任务目录：`/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T001-baseline-freeze`
- 执行报告：`/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T001-baseline-freeze/REPORT.md`
- 结论：**accepted**

## 核心判断

T001已回答唯一问题：第一阶段后续基础验收与算法对照的配置、原始请求来源、计算profile、静态P/D绑定、SLO和统计口径有了唯一冻结引用。确认本任务的 `results/frozen/` 为后续任务的条件来源；不复制另一套含义相同的正式配置。

审核通过只代表“基线条件冻结与静态预检通过”。没有端到端仿真、持续负载、KV/EP竞争强度或算法收益结论，也不授权直接运行完整19366请求包级实验。

## 检查记录

| 验收项 | 已核对的代码与原始结果 | 判断 |
| --- | --- | --- |
| A1 条件表 | TASK.md、REPORT.md、results/baseline.md、实际frozen/config.json；核对32GPU、P16/D16、TP1、EP16/16、61层、P chunk/batch和D batch、网络与本地端口、SLO、窗口、指标和模型假设 | 通过 |
| A2 配置与差异 | 独立递归比较原base与冻结JSON，恰9项差异；含显式写入原先缺省的decode_admission_limit=0，其他均为任务规定的开关、观测和路径；在WSL simai中通过实际 ./inference validate | 通过 |
| A3 输入身份与覆盖 | 独立核对manifest内4个来源与4个冻结文件的SHA256和字节数；profile/workload与原件逐字节相同；19366个唯一请求、无请求级SLO覆盖；重新检查Prefill/EP及Decode batch1至64的上下文覆盖 | 通过 |
| A4 固定绑定 | 使用WSL Python3.12.16重新生成完整binding_plan，逐字段等于Windows Python3.13生成的冻结文件；独立核对绑定内容哈希、请求覆盖、P/D计数 | 通过 |
| A5 开关与执行语义 | 核对KVRuntime与CONTROLLED_KV_MODES、Prefill/KV/Decode代码；layer_ready_immediate无受控门控，max_block_bytes仍影响分块；保留EP、Chunked/packed Prefill、连续Decode、资源与完成依赖 | 通过 |
| A6 范围与记录 | 执行前后Git状态日志逐字节相同，manifest中的基线状态文件哈希正确；原始失败记录保留，报告未将静态验证写成仿真成功；审核仅增加review前缀的证据、REVIEW.md并更新STATUS.json | 通过 |

## 审核新增证据

运行目录：`/home/m0nesy/Postgraduate/Inference`。

1. `env CONDA_EXE=/home/m0nesy/miniconda3/bin/conda ./inference --config outputs/agents/2026-11-07/T001-baseline-freeze/results/frozen/config.json validate`：退出0，见 `logs/review-wsl-validate.txt`。
2. `env CONDA_EXE=/home/m0nesy/miniconda3/bin/conda PYTHONPATH=. ./scripts/simai-python work/agents/2026-11-07/T001-baseline-freeze/review_static.py`：退出0，34项独立静态检查通过，见 `logs/review-static.txt` 和 `results/review-verification.json`。
3. 审核解释器：`/home/m0nesy/miniconda3/envs/simai/bin/python`，Python3.12.16。执行agent此前无法使用该环境的缺口已由本轮审核补齐，不需要为此返工或重装环境。

冻结config SHA256：`565572ab4d2dde71bd265bfc8a240f7b433bf5987886505c231ea9a8f049a8f1`。
冻结manifest SHA256：`811cc29616c0e1539a43109ae78d5bce14f23fde6b97a7e157c63ef956b0af78`。
绑定内容SHA256：`6fd36888a9e6529550efbd870af8099478ec608a5a6b0250bebf4e13f08d2aa5`。

## 问题与返工要求

无阻断项，不要求重跑冻结任务。以下口径澄清随审核结论一并保留，未改动执行报告或冻结文件：

- input-audit.json把 `input_tokens + output_tokens - 1` 的最大值14088称为最大Decode上下文。它对应保守的最终KV范围上界。执行器在最后一个Decode计算步查询的是 `input_tokens + output_tokens - 2`，实际最大值为14087。审核已重新验证两者及所有batch大小均被曲线覆盖，因此不影响配置有效性；后续引用时区分两种口径。
- 清单中原始来源路径使用Windows反斜杠，审核按跨平台分隔符规范化后核对通过；实际冻结配置的profile、workload和evaluation路径均为可在Linux加载的相对路径。
- 配置中的KV policy字段与真正门控创建应分开理解：实际由runtime.kv_transfer_mode决定是否创建受控KV调度器；当前基础模式不创建。10μs配置值保留但不产生受控KV决策延迟。

## 下一轮条件

可以规划下一项“有界样本的基础链路验收”。新任务需明确样本选择、到达时间处理、绑定计划复用、有限运行窗口与通过标准；沿用本轮冻结模型、拓扑、执行和SLO条件，并记录所有用于短程验证的必要覆盖项。

有界子集的绑定应从本轮全量binding_plan按request_id投影并保持原端点，不能仅对子集重新按轮询/随机生成后宣称复用了同一计划。实际CLI/runner如何传入或核对该计划，应在下一任务中明确并验证。

本审核不自动创建或执行下一项，不开展算法比较、DeepEP适配、扩容或全量轨迹运行。