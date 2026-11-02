# 常见基线机制覆盖

此处关注通用推理引擎机制，不把本仓库自有的绑定、KV SLO、带宽保护和前缀缓存计作缺失优化。
本仓库运行的是计算 profile + ns-3 网络仿真，不能把引入一个仿真调度机制称为实现了真实GPU内核。

| 通用机制 | 当前实现与缺口 |
|---|---|
| Decode 连续批处理 | 已有，每轮每请求生成1 token；批次共享计算与通信。仍采用最大上下文 padding 估算，不是对每条不同长度序列分别计时的 ragged attention 模型。 |
| Chunked Prefill | 已有 token 分块与块边界请求轮转，历史上下文进入计时。另已实现可选的P池多请求token预算合批：running优先、waiting FCFS、预算截块及共享集合通信。多请求attention时延仍采用独立估计之和，未验证真实varlen kernel。 |
| 分页 KV/PagedAttention | 没有独立分页块池、页表、内部碎片和逐页容量增长模型；当前对象字节账本按整请求预留。公开 attention 数据使用的block_size=16不能等同于已经实现分页分配器。 |
| 显存不足时抢占/重计算 | 没有释放活动请求KV再重计算的流程；当前主要等待可用容量。块边界轮转不会释放已有KV。CPU/SSD offload也没有数据迁移和成本模型。 |
| GPU执行优化 | 没有独立模拟 CUDA Graph、torch.compile、跨算子融合和CPU异步调度开销。公开算子时延已包含来源内核的部分优化，不能称其完全未计入FlashInfer/优化内核成本。 |
| 集合通信优化 | 有固定ring AllReduce与真实网络共享；没有NCCL按规模/拓扑选择ring/tree、NVLS等算法，也没有真实NVLink拓扑。节点内链路为共享FIFO。 |
| 权重/KV量化 | 现有70B配置FP16，没有独立的低精度时延、显存、通信与质量假设。 |
| Speculative decoding | 没有草稿生成、目标模型验证、接受率和回滚流程；当前每个D成员每轮只返回1个token。 |
| 资源弹性 | P/D比例、TP大小与实例数量由配置固定；没有运行时扩缩容或P/D资源自动调整。PP/EP也尚未实现，当前稠密70B基线不要求EP。 |

下一步优先补齐多请求 Prefill 打包，以及异长 Decode 的计算模型与实测校准；再按显存压力决定是否加入分页/抢占。
CUDA Graph/量化/推测解码应通过匹配测量与完整成本模型接入，不能直接假定固定加速倍数。

常见机制的官方参考：
- https://docs.vllm.ai/en/stable/configuration/optimization/ （chunked prefill、token预算、显存抢占重计算、CUDA Graph）
- https://docs.vllm.ai/en/stable/design/paged_attention/ （分页KV与attention数据布局）
- https://docs.vllm.ai/en/stable/features/speculative_decoding/ （推测解码）
