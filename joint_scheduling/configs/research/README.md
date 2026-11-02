# 主研究配置：Azure / DeepSeek-V3 / EP 与 KV

本目录是 EP/KV 仿真研究的统一入口。计算由离线 profile 驱动，通信由 ns-3 RDMA 分组模型驱动；所有事件在 CPU 上推进。短程功能验证仅解释对应仿真场景。

## 固定条件

| 项目 | 共同设置 |
| --- | --- |
| 请求 | `data/azure/conv_original.json`，19,366 个 Azure conv 请求，保留成对输入/输出长度与原始相对到达时间 |
| 注入时长 / 平均到达率 | 3501.721937001 秒 / 5.530421989 请求每秒，未压缩时间轴 |
| 模型 | DeepSeek-V3 671B / H800 估算 profile，`estimated_simulation_profile`，用于仿真计算代价 |
| 仿真拓扑 | 20 节点 × 8 GPU = 160 GPU，P/D 为 32/128 GPU；10 Leaf × 4 Spine；每 GPU 独立 400 Gbps NIC，节点合计 3.2 Tbps；每 Leaf 上联 4×1.6=6.4 Tbps 逻辑聚合容量，与下联 1:1 |
| 节点内通信 | `gpu_ports`，每 GPU 发送/接收端口各 1280 Gbps（160 GB/s）、0.5 μs；独立端口并发，共用发送或接收端口排队 |
| 并行 | TP1；P 为 Attention DP32 / Expert EP32，D 为 DP128 / EP128；EP 按 GPU rank，P/D 各一个模型单元 |
| 执行语义 | 异步源 batch dispatch → 专家 FIFO → combine；没有全 EP 组 batch 屏障，Decode 是聚合 dense step 后执行 MoE 层 |
| Prefill / Decode | chunk 512 token；P 总预算 2048 token、最多 16 请求；D 最大 batch 64 |
| 网络 | DCQCN，包 payload 8000 B，交换机 buffer 16 MiB；各组完全相同 |
| 观测 / 调度开销 | 网络采样间隔 1 ms、可见延迟 100 μs；调度开销 10 μs，各组相同 |
| SLO | TTFT 1 秒、ITL 100 ms，是实验假设，Azure 源没有提供业务 SLO |
| 可见输出上限 | 全局 2048 token，给 MLU/绑定预测使用；当前轮询物理预留仍沿用仿真输出长度，属于当前整请求容量模型 |
| 统计窗口 | [60, 3501.721937001) 秒，排除预热和停止注入后的排空；仿真截止 4101.721937001 秒 |
| 其他功能 | `balanced_random`：P 轮询、D 每轮均衡随机，独立绑定 seed 7；绑定窗口、预测、联合选择、前缀缓存均关闭；必要记录 `essential` |

完整参数以 `base.json` 和启动生成的 `comparison_audit.json` 为准。当前 `rank_unit=gpu`、TP1；DP 与 EP 使用同一批 GPU，不将两者相乘。旧 `tp_team` 模式仍保留；当前语义见 [EP 说明](../../backend/EXPERT_PARALLEL.md)。模型估算来源、覆盖和限制见 [profile 说明](../../backend/MOE_PROFILE.md)。

