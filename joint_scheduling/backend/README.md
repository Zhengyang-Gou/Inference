# Inference 的可重复构建 ns-3 后端

运行时已拆为请求、P/D、KV、计算、通信、显存与记录组件，`runner.py` 保留输入检查和运行/导出入口。所有权、兼容端口与功能组合见 [研究系统说明](../RESEARCH_SYSTEM.md)。每次结果新增 `reproducibility.json`、源码归档及 `runtime_components.json`。

DeepSeek-V3/H800 的 TP1、P-EP32 / D-EP128 主 profile 已提供公开采样推导版，支持 P/D 独立权重和显存账本；旧 TP4/EP8 profile 保留，覆盖 16K 上下文、长请求分块和合批；生成、AICB/AIOB 导入及估算边界见 [MoE profile](MOE_PROFILE.md)。

`slo_mlu` 支持 KV 晋升和 EP 分阶段保护，`runtime.network_priority_enabled` 控制仿真 NIC/交换机数据优先级。RPC 为版本 4，增加 GPU 本地端口命令，需要重建后端；覆盖只进入隔离构建，详见 [MLU 调度](../kv_scheduler/MLU.md)。

当前已能在 CPU 上运行请求端到端基线，支持整份交接、Prefill 后分块，以及逐层 KV 就绪后分块立即传输。

实验性 EP 路径已接入分组网络仿真：节点内 TP、跨 TP team 的 dispatch→专家计算→combine 可与 KV 共享链路；支持轮询和 P 轮询/D 均衡随机的静态绑定，联合选择和绑定预测明确禁用。配置、160 GPU 主布局及旧 128 GPU 对照和模型边界见 [EP 接入说明](EXPERT_PARALLEL.md)。

绑定窗口、完整预测诊断和有界联合选择均已接入，默认关闭。`binding.joint_selection_enabled=true` 与 `policy=joint_lexicographic` 启用候选剪枝＋增量贪心，并在绑定时整批原子预留；轮询保留原 Prefill admission 预留时机。联合模式默认不调用完整预测器，声明决策开销与实际墙钟诊断分开；具体模型和边界见 [绑定说明](../binding/README.md)。

**网络使用 vendored ns-3 源码的实际分组、队列、ECMP、RDMA 与 DCQCN 模型。请求执行器是显式的简单基线，不是完整 Vidur/Sarathi 批处理的复现。** 显式逐层执行与 KV 分块立即传输已实现，动态带宽预算、集合通信保护和 SLO 启动门控已实现；候选剪枝＋增量贪心联合绑定已实现。

## 首次准备与运行

需要 Linux、C++ 编译器和 Conda，项目环境 `simai` 使用 Python 3.12；无需 GPU、MPI 或管理员安装。构建从本仓库的普通源码文件读取，不需要 Git 或子模块。在 Inference 仓库根目录执行：

```bash
./joint_scheduling/bootstrap.sh
conda activate simai
./inference backend build --jobs 2
./inference backend doctor
./inference --config joint_scheduling/configs/baseline_smoke.json run-baseline --output joint_scheduling/artifacts/my-first-run --plot
```

`bootstrap.sh` 创建或更新名为 `simai` 的 Conda 环境，显式使用 conda-forge 安装 Python 3.12，并通过项目 requirements 安装固定 CMake 和绘图依赖；根目录 `environment.yml` 记录等价环境定义。运行入口自动选择 `simai`；手动执行 Python 时使用 `conda activate simai`。

已有构建时再次 build 会增量编译。根目录 `inference` 包装 `joint_scheduling/jsim`，定位仓库根目录，并保留调用方的相对路径语义。新运行拒绝覆盖旧结果。初次 bootstrap 需要下载 Conda 与 Python 依赖。

默认配置没有计算性能文件或负载，run-baseline 会明确报错，不自动退回合成数据。上述 baseline_smoke.json 使用显式标记的 synthetic 数据，仅验证管线，不能用于评价真实模型性能。

## 长期维护与回归

