# 第三方代码的保留范围

本目录同时保存当前运行依赖和后续推理研究需要的实现。暂未接入主流程不等于无用；版本、许可和 ns-3 模块保留范围见 [manifest.json](manifest.json)。

| 组件 | 当前用途 | 后续保留理由 |
| --- | --- | --- |
| ns3 | 当前实际使用的分组网络、RDMA、ECMP 与拥塞控制后端 | 拓扑扩展、通信观测、并行仿真及新的通信模型 |
| vidur | 推理执行参考，未接入当前简单基线 | P/D 分离、连续批处理、实例本地调度和性能预测 |
| aicb | 推理性能采样参考，未接入当前 JSON profile 管线 | 真实模型计算采样、推理 workload 及 GPU 采样工具 |
| simccl | 集合通信模型参考 | 更完整的 AllReduce、AllGather、AlltoAll 等流量与依赖模型 |
| astra_sim | SimCCL 依赖的公共类型及原通信框架 | 保留 SimCCL 依赖闭包、分析模型和后续接入参考 |

ns-3 中保留当前依赖的 core、network、internet、point-to-point、bridge、traffic-control、stats、config-store。额外保留 applications、flow-monitor、topology-read、point-to-point-layout、nix-vector-routing、mpi、mtp，以及 point-to-point-layout 依赖的 mobility；同时保留这些模块的模型源码、文档、测试和公共测试支撑。

移除了与本项目推理集群研究无关的无线/蜂窝/水下网络、移动自组网协议、外部网络仿真接桥及可视化模块，以及未使用的 Python 绑定生成器、上游示例程序和整套通用手册。保留的 mobility 是布局工具的实际依赖，不按名称简单归类删除。

当前正式构建仍只启用既有网络依赖；保留的后续模块并不表示已经接入或完成回归。原作者声明及组件许可证继续保留。以后扩展超出此范围时，应根据来源版本重新引入所需模块，并同步源码清单、内容锁和测试。