第一步已切换为 TP1、P-EP32 / D-EP128：P 为 4 节点，D 为 16 节点；Leaf 0–3 各一 P 一 D、Leaf 4–9 各两 D。1:4 是参考单元配比，尚未判定最优。P/D 每 GPU 权重分别约 36.6093 / 22.3323 GiB，KV 为 70272 B/token 单副本；双微批重叠和 DeepEP 聚合转发仍未实现。对外和节点内参考带宽见 [DeepEP H800 配置](https://github.com/deepseek-ai/DeepEP/blob/v1.2.1/README.md#performance)。`nic_bandwidth_gbps=3200` 始终按节点合计，`nic_model=per_gpu` 将其均分为 8 个独立网卡端点；`intra_node_bandwidth_gbps=1280` 在 `gpu_ports` 模式按单 GPU 的发送/接收端口计。上联为逻辑聚合容量，局部网络为端口模型，均不等价于完整 IB/NVSwitch/DeepEP 实现。旧共享 NIC/FIFO 模式保留，既有运行按原配置解释。

## 两套对照

`kv_policy.json`：Immediate / FIFO / MLU × 网络 seeds 7、19，共 6 次。三组均关闭原生网络优先级。Immediate 与 FIFO 只改变逐层 KV 的放行模式；MLU 同时启用其必需的 SLO 分级和集合通信保护。因而这里测量的是完整 MLU 调度策略的作用，不能把其收益单独归因于分级或保护中的某一个组件。

`network_priority.json`：固定 MLU，仅切换原生主机/交换机优先级，seeds 同为 7、19，共 4 次。它单独衡量网络优先级机制，不混入主策略对照。

以上规格显式标注 `experiment_type="end_to_end"`。各次自动冻结配置、请求、profile、源码（含未提交修改）和后端身份，suite 输出 `report.md`、统计 JSON/CSV、各次 `reproducibility.json`，见 [系统边界](../../RESEARCH_SYSTEM.md)。

P/D 在请求到达时提交绑定，随后不迁移。D 每轮将全部 128 个实例打乱后各用一次，打乱只使用 `binding.seed`，不随网络 seed 或 KV 调度结果变化。对照预检核对绑定计划身份，端到端 suite 只生成一份 `inputs/binding_plan.json`，所有 case/网络 seed 复用并在运行前校验；每次运行另存 `binding_plan.json` 及哈希。Azure 全量的跨 Leaf 请求为 18154/19366（93.7416%），这是计划统计，尚未执行全量仿真。旧 `round_robin` 保留轮询行为，但在两池大小不同的新布局中不保证同 Leaf。

从仓库根目录预检，不会创建输出目录或启动网络后端：

```bash
./inference run-experiments --spec joint_scheduling/configs/research/kv_policy.json --output joint_scheduling/artifacts/research/kv-policy --dry-run
./inference run-experiments --spec joint_scheduling/configs/research/network_priority.json --output joint_scheduling/artifacts/research/network-priority --dry-run
```

确认所选研究条件后，去掉 `--dry-run` 即执行整套实验。原始轨迹注入约 58 分钟仿真时间，每个 seed 都运行完整输入；包级仿真的实际耗时可能很长。此次整理只完成全量预检，没有执行这 10 次全量实验。

## 短程连通性验证

```bash
./inference run-experiments --spec joint_scheduling/configs/research/validation.json --output joint_scheduling/artifacts/research/validation --fail-fast
```

`validation.json` 使用相同模型和拓扑，但显式选取 Azure 原始轨迹的前 1 秒：其中只有 **1 个请求（输入 374、输出 44）**，仿真和统计窗口为 5 秒、seed 7。三种策略已完成ns-3 仿真后端验证。它用于检查输入、模型、调度与后端是否接通，不代表竞争压力，也不能证明 160 GPU 的负载足够。

## 防止对照条件混用

每个规格的 `comparison` 声明允许变化的字段、固定功能开关和 profile/workload SHA-256。预检会加载各组实际配置，核对所有组间差异、模型与完整请求的覆盖、EP 配置及输入身份。隐藏的带宽、批量、包大小等变化会在创建输出目录之前失败；原始输入在预检后变化也会被拒绝。

输出中保留 `comparison_audit.json`、冻结输入、每次实际配置及 `effective_baseline`，不只依赖实验名字。改变压力（例如压缩到达时间）应另建明确命名的输入和规格，更新输入哈希，整套策略共同使用；不能只修改某个策略的负载。旧的小型配置仍用于功能测试，分类见 [配置目录](../README.md)。

## 固定流量竞争归因

```bash
./inference run-experiments --spec joint_scheduling/configs/research/contention_diagnostic.json --output joint_scheduling/artifacts/research/contention --dry-run
./inference run-experiments --spec joint_scheduling/configs/research/contention_diagnostic.json --output joint_scheduling/artifacts/research/contention --fail-fast
```

此规格标注 `frozen_network_replay`：先端到端运行，再按相同提交时间、字节数、优先级和端点顺序重放 both / EP only / KV only，最后自动生成比较与报告。不依赖聊天工作区脚本。

输入是原诊断的 4 个真实 Azure 请求，按完整轨迹目标 128 请求/秒缩放时间后选取前 4 个，片段实际约 19.65 请求/秒、注入约 0.2035 秒。它不是持续 128 请求/秒。使用当前 160 GPU、TP1、P-EP32 / D-EP128、估算 profile 与 8000 B payload；使用无门控逐层立即 KV、网络优先级关闭。端到端请求结果在 `run/result/`，固定流量结果在 `replay-*/`，归因在 `comparison.json` / `report.md`，不能将重放的时延差直接解释为请求 SLO 改善。
