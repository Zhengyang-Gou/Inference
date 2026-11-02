# 无本地 GPU 的性能数据验证

本项目用公开算子测量约束计算模型，用 ns-3 执行共享网络通信。
公开数据验证包括测量来源核查、分组留出预测和参数敏感性分析；它不能替代目标引擎或真实128GPU集群的端到端实测。

## 固定有数据支持的组合

公开数据当前对应 Meta-Llama-3-70B、A100、FP16、TP4。新实验可以明确选择同名模型，
保留旧Llama3.1代理组合的兼容默认值：

```bash
./inference build-public-profile \
  --mlp /path/to/mlp.csv --attention /path/to/attention.csv \
  --source-commit VIDUR_COMMIT --tp 4 --prefill-chunk-tokens 512 \
  --target-model meta-llama/Meta-Llama-3-70B --output /path/to/new-profile.json
```

选择同名模型只消除跨模型转移假设，不会把算子组合升级为真实整引擎测量。
CSV标记的A100型号与仿真配置的80GiB显存分开记录；GPU具体形态和互联需另有证据才能视为匹配。

## 留出验证

```bash
./scripts/simai-python -m joint_scheduling.backend.profile_validation \
  --mlp /path/to/mlp.csv --attention /path/to/attention.csv \
  --tp-size 4 --folds 5 --output /path/to/new-validation
```

- 同一坐标的重复测量先聚合，避免重复样本分居训练与验证两侧。
- 各条曲线的端点始终保留，内部坐标分5折，每个内部点恰好留出一次。
- 使用与生产配置生成器相同的区间内线性插值；测试点不进入对应训练曲线。
- 分别报告层算子、embedding、零上下文Prefill、Decode上下文/批量轴、带历史Prefill的上下文/query轴。
- 输出每个测试点的观测、预测、绝对毫秒误差和相对误差，以及训练坐标哈希。
- MAPE/P95按测量族分别报告，不赋予整引擎一个全局“准确率”。零观测值没有相对误差分母，另行计数。
- 只有1/2个坐标的曲线不满足内部留出条件，保留不可验证的覆盖统计。
- 短上下文锚定、混合长度批次、两轴同时缺失的组合预测，不因单轴留出通过而得到验证。另有组合代理的双轴留出检查，仍不能验证缺失的最终投影/执行开销。

## 双轴组合代理留出

CLI还输出 `composed_grid_predictions.csv` 与对应摘要：同时留出完整的query/batch坐标和已有上下文坐标，
并从线性层、embedding训练曲线中移除对应query坐标。5×5折使每个符合条件的内部坐标对只测试一次。
参考值来自有直接测量的算子组合（80层加一次embedding），不是测得的整前向。
缺少直接线性层/embedding测量的组合无法建立独立的组合参考；保留训练曲线不能覆盖的点也不外推。
两类跳过原因和数量均明确输出，不能把跳过点算成验证通过。
该检验仍未验证异长批次padding模型或真实引擎调度。

## 公开端到端结果交叉核查

只有模型、精度、GPU/互联、TP、引擎/执行优化、批量/长度、指标口径足够一致时，才可比较绝对时延并解释差异。
算子数据本身的留出检验不是独立来源验证。同一模型名称、GPU数量相同，也不足以把另一套服务系统的TPOT拟合为本系统的计算时延。
未找到严格可比测量时，记录不匹配项，保留未验证状态，不引入无依据修正系数。

## 敏感性实验

`experiments.sensitivity.perturb_profile` 支持统一计算时延倍率和每个P块/D批的串行附加开销。
它同时变换完整Prefill、cached points、chunked points和Decode批次曲线，保持KV字节、权重和集合通信消息大小/次数声明不变。
额外开销借用原有计算片段计时，是固定串行成本代理，并非CPU异步引擎的精细模拟。
链路带宽倍率由实验配置统一施加；时延/队列缓冲等其他网络参数固定。

对三种KV条件使用相同工作负载、模型文件和SLO，逐项改变计算、带宽和开销。
所有请求、token和逻辑KV必须守恒，截止时未完成者必须保留并计入SLO判定。
扰动幅度是预设分析范围，不是数据已证实的误差界或置信区间；单因素结果不覆盖多因素交互。

筛选或压缩后的有限诊断负载只支持相应场景结论，不能标记为生产容量。