- `backend.lock.json` 固定 ns-3 源码内容 SHA256、CMake 版本、RPC 协议和完成语义；上游提交号保留为来源记录。`third_party/ns3/SOURCE_MANIFEST.json` 列出 vendored 源文件与 SHA256，内容与锁不匹配时停止构建并给出原因，不读取上游 Git 元数据。
- 第三方组件总清单为根目录 `third_party/manifest.json`，许可路径与来源由该组件清单记录。当前只使用 `third_party/ns3` 构建网络后端，其余保留组件不参与基线构建。
- ns-3 已按当前依赖闭包及后续推理研究用途裁剪。清单中的 active_modules / future_modules 记录保留理由，目录检查同时验证对应源码存在；后续扩展不得仅因暂未接入就删除相关参考组件。
- 不在 vendored 源码目录里编译；在 `joint_scheduling/artifacts/backend/current/source/` 中构建隔离副本。
- 隔离构建补两处必要兼容：上游 CMake 版本范围拼写；point-to-point 实际依赖但未声明的两个序列头实现。后者来自同版本上游的 applications 模块，不替换网络算法。
- 构建指纹包含源文件、工具链、平台和仓库位置；清单保存可执行文件 SHA256。运行拒绝陈旧或被替换的二进制。
- 编译器、源版本或仓库位置变化时 build 会保留不兼容的旧生成目录并重新配置。doctor 同时检查动态加载和协议自检。
- 构建日志在 `joint_scheduling/artifacts/backend/current/build.log`，运行日志在对应 `network/backend.log`；RPC 有超时，异常退出会关闭子进程。
- `check.sh` 使用同一套测试检查指标、资源、分组网络仿真及端到端确定性。GitHub Actions 工作流已加入，提交到远端后可在 CI 执行。

```bash
./joint_scheduling/check.sh
./inference backend smoke
```

smoke 验证提高带宽降低传输时间、接收端汇聚竞争、12 个请求端到端完成、相同种子轨迹一致，以及结束后 KV 占用/预留归零。环境或依赖升级后应重跑 doctor/build/check。固定版本和回归支持长期维护，无法预先保证任意未来环境都不需要适配。

## 统一配置

`runtime` 是运行入口配置，设置种子、负载路径、终止时间、拥塞控制、MTU、交换机缓存和 RPC 超时；支持 dcqcn 或 none，路由使用已实现的 ECMP。

`runtime.rpc_submission_batch_size` 控制同一回调内 SEND/LOCAL/LOCAL_PORTS/TIMER 的管道提交批次，默认 128，允许 1–256；设为 1 恢复逐条同步提交。每次仿真只启动一个常驻 ns-3 子进程，批量提交减少 Python 与该进程的同步等待。每条命令仍保留独立编号与原有顺序，逐条检查原协议的 OK 回复；STATS 和 ADVANCE 前必须提交完此前命令，完成回调和仿真时钟规则保持不变。它只影响模拟程序的实际耗时，不代表真实 EP dispatch 的通信合并，也不增加模型中的计算/通信重叠。协议仍为 v4，此项 Python 优化无需重新构建后端。

计算数据由 `feedback.compute_profile_path` 指定，负载由 `runtime.workload_path` 指定，均相对于配置文件。CLI 的 `--profile/--workload` 可覆盖，显式 CLI 路径相对于当前目录。

### 计算性能文件

`build-public-profile --mlp ... --attention ... --source-commit ... --tp 4 --output ...`
可将公开 Vidur 的 A100、Llama-3-70B 算子 CSV 组成 Llama-3.1-70B FP16 结构代理。
此适配器验证隐藏维度、Q/KV 头、MLP、词表、TP、batch=1 和 FlashInfer，转换毫秒为秒；
逐层计算重复 80 层、每层两次 AllReduce、每 token KV 为 327680 bytes。
每个 rank 的权重核算包括分片矩阵与复制的 RMSNorm。

导出的 `provenance.synthetic=true` 表示它仍是**公开算子数据组成的估算**，作为 Llama-3.1 的仿真算子组合。
当前不包含最终词表投影、最终 norm、embedding 输出集合通信、CPU/采样开销，也未单独验证 3.1 的 RoPE 内核；
低于最小 attention 样本长度时使用明确记录的低长度锚点。超出最大样本长度不外推。
TP 集合通信由本项目 ns-3 拓扑重新执行，通信时间由仿真网络模型单独给出。
详细来源、commit、哈希、插值方法和省略项均写入参数文件。

参照 `fixtures/compute_profile.json`：

