# 实验性 EP 接入：节点内 TP，跨节点专家路由

主研究使用 32 GPU、P16/D16、TP1，两侧 DP16/EP16，EP rank 对应 GPU。P/D 各一个模型单元；DP 通过独立请求、批量和 KV 状态体现，不增加额外 DP 大数据流。原 P32/D128 的 160 GPU 布局保留为显式参考。

独立的路由/激活字节与实际提交依赖核验见 [EP 流量核验](EP_FLOW_AUDIT.md)。2026-11-04 已将主周期路由修正为满足 8 个专家组、每 token 最多 4 组的约束，profile 加载时显式校验；P 侧仍为直接发送模型。并行口径和 32/48 GPU 候选见 [部署调研](DEPLOYMENT_RESEARCH.md)。

DeepSeek-V3/H800 的完整结构与长上下文估算 profile、公开数据来源和 AICB 导入入口见 [MoE profile](MOE_PROFILE.md)。旧 EP fixtures 仍仅用于小规模功能测试。

当前自有请求执行器可以将 EP dispatch/combine 提交给同一 ns-3 后端，和 KV 共用仿真服务器 NIC、Leaf/Spine、队列和 DCQCN。
实现位于 `expert_parallel.py`，无需重编译网络后端，也没有改动 vendored 第三方源码。
此接入直接复用网络点对点接口；不调用上游 Vidur 的 EP 推理预测，也不把原 TP Ring AllReduce 改名为 EP。

KV 调度可选 [MLU 分级策略](../kv_scheduler/MLU.md)：按 EP 阶段登记需求，利用计算空档发送 KV，必要时晋升紧急 KV，并通过可开关的真实主机/交换机优先级执行。原整调用保护保留在 `slo_slack` 对照中。

## 已实现范围

- 一个 placement 仍是一组 TP GPU，负责一个 attention 请求队列及其 KV 分片。EP 模式强制该 TP 组完全位于一台服务器。
- 多个同角色、同 TP 宽度的 placement 组成一个 EP 组，P/D 分组独立，所有 placement 必须且只能属于一个组。
- `rank_unit=gpu` 要求 TP1，每个 placement 是一张 GPU，EP 组内按 GPU rank 分配专家。旧 `rank_unit=tp_team` 保留同 TP 坐标组成通信组的规则，TP4×EP8 仍表示 32 GPU。
- 专家按连续 expert ID 等分给组内各 placement；P/D 可以使用不同 EP 规模，同一阶段的各模型单元须使用相同规模。主研究 256 专家，P/D 的 EP16 均为每 GPU 16 个；160 GPU 参考布局的 P-EP32、D-EP128 分别为每 GPU 8、2 个。
- 显式周期路由表为每个新 token 指定 top-k 个不同专家，支持非均匀路由；不强行使用等字节 AllToAll。
- 执行顺序为当前源 batch 的 dispatch 全部完成 → 各目标专家计算和节点内 TP → combine 全部完成 → 当前层就绪/下一步。
- 目标 TP team 的普通计算、专家计算和对应本地 TP barrier 使用同一 FIFO 执行通道。来自多个源的专家任务会排队，不能在同一组 GPU 上同时执行两份计算。
- 节点内 TP 仍执行原 ring AllReduce，并标为 `tp_allreduce`；EP 两阶段分别标为 `ep_dispatch`、`ep_combine`。底层网络将它们归为 collective 类流量。
- 支持五种 KV 模式、Chunked/合批 Prefill、Decode 连续批处理、P/D 独立前缀复用和原容量生命周期。
- 前缀命中只路由尚需计算的 query，完全命中仍计算最后一个 query；Decode 只路由每成员一个新 token，不按上下文长度放大发送量。
- 路由以请求自己的 token 游标索引，分块/组批方式不改变其专家分配；历史 token 不重新 dispatch。
- 活跃 EP 调用的 dispatch/combine 路径进入现有集合通信保护。在计算及队列等待期间持续预留这组资源，直到 combine 完成，属于偏保守的保护近似。

## 配置

可运行最小示例：`configs/ep_smoke.json`，8 GPU、4 台服务器、TP2、EP2、P/D 各一组；计算和请求明确标为 synthetic。

```bash
./inference --config joint_scheduling/configs/ep_smoke.json validate
./inference --config joint_scheduling/configs/ep_smoke.json run-baseline --output joint_scheduling/artifacts/ep-example
```

配置新增可选顶层段，旧配置默认关闭：

```json
"expert_parallel": {
  "enabled": true,
  "groups": [["p0", "p1"], ["d0", "d1"]]
}
```

当前 EP 支持 `round_robin` 和 `balanced_random` 静态绑定；主研究使用后者（P 轮询、D 每轮均衡随机、独立 `binding.seed=7`），详见 [绑定说明](../binding/README.md)。必须显式关闭 `binding.joint_selection_enabled`、`binding.prediction_enabled`。
绑定窗口可以与轮询组合。开启 EP 联合绑定或直接调用现有两种预测器均明确报错，不静默使用仅认识 TP 的估计。

计算文件必须为 schema 2；分阶段 profile 通过 `weights_bytes_per_gpu_by_phase` 与 `provenance.expert_parallel_sizes` 指定 P/D 独立权重及 EP 规模，账本按 GPU 角色扣减。旧 profile 不含这些字段时沿用同一权重和规模。新增 `expert_parallel` 元数据，完整示例为 `fixtures/ep_compute_profile.json`：

