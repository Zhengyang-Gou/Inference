# Azure 2023 / 2024 研究负载

当前研究负载只保留 Azure 2023 和 2024 来源；FreeInference 和人工缩放、突发、对齐、代表性切片等派生输入已撤下。历史实验按各自 outputs/ 冻结输入解释，不能继续把旧样本路径当作可运行入口。

| 版本 | 本地状态 | 内容 |
| --- | --- | --- |
| 2023 | 已保留 | 原始 conversation/code CSV；保留配对长度及原始相对时间的 conv_original.json |
| 2024 | 尚未下载 | 官方 conversation/code 多日轨迹，需下载、固定哈希、审计长度/profile 覆盖后再接入 |

2023 原始文件为 raw/AzureLLMInferenceTrace_conv.csv（19366 请求）和 raw/AzureLLMInferenceTrace_code.csv（8819 请求）。标准化完整对话输入 conv_original.json 有19366 请求，跨度3501.721937001秒，平均5.530421989请求/秒，未截短、未循环、未压缩时间。保留文件 SHA-256 见 manifest.json。

官方说明：[2023](https://github.com/Azure/AzurePublicDataset/blob/master/AzureLLMInferenceDataset2023.md)、[2024](https://github.com/Azure/AzurePublicDataset/blob/master/AzureLLMInferenceDataset2024.md)。两者均提供 TIMESTAMP、ContextTokens、GeneratedTokens，不提供 prompt 文本、业务 SLO 或 DeepSeek-V3 专家路由。

需要短程验证或到达率扫描时，从原始2023/2024输入按明确规则临时生成到该任务 outputs/inputs/，保留来源哈希、长度配对、筛选和时间处理记录；不再向 data/ 增加固定突发/缩放/压力样本。不把基础单元测试夹具当作正式研究负载。
