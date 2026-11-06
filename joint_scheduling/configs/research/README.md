# 主研究配置：Azure 2023 / 2024、32 GPU、EP 与 KV

当前研究负载范围仅为 Azure2023/2024。数据目录见 [Azure 输入](../../../data/azure/README.md)。2023 原始 conversation/code 与完整原时间轴对话 JSON 已保留；2024 尚未下载和验证，不能宣称已经接入或沿用2023的承载率结论。

2026-11-06 按用户要求移除了 FreeInference 和所有人工派生研究负载，以及引用这些输入的固定实验规格。历史结果按各自 outputs/ 冻结输入解释；基础 synthetic 回归夹具和通用导入器保留，不属于正式研究负载。

## 固定机制与条件

| 项目 | 当前条件 |
| --- | --- |
| 主输入 | data/azure/conv_original.json，19366个2023对话请求，保留配对长度、源顺序和原始相对到达时间 |
| 模型 | DeepSeek-V3/H800 估算计算 profile；非实机部署保证 |
| 布局 | 32 GPU，P16/D16，TP1；两侧 Attention DP16 / Expert EP16 |
| 网络 | 每GPU独立400 Gbps NIC；节点内GPU发送/接收端口各160 GB/s；具体参数以base.json为准 |
| Prefill | chunk512、总token预算2048、最多16请求 |
| Decode | 最大batch64；显式61层，前3 Dense/后58 MoE；本层计算与对应EP交错，全部层完成后返回token |
| EP | 真实层编号确定性合成路由；按token/目标GPU激活去重，保留expert assignment计算和GPU FIFO；本地合并后回传 |
| 调度 | 现有Immediate/FIFO/MLU与SLO分级、集合通信保护；观测间隔1ms、延迟100μs、调度开销10μs |
| SLO | TTFT1秒、ITL100ms，均为人工实验假设 |
| 限制 | 合成路由、离线计算曲线估算，本地输出合并额外成本显式为0；无双微批/DeepEP节点聚合 |

## 保留的正式规格

- base.json 与 base_32gpu.json：主32GPU布局；base_48gpu.json、base_160gpu.json：显式规模参考。
- kv_policy.json / kv_policy_32gpu.json / kv_policy_48gpu.json / kv_policy_160gpu.json：原始2023完整对话输入的策略对照。
- network_priority.json / network_priority_160gpu.json：同原始输入的网络优先级消融。

规格中的输入/profile哈希、冻结绑定计划、实际有效配置和后端身份仍需校验。网络后端的支持情况和近似见 [后端说明](../../backend/README.md)；后端切换不能改变请求长度或偷偷改变负载。

先预检，不启动完整包级运行：

```bash
./inference run-experiments --spec joint_scheduling/configs/research/kv_policy.json --output outputs/azure2023-policy --dry-run
./inference run-experiments --spec joint_scheduling/configs/research/network_priority.json --output outputs/azure2023-priority --dry-run
```

原始轨迹约58分钟仿真时间，未授权时不自动启动全量包级矩阵。短程核验使用基础测试夹具，或由任务在自己的 outputs/inputs/ 明确生成有界 Azure2023/2024 输入及临时规格，不向 data/ 新增固定派生负载。2024 需要先下载并审计，不能把现有2023重命名冒充2024。

固定流量网络归因机制仍保留；需由任务给出临时审计输入。只有同一后端包级自身重放才检查ACK误差≤1ns，流级与包级结果不作该精确对齐。冻结流量差值不等于端到端吞吐或SLO收益。
