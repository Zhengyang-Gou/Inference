# Prefill 按 token 分块

设置 `runtime.prefill_chunk_tokens: 512` 后，每个 P 实例一次处理一个请求的至多512个新 token。
每个块依序经过全部 Transformer 层，再在块边界把未完成请求放回队尾。
已有请求保留其 KV、预留和传输 pin；新请求可以在下一块开始前得到执行机会。
上述默认模式实现 P 池内部的分块轮转，仍是一轮一个 Prefill 请求。另可开启 [token预算多请求合批](PREFILL_BATCHING.md)，一次前向处理多个请求。
D 池可以同时开启 `runtime.decode_batch_size: 32`。P/D 仍分离。

首 token 只在最后一个块的全部层与集合通信完成后返回。D 仍等待该请求完整输入 KV 实际到达。
对于逐层/受控 KV 模式，每个 token 块的每层完成后即可发送对应新增 KV；
whole_ready_immediate 仍等完整 Prefill 完成才发送整份 KV，whole_ready_chunked 等待完成再发送累积块。
层级完整就绪事件仅在最终 token 块对应层完成后产生，块内层就绪使用独立事件。

## 计算数据

后续块的 attention 时延与已处理上下文有关。不能把完整 Prefill 时间按 token 数简单等分，
也不能每个块都按零历史上下文查询。schema 2 的 `phases.prefill.chunked` 增加以下共享层采样：

```json
{
  "collectives_per_layer": 2,
  "groups": [
    {"chunk_tokens": 512, "points": [
      {"cached_tokens": 0, "compute_seconds": 0.001, "embedding_seconds": 0.0001, "collective_bytes": 8388608},
      {"cached_tokens": 4096, "compute_seconds": 0.002, "embedding_seconds": 0.0001, "collective_bytes": 8388608}
    ]}
  ]
}
```

示例仅演示字段，不是测量结果。compute_seconds 是一层计算，不含显式网络通信；
embedding_seconds 仅加在每个块的第一层。当前 chunked profile 明确使用同构层时延，
各层 KV 字节布局仍读取原 layers。按 cached context 和 chunk token 两个轴插值，越界报错。
完整工作负载的每个块在运行前检查覆盖，不能用旧单请求 profile 自动估计块时延。

公开 Vidur 导出器新增 `--prefill-chunk-tokens 512`。它用 batch=1、is_prefill=True 下不同
query chunk 和 kv_cache_size 的实测 attention 算子组合，线性层和 embedding 只处理新 token。
短于最小带历史上下文测量的块，attention 锚定到最小实测 query（当前 A100 数据是64），
线性层仍按新 token 数取样。该保守估算和来源均写入 provenance；不能当作完整引擎端到端实测。

```bash
./scripts/simai-python -m joint_scheduling.backend.public_profile \
  --mlp /path/to/mlp.csv --attention /path/to/attention.csv \
  --source-commit VIDUR_COMMIT --tp-size 4 --prefill-chunk-tokens 512 \
  --output /path/to/chunk_batch_profile.json
```

关闭时使用 `prefill_chunk_tokens: 0`，恢复整请求 FIFO。现有配置默认0。
chunk 可与自有 prefix_cache、绑定预测和联合选择同时开启。缓存命中初始化已处理 token 游标，
完整命中仍重算最后一个查询；每层只物化和发送本 chunk 与未命中前缀段相交的区间。
P/D 独立命中，P 已命中而 D 缺失的 KV 只提交一次，不随后续 chunk 重复提交。
绑定预测读取延迟可见的 token 游标、当前 chunk 和批次成员，按块边界轮转或 token 预算合批，
详见 [绑定模块](../binding/README.md)。KV SLO 的 Prefill 剩余工作预算包含后续块，并保守计入
同一个 P 上当前可见的其他未完成 Prefill 工作；不读取未来到达。

## 容量与记录

按整请求预留输入 KV 和输出增长容量，按各块实际完成增量转为占用。
分片范围采用累计前缀的 rank 分配差值，避免每块独立向上取整造成预留溢出。
新队首因容量不足无法开始时，允许已持有容量的部分完成请求继续执行并排空。
仍未实现真正的分页块分配、KV 逐页增长及显存抢占/重计算。

`prefill_chunk_summary.json` 始终输出启动/完成块数与已完成新 token 数。
debug 模式额外输出 prefill_chunk_started/completed、prefill_chunk_layer_ready 及计算上下文。
运行截止在块内时保留请求和资源，不提前返回首 token，也不伪造完成。

测试覆盖历史上下文开销、末尾不足整块、奇数大小的 rank 分片、五种 KV 模式、
短请求越过长请求、D 完整 KV 依赖、容量不足时的继续执行与块内截止。
