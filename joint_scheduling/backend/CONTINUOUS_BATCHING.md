# Decode 连续批处理

研究用 MoE profile 现在提供显式逐层 Decode 曲线。一个 batch 的成员及上下文在本 token 步骤内固定，逐层推进计算、TP 和对应 MoE，全部层完成后统一返回各成员 token，再决定下一轮成员。`decode_batch_summary.json` 记录执行语义和截止时的层进度；旧聚合 profile 的执行保持兼容。

P/D 保持分离，Prefill 默认单请求 FIFO，也可开启 [token 分块轮转](CHUNKED_PREFILL.md)。Decode 每个实例在每轮开始时从就绪队列取至多
`runtime.decode_batch_size` 个请求，执行一次共同前向；每个成员生成一个 token。
只有 Prefill 完成且全部输入 KV 实际收到的请求才能加入。轮次中到达的请求等待下一轮，
本轮完成后未结束的成员回到队尾，完成成员释放各自 KV 和 Decode admission 槽位。
没有为了凑满 batch 而额外等待。批量上限是每个 D 实例的上限。

旧配置默认 `decode_batch_size: 1`，保持原有单请求轮转。设置为 32 可启用连续批处理，
三种 KV 模式均可使用相同批量上限。旧的 `decode_admission_limit: 3` 会将并发限制到 3，
比较连续批处理效果时应明确将它设为 0，避免沿用此前保护串行 Decode 的实验设置。

## 时延与通信

计算配置的 `phases.decode` 保留单请求 `points`，增加 `batches`：

```json
{"batch_size": 4, "points": [
  {"tokens": 128, "compute_seconds": 0.028, "collective_bytes": 65536},
  {"tokens": 1024, "compute_seconds": 0.030, "collective_bytes": 65536}
]}
```

这里是字段示意，使用示意数值。`tokens` 表示单序列上下文长度，`compute_seconds` 是整个
batch 的计算时间，`collective_bytes` 是一次集合通信的整个 batch 消息大小。
`collectives_per_step` 在每个 batch 执行一次，不能再乘成员数。
`batches` 必须包含与原 `points` 完全相同的 batch=1 曲线，并按批量递增。

异长序列目前采用最大上下文 padding 模型；先在上下文轴插值，再在批量轴插值，不做范围外推。
启动前检查所设最大批量及完整请求上下文范围，旧的单请求配置无法自动推导多请求时延。
批次计算完成后仍逐次等待 ns-3 中真实完成的 AllReduce，再返回该轮 token。
KV 按请求分别预留、增长和释放；模型尚未额外核算 batch 激活/临时工作区显存。

公开 Vidur 数据生成器现支持按已有 batch 样本选 attention 算子，并按 batch token 数选线性层和
embedding 算子，仍是 A100 上匹配形状的算子组合估算，不是实际部署的端到端吞吐测量。
原有低上下文锚定、缺少最终词表投影等估算限制保留在 profile provenance 中。

```bash
./scripts/simai-python -m joint_scheduling.backend.public_profile \
  --mlp /path/to/mlp.csv --attention /path/to/attention.csv \
  --source-commit VIDUR_COMMIT --tp-size 4 --output /path/to/batch_profile.json
```

将 `feedback.compute_profile_path` 指向生成文件，并设置 `runtime.decode_batch_size`。
`binding.prediction_enabled` 与 `binding.joint_selection_enabled` 可同时使用。绑定快照保存活动批次的全部成员，
预测按就绪队列组成连续批次，调用同一批量/上下文时延曲线，共享计算和集合通信各计一次。
新增绑定会重新计算已有请求的交接与后续 ITL；延迟快照中的活动批次从可见进度保守续算。
在线评分受确定性工作预算约束，批处理预测不保证达到原单请求预测器的墙钟开销目标。
KV SLO 的排队预算已按批次估算；活动批次的预算保守计入一整个批次时长。

## 记录与验证

每次运行都输出 `decode_batch_summary.json`，记录启动/完成批次数、完成批量分布及截止时仍在
执行的成员。debug 记录模式额外保留 `decode_batch_started/completed` 及批次成员、上下文长度，
共享通信按批次首成员归属，可通过对应批次成员列表追溯。

测试覆盖异长上下文与批量插值、缺少覆盖报错、轮次间加入和退出、逐请求 token 完整性、
批次共享通信次数，以及三种 KV 模式下所有请求完成后的显存释放。
