# 前缀缓存与 P/D 独立复用

`prefix_cache` 管理仿真元数据，不生成真实 KV 张量。模块负责最长已驻留前缀查询、对象位置/就绪状态、共享引用与有界 LRU 淘汰；显存核算复用 `reservations.CapacityLedger`。已接入五种 KV 模式和整体方案预测器，P/D 绑定可选轮询或 binding 内的增量贪心；预测器在延迟可见快照的克隆缓存上调用纯查询，不改变真实 LRU/统计，联合选择提交前验证采样对象仍有效，并原子添加共享引用。

## 配置

```json
"prefix_cache": {
  "enabled": true,
  "prefill_reuse_enabled": true,
  "decode_reuse_enabled": true,
  "max_bytes_per_instance": 67108864,
  "eviction_enabled": true
}
```

省略本段等价于 `enabled=false`，其余默认值如上。关闭后不分割共享对象、不查询或保留缓存，计算与 KV 传输恢复完整请求行为。P/D 复用开关独立控制本侧命中，仍可保留新缓存。预算是每实例所有 rank 的缓存字节总和，活动条目也计入。关闭淘汰后已有缓存保留，新条目放不下时跳过保留；真实显存不足的请求仍需等待。

## 请求身份

请求可增加有序 `prefix_blocks`：

```json
{
  "request_id": "r0",
  "arrival_seconds": 0,
  "input_tokens": 192,
  "output_tokens": 3,
  "prefix_blocks": [
    {"id": "system-v1", "tokens": 64},
    {"id": "document-v1", "tokens": 64}
  ]
}
```

这些块描述输入开头连续的缓存段，总长度不得超过输入。同 ID 声明相同内容，必须具有相同长度。键由 profile SHA256 与完整有序上文链生成；相同块 ID 出现在不同上文下不会误命中。ID 是负载提供的内容身份，不校验真实 token；真实 trace 应使用包含模型与分词语义的内容身份。当前每次运行固定一个 profile/model，不跨 profile 迁移缓存。

负载生成器的请求类别也支持 `prefix_blocks`，生成结果会经过运行器的身份与长度校验。

## 计算样本

开启缓存要求 schema 2。每层可增加 `cached_points`：

```json
"cached_points": [
  {
    "cached_tokens": 64,
    "points": [
      {"tokens": 128, "compute_seconds": 0.00008, "collective_bytes": 8192},
      {"tokens": 192, "compute_seconds": 0.00015, "collective_bytes": 16384}
    ]
  }
]
```

`tokens` 是完整输入长度，`cached_tokens` 是缓存计算上下文长度，必须小于输入。集合通信次数沿用每层的 `collectives_per_step`。按输入长度和缓存长度的邻近样本插值，两组样本都必须覆盖目标输入。缺少覆盖时明确报错，不外推、不按命中比例自动扣时间。运行前校验负载中所有可能命中边界，缺少样本时不创建结果目录。

完整输入 KV 命中仍以 `cached_tokens=input_tokens-1` 重算最后一个查询以产生首 token；KV 保持共享，新增输出使用私有对象。关闭缓存或关闭 P 复用时不要求命中计算样本。示例采样全部为 synthetic，包含对缓存上下文的注意力成本，不代表真实 GPU 性能。

## 生命周期与容量

轮询查询发生在实际开始 Prefill 前；联合模式在延迟可见快照上查询并在绑定时整批预留，只命中已就绪且仍驻留的缓存，不读取未来请求或提前命中正在计算/接收的对象。命中引用随请求一起原子提交；各前缀段按层、TP rank 独立分片并准确核算零头。整个段所有层/rank 就绪后才发布为可复用缓存。

已缓存的 P 层可提前加入逐层传输队列；未命中层在实际计算完成后就绪。整份/Prefill 后分块模式仍等待 Prefill 结束。D 仅补传缺失段，Decode 仍等待 Prefill 与全部输入 KV 就绪。P 命中而 D 未命中不会减少传输字节。

可与 Chunked Prefill、Prefill token 预算合批和联合绑定组合。chunk 时延来自显式 query/history 曲线，
无需另外提供整请求 `cached_points`；未开启 chunk 时沿用原有 `cached_points` 要求。
P 命中初始化计算游标，完整命中仍重算最后一个查询。按 chunk 与前缀段的交集逐层增量物化，
各段独立进行 TP 分片累计差分；命中部分不重复写入。已就绪的 P 命中且 D 缺失部分只加入传输队列一次。
缓存仍在请求 Prefill 完成/目标完整接收后发布，不把部分完成的 chunk 提前宣布为完整命中。

缓存持有独立引用；请求结束释放请求引用和私有增长对象。LRU 只淘汰没有活动引用和读 pin 的条目；传输启动时加 pin，ACK 后解除。在容量回收时保护当前候选计划的命中对象，全部资源仍通过同一账本原子提交。缓存预算不足时请求可继续执行，但跳过新缓存保留。正常完成后的缓存占用不应被误判为泄漏。

## 观测与运行

事件包括 `prefix_cache_lookup`、`prefix_cache_inserted`、`prefix_cache_evicted`；lookup 记录 P/D 命中 token、计算上下文和节省传输字节。`observation/prefix_cache_summary.json` 记录命中、驻留字节和各对象的位置、就绪时间、可淘汰状态；关闭状态观测仍输出独立缓存汇总。实验汇总可比较命中量、驻留量和节省传输字节。

```bash
./inference --config joint_scheduling/configs/prefix_cache_smoke.json run-baseline --output joint_scheduling/artifacts/prefix-example --plot
./inference run-experiments --spec joint_scheduling/configs/prefix_cache_comparison_example.json --output joint_scheduling/artifacts/prefix-suite
./scripts/simai-python -m unittest discover -s joint_scheduling/backend/tests -p test_prefix_cache.py -v
```

示例先预热 P/D0，再让同一 P 配 D1，区分 P 命中、D 未命中和双方命中。比较规格为缓存关/开 × 三种逐层模式 × 两个 seed，共 12 次。全部计算数据明确标为 synthetic，只验证机制。