```json
"expert_parallel": {
  "activation_layout": "tp_sharded",
  "dense_compute_excludes_experts": true,
  "num_experts": 4,
  "top_k": 2,
  "hidden_size": 4096,
  "dispatch_element_bytes": 1,
  "combine_element_bytes": 2,
  "routing_pattern": [[0, 2], [1, 3], [0, 3], [1, 2]],
  "prefill_layer_ids": [0, 1],
  "decode_layers": 2,
  "expert_compute_seconds_per_assignment": {"prefill": 0.00002, "decode": 0.00005},
  "expert_tp_collectives_per_call": 1
}
```

`compute_seconds` 必须只包含 attention、普通 dense/shared 等未单独建模的计算，排除 routed expert 计算和全部显式通信。
专家计算默认是该 TP team 上每个 token-expert assignment 的耗时乘 assignment 数。
可选 `expert_compute_seconds_per_active_expert: {"prefill": ..., "decode": ...}` 增加实际访问的不同专家数对应的权重/启动成本，
执行与 SLO 估计使用同一公式；缺省为零，兼容旧 profile。这仍是需要通过敏感性分析考察的仿射假设。
`weights_bytes_per_gpu` 必须同时核算复制/分片的 dense 权重及本地专家权重，不能直接使用 dense Llama 的权重和计算文件。
开启 EP 时必须提供 EP 文件；EP 文件也不能以关闭 EP 的方式运行，避免漏算专家。

通信量按 assignment、hidden channels 和两阶段独立数据类型计算。先把 hidden channels 精确分片到 TP 坐标，再乘 token-expert assignment 数。
同一 GPU 上的专家路由无需发送；同服务器的不同 TP team 走配置指定的本地 GPU 端口或旧共享 FIFO，跨服务器走 RDMA。
例如 hidden=5、TP2 分成 3+2 个通道，不能分别向上取整出 6 个通道。
量化、打包、解包与路由内核耗时没有自动包含在元素字节数中，须纳入适当的计算 profile。

## 旧 128 GPU 布局（独立对照）

`configs/cluster_128gpu_tp4_ep8_8gpu_per_node.json` 固定以下布局，profile/workload 仍留空，待冻结目标 MoE 模型和实际负载：

| 项目 | 值 |
| --- | --- |
| 服务器 / GPU | 16 台 × 8 GPU = 128 GPU |
| P:D | 64:64 GPU，8:8 台服务器 |
| TP | 4，每节点两个 TP 实例，全部节点内 |
| P/D 请求队列数 | 各 16 |
| EP | 8 个 TP team/组，每组 32 GPU、跨 4 台服务器 |
| 模型组数 | P 2 组、D 2 组 |
| NIC | 每 GPU 独立 400 Gbps，节点合计 3.2 Tbps |

分组为 P `p0..p7` / `p8..p15`，D `d0..d7` / `d8..d15`。
跨节点 EP 与 P→D KV 可共享 NIC 和交换机链路，实际竞争还要求执行时间重叠。
主研究及该 EP 模板采用 `gpu_ports`：节点内 TP、同节点 EP 共用实际涉及的 GPU 发送/接收端口；不同 GPU 对或相反方向可以并发。同一端口预约排队，不复刻完整 NVSwitch 仲裁。旧 `shared_fifo` 行为保留。
原来的 2 GPU/节点、跨节点 TP 配置仍可作为另一种并行方案的对照。

## 输出与验证边界

`expert_parallel_summary.json` 始终记录开始/完成调用数、两阶段 payload 字节、跨节点字节、专家工作量、计算排队等待、截止时仍活跃的依赖。
`resolved_config.json` 与结果摘要明确标出 EP 执行模型。debug 的 `events.jsonl` 包含每次路由、专家任务与三种 collective 标签；essential 模式保留汇总并与 debug 使用相同执行逻辑。
网络端口的 `collective_bytes` 包含实际包头和重传，不能直接与 summary 中的 EP payload 字节相等。

此版本是可运行的实验模型，仍有以下边界：

- 每个源 batch 独立发起路由并等待自己的结果，没有全 EP 组同步组批的 NCCL AllToAll barrier；不能用它声称复现某个框架的完整 EP 调度。
- 周期路由表是显式输入，不是真实模型 router，也没有自动读取 Mooncake hash_ids 推导专家。
- Prefill 在指定 MoE 层插入专家阶段；Decode 的已有 profile 是整个 dense token-step，当前先执行这个 dense step，再逐次执行声明的 MoE 层，未建模逐层交错。
- 专家计算采用一阶线性模型；激活/路由 buffer 显存、通信 kernel 的 GPU 占用、DeepEP 的具体通信算法尚未建模。
- 联合选择和绑定预测尚未扩展至共享专家队列，因此被显式禁用。KV SLO 门控增加了孤立 EP 调用成本估计，但没有完整预测所有专家队列等待，不构成 SLO 保证。

回归覆盖整数路由与偏斜、奇数通道分片、五种 KV 模式、两种 Prefill 组批方式、独立 GPU 执行通道、前缀命中、Decode 新 token 计数、截止依赖、真实共享链路与记录模式等价。
合成回放验证接入和资源竞争，不代表 128 GPU 的真实承载率或策略收益。
