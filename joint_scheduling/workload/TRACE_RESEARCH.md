# 真实推理请求 trace 调研与接入判断

核对日期：2026-11-04。研究主布局已采用 32 GPU、P16/D16、TP1、两侧 DP16/EP16。本次完成公开来源核对、原始文件检查和当前 16K profile 覆盖审计；Azure 仍作为回归输入，实验主负载尚未替换。

后续适配已完成：FreeInference 原始 JSONL/ZST、目录、多层子 agent 和排除项审计已接入 `import-trace`；新增的 163840-token 估算 profile 覆盖该日期分片全部 1192 个有效请求。提供独立的 32 GPU 验证与 1 rps 规格，见 [FreeInference 输入及复现](../../data/freeinference/README.md)。下列 16K 统计保留为原始兼容性检查，不能解读成新 profile 的覆盖率。

## 选择建议

**以 FreeInference Agentic Trace 2026 作为现代真实负载的主候选，以 Mooncake FAST’25 tool-agent 作为近期接入对照。** 前者有明确的 2026 年采集窗口和会话信息，后者已被现有 Mooncake 导入器识别。当前主要障碍是长上下文曲线和会话语义，不是文件能否下载。SwissAI/OpenTela 适合作为另一类通用 serving 验证集，需先流式审计全量、明确数据使用条款。