- 明确记录来源与 synthetic。仿真研究应固定模型、设备代价标签与 TP 对应的计算参数，并进行覆盖检查和敏感性分析。
- `compute_seconds` 不包含本后端显式模拟的通信；每个阶段声明 TP 和每步 AllReduce 次数。
- `collective_bytes` 是单次 AllReduce 的每 rank 输入大小；当前采用 ring reduce-scatter + all-gather 的流量及依赖，不复刻 NCCL 自动算法选择。
- 按 token 数在提供点之间线性插值，超出覆盖范围或 TP 不匹配时报错，不静默外推。
- `kv_bytes_per_token` 是整个实例的有效 KV 字节数，应包含分片/复制的实际影响；`weights_bytes_per_gpu` 是每 GPU 固定权重占用。
- Prefill 查输入长度；Decode 从输入长度起随已处理输出增长。Decode 默认每步一个请求，设置 runtime.decode_batch_size > 1 并提供批量时延曲线可启用连续批处理；EP 需额外专家 profile 和显式组映射，见上述实验路径。不支持 PP 或 KV 重分片。

### 请求文件

参照 `fixtures/workload.json`：唯一请求 ID、到达时间、输入和精确输出 token 数，可覆盖 TTFT/ITL 阈值。输出数用于模拟长度与容量，不能把未知 EOS 的上限当成精确输出数。

## 唯一时间与反馈

请求到达、计算结束、局部传输和网络完成全部进入 ns-3 时间轴。Python 只处理回调并提交后续工作，不另行推进计算时钟。网络完成会在外部截止时间之前中断 advance，从而立即安排依赖它的计算。

每个性能采样步骤将计算均分到声明的 AllReduce 前。AllReduce 前一轮完成后才提交下一轮，最后推进计算或返回 token。schema 1 保留阶段级模型；schema 2 对每层分别执行这个依赖过程，层完成后产生 KV 就绪事件。传输可以与后续层重叠，但会通过共享网络真实影响后续集合通信，不假设其免费重叠。

网络完成采用“发送端已确认全部字节 ACK”的保守语义，此时目标可用且源端读依赖解除。它可能晚于目标 GPU 最早可读时刻，是本仿真的传输完成定义。

## 拓扑与局部互联

```bash
./inference export-topology --format simai --output joint_scheduling/artifacts/network.topo
```

后端文件使用 SimAI 六字段头部与链路语法。`topology.nic_model=shared_node` 保留每服务器共享 NIC；`per_gpu` 为每 GPU 一个全双工 RDMA 端点，`nic_bandwidth_gbps` 始终表示节点合计并均分到 GPU。节点内 `intra_node_model=shared_fifo` 保留整节点 FIFO；`gpu_ports` 为每 GPU 独立的发送/接收端口，带宽字段表示单端口能力，相反方向和不冲突的传输可并发，共用端口排队。SLO 资源估计和网络遥测使用相同端点/端口映射。它不模拟完整 NVSwitch 仲裁或多 rail 网络。

`topology.intra_node_bandwidth_gbps/intra_node_latency_us` 控制局部链路。原生文件的 GPUsPerServer=1 表示每个网络端点聚合一台服务器，不能误读为计算实例只有一个 GPU。baseline_smoke.json 的 P/D 在不同 Leaf，覆盖跨 Leaf 交接。

## 基线与容量规则

- 默认 P/D 按到达顺序轮询；可用 `joint_selection_enabled` 和 `policy=joint_lexicographic` 启用有界联合选择，整批原子预留。每 P 默认 FIFO 执行一请求，可选按 token 块轮转；每 D 默认逐请求轮转，可选连续批处理。
- 默认 whole_ready_immediate 在首 token 后按 TP rank 提交整份 KV；另支持 whole_ready_chunked 和 layer_ready_immediate，见下节。三者均无 SLO 优先级或带宽启动门控。
- 大逻辑传输内部拆为不超过 64 MiB 的可靠消息，规避上游 32 位序列号限制；这不是逐层 KV 调度。
- P 启动前原子预留 P 输入 KV 与 D 输出增长；逐层数据按各层实际 rank 分片核算，预留转占用不增加总承诺。源端按块读 pin 保留，目标端按块完成转换占用；结束后释放无用预留。
- 账本支持对象去重、共享引用和读 pin；可选 `prefix_cache` 已接入前缀匹配、P/D 独立命中、缺失 KV 补传和 LRU 淘汰，见 [前缀缓存说明](../prefix_cache/README.md)。开启后，已完成请求的缓存可继续占用显存；私有对象和剩余预留仍释放。
- 终止时未完成请求保持 incomplete，不伪造完成；超过单请求容量的请求记为 failed。

