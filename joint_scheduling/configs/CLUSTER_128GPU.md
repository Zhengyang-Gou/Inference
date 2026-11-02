# 128 GPU 固定拓扑基准

新增 `cluster_128gpu_tp4_ep8_8gpu_per_node.json`：16 台 × 8 卡、P/D 各 64 卡、节点内 TP4，EP8 每组跨 4 台服务器，P/D 各两个模型组。
它供“跨节点 EP 与 KV 竞争、TP 节点内”的实验使用；不沿用 dense Llama 计算参数，需要 MoE 专家 profile，当前使用轮询并拒绝 EP 联合预测。详见 [EP 配置与边界](../backend/EXPERT_PARALLEL.md)。以下两份为原 dense TP 方案的对照。

本基准固定物理拓扑、P/D 资源分配与 TP，供后续冻结计算 profile、负载、批处理参数和 SLO。
当前仅完成拓扑定义；配置保留空的 profile/workload 路径，不自动使用 synthetic 数据代替真实研究输入。
默认轮询绑定用于校准基线，联合选择等算法开关在对照实验中显式设置。

## 固定配置

| 项目 | 通信竞争基准 | 节点内 TP 对照 |
| --- | --- | --- |
| 配置文件 | `cluster_128gpu_tp4_2gpu_per_node.json` | `cluster_128gpu_tp4_8gpu_per_node.json` |
| GPU 总数 | 128 | 128 |
| P/D GPU 数 | 64 / 64 | 64 / 64 |
| P/D TP | 4 / 4 | 4 / 4 |
| P/D 实例数 | 16 / 16 | 16 / 16 |
| GPU/服务器 | 2 | 8 |
| 服务器总数 | 64 | 16 |
| P/D 服务器数 | 32 / 32 | 8 / 8 |
| Leaf / Spine 数 | 8 / 4 | 8 / 4 |
| 服务器/Leaf | 8 | 2 |
| GPU/Leaf | 16 | 16 |
| 聚合 NIC 带宽/服务器 | 200 Gbps | 800 Gbps |
| 聚合 NIC 带宽/GPU | 100 Gbps | 100 Gbps |
| 每条 Leaf–Spine 链路 | 400 Gbps | 400 Gbps |
| Leaf 下行/上行容量，每方向 | 1600 / 1600 Gbps | 1600 / 1600 Gbps |
| 网络链路时延 | 1 μs | 1 μs |
| 共享本地 FIFO 带宽/时延 | 1600 Gbps / 0.5 μs | 1600 Gbps / 0.5 μs |

上述带宽、时延和每 GPU 80 GiB 容量是显式仿真假设，不是实测设备规格。
计算数据优先保持现有受支持的 Meta-Llama-3-70B FP16、A100、TP4 组合；尚未在这两份配置中绑定该数据。
设备标称 80 GB 与配置单位 GiB 不自动等价，需要在冻结目标硬件时校准。
当前节点内单共享 FIFO 不复刻 NVLink/NVSwitch；节点 NIC 为一个聚合端点，不模拟多个独立网卡或 rail。

## 部署映射

Leaf `l` 管理 GPU `16l` 到 `16l+15`。
两个 P 实例分别使用前 8 张 GPU 的两组连续 4 卡，两个 D 实例使用后 8 张 GPU 的两组连续 4 卡。
例如 Leaf 0：`p0=[0,1,2,3]`、`p1=[4,5,6,7]`、`d0=[8,9,10,11]`、`d1=[12,13,14,15]`。

两份配置保持相同 GPU ID、实例划分和 Leaf 归属：

- 2 GPU/节点：每实例跨同一 Leaf 内两台服务器；每服务器属于一个角色和一个实例。TP ring 与 KV 交接共享服务器网卡和 Leaf 端口。
- 8 GPU/节点：每实例全部位于一台服务器；每服务器放同角色的两个 TP4 实例。P 与 D 不共置于同一服务器，KV 跨服务器，TP ring 走本地 FIFO。

因此 8 卡对照中的远程 KV 与 TP 集合通信不共享当前模型的 NIC 路径。
它用于检查“没有跨节点 TP 竞争时”策略是否仍有收益或引入开销，不能要求集合通信保护在此仍显著获益。
2 卡基准描述明确的跨节点 TP 场景；其结果不能推广为所有 8 卡节点集群的结论。

## 配比与 TP 的选择理由

P:D=1:1 是便于策略对照的初始分配，不是最优配比结论。
后续按同一负载、SLO、组批规则和匹配 profile 校准各阶段承载率；再考虑 1:3、1:1、3:1 等资源分配消融。
不能按输入/输出 token 数之比直接确定 P/D GPU 数之比。

TP4 保持现有性能数据组合和两侧等 TP 的 KV 分片映射。当前后端不支持 P/D 不同 TP 的 KV 重分片。
70B FP16 的权重按参数量粗估约 140 GB，总量除以 TP4 约 35 GB/卡；实际容量还要扣除 KV、缓冲及安全余量。
TP2、TP8 都需要独立匹配时延数据和容量分析，不能用固定倍数推算。

## 比较时保持的条件

这两份配置固定每 GPU 聚合 NIC 带宽及 Leaf 上下行容量比例。
如果把 8 卡节点仍设为单节点 200 Gbps，网络注入能力/GPU 会降低为 25 Gbps，同时改变通信布局和网络资源，结论需分别解释。
每条对照运行仍应冻结 profile、请求长度配对、SLO、缓存、Prefill/Decode 批处理与声明调度开销。
观测计算/执行忙碌、队列增长、集合通信时延和达标完成吞吐，不用配置或有限回放宣称生产容量。

验证可使用 `inference --config ... validate` 和 `export-topology --format simai`。
实际仿真还必须提供覆盖完整输入/Decode 上下文与批量范围的 profile 及负载。

参考：

- NVIDIA HGX A100 的 4/8 GPU、NVLink/NVSwitch 和网卡配置：[官方说明](https://developer.nvidia.com/blog/introducing-hgx-a100-most-powerful-accelerated-server-platform-for-ai-hpc/)。本基准没有复刻整机规格。
- P/D 资源和并行度随负载及 SLO 联合选择：[DistServe](https://www.usenix.org/conference/osdi24/presentation/zhong-yinmin)。1:1 不构成该负载的最优分配。
