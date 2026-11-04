# EP 流量生成核验

本页保存 2026-11-03 **旧路由**的核验记录：直接发送模型的整数路由、字节、TP 分片和完成依赖一致，但与参考系统存在差异。2026-11-04 已修复主 profile 的分组约束，当前周期为 2240 token、每 token 4 个专家组；本页旧表和字节比例不作为新路由结果。部署依据、路由变更与小规模候选见 [部署调研](DEPLOYMENT_RESEARCH.md)。核验工具自身只读，不修改输入或仿真时序。

## 可重复执行

静态核验主配置，不启动 ns-3，也不执行完整 Azure 负载：

```bash
./inference --config joint_scheduling/configs/research/base.json audit-ep \
  --output joint_scheduling/artifacts/ep-audit-layout \
  --expert-groups 8 --max-groups-per-token 4
```

合成小样本的实际流与依赖核验：

```bash
./inference --config joint_scheduling/configs/ep_smoke.json audit-ep \
  --output joint_scheduling/artifacts/ep-audit-smoke --simulate
```

主模型的实际流核验使用短程验证结果中已冻结的 `config.json`，加相同的 `--simulate`。`configs/research/validation.json` 是实验规格，不能直接作为全局 `--config`；应选其某次 baseline 输出中的 `config.json`。`--simulate` 完整执行所选配置，没有自动截短负载。当前实际流核验要求前缀缓存关闭，以便从现有事件独立恢复 token 游标；静态核验不受这个限制。

输出包含 `audit_config.json`、`compute_profile.json` 和 `audit_summary.json`；实际运行另存 `run/`，沿用现有的源码、输入和后端身份归档。已有目录拒绝覆盖。`layout.status` 与可选的 `routing_constraint.status` 分别说明实现一致性、参考 gate 约束是否匹配；约束不匹配作为模型差异报告，不修改路由，也不冒充实现错误。路由参考参数必须成对提供，不会自动把任何模型都当作 DeepSeek-V3。

## 核验方法与结果

`experiments/ep_reference.py` 按 token 枚举专家、目标 rank 和物理节点，不复用生产代码的周期计数及流量公式。它对照生产函数 `ep_flows` 检查：每 token 的 top-k assignment 数、目标映射、各 TP 坐标通道数、自 rank 不发流、dispatch/combine 方向及元素字节数。

旧主配置涵盖全部 160 个源 rank，每源检查 4 组 token 长度/游标，共 640 组、1280 个阶段。新周期仍按这些源/游标检查；独立测试另外覆盖同 token 命中同 rank 多个专家、奇数 hidden/TP 分片，以及不同 batch 成员的 token 身份。

`experiments/ep_audit.py` 通过已有只读 `RuntimeHooks` 观察实际执行，不增加仿真事件。检查每个计算步骤的 token 游标、MoE 层顺序、每条实际流的端点/字节/提交时间、全部 dispatch ACK 后才开始专家计算、全部专家完成后才开始 combine，以及 combine ACK 后才结束调用。截止时允许未完成调用，但所有已开始阶段必须提交齐其声明的流，未开始的 combine 不计入已发送字节。

160 GPU、Azure 首秒的 1 个请求（输入 374、输出 44）、5 秒仿真中，共检查 2552 次 EP 调用、43268 条 EP 逻辑流，全部完成且核验通过。P 为 58 次调用，D 为 43×58=2494 次；assignment 分别为 173536 和 19952。此结果只验证该输入的流量与依赖，不说明持续负载的网络压力或策略收益。

测试还故意注入双倍流量、错误路由计数、错误实际流字节、提前计算/合并、虚假 ACK 就绪和截止时漏提交流，要求核验失败。Dense/MoE 的原冻结结果哈希继续通过。

## 与参考系统的差异

### 1. 普通 DeepEP 和低延迟 DeepEP 的字节口径不同

参考 DeepEP v1.2.1，固定提交 `9af0e0d0e74f3577af1979c9b9e1ac2cad0104ee`：

