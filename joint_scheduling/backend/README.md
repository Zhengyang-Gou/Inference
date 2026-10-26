# Inference 的可重复构建 ns-3 后端

当前已能在 CPU 上运行：请求到达 → P 计算/集合通信 → 首 token → 整份 KV 交接 → D 输出后续 token → 指标与绘图。

**网络使用 vendored ns-3 源码的实际分组、队列、ECMP、RDMA 与 DCQCN 模型。请求执行器是显式的简单基线，不是完整 Vidur/Sarathi 批处理的复现。** 联合优化、逐层 KV 与 SLO 门控尚未实现。

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
- 隔离构建只补两处必要兼容：上游 CMake 版本范围拼写；point-to-point 实际依赖但未声明的两个序列头实现。后者来自同版本上游的 applications 模块，不替换网络算法。
- 构建指纹包含源文件、工具链、平台和仓库位置；清单保存可执行文件 SHA256。运行拒绝陈旧或被替换的二进制。
- 编译器、源版本或仓库位置变化时 build 会保留不兼容的旧生成目录并重新配置。doctor 同时检查动态加载和协议自检。
- 构建日志在 `joint_scheduling/artifacts/backend/current/build.log`，运行日志在对应 `network/backend.log`；RPC 有超时，异常退出会关闭子进程。
- `check.sh` 使用同一套测试检查指标、资源、真实网络及端到端确定性。GitHub Actions 工作流已加入，提交到远端后可在 CI 执行。

```bash
./joint_scheduling/check.sh
./inference backend smoke
```

smoke 验证提高带宽降低传输时间、接收端汇聚竞争、12 个请求端到端完成、相同种子轨迹一致，以及结束后 KV 占用/预留归零。环境或依赖升级后应重跑 doctor/build/check。固定版本和回归支持长期维护，无法预先保证任意未来环境都不需要适配。

## 统一配置

`runtime` 是运行入口配置，设置种子、负载路径、终止时间、拥塞控制、MTU、交换机缓存和 RPC 超时；支持 dcqcn 或 none，路由使用已实现的 ECMP。

计算数据由 `feedback.compute_profile_path` 指定，负载由 `runtime.workload_path` 指定，均相对于配置文件。CLI 的 `--profile/--workload` 可覆盖，显式 CLI 路径相对于当前目录。

### 计算性能文件

参照 `fixtures/compute_profile.json`：

- 明确记录来源与 synthetic。真实研究应换成目标模型、硬件与 TP 下的性能采样。
- `compute_seconds` 不包含本后端显式模拟的通信；每个阶段声明 TP 和每步 AllReduce 次数。
- `collective_bytes` 是单次 AllReduce 的每 rank 输入大小；当前采用 ring reduce-scatter + all-gather 的流量及依赖，不复刻 NCCL 自动算法选择。
- 按 token 数在提供点之间线性插值，超出覆盖范围或 TP 不匹配时报错，不静默外推。
- `kv_bytes_per_token` 是整个实例的有效 KV 字节数，应包含分片/复制的实际影响；`weights_bytes_per_gpu` 是每 GPU 固定权重占用。
- Prefill 查输入长度；Decode 从输入长度起随已处理输出增长。目前每步只有一个请求，不支持连续批处理、PP/EP、KV 重分片或缓存命中加速。

### 请求文件

参照 `fixtures/workload.json`：唯一请求 ID、到达时间、输入和精确输出 token 数，可覆盖 TTFT/ITL 阈值。输出数用于模拟长度与容量，不能把未知 EOS 的上限当成精确输出数。

## 唯一时间与反馈

请求到达、计算结束、局部传输和网络完成全部进入 ns-3 时间轴。Python 只处理回调并提交后续工作，不另行推进计算时钟。网络完成会在外部截止时间之前中断 advance，从而立即安排依赖它的计算。

每个性能采样步骤将计算均分到声明的 AllReduce 前。AllReduce 前一轮完成后才提交下一轮，最后推进计算或返回 token。这是阶段级模型，没有 Transformer 逐层 KV 就绪，也不假设未经采样的通信/计算重叠。

网络完成采用“发送端已确认全部字节 ACK”的保守语义，此时目标可用且源端读依赖解除。它可能晚于目标 GPU 最早可读时刻，不代表 GPU DMA 的精确测量。

## 拓扑与局部互联

```bash
./inference export-topology --format simai --output joint_scheduling/artifacts/network.topo
```

后端文件使用 SimAI 六字段头部与链路语法，网络端点聚合为物理服务器：每服务器共享一张全双工 NIC。GPU → 服务器映射保留在配置。节点内 GPU 传输使用一个可配置共享局部 FIFO 链路；节点间走实际 RDMA 网络。这不是逐 GPU 独占 NIC 的 rail-optimized 模型，也不模拟 NVLink 硬件拓扑细节。

`topology.intra_node_bandwidth_gbps/intra_node_latency_us` 控制局部链路。原生文件的 GPUsPerServer=1 表示每个网络端点聚合一台服务器，不能误读为计算实例只有一个 GPU。baseline_smoke.json 的 P/D 在不同 Leaf，覆盖跨 Leaf 交接。

## 基线与容量规则

- P/D 分别按到达顺序轮询，绑定不变；每 P FIFO 执行一请求，每 D 轮询执行一请求的一 token 步骤。
- P 返回首 token 后，将整份 KV 按相同 TP rank 对应交接并立即提交，没有 SLO 优先级或启动门控。
- 大逻辑传输内部拆为不超过 64 MiB 的可靠消息，规避上游 32 位序列号限制；这不是逐层 KV 调度。
- 账本原子预留 P 输入 KV 与 D 最大增长，预留转占用不增加总承诺，源端读 pin 解除之前保留 KV，结束后释放无用预留。
- 账本支持对象去重、共享引用和读 pin；当前基线尚不产生或复用共享前缀缓存。
- 终止时未完成请求保持 incomplete，不伪造完成；超过单请求容量的请求记为 failed。

配置中的 joint_lexicographic/slo_slack 仍是尚未实现的策略选项。显式运行 run-baseline 时采用以上规则，实际策略记录在 resolved_config.json 和 trace 元信息的 effective_baseline 中。

## 输出

```text
run/
├── resolved_config.json
├── compute_profile.json
├── workload.json
├── events.jsonl
├── observation/          # 状态时间线、延迟可见状态和实例/通信统计
├── final_resources.json
├── network/
│   ├── network.topo
│   ├── backend_metadata.json
│   └── backend.log
├── result/                # evaluation 的 CSV/JSON、事件和配置快照
└── figures/               # --plot 时生成
```

网络后端锁定 CMake 3.31.10；CI 配置使用 Ubuntu 24.04 与 Conda 的 `simai` Python 3.12 环境。配置/统计代码保持 Python 3.8 语法兼容，其他环境用 CI/本机回归确认。目录、工具链或网络源码调整后应重新执行 build、doctor 和 check。新模型、协议或更精细本地调度应先补实现与回归，再更新版本锁。

## 观测与实验支持

基线执行器已接入 observation/。观测将计算时间与等待集合通信的实例忙碌时间分开统计，记录 KV/collective 逻辑消息生命周期及显存变化，并落实 feedback.observation_delay_us。观测不创建 ns-3 事件，尚未到达的请求不会出现在可见状态中；scheduling_overhead_us 仍未用于调度执行。

workload/ 可生成请求文件，experiments/ 可执行多配置与多 seed 矩阵，输入路径会解析并冻结。每次仍采用本节声明的简单执行规则，不实现联合优化或连续批处理。
