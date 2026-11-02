# 研究系统边界与推荐流程

当前研究问题是：P/D 分离的 MoE 推理中，KV 传输如何影响 EP 通信，SLO 调度如何影响请求表现。正式运行路径是自有 profile 驱动执行器 + ns-3 RDMA 网络；GPU 计算时间由 profile 给出，CPU 上推进仿真。没有运行 vLLM 或目标 H800 集群实测。

唯一推荐配置入口为 [research/README.md](configs/research/README.md)。完整 Azure 策略矩阵、网络优先级消融、短程集成验证和固定流量诊断有不同的命名与输出类型，不能互相替代。

## 运行时组件

`backend/runner.py` 保留 `run_baseline` 命令接口，负责输入检查、创建运行、调度协调器与结果导出。`backend/execution/coordinator.py` 用组合方式装配组件，不再继承绑定、EP、Prefill 和 SLO 四组算法。

| 组件 | 状态所有者与职责 | 对外接口示例 |
| --- | --- | --- |
| `execution/requests.py` | 不可覆盖的请求输入；生命周期、P、D、KV、绑定、显存、计算进度分区 | `RequestRecord`；旧算法保留 mapping 访问 |
| `execution/lifecycle.py` | 请求表、请求计数；到达、失败与完成 | `arrive`、`fail_request`、`finish_request` |
| `execution/adapters.py:InstanceState` | 实例定义、P/D 队列、busy 与当前执行成员 | 由 P/D 组件按原有顺序管理队列与执行槽 |
| `execution/prefill.py` | Prefill chunk/batch 状态、成员与进度；复用原合批算法 | `kick_prefill`、`execute_prefill`、`prefill_done` |
| `execution/decode.py` | Decode batch 状态、组批与逐成员 token 返回 | `kick_decode`、`decode_done` |
| `execution/compute.py` | 计算段执行状态；推进计算/通信依赖 | `execute_step`，委托 EP 组件执行 MoE |
| `backend/compute_profile.py` | 计算耗时、字节与覆盖范围插值；不持有仿真时钟 | `ComputeProfile.lookup*`；旧 `backend.runtime` 导入兼容 |
| `execution/budgets.py` | 可见 SLO 预算估计与其短期缓存 | `request_budget`、`mlu_budget` |
| `execution/kv.py` | 采样/门控事件、策略实例、发送许可与完成反馈 | `enqueue_blocks`、`request_dispatch`、`start_kv_transfer` |
| `kv_scheduler/` | 读取预算/网络快照，计算发送许可及优先级；不直接调用网络进程 | `refresh`、`admit`、`completed` |
| `execution/network.py` | 逻辑传输及 TP collective 标识、就绪回调 | `transfer`、`allreduce`，通过 `backend/client.py` 提交 |
| `execution/adapters.py:ExpertRuntime` | EP 调用、专家 FIFO 和 lane 状态；保留原 EP 算法 | `execute_ep_step`、`expert_call` |
| `execution/adapters.py:BindingController` | 绑定窗口、候选、预测快照及诊断；保留原绑定算法 | `init_binding`、`bind_window`、`finish_binding` |
| `execution/memory.py` + `reservations/ledger.py` | admission/前缀计划与源读依赖；账本独占容量、预留、物化、pin/unpin、释放 | `prepare_prefill`；`ledger.try_commit/materialize/release_request` |
| `execution/telemetry.py` | 事件、实例采样和各组件结束摘要；不选择调度策略 | `emit`、`capture`、`close` |
| `backend/client.py` + ns-3 | 唯一仿真时钟、传输、队列和 sender ACK 完成语义 | `call_at`、`submit`、`network_snapshot` |

每个可变运行时字段在 `owned_fields` 中只登记一个所有者，组件禁止创建未声明的字段；各次输出的 `runtime_components.json` 可审计实际分工。协调器的旧方法/字段通过显式注册表转发，`runner._Baseline` 保留兼容名称。

迁移边界仍然存在：原绑定、EP 和 packed Prefill 算法通过兼容端口读取其他组件的状态，旧请求 mapping 访问保留。请求的顶层输入不能被执行进度覆盖，预测快照与运行时独立；进度分区尚未全部转换为强类型记录。后续新功能应直接使用所属组件接口，避免增加跨组件写入。这次迁移不改变算法、事件顺序或 EP 同步语义。

