# FreeInference 固定样本与 32 GPU 接入

来源：[HarvardMadSys FreeInference Agentic Trace](https://huggingface.co/datasets/harvardMadsys/freeinference_agentic_trace)，固定 revision `bb26cf72c9c20f2b05f765d88548156a74d3c253`，依 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 使用并注明作者数据集。原始压缩文件保留在 `raw/2026-05-17.jsonl.zst`；原始下载地址与所有输入 SHA-256 见 `manifest.json`。

这里只使用 **2026-05-17 会话起始日期分片**，不是全平台完整一天，也不是全量约 119 万请求的代表性抽样。内部请求可跨午夜；观察区间为 113425.196000001 秒（约 31.51 小时）。该分片偏稀疏，原始时间版的有效请求均值约 0.010509 请求/秒，不能据此代表全平台流量。

## 输入文件

| 文件 | 内容和修改 |
| --- | --- |
| `agentic_2026-05-17_original.json` | 1375 次调用中的 1192 个正输入/输出长度对；显式跳过 183 个输出长度非正的调用；保留原相对时间，未截短长度；TTFT 1 秒 / ITL 100 ms 为实验假设 |
| `agentic_2026-05-17_original.audit.json` | 每个排除项、原因、来源身份和 profile 覆盖检查 |
| `agentic_2026-05-17_1rps.json` | 同一批 1192 请求；完整时间轴统一压缩到 1192 秒，平均 1 请求/秒；`synthetic=true` 表示时间派生，源数据本身仍为真实来源 |
| `agentic_2026-05-17_1rps.audit.json` | 缩放版审计；长度配对哈希与原始时间版相同 |
| `agentic_2026-05-17_single_validation.json` | 明确选择最小 `input_tokens+output_tokens`（并以到达时间打破平局）的一条请求，将到达重置为 0；仅连通性验证，不是代表性/压力样本 |

单请求为 `freeinference-000000934`：输入 **4535**、输出 **4**；原相对到达 57805.412 秒。其来源会话、原始到达和选取规则均写入负载。未用人工缩短长度加速验证。

## 长上下文与执行配置

匹配 profile 是 `joint_scheduling/profiles/deepseek_v3_h800_tp1_ep16_ep16_160k_estimated.json`，覆盖上限 **163840**。当前样本最大输入为 137320，最后 Decode context 最大为 137989，1192 个有效请求全部覆盖；原 16K 只能覆盖其中 38 个。

新网格沿用原官方 H800 4K CUDA 计算锚点、TP1/EP16 权重、KV 字节与分组路由约束，扩展上下文计算曲线。163840 是当前仓库所保留的模型配置上限；该 profile 保持 `estimated_simulation_profile`，长上下文代价是解析外推，未新增实机测量。不能把曲线覆盖等同于精确的长上下文性能验证。

32 GPU、P16/D16、两侧 DP16/EP16、网络与 KV 参数使用 `base.json`。两个 FreeInference 规格共同设置 `reservations.reserve_max_output_tokens=8192`，作为调度可见的实验上限，覆盖该样本最大实际输出 4138；源数据未提供此业务承诺。该设置不读取每条请求精确 EOS 来决定优先级，也不改变已有 Azure 规格。

## 使用方法

导入原始时间轴；输出路径必须不存在：

```bash
./inference import-trace \
  --trace data/freeinference/raw/2026-05-17.jsonl.zst --format freeinference \
  --source-url https://huggingface.co/datasets/harvardMadsys/freeinference_agentic_trace/resolve/bb26cf72c9c20f2b05f765d88548156a74d3c253/release/2026-05-17_2026-05-23/traces/2026-05-17.jsonl.zst \
  --sha256 f768c3a44d6c7eb02340adcb7bceaf5a451e20dc568d7a96d69f78bfb70e09a8 \
  --invalid-requests skip --request-prefix freeinference \
  --profile joint_scheduling/profiles/deepseek_v3_h800_tp1_ep16_ep16_160k_estimated.json \
  --ttft-slo-seconds 1 --itl-slo-seconds 0.1 \
  --output joint_scheduling/artifacts/freeinference/original.json
```

添加 `--target-rps 1` 并更换输出路径即可生成缩放版。固定配对、原始来源顺序和时间缩放因子写入 provenance；不会重复数据补足时长。

32 GPU 的三种策略连通性检查：

```bash
./inference run-experiments \
  --spec joint_scheduling/configs/research/freeinference_validation.json \
  --output joint_scheduling/artifacts/freeinference/validation --jobs 2 --fail-fast
```

1192 请求、Immediate/FIFO/MLU × seeds 7/19 的 1 请求/秒对照预检：

```bash
./inference run-experiments \
  --spec joint_scheduling/configs/research/freeinference_kv_policy_1rps.json \
  --output joint_scheduling/artifacts/freeinference/kv-policy-1rps --dry-run
```

该套对照统计窗口为 [60,1192) 秒、截止 1792 秒，输入/profile/绑定种子均冻结。1 请求/秒仅是可调的明确缩放起点，尚未验证是否产生足够的 KV↔EP 竞争；去掉 `--dry-run` 会执行完整包级实验，本次只预检该全量对照。

当前按来源时间戳做外生请求重放，精简来源信息放在 `trace_metadata`，不把源完成时刻当成仿真完成时刻。缓存 token 不从 prompt 中扣减，也不从 16-token block IDs 推断 provider token 数。没有闭环 agent 依赖、工具执行或真实 DeepSeek-V3 gate 轨迹。

## 已完成的验证

- 原始时间版、1 rps 版的 1192 个有效请求全部通过 163840 网格覆盖检查，未截短；两版长度配对哈希相同。
- 真实原文件的默认质量模式拒绝 183 个输出非正调用并留下审计；使用旧 16K profile 时拒绝 1154 个越界请求，不写出负载。
- 两个新实验规格预检通过，共 3＋6 次计划运行。32 GPU 的 Immediate/FIFO/MLU 单请求 ns-3 运行均完整完成、请求失败数为 0。
- 单请求三策略 TTFT 均为 1.092110659 秒，总时延约 1.14355—1.14356 秒；按本实验 1 秒 TTFT 目标，均未达标。执行完成与 SLO 达标分别报告。
- 全部六组共 291 项测试和后端 smoke 通过；原有 Dense/MoE 冻结结果继续通过。两处旧绑定测试已改为同时覆盖 32/160 GPU 布局。最终流式适配器能逐字节复现已冻结的原始时间版与 1 rps 版。

该验证没有运行完整 1192 请求的包级对照，也没有验证全量各周分布或闭环 agent 行为。