| 候选 | 采集时间与公开版本 | 公开内容 | 对当前研究的判断 |
| --- | --- | --- | --- |
| [FreeInference / HarvardMadSys](https://huggingface.co/datasets/harvardMadsys/freeinference_agentic_trace) | 明确采集于 2026-05-17—09-05；核对时最近更新 2026-11-03 | 1,186,582 次 LLM 请求、12,002 个 agent 会话；请求时间、provider token 计数、工具调用、前缀块；CC BY 4.0 | 最明确的新采集负载；格式已适配，163840 网格覆盖已检查分片，全量各周仍待扫描 |
| [SwissAI serving trace / OpenTela](https://huggingface.co/datasets/eth-easl/swissai-serving-trace) | OSDI’26 资料，数据集最近更新 2026-07-29；未给出完整采集起止 | `trace.jsonl` 16,329,237 行、约 6.98 GB；到达/完成时间、状态、模型、成对 token 计数 | 适合通用 serving；样本包含 2025 年请求，不能称全部采集于 2026 年 |
| [Mooncake FAST’25 更新版](https://github.com/kvcache-ai/Mooncake/blob/main/FAST25-release/README.md) | FAST’25 的更新发布；真实部分为线上一小时采样，未披露日历采集日期 | conversation 12,031、tool-agent 23,608、synthetic 3,993 条；到达时间、成对长度、512-token 前缀哈希 | tool-agent 最易接入，且适合 KV 研究；synthetic 必须单列为合成负载 |
| [UW SyFI TraceLab](https://tracelab.cs.washington.edu/) | 2025-09-23—2026-07-24 | 665,453 次 agent step、8,058 会话；缓存和工具等待等信息 | 适合未来会话/缓存研究；其长前缀与当前 16K、缓存关闭的设置差距大 |
| [Alibaba xMaaS 2026](https://github.com/alibaba/clusterdata/tree/master/cluster-trace-v2026-maas) | ATC’26，匿名六个月；公开日期相对化 | 日级实例段、5 分钟实例观测、小时级应用统计 | 可用于 P/D 资源比例与利用率分析；公开表不含逐请求到达＋成对输入输出长度，不能直接替代本系统的 request trace |

FreeInference 的采集时间和规模取自作者数据卡；SwissAI 的版本/行数取自数据卡和固定 revision 的 metadata，相关论文见 [OpenTela OSDI’26](https://www.usenix.org/conference/osdi26/presentation/yao)。Alibaba 的判断依据是作者公开的 [字段 schema](https://raw.githubusercontent.com/alibaba/clusterdata/master/cluster-trace-v2026-maas/docs/schema.md)。论文/发布年份与原始数据采集年份分别记录。

## 下载并核验的实际内容

以下是本地计算结果。Mooncake 三份均为全文件；FreeInference 仅检查 `2026-05-17` 会话日期分片；SwissAI 仅检查文件前 131,072 字节的完整行。后两者都不是全数据集分布估计。正长度对要求输入/输出均为正整数，缺失或非正计数单独记录，没有补造或截短 token 数。

| 文件/范围 | 检查行数／请求数 | 正长度对 | 输入 P50 / P95 / 最大值 | 当前 16K 覆盖的正长度对 |
| --- | ---: | ---: | --- | ---: |
| Mooncake conversation，全量 | 12,031 | 12,031 | 6,909 / 39,552 / 126,195 | 9,206 / 12,031＝76.52% |
| Mooncake tool-agent，全量 | 23,608 | 23,608 | 6,346 / 26,114 / 126,195 | 21,073 / 23,608＝89.26% |
| Mooncake synthetic，全量 | 3,993 | 3,993 | 11,587 / 49,009 / 191,378 | 2,385 / 3,993＝59.73% |
| FreeInference，会话日期分片 2026-05-17 | 1,375 | 1,192 | 73,894 / 112,021 / 137,320 | 38 / 1,192＝3.19% |
| SwissAI，文件开头的完整 318 行 | 318 | 233 | 116 / 169 / 285 | 233 / 233＝100%，仅此文件前缀 |

覆盖计算沿用当前执行器的曲线边界：Prefill 输入和最后一次 Decode context 均须落在 16,384 范围；后一坐标为 `input + max(0, output - 2)`。Mooncake 同时调用了仓库现有 `read_trace`、`scale_trace` 和 `profile_coverage`，三份格式均被识别，但长请求会被 coverage 检查拒绝。这个“格式接受”不代表当前 profile 可运行整个 trace。

FreeInference 分片包含 25 个根会话，183 个请求缺少正的输入输出配对；不将它们一律解释成失败。分片按会话日期组织，内部请求可能跨午夜，不能当成全平台完整一天。SwissAI 前缀中 85 行状态为 ERROR 且无正长度对，233 行为 DEFAULT；首条到达时间为 `2025-08-21T14:34:25.590Z`。未下载约 7 GB 全文件，因此没有声称 SwissAI 整体长度或到达分布已核验。

Mooncake conversation/tool-agent 均跨 3536.999 秒、只有 1,180 个不同时间戳，正间隔中位数约 3 秒；同一时间戳最多分别有 28/47 个请求。这是公开文件的时间粒度，不能将这些并发批次直接解释成原服务毫秒级真实突发。更换负载后仍应分别报告原时间轴和明确标注的统一时间缩放扫描。

## 各格式的接入约束

### FreeInference

原始路径是 `release/<week>/traces/<day>.jsonl.zst`，每行一个会话，内部 `requests` 为 LLM 请求，工具调用可嵌套子 agent；也提供每周 `processed/requests.parquet`。具体结构见 [作者代码与说明](https://github.com/HarvardMadSys/freeinference_agentic_trace)。当前原始格式适配遵守以下规则，Parquet 尚未接入：

- 到达取请求自身 `start_ms`，转相对时间；保存会话、step、turn、父子关系和原模型标识。完成时间、TTFT 是观测结果，不能作为本系统预定的完成结果。
- 长度取 `provider_tokens.prompt` / `completion`，单独保存缺失/非正原因。当前关闭缓存时使用完整 prompt，不从 prompt 扣除 cached tokens。
- `block_ids` 是基于 `o200k_base` 的 16-token 块，最后不足一块的部分不保留，块标识的作用域为周。它们不等于 provider 的 tokenizer，也不能用块数替代完整输入长度。
- 将外部模型的 token 长度用于 DeepSeek-V3 时应标为“按源 token 数重放的请求形状”。公开文件没有原文，无法做精确 DeepSeek-V3 重新分词。
- 当前按固定时间戳重放只能研究外生请求流。若讨论 agent 整体完成时间，就需根据上一步仿真完成时刻与工具/用户等待生成下一步到达；现有独立请求模型尚未表达这种反馈。

单分片输入最大值已超过 128K，完整数据需要先流式扫描最大上下文、批量边界与模型分布，再决定 profile 覆盖范围。单纯把 `max_context` 改大仍属于离线计算假设的外推，需记录来源和敏感性。

### Mooncake FAST’25

优先使用 `FAST25-release/traces/` 的 conversation/toolagent 文件，不混用旧 `arxiv-trace/mooncake_trace.jsonl`。已有 `import-trace --format mooncake` 能读取相对毫秒时间、`input_length`、`output_length` 和前缀身份，接入成本最低。当前主研究关闭前缀缓存，哈希信息不会自动变成缓存命中收益。

两份真实文件的最大输入均为 126,195、最大输出 2,000；在当前坐标定义下，128K（131,072）曲线足以覆盖这两份长度。仍需检查 chunk history、packed batch、Decode batch 和显存。以本系统的 70,272 B/token 计，一个 128K 上下文约占 8.58 GiB KV；EP16 每卡模型内剩余约 20.35 GiB，长请求会明显压缩并发容量。该容量估计尚未包含 activation/workspace/DeepEP buffer。

不建议默认只保留 16K 内请求：tool-agent 会删去 2,535 条，conversation 会删去 2,825 条，并改变长度尾部、前缀和突发结构。若为小规模检查而筛选，应另命名为受限子集，公开原始/保留数量与筛选规则。

### SwissAI / OpenTela

建议使用固定 revision 的 `trace.jsonl`，按行读取；不要将同目录不同 schema 的旧文件一并自动推断。映射 `created_at` 为到达，`reported_token_input/output` 为长度；状态为 ERROR、计数 `-1` 等记录另存质量审计。`finished_at` 可用于理解来源服务，不能强制成为待验证调度器的服务时间。

数据卡声明许可为 `other`，没有在本次 metadata 清单中找到明确 LICENSE 文件；研究使用前需核清作者的数据条款。未将公开下载等同于已确认通用再分发许可。现有导入器没有 SwissAI 专用格式，且当前整文件读取方式不适合直接加载约 7 GB JSONL，后续宜使用流式转换。

## 下一阶段实验口径

1. 固定 32 GPU、P16/D16、网络和路由参数，Azure 保留回归。
2. 先补 Mooncake conversation/tool-agent 所需的 128K 估算曲线覆盖与显存预检，分别导入为独立输入；整个策略对照共享相同 trace、profile、绑定 seed 和输入哈希。
3. FreeInference 的独立会话展开/质量审计适配器已完成；后续对全量各周扫描，把外生请求重放和会话反馈实验分开声明。
4. 保留真实输入输出配对与到达顺序，以同一统一缩放因子扫描持续低负载、临界与过载；SLO 是独立实验假设，不能从来源服务的完成时间反推本研究目标。
5. 网络竞争继续用相同提交轨迹的 both / EP-only / KV-only 重放核验，并检查共用有向端口的时间重叠。新 trace、队列增长或 GPU 忙碌本身不能证明 KV↔EP 干扰。

这些请求 trace 没有提供 DeepSeek-V3 的逐 token gate 分数或专家 ID。替换请求源只更新到达与长度等工作负载条件，当前专家路由仍是满足 8 组／最多 4 组约束的确定性均衡模型。

## 可复核身份与本次验证

| 来源 | 固定版本 |
| --- | --- |
| Mooncake | Git revision `e5ef0e427a49cd2f8ad5b4356b09435095030439` |
| FreeInference | HF revision `bb26cf72c9c20f2b05f765d88548156a74d3c253` |
| SwissAI | HF revision `c3dd66602b564ce189f7db81115620413e646cd5` |
| 当前 EP16/16 profile | SHA-256 `3132eaade8c48121bc4dfab5457ac4485dc37e1e9af783a645ec93159a240537` |

本次原始文件下载清单、HTTP 部分读取范围、SHA-256、长度分位数及覆盖数量保存在 `work/trace-research-2026-11-04/sample-manifest.json` 和 `candidate-audit.json`；审计脚本为同目录 `audit_candidates.py`，这些是调研产物而非运行时依赖。32 GPU 主入口的 33 项相关测试通过，20 个研究规格（56 次计划运行）预检通过；Immediate/FIFO/MLU 三种策略的 Azure 单请求 ns-3 检查均完成。未执行新 trace 的完整策略实验。