## 输入与结果身份

每次端到端运行保存：

- 可直接再次运行的 `config.json`、`resolved_config.json`、`workload.json`、`compute_profile.json`；
- `reproducibility.json`：三者 SHA-256、源码树摘要、Git HEAD、后端 fingerprint/二进制哈希、模型估算状态及执行语义；
- 完整源码 `source.tar.gz` 和逐文件清单，包含未提交、未跟踪且未忽略的源文件；源码归档排除生成输出；
- 请求/token 指标、最终资源与组件摘要。

批量实验在 `inputs/source/` 冻结一次源码，各运行用相对路径引用；移动结果时保留整个 suite。独立运行的源码在自身 `source/` 下。恢复源码时在新目录解压，按后端构建说明重新构建；源码清单的缺失路径表示当时已删除。构建缓存不属于源码归档，后端二进制通过其哈希及构建 manifest 识别。

结果内 `config.json` 使用相对于自身的冻结输入路径。可以把结果移动到其他目录，再在相同执行源码/后端环境运行 `./inference --config <原结果>/config.json run-baseline --output <新结果>`，无需原聊天工作区的数据路径。`resolved_config.json` 包含额外诊断字段，用于审阅，不直接作为配置输入。

源码归档解压后没有 `.git` 时也可以运行：新结果的 Git HEAD 为 null，使用源码清单摘要标识该导出版本。

`end_to_end` 输出允许网络反馈影响后续计算/发送，用于请求 TTFT、ITL 和 SLO；`frozen_network_replay` 固定流量提交时刻、大小、优先级及端点顺序，用于网络干扰归因，不能当作请求收益。诊断同时保存两种结果，分别位于 `run/` 与 `replay-*/`。`report.md` 是阅读入口，JSON/CSV 是数值依据。

正式诊断实现位于 `experiments/contention.py`，无需聊天工作区、私有路径或运行时 monkey patch。`RuntimeHooks` 仅提供事件/网络快照的副本，不额外采样、不增加仿真事件；回调不得修改仿真后端。

## 功能组合和模型限制

| 路径 | 支持 | 当前限制 |
| --- | --- | --- |
| Dense | 前缀复用、chunk/packed Prefill、连续 Decode、轮询/绑定预测/联合选择的已有组合 | 需要对应缓存/批量曲线；预测为有界近似，不能作为 SLO 保证 |
| MoE EP | 节点内 TP、跨节点 dispatch/combine、专家 FIFO、chunk/packed P、连续 D、KV/MLU | EP 仍禁止联合选择与绑定预测；主研究另关闭前缀缓存和绑定窗口 |
| 固定网络诊断 | audited 单 case/seed、固定请求、逐层立即 KV、1 ms 观测、节点内 TP | 原生网络优先级关闭；流类别须有独立端点对；端口重用冷却必须满足；输入未排空时失败 |

当前 EP8 是 8 个 TP team，不是其他引擎中 EP rank 数的通用定义；异步源 batch，没有全组 batch 屏障；Decode 仍为聚合 dense step 后执行 MoE 层。H800 profile 是 `estimated_not_target_calibrated`，专家路由是模型假设，SLO 来自实验配置。该轮整理不接入 vLLM、不升级这些执行语义。

`output_tokens` 是仿真终止真值，MLU/绑定预测使用声明的可见输出上限。轮询路径的物理显存预留仍沿用原实现，按仿真输出增长量计算；它不是实机引擎的在线 KV block 分配器。本次只拆清职责，保留该行为供后续单独升级。

## 第三方与回归入口

`third_party/ns3` 是实际网络后端；Vidur、AICB、SimCCL、ASTRA-Sim 当前为参考或采样/工作负载工具来源，未充当在线请求执行器。具体来源/许可证以 [manifest](../third_party/manifest.json) 为准。

完整回归运行 `./joint_scheduling/check.sh`。原功能测试保留；新增接口/状态所有权、观测副本、脏源码归档、固定流量完整重放测试。`backend/fixtures/runtime_equivalence.json` 冻结整理前 Dense/MoE 参考产物哈希，检查 token 时刻、请求结果、传输统计和资源释放不变；小 fixture 仅验证机制。Azure 四请求诊断另外按原输入重跑核对，不能用它评价持续负载承载率。