joint_lexicographic 需显式开启 joint_selection_enabled，详见 binding/README.md；slo_slack 是策略接口名称，实际执行模式仍需显式选择 layer_ready_slo。run-baseline 使用 runtime.kv_transfer_mode（可由 --kv-mode 覆盖）选择实际 KV 模式，真实执行规则记录在 resolved_config.json 和 trace 元信息的 effective_baseline 中。

## 逐层 KV 与三组对照

```bash
./inference --config joint_scheduling/configs/layered_smoke.json run-baseline --kv-mode layer_ready_immediate --output joint_scheduling/artifacts/layered-run --plot
./inference run-experiments --spec joint_scheduling/configs/kv_comparison_example.json --output joint_scheduling/artifacts/kv-comparison
```

对照规格使用同一份四层 synthetic profile、同一份请求文件，扫描 3 种模式 × 10/40/200 Gbps NIC × 2 个网络 seed，共 18 次。每组仍使用相同的轮询绑定、FIFO Prefill、逐 token Decode、计算时间、KV 字节和集合通信工作量；只改变 KV 提交时机或消息粒度。低带宽下立即放行也可能拖慢集合通信，应同时检查 TTFT、handoff、后续最大 ITL、E2E、失败/未完成数和达标完成吞吐。

| runtime.kv_transfer_mode | 就绪和提交规则 |
| --- | --- |
| whole_ready_immediate（默认） | Prefill 完成后，每 rank 一条整份逻辑消息 |
| whole_ready_chunked | 各层生成同样的块，全部等 Prefill 完成后提交 |
| layer_ready_immediate | 每层完成后立即提交该层的块，P 继续下一层 |

后两组使用完全相同的层/rank/offset 分块，块大小上限为 kv_scheduler.max_block_bytes；第三组相对第二组用于识别提前传输的效果。首组与第二组用于观察消息粒度的影响。max_concurrent_transfers、max_inflight_bytes、safety_margin_fraction 和 slo_slack 尚不控制这三组：就绪块全部提交，后端决定实际速率，不能将其称为带宽调度。

schema 2 性能文件保留顶层 provenance、network_communication_excluded、kv_bytes_per_token、weights_bytes_per_gpu；Decode 格式与 schema 1 相同。Prefill 格式改为：

```json
"prefill": {
  "tp_size": 4,
  "layers": [
    {
      "kv_bytes_per_token": 8192,
      "collectives_per_step": 1,
      "points": [
        {"tokens": 128, "compute_seconds": 0.00016, "collective_bytes": 16384},
        {"tokens": 512, "compute_seconds": 0.0006, "collective_bytes": 65536}
      ]
    }
  ]
}
```

层号按数组从 0 编号，各层 KV 字节/token 之和必须等于顶层 kv_bytes_per_token。每层独立插值并检查输入范围，TP 不匹配或超出任一层覆盖范围即报错；不会把 aggregate profile 自动伪装成逐层测量。schema 1 继续支持整份及 Prefill 后分块，所有 layer_ready_* 模式必须显式提供 schema 2。

这里的 layer_ready 是该层建模计算和集合通信依赖结束后的保守就绪时刻；尚未模拟层内算子级 KV 提前就绪、GPU DMA/内存带宽竞争及传输启动的真实硬件开销。四层 fixture 的时间来自人工拆分，不能证明真实模型有同样收益。

轨迹增加 layer_ready、kv_all_received 和块的 layer_id/block_id/offset_bytes/ready_at_seconds。目标 KV 按完成块累积；D 仅在 Prefill 和全部输入 KV 都完成后入队。单输出 token 请求不交接 KV。源端对象在 Prefill 引用与所有在途块读取依赖解除后才回收；当前按请求保留源对象，不做已传层的部分内存回收。截止时仍在途的块与资源继续保留为 incomplete。

## 输出

默认 `recording.mode="essential"` 保存输入、请求/token 轨迹、指标及资源汇总；逐事件状态、网络采样明细和调度明细仅在 `"debug"` 时写盘。两种模式的调度输入和仿真时钟相同，详见 [实验记录规则](../RECORDING.md)。

