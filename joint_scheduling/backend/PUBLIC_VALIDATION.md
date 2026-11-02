# 仿真计算模型的离线验证

本项目用公开离线算子数据约束计算代价，用 ns-3 执行共享网络通信。验证包括数据来源、覆盖范围、留出预测误差、请求/字节守恒和仿真敏感性，全部在 CPU 上完成。

## 固定输入与模型口径

Dense 数据代理保留 Meta-Llama-3-70B / A100 / FP16 / TP4 组合，同时支持旧 Llama-3.1 代理名称。MoE 主线使用 DeepSeek-V3 / H800 / TP4 / EP8 的估算仿真 profile，见 [MOE_PROFILE.md](MOE_PROFILE.md)。设备名称是代价模型标签，显存与互联参数由仿真配置单独定义。

```bash
./inference build-public-profile \
  --mlp /path/to/mlp.csv --attention /path/to/attention.csv \
  --source-commit VIDUR_COMMIT --tp 4 --prefill-chunk-tokens 512 \
  --target-model meta-llama/Meta-Llama-3-70B --output /path/to/new-profile.json
```

输入文件的来源、哈希、单位和模型形状随 profile 保存。算子组合、插值与外推网格均标明为仿真假设。

## 留出验证

```bash
./scripts/simai-python -m joint_scheduling.backend.profile_validation \
  --mlp /path/to/mlp.csv --attention /path/to/attention.csv \
  --tp-size 4 --folds 5 --output /path/to/new-validation
```

- 重复坐标先聚合，避免同一坐标分居训练与验证两侧。
- 端点保留，内部坐标分折，每个符合条件的内部点恰好留出一次。
- 使用与仿真 profile 生成器相同的区间内线性插值，测试点不进入对应训练曲线。
- 分别报告线性层、embedding、Prefill 与 Decode 的长度/批量轴，以及带历史 Prefill 的上下文/query 轴。
- 保存观测、预测、绝对误差、相对误差与训练坐标哈希；零观测值不计算相对误差。
- 数据点不足、缺少直接算子组合或不能覆盖的点单独列出，不计为验证通过。

同时留出 query/batch 和已有上下文坐标的双轴组合检验，保留在 `composed_grid_predictions.csv`。组合参考为各层算子加一次 embedding；单轴或双轴检验只解释对应插值模型，不自动验证异长批次和未建模成本。

## 敏感性实验

`experiments.sensitivity.perturb_profile` 支持统一计算时延倍率，以及每个 P 块/D 批的串行附加开销。它变换完整 Prefill、cached points、chunked points 和 Decode 曲线，保持 KV、权重和通信消息大小/次数声明不变。

带宽、时延、缓冲、容量和组批参数作为独立仿真变量；同一档位的各策略使用相同请求、profile 和 SLO。检查请求、token、逻辑 KV 字节和资源释放守恒，保留截止未完成样本。

扰动幅度是实验定义的分析范围，不是置信区间。有限片段只支持对应仿真场景；持续负载承载率由完整窗口内的积压和 SLO 结果评估。
