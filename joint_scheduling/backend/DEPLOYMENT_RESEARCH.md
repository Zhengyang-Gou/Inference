# DeepSeek-V3 真实部署调研与缩规模方案

核对日期：2026-11-04。研究对象保持 DeepSeek-V3 671B、FP8 权重估算和 H800 80 GiB 容量口径。此次修正路由分组约束，提供小规模候选。后续已按用户选择将 32 GPU、P16/D16 设为主研究入口；160 GPU 保存在 `base_160gpu.json` 作为原参考布局。下文原部署和验证记录保留各自规模口径。

## 公开部署依据

| 资料 | Prefill | Decode | 解释 |
| --- | --- | --- | --- |
| DeepSeek-V3 技术报告 §3.4 | Attention TP4＋DP8，共 32 GPU；MoE EP32 | Attention TP4＋DP80，共 320 GPU；MoE EP320，含冗余和 shared expert 部署 | 原技术报告的部署方案；Attention 与 MoE 对同一批 GPU 采用不同分组 |
| 官方 profile-data | TP1／EP32 | TP1／EP128 | 官方说明与其在线部署相符或接近；是当前代价锚点的来源 |

技术报告还包含冗余专家、节点转发和双 micro-batch；不能只摘取 EP320，再套用当前固定 256 专家均分的执行器。[DeepSeek-V3 技术报告](https://arxiv.org/html/2412.19437v2#S3.SS4)。官方 profiling 则使用平衡路由并包含两微批重叠，其并行配置有明确出处，但不是任意请求率下的最小部署或最佳 P:D 比例。[固定版本 profile-data](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md)。

SGLang 的 DPA 在一个模型并行组内部划分 Attention 数据并行，EP 同时分配该组的专家。因此其 `--tp` 不能直接当成本仓库 `phases.*.tp_size`。例如一个 16 GPU 组用 `--tp 16 --attn-dp-size 16 --ep 16` 表示 Attention TP1／DP16、专家 EP16 的分组口径；这些宽度使用同一组 16 张卡。DeepEP 后端目前要求 EP 宽度等于引擎的 TP/world 组宽度。[DPA 参数说明](https://github.com/sgl-project/sglang/blob/main/docs/docs/advanced_features/dp_dpa_smg_guide.mdx)、[EP 参数约束](https://docs.sglang.io/docs/advanced_features/expert_parallelism)。这只是并行语义映射，当前仿真没有复现整个 SGLang 执行过程。

SGLang 的 DeepSeek-V3 部署文档列出 FP8 的 H100/H800 建议规模为 2×8 GPU；8×H200 的容量条件不同。更小的低比特方案需要更换量化和代价模型，不能按当前 FP8 profile 直接解释。[官方部署文档](https://github.com/sgl-project/sglang/blob/main/docs/cookbook/autoregressive/DeepSeek/DeepSeek-V3.mdx)。

## 本仓库的 TP、DP、EP

原 160 GPU 参考布局在 `rank_unit=gpu`、TP1 下：

- P 的 32 张 GPU 同时形成 Attention DP32 和 Expert EP32；每卡 8 个 routed experts。
- D 的 128 张 GPU 同时形成 Attention DP128 和 Expert EP128；每卡 2 个 routed experts。
- P/D 是两个独立的完整模型池，总计 32＋128＝160 GPU。Attention DP 和 EP 不相乘，也不额外产生一份 DP 大流量。

这组数字本身成立。P:D＝1:4 是高吞吐参考取舍，是否适合本研究负载需要测量。旧 `rank_unit=tp_team` 中，一个 EP rank 是 TP team，`TP4×EP8` 表示 32 张 GPU；这与技术报告中的“Attention TP4、MoE EP32 个 GPU rank”不能直接互换。本执行器还没有独立设置 Attention TP 和 Expert TP 的完整布局接口。

## 路由约束修正

生成器现在从模型配置读取并核对 `n_group=8`、`topk_group=4`、256 experts、top-k＝8。profile 导出这两个约束，加载器拒绝缺一项、非法组数或跨越过多组的路由行。官方 gate 的分组约束来源见 [671B 配置](https://github.com/deepseek-ai/DeepSeek-V3/blob/9b4e9788e4a3a731f7567338ed15d3ec549ce03b/inference/configs/config_671B.json) 和 [Gate 实现](https://github.com/deepseek-ai/DeepSeek-V3/blob/9b4e9788e4a3a731f7567338ed15d3ec549ce03b/inference/model.py#L584-L593)。

新的确定性合成路由覆盖全部 70 种四组组合，每组每次选 2 个不同专家；组内位置轮换，周期为 2240 token，每个专家在完整周期恰好有 70 次 assignment。互补组交错排列，使偶数长度的请求前缀也具有平衡的组需求，避免生成顺序制造固定组热点。计数器预先编译完整周期计数，单 token Decode 不再扫描整个周期。

这与真实 gate 的学习评分仍有区别：真实 top-8 不要求每个入选组恰好 2 个专家，也可能只使用少于 4 组。该路由是满足官方约束的均衡实验输入，尚未具备请求、层和数据相关的真实热点。专家组也不等于物理节点，EP 和放置改变后应重新计算节点扇出。

已更新主 TP1／EP32/128、旧 TP4／EP8 及两个小规模 profile；计算曲线、专家时延系数、KV 字节和同布局权重估计保持原值。所有引用新主 profile 的研究规格更新了输入 SHA-256。旧运行保持自身冻结输入身份，后续曲线使用新 profile 成套重跑；此前“P 流量恰好为节点参考的两倍”是旧路由的结论，需要重新核验。

## 可缩到多小

以下为当前模型的每卡权重，按 80 GiB 容量、预留 5% 后的 76 GiB 预算计算：

| Attention TP | Expert EP（本仓库 team 口径） | 单池 GPU | 权重/卡 | 扣除安全预留和权重后的余量 |
| --- | --- | ---: | ---: | ---: |
| 1 | 8 | 8 | 93.72 GiB | −17.72 GiB，不满足容量 |
| 1 | 16 | 16 | 55.65 GiB | 20.35 GiB |
| 1 | 32 | 32 | 36.61 GiB | 39.39 GiB |
| 1 | 128 | 128 | 22.33 GiB | 53.67 GiB |
| 2 | 8 | 16 | 46.86 GiB | 29.14 GiB |
| 4 | 4 | 16 | 42.47 GiB | 33.53 GiB |

这些是仓库的容量推导，余量供其 KV／预留模型使用；activation、kernel workspace 和 DeepEP buffer 尚未逐项建模。TP1 下，每池至少 16 卡的候选较为合理。TP2/TP4 可以降低复制权重，但引入 TP 通信及不同的专家分片语义，不作为此次缩规模的第一步。

| 方案 | P | D | 总 GPU | 权重/卡 P／D | 用途 |
| --- | --- | --- | ---: | --- | --- |
| 原参考 | TP1、DP32、EP32，4 节点 | TP1、DP128、EP128，16 节点 | 160 | 36.61／22.33 GiB | 保留原公开 profiling 布局参考 |
| 小规模对照 | TP1、DP16、EP16，2 节点 | TP1、DP32、EP32，4 节点 | 48 | 55.65／36.61 GiB | 在 D 侧保留更多并发容量 |
| 首选机制验证起点 | TP1、DP16、EP16，2 节点 | TP1、DP16、EP16，2 节点 | 32 | 55.65／55.65 GiB | 在完整模型和跨节点 EP 下减少闲置资源 |

每节点仍是 8 GPU、每 GPU 400 Gbps NIC、节点内 TX/RX 各 160 GB/s；保留 1:1 上下联总容量、包大小、SLO 和调度参数。缩规模同时改变了专家放置和本地/跨节点比例，因此不能将其效果只解释为“GPU 数减少”。

## 候选配置与实验入口

`configs/research/base.json` 已采用 32 GPU、P16/D16，与显式的 `base_32gpu.json` 一致。`kv_policy.json`、`network_priority.json`、`validation.json` 和 `contention_diagnostic.json` 随之切换；原规格保存在对应的 `*_160gpu.json`。`base_48gpu.json` 以及匹配的 `ep16_ep32` profile 仍供规模对照，两个显式规模均保留 `validation_*gpu.json` 和 `kv_policy_*gpu.json`。每个规格内部固定输入、布局、profile 和绑定种子。

```bash
./inference run-experiments --spec joint_scheduling/configs/research/validation_32gpu.json --output joint_scheduling/artifacts/research/validation-32gpu --jobs 2
./inference run-experiments --spec joint_scheduling/configs/research/kv_policy_32gpu.json --output joint_scheduling/artifacts/research/kv-policy-32gpu --dry-run
./inference --config joint_scheduling/configs/research/base_32gpu.json audit-ep --output joint_scheduling/artifacts/research/audit-32gpu --expert-groups 8 --max-groups-per-token 4
```

短程验证只包含 Azure 原轨迹首秒的 1 个请求。四请求的网络竞争诊断另有 `contention_32gpu.json`，使用原有固定流量 both／EP only／KV only 重放，截止时间延长到 15 秒用于排空；该片段的实际注入率约 19.65 rps，不是持续 128 rps。

```bash
./inference run-experiments --spec joint_scheduling/configs/research/contention_32gpu.json --output joint_scheduling/artifacts/research/contention-32gpu --fail-fast
```

## 本次实际验证

- 4 个 DeepSeek-V3 profile 的 2240 行路由均满足分组约束，组数超限为 0；16 个研究规格完成输入身份与布局预检。
- 更新后 160 GPU 的 Immediate／FIFO／MLU 三种策略均完成 Azure 单请求验证；每次请求完整完成且无失败。
- 32 和 48 GPU 的只读实际流核验分别检查 2552 次 EP 调用、29696／34800 条 EP 逻辑流，全部完成，无活动调用或在途流残留。该请求输入 374、输出 44 token，验证范围为输入接通和执行依赖。
- 32 GPU 的四请求诊断完成 1 次端到端运行和 3 次固定流量重放，涉及 81389 条跨节点逻辑流；both 重放与原运行的 ACK 完成时刻误差为 0 ns。
- 完整 `check.sh` 的 277 项测试与后端 smoke 全部通过，无失败或跳过；原 Dense/MoE 冻结 fixture 的 token、资源与汇总哈希继续通过。

| 四请求固定重放的流类别 | 流数 | 加入另一类流量的最大附加时延 | 增加超过 1 μs 的流数 |
| --- | ---: | ---: | ---: |
| EP | 81084 | 0.254 μs | 0 |
| KV | 305 | 0.288 μs | 0 |

这里采用无门控逐层立即 KV、网络优先级关闭。即使缩到 32 GPU，这个短片段的 KV／EP 相互干扰仍然很弱；不能据此宣称规模问题已解决，或将冻结重放的流时延当作请求 SLO 收益。EP 自身的排队和 KV 对 EP 的额外干扰也是不同指标。

## 如何判断是否适合验证调度算法

先在 32 GPU 上检查 EP 与 KV 在同一有向端口上的时间重叠、队列和隔离重放时延差，再用 48 GPU 做规模对照。请求积压、专家 FIFO 等待和 GPU 忙碌升高，只能说明系统受压；需要上述网络证据才能认定存在网络竞争。

随后固定布局、量化和网络参数，用相同的成对长度与到达序列逐级加压，覆盖低负载、临界负载和过载。持续窗口中同时报告 SLO 达标吞吐、EP 延迟、KV 交接、积压与未完成请求。若先遇到计算/显存瓶颈，应如实记录，并检查 EP/KV 隔离差，不通过任意修改某个策略的带宽或流量来制造收益。

需要复现普通 DeepEP 的性能时，P 侧节点聚合转发、GPU 通信开销及两微批重叠仍需单独扩展。此次解决的是官方分组约束和可运行的小规模候选。