动态模式的完整算法、参数和保护边界见 [KV 调度说明](../kv_scheduler/README.md)。运行 adaptive_smoke.json，或用 --kv-mode layer_ready_slo 启用 SLO 排序，layer_ready_fifo 作相同保护下的 FIFO 对照。adaptive_comparison_example.json 使用不规则到达及不同请求 SLO。

dynamic_window_smoke.json 演示带宽、KV 完成反馈与集合通信阻塞预算共同决定在途字节数；dynamic_window_comparison_example.json 扫描三种策略及 10/40/200 Gbps，dynamic_window_ablation_example.json 在相同硬上限下比较固定与动态额度。

STATS 由适配器采集实际端口的分类发送字节、队列及 QP ACK 进度；network_samples.jsonl 记录采集/可见时间。kv_scheduling_decision 记录每次决策的请求时间预算和共享资源额度，kv_scheduler_summary.json 记录放行与未完成工作。预计达标并非实际达标保证，比较必须同时保留所有延迟与吞吐。

```text
run/
├── resolved_config.json
├── compute_profile.json
├── workload.json
├── events.jsonl
├── observation/          # 实例/通信/网络汇总；完整时间线与快照需 debug
├── final_resources.json
├── network/
│   ├── network.topo
│   ├── backend_metadata.json
│   ├── backend_rpc_summary.json
│   └── backend.log
├── result/                # evaluation 的 CSV/JSON 和配置快照；引用根目录的唯一事件文件
└── figures/               # --plot 时生成
```

`network/backend_rpc_summary.json` 记录提交命令数、提交批次数、查询/推进次数与同步交换批次数（提交批次加查询/推进，不含握手和关闭），用于定位执行器开销。调大提交批次不会减少逻辑流或完成事件数。

网络后端锁定 CMake 3.31.10；CI 配置使用 Ubuntu 24.04 与 Conda 的 `simai` Python 3.12 环境。配置/统计代码保持 Python 3.8 语法兼容，其他环境用 CI/本机回归确认。目录、工具链或网络源码调整后应重新执行 build、doctor 和 check。新模型、协议或更精细本地调度应先补实现与回归，再更新版本锁。

## 观测与实验支持

基线执行器已接入 observation/。观测将计算时间与等待集合通信的实例忙碌时间分开统计，记录 KV/collective 逻辑消息生命周期及显存变化，并落实 feedback.observation_delay_us。普通状态观测不创建 ns-3 事件，尚未到达的请求不会出现在可见状态中。动态网络观测按 network_observation_interval_us 采样，并延迟 observation_delay_us 可见；scheduling_overhead_us 在受控 KV 模式中形成实际决策延迟。

workload/ 可生成请求文件，experiments/ 可执行多配置与多 seed 矩阵，输入路径会解析并冻结。本地执行支持轮询/联合绑定与 FIFO/Chunked/合批 Prefill、Decode 连续批处理及前缀缓存组合。联合预测在分块/合批配置下使用有界队列重放和聚合网络近似，详见 binding/README.md。网络 RPC 协议现为 v4，支持分类 SEND/LOCAL、LOCAL_PORTS 与 STATS；仅适配器修改，无 vendored 网络算法变更，升级后需重新 build。

PFC 压力验证还修正了两个阻断拥塞运行的问题：适配器为交换机端口配置仅供单跳 PFC 控制包使用的地址；隔离构建修正上游 `QbbNetDevice::Resume` 将优先级误当 QP 索引的空指针路径，按设备类型恢复普通/NVLS 发送。原始 vendored 源码及内容锁保持不变，精确修补代码随 build.py 纳入构建指纹。真实 6 源 incast 测试要求观察到 PFC 并全部完成。此前失败日志已随历史实验记录清理；修复实现与回归测试保留。

Decode 连续批处理的配置、时延模型和验证说明见 [CONTINUOUS_BATCHING.md](CONTINUOUS_BATCHING.md)。

Prefill token 分块轮转及上下文时延数据要求见 [CHUNKED_PREFILL.md](CHUNKED_PREFILL.md)。

公开算子留出验证、匹配模型选择及无GPU时的误差边界说明见 [PUBLIC_VALIDATION.md](PUBLIC_VALIDATION.md)。

P 池内部的多请求合批已支持 token预算、请求数上限和每请求chunk上限，见 [PREFILL_BATCHING.md](PREFILL_BATCHING.md)。
