# 冻结的 Azure 请求输入

本目录保存公开轨迹的原始 CSV 和本系统导入后的请求。来源为 [AzurePublicDataset](https://github.com/Azure/AzurePublicDataset)，文件身份以 [manifest.json](manifest.json) 中的 SHA-256 为准；不包含 prompt 文本、专家路由、业务 SLO 或会话依赖。

| 原始文件 | 请求数 | 相对到达跨度（秒） | 全段平均请求/秒 |
| --- | ---: | ---: | ---: |
| `raw/AzureLLMInferenceTrace_conv.csv` | 19,366 | 3501.721937001 | 5.530421989 |
| `raw/AzureLLMInferenceTrace_code.csv` | 8,819 | 3435.948056001 | 2.566686 |

`conv_original.json` 是研究主输入：保留顺序、成对输入/输出长度、原始到达间隔，首个请求平移至 0；未进行时间压缩、循环补量或长度截断。总输入上限 14050、输出上限 1000、最后 Decode step 的最大上下文 14087，处于当前 profile 的 1–16384 覆盖内。

`conv_first_second_validation.json` 显式选取原始时间 [0,1) 秒，其中只有首个请求（374/44）；仅用于接通真实模型配置和后端。下一个请求在 4.314579 秒，因此这个片段不能作为持续负载。code CSV 已冻结供后续独立实验使用，未加入当前 conv 主对照。

`conv_four_requests_128rps_diagnostic.json` 是原竞争诊断的四请求片段，保留修正后的来源/选择范围记录；完整轨迹目标到达率为 128 请求/秒，片段实际约 19.65 请求/秒。用于固定流量重放，不是持续负载。全部准备文件均在 `manifest.json` 固定哈希。

在仓库根目录复现完整导入（输出请选择新路径）：

```bash
./inference import-trace --trace data/azure/raw/AzureLLMInferenceTrace_conv.csv --format azure --sha256 2f1e5b666d4e3055fdbba98598ce2ec307767b9064e03e2fa46676dbcc7d0bf8 --source-url https://github.com/Azure/AzurePublicDataset/blob/master/data/AzureLLMInferenceTrace_conv.csv --request-prefix azure-conv --profile joint_scheduling/profiles/deepseek_v3_h800_tp4_ep8_estimated.json --output joint_scheduling/artifacts/azure-conv-reimported.json
```

追加 `--duration-seconds 1` 可复现验证片段，需使用不同输出路径。未指定 `--target-rps` 时保持原始相对到达；指定时均匀缩放完整时间轴，并记录缩放率和片段实际到达率。SLO 是实验配置假设，不能从这些长度/时间数据推导。导入不会自动生成前缀共享身份。