- [layout.cu L73–94](https://github.com/deepseek-ai/DeepEP/blob/9af0e0d0e74f3577af1979c9b9e1ac2cad0104ee/csrc/kernels/layout.cu#L73-L94) 分别统计按 token 去重的 rank 和 RDMA 域计数。普通跨节点路径使用节点聚合及转发；[combine 转发阶段](https://github.com/deepseek-ai/DeepEP/blob/9af0e0d0e74f3577af1979c9b9e1ac2cad0104ee/csrc/kernels/internode.cu#L1618-L1702) 在发出 RDMA 前归并节点内贡献。
- [internode_ll.cu L152–174](https://github.com/deepseek-ai/DeepEP/blob/9af0e0d0e74f3577af1979c9b9e1ac2cad0104ee/csrc/kernels/internode_ll.cu#L152-L174) 的低延迟发送按目标专家处理。因此不能把全部 EP 路径都改成按 rank 去重。当前模型接近按专家 assignment 发送的激活数据量，但将同端点的数据聚合成逻辑流，不复现其 kernel、QP 或发送流水。

保持 **2026-11-03 的旧路由不变**，只比较激活 payload：

| 阶段 | 当前平均跨节点激活份数/token | 每远端节点一份的参考 | 当前/参考 |
| --- | ---: | ---: | ---: |
| P-EP32 | 6 | 3 | 2 |
| D-EP128 | 7.5 | 7.5 | 1 |

这是所有源 rank 和完整 256-token 路由周期的均匀平均。P 每 token 在每个远端节点命中两个 rank，当前分别发送；D 的当前周期表没有该重复。当前主周期表也没有同 token 命中同 rank 的重复，因而单纯按 rank 去重不会改变其激活字节。

实际 Azure 单请求中，P 的跨节点 dispatch 为 932929536 B，每节点一份的参考为 466464768 B；D 分别为 135948288 B 和 135948288 B。combine 按本 profile 的 BF16 元素计为对应 dispatch 的两倍。这些数值不含参考系统的额外元数据，也没有模拟转发端点、局部 NVLink 流量及流水等待；**2 倍只表示此路由下的 P 跨节点激活字节比例，不能解释为时延比例或实测高估倍数。**

### 2. 当前周期路由不满足 DeepSeek-V3 的分组 gate 约束

DeepSeek-V3 官方 [671B 配置](https://github.com/deepseek-ai/DeepSeek-V3/blob/9b4e9788e4a3a731f7567338ed15d3ec549ce03b/inference/configs/config_671B.json) 声明 8 个专家组、每 token 最多选择 4 组；[Gate 实现](https://github.com/deepseek-ai/DeepSeek-V3/blob/9b4e9788e4a3a731f7567338ed15d3ec549ce03b/inference/model.py#L584-L593) 先做组筛选再选专家。这里的专家组与物理节点不是同一概念。

旧周期表首行为 `[0,32,64,96,128,160,192,224]`。按每组连续 32 个专家计算，每行均访问 8 组，256/256 行都超出该参考限制。2026-11-04 的主 profile 已改为匹配约束的 2240-token 均衡周期，并在加载器中增加守卫；学习式 gate 评分仍未建模。修正路由之后，前述字节比例需要重算。

### 3. 激活 payload 不等于 DeepEP 实际消息字节与执行时序

当前 `dispatch_element_bytes=1` / `combine_element_bytes=2` 只表示激活元素。参考低延迟 FP8 dispatch 的[消息定义](https://github.com/deepseek-ai/DeepEP/blob/9af0e0d0e74f3577af1979c9b9e1ac2cad0104ee/csrc/kernels/internode_ll.cu#L69-L84) 还包含源索引/保留字段和 scale。hidden=7168 时，其该记录为 7408 B，而纯激活为 7168 B，多出的 240 B 尚未包含同步控制和网络包头。ns-3 已建模的包头、ACK 等也不能当成上述 kernel 元数据已被计入。

当前源 batch 独立执行，Decode 先执行聚合 dense step，再执行 58 个 MoE 层；没有复现普通路径的节点转发流水、逐层 dense/MoE 交错或双 micro-batch。通过当前依赖检查只说明符合这些声明的时序。

## 使用边界

现有模型可继续用于明确标注的直接 assignment 流量实验。gate 分组约束现已修复；普通 DeepEP 的 P 侧节点聚合转发仍需独立对照，再检查 KV/EP 干扰和策略排序是否稳定。旧运行保留其冻结输入；新 profile、流量拓扑或时序应重新运行结果。
