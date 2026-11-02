# DeepSeek-V3 H800 profile

主研究使用 `../profiles/deepseek_v3_h800_tp1_ep32_ep128_estimated.json`：TP1、P-EP32 / D-EP128，EP rank 对应 GPU。Attention 分别为 DP32、DP128，与专家使用同一批 GPU。
这是**公开 H800 计算采样推导的估算模型**，`synthetic=true`、`profile_kind=estimated_simulation_profile` 始终保留。
旧 `deepseek_v3_h800_tp4_ep8_estimated.json` 和 128 GPU 配置保留用于回归与独立对照。
两层 EP fixtures 继续仅用于快速功能测试，不作为这个模型的来源。

## 来源和原始设置

- 结构取自保留的 `third_party/vidur/data/hf_configs/deepseek_v3_config.json`：
  61 层、前 3 层 dense、58 层 MoE、256 routed experts、top-8、hidden 7168。
  生成器核对所有关键维度，不把 AICB 的冗余专家数 288 当作路由专家数。
- 计算取自 [DeepSeek 官方 trace](https://github.com/deepseek-ai/profile-data)，固定 revision
  `449602428a1b023acb8a505d4f34fef536535db6`；核验原始文件 SHA-256。
  trace 的设备字段明确为 NVIDIA H800。
- Prefill 参考：EP32/TP1、4K prompt、每 GPU 16K query tokens、两个 micro-batch；
  每 micro-batch 8192 tokens，均衡路由下每 GPU 65536 assignments、8 个专家。
- Decode 参考：EP128/TP1、4K prompt、每 GPU batch 128、两个 micro-batch；
  每 micro-batch 64 tokens，均衡路由下每 GPU 512 assignments、2 个专家。

当前执行器没有复刻原引擎的双 micro-batch 重叠和 gate，目标仿真使用下述显式代价假设。

## 生成与复现

按上述固定 revision 下载 `prefill.json`、`decode.json` 后，在仓库根目录执行：

```bash
./inference build-moe-profile \
  --model-config third_party/vidur/data/hf_configs/deepseek_v3_config.json \
  --prefill-trace /absolute/source/prefill.json \
  --decode-trace /absolute/source/decode.json \
  --tp 1 --prefill-ep 32 --decode-ep 128 --max-context 16384 \
  --prefill-chunk-tokens 512 --prefill-batch-token-budget 2048 \
  --decode-batch-size 64 --output /absolute/new-profile.json
```

同时生成 `new-profile.audit.json`，保存源文件哈希、算子分组、内核计数与中位数、通信及其他内核排除数量、
字节计算和限制。输出已存在时拒绝覆盖；写出前检查所有层分块与合批覆盖。

## 计算和通信分离

只抽取 CUDA `kernel` 事件，将微秒转为秒。CPU 调用、同步等待、DeepEP dispatch/combine/notify、
NCCL 不进入 compute time，网络由 ns-3 独立执行。
同一数学算子的不同 launch 配置合并取中位数，再累加不同算子的中位数，避免把预热配置算成额外操作。
未归类的量化、打包、setup 内核有明确计数，缺失成本在模型限制中显式列出。

导出网格采用以下**假设**：

- 线性计算取 Prefill 每 token 计算分支、Decode 权重/启动分支、内核下限的最大值。
  矩阵工作按 source TP / target TP 缩放；norm/router 保留复制成本。
- Prefill attention 按 `query * (cached + (query+1)/2)` 相对源 4K causal attention 工作量缩放；
  Decode attention 按 batch×context 缩放，保留内核下限。
- 专家计算拟合 `a * assignments + b * active_experts`，两项由 P/D 参考共同确定后按目标 TP 分摊。
  执行时只对目标切片中非零 expert ID 收第二项，避免把整个 EP group 的专家全算作已访问。
- 前 3 层 dense MLP 和其余层 shared MLP 使用独立曲线，chunk `layer_overrides` 保留这一差异。
- 周期均衡 top-8 路由是实验假设；Azure 不提供专家 ID，不能声称复现真实 gate 或数据相关热点。

16K 是**仿真代价曲线覆盖范围**。研究通过离线数据检验、参数敏感性和请求/字节守恒评估模型假设。
当前 Decode 仍执行 aggregate dense step 后再执行 MoE 层，未复刻真实层间交错。

## KV 和权重

MLA 每层每 GPU 保存 BF16 的 512 维 compressed latent + 64 维 rotary key，即 1152 B/token，61 层合计 70272 B/token。主研究 TP1 仅一份物理 KV，P→D 的初始 payload 为输入长度×70272 B。旧 TP4 模式保留四份副本的原核算规则。

权重估算：非 routed 参数按 TP 分片，routed 参数按该阶段 EP×TP 分片；矩阵 FP8、每 128×128 block 一个 FP32 scale、embedding/norm BF16。主 profile 的 `weights_bytes_per_gpu_by_phase` 分别为 P 39308906160 B（36.6093 GiB）、D 23979177648 B（22.3323 GiB）；显存账本按 GPU 角色扣除对应值。旧标量 `weights_bytes_per_gpu` 在分阶段 profile 中取两者最大值供兼容读取，不作为 D 的实际容量扣减。

`provenance.expert_parallel_sizes` 声明两阶段 EP32/EP128；旧 `expert_parallel_size` 单一规模仍可读取。旧 profile 没有分阶段权重时，两侧沿用同一个权重值。workspace、activation buffer 和 MTP 不在当前容量模型中。`--ep` 保留作为两阶段默认值，`--prefill-ep` / `--decode-ep` 可分别覆盖。

## AICB/AIOB 导入

同一入口可使用 `--aiob-manifest /absolute/manifest.json`，与公开 trace 参数互斥。
外层字段为 `schema_version:1, model:"deepseek-ai/DeepSeek-V3", gpu:"NVIDIA H800", samples:[P_sample,D_sample]`。
每个 sample 必须声明：

- `phase, path, time_unit`：`time_unit` 为 seconds/milliseconds/microseconds；
  可用 `section_units` 对混合单位逐 section 覆盖。
- `tp_size, expert_timing_tp, ep_size, tokens, context_tokens, expert_assignments, active_experts`：
  分别注明离线数据坐标；`expert_timing_tp` 取数据中专家 GEMM 的分片，不能盲目复制命令行 TP。
- `roles`：七个键 `attention_projection, attention_core, dense_mlp, shared_mlp, router, norm, expert`，
  每个值为该角色所用 AIOB section 名称列表。

原文件格式为 `section_name:` 后缩进的 `time_gpu_avg: value`。文件、section、时间单位缺失即拒绝，
不会使用上游默认时间 1，也不会把 shared MLP 冒充大 dense MLP。
现有 AIOB DeepSeek 入口未采齐全部 norm/router/dense-shape 成本，须补充相应采样。
即使参考点来自 AIOB，未采到的网格仍是推导估算，仍按估算仿真参数记录。

## Azure 验证口径

目标：chunk≤512、packed budget≤2048、Decode batch≤64、context≤16384。
Azure conv 最大输入 14050、最后 Decode context 14087，code 对应 7437、7839，均在范围内。
除 `import-trace --profile ...` 检查总长度外，还检查每个 layer override 的 packed Prefill query/history 边界。
保留全部长度配对和请求顺序；扫描到达率只统一缩放时间。
Azure 没有业务 SLO、调用者输出上限和 expert 路由；这些配置是独立实验假设。
