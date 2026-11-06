# flowSim 流级网络后端

本文件是 flowSim 后端的构建、切换、字段含义、模型限制与当前支持范围的唯一说明。
后端选择的唯一入口是 `feedback.time_authority` / `feedback.network_backend` 这一对字段，
由 `joint_scheduling/backend/factory.py` 解析；Prefill/Decode/EP/KV/MLU/策略代码中没有第二处后端分支。

## 1. 角色

| 后端 | 选择 | 时间权威 | 用途 |
| --- | --- | --- | --- |
| ns-3 包级 | `time_authority="ns3"`, `network_backend="ns3_alibabacloud"`（默认） | ns-3 事件循环 | 包级复核：队列、ECN/PFC、DCQCN、ACK 时序 |
| flowSim 流级 | `time_authority="flowsim"`, `network_backend="flowsim"` | flowSim 事件队列 | 快速端到端策略筛选：流完成时间与带宽争用 |

只允许这两个组合；混用（`ns3 + flowsim`、`flowsim + ns3_alibabacloud`）在 `load_config` 阶段报错。
旧配置不写这两个字段时仍是 ns-3 默认值。

**不是**第二种执行语义：请求到达、P/D 绑定、逐层 Prefill/Decode、EP 路由去重、KV 放行、
SLO/MLU 策略、观测采样和评估口径全部复用同一套 Python 运行时；只有网络服务过程被替换。

## 2. 来源固定

| 项 | 值 |
| --- | --- |
| 上游仓库 | <https://github.com/netiken/m4> |
| 上游提交 | `95489830743c4ac2a97a050c86c7c2f32f264b04` |
| 上游路径 | `flowsim/` |
| 引入文件 | `third_party/flowsim/` 下 14 个上游源码（`Type.h`、`Chunk.*`、`Device.*`、`Link.h`、`Event.*`、`EventList.*`、`EventQueue.*`、`Topology.*`） |
| 内容哈希 | `third_party/flowsim/SOURCE_MANIFEST.json`，逐文件 sha256；摘要 `e60595b85ef70f988b98ce6f1964129a2f8d59eb632ba36e586d053bc15766d2` |
| 锁文件 | `joint_scheduling/backend/flowsim.lock.json`（协议版本、提交号、源摘要、补丁摘要、完成语义） |
| 许可证 | **上游未声明**：仓库没有 LICENSE 文件；部分文件头引用的 MIT LICENSE 缺失。见 `third_party/flowsim/LICENSE-NOTICE.md` |
| 未引入 | `main.cpp`、`npy.hpp`、`TopologyBuilder.*`、`Makefile`、`run.sh`、`compare.py`、`convert.py`、`plot.py`、`scan.py`、`test.py`、`inference/rapidyaml` 子模块、m4 训练代码/数据/checkpoint |

上游 `flowsim/` 是一个离线批处理工具（读取 `.npy` 到达时间与流大小，整段跑完再写 FCT）。
本任务把它作为**流级仿真核心**接入在线运行时，因此新增 driver 与本地补丁，而不是改写成另一个模拟器。

### 本地补丁（`joint_scheduling/backend/flow_patch.py`）

补丁只施加到隔离构建副本，`third_party/flowsim` 保留上游原始字节；每处替换都带“必须恰好匹配一次”的断言，
匹配失败即停止构建。补丁摘要记录在 `flowsim.lock.json` 的 `patch_sha256`。

| # | 文件 | 改动 | 原因 |
| --- | --- | --- | --- |
| 1 | `EventQueue.*` | 新增 `next_event_time()` 与 `advance_to()` | 上游只有阻塞式 `proceed()`；在线适配必须能在没有事件的截止时刻推进时钟（采样定时器可能尚未提交） |
| 2 | `EventQueue.cpp` | `proceed()` 允许事件时间等于当前时间 | 上游 trace 不会在同一纳秒到达两条流；在线提交与 0 延迟定时器会 |
| 3 | `EventQueue.cpp` | 事件 id 从 1 开始 | 上游 0 既是首个 id 又是“无完成计划”标记，二者冲突（见 #6/#7） |
| 4 | `Chunk.*`、`Topology.cpp` | 记录路径传播延迟并加到完成时间；剩余字节为 0 的 chunk 立即完成 | 上游 `Link::latency` 从未被读取，完成时间只有串行化项，小流会在 1 ns 内“完成” |
| 5 | `EventList.cpp` | `invoke_event()` 先复制 event 再 `pop_front()` | 上游先弹出再通过引用调用回调，是 heap-use-after-free（AddressSanitizer 在首次实测即报告） |
| 6 | `Topology.cpp` | `cancel_all_events()` 不再取消 id 0 | 列表已弹出后取消该 id 会解引用失效迭代器（第二处 use-after-free） |

以上 4、5、6 属于**必须**修复才能在线运行的问题；1、2、3 是单时间权威与截止语义所必需。
补丁不改变公平分配算法、不引入优先级、不改变拓扑。

## 3. 构建与切换

```bash
cd /home/m0nesy/Postgraduate/Inference
export CONDA_EXE=/home/m0nesy/miniconda3/bin/conda

# 只构建/检查 flowSim（不编译 ns-3）
./joint_scheduling/jsim backend build-flowsim --jobs 2
./joint_scheduling/jsim backend doctor          # 同时报告 ns-3 与 flowSim 状态

# 小样本端到端回归（流级）
./joint_scheduling/jsim backend smoke-flowsim

# 切换后端：配置里改两个字段
#   "feedback": {"time_authority": "flowsim", "network_backend": "flowsim", ...}
./joint_scheduling/jsim --config joint_scheduling/configs/baseline_smoke_flowsim.json \
    run-baseline --output joint_scheduling/artifacts/flowsim-first-run
```

对照实验（同一 5 请求 Azure 样本，两后端各一次）：

原派生负载的后端配对规格已按用户要求移除。端到端复核使用当前任务 outputs/inputs/ 中由保留的原始Azure2023/2024生成的明确有界输入与临时规格，保证两后端共享同一输入及绑定计划。不要引用已删除的固定压力输入。

构建产物在 `joint_scheduling/artifacts/backend-flowsim/current/`：
`source/`（隔离副本，已打补丁）、`build/joint-scheduling-flowsim`、`build.log`、`build_manifest.json`。
构建指纹包含源摘要、补丁、driver、工具链、平台与仓库路径；运行前校验可执行文件 SHA256，
陈旧或被替换的二进制会拒绝启动。flowSim 后端**不**读取 `backend.lock.json`，也不调用 `require_backend()`：
ns-3 未构建时流级后端仍可独立运行。

## 4. 单时间权威与生命周期

- 协议与 ns-3 适配器相同（`protocol_version=4`）：`SEND` / `TIMER` / `LOCAL` / `LOCAL_PORTS` / `STATS` / `ADVANCE` / `CLOSE`。
- **flowSim 事件队列是唯一时间权威**：流完成、`TIMER`（计算完成、采样、调度触发）都在同一个 `EventQueue` 上。
  Python 侧 `now_ns` 只镜像 driver 返回的时间，不存在两个时钟各自前进。
- `ADVANCE deadline` 最多推进到 `deadline`：若下一个事件不超过截止，只处理**那一个时间戳**并返回
  `TIME now ids...`；否则把时钟推进到 `deadline` 并返回空列表。这样每个完成 epoch 都会先回到 Python，
  上层可以立即提交后继计算/通信，语义与 ns-3 适配器的“完成即中断推进”一致。
- 同一纳秒内的事件按插入顺序处理（含回调中新排入的同刻事件），顺序确定且可复现。
- 完成回调恰好一次：流完成时 `FlowComplete` 先记字节、再清除 chunk 指针、最后从活跃集合移除；
  重复回调被忽略并计入 `diagnostics`。`TransferProgress` 的 `source_reads_complete`/`destination_ready`
  在流级模型中同时置真：字节全部到达目的地即为完成，**没有 ACK**。
- 速率变化时先结算已传字节：每次提交新流、每次处理完成 epoch、每次 `STATS` 都先按
  `(now - transmission_start) × rate` 结算，再重算瓶颈与完成事件。上游在重排时会取消旧完成事件，
  因此不会留下幽灵回调；补丁 #7 修掉了“首个流的旧完成事件未被取消”的退化（否则首发流会保留竞争前的完成时间）。
- 截止时仍在活跃的流保留在 `STATS.flows` 中，`acked_bytes` 反映已服务字节；运行结束时如实保留，不补记完整字节。

## 5. 资源模型

- **逐方向链路**：拓扑文件里每条链路按全双工展开为两条有向链路（`link:src->dst`），
  因此源 NIC 发送容量、目标 NIC 接收容量、叶脊上联容量天然按方向分离；互不共享链路/端口的流并行。
- **公平分配**：每条有向链路上对本流集合做最大最小公平（progressive filling，逐轮固定瓶颈链路）。
  一条流在其路径的每个瓶颈上取最小速率；同一 KV 整份传输只占**一个**流，只有一份公平权重。
- **路由**：先按跳数最短路（BFS），再在候选下一跳中按 `mix(seed, src, dst, flow_id, device)` 选择，
  等价于确定性 ECMP 哈希；路径表在 `network/flowsim_routes.json` 中导出（设备、链路、ECMP 组），
  同一 seed + 同一提交顺序可复现。ns-3 的 ECMP 哈希作用于 5 元组，具体分配到哪条上联可能不同，
  这是**已声明近似**，不通过调参抹平。
- **传播延迟**：完成时间 = 串行化时间（`bytes / rate`）+ 路径各跳 `link_latency_us` 之和。
  没有逐跳存储转发、没有排队延迟、没有丢包/重传。
- **节点内**：`gpu_ports` 模型下每个 GPU 有独立 TX/RX 端口，端口内 FIFO 串行、方向独立、不共享全节点锁；
  `shared_fifo` 模型下一个节点内所有本地传输共享一个 FIFO。两者都与 ns-3 适配器的本地服务顺序一致，
  本地流量不经过网络图，也不与远端流争用链路。
- **`64 MiB` 上限**：这是 ns-3 可靠接收窗口的消息拆分上限，不是 KV 块大小。flowSim 侧一个逻辑传输就是一条流，
  不做 64 MiB 拆分，因此不会重复计字节、重复回调或给同一 KV 多份公平带宽。
- **优先级**：`NetworkTransfer.priority`（0–3）会被校验、随流记录，并进入逐链路 `data_priority_bytes`，
  但流级模型**不实现严格优先级队列**：带宽仍按等权 max-min 分配。因此 `runtime.network_priority_enabled=true`
  与 flowSim 组合在配置阶段即失败（不静默忽略），需要优先级语义的实验必须用 ns-3。

## 6. 能力与观测映射

`NetworkObserver` 从 `network_snapshot()` 读取 `time_ns`、`resources[]`、`flows[]`。下表给出 flowSim 中每一项的实际来源。
单位：字节、纳秒、`capacity_bps` 为 bit/s。

| 字段 | flowSim 来源 | 单位/定义 | 说明 |
| --- | --- | --- | --- |
| `time_ns` | `EventQueue::current_time` | ns | 与 Python `now_ns` 相同，读取快照不推进时间 |
| `resource_id` | `link:src->dst` 或 `local:gpu-N:tx/rx`、`local:N` | 字符串 | 逐方向链路；本地端口与 `topology.local_resources()` 的键一致 |
| `node_id` / `peer_id` | 有向链路两端设备 id；本地资源为宿主/GPU | 整数 | 设备编号：0..host-1 是端点，其后是交换机 |
| `capacity_bps` | 链路带宽 ×8；本地端口为提交的速率 | bit/s | 由拓扑文件/配置决定 |
| `tx_bytes` | 该有向链路上所有流已服务字节之和（活跃流按 `rate × elapsed` 结算，完成流按整份大小） | 字节 | 随服务进展更新，不是运行结束补记 |
| `kv_bytes` / `collective_bytes` / `other_bytes` | 按流的 `kind` 分类累计 | 字节 | `other` 恒为 `tx - kv - collective` |
| `data_priority_bytes[4]` | 按 `priority` 分类累计 | 字节 | 记录真实字节；**不代表**优先级调度效果 |
| `queue_bytes` | 恒 0，另给出 `active_jobs`（本地端口） | 字节 | 流级模型没有包队列，不伪造积压 |
| `pfc_pause_count` | 恒 0 | 次 | 不建模 PFC；消费者得到明确的 0 而不是随机噪声 |
| `latency_ns` | 链路传播延迟 | ns | 仅链路资源有 |
| `flows[].id` | 逻辑传输对应的 driver 事件 id | 整数 | 与 `TransferProgress.transfer_id` 通过 `logical_id` 关联（`LineRpcBackend.network_snapshot` 填充） |
| `flows[].acked_bytes` | 已服务字节（`min(size, elapsed × rate)`） | 字节 | **不是 ACK**：没有 ACK 机制 |
| `flows[].size_bytes` | 提交的逻辑传输大小 | 字节 | 与 ns-3 相同 |
| `flows[].rate_bytes_per_second` | 当前公平速率 | 字节/秒 | 流级模型独有，便于诊断 |
| `flows[].path` | 实际设备路径 | 列表 | 便于核对路由近似 |
| `diagnostics` | 活跃 id 数、流启动/完成数、定时器数 | 计数 | 不参与策略 |

观测延迟、EWMA、利用率、峰值与采样间隔仍由 `observation/network.py` 计算：`utilization`
= `tx_bytes_per_second × 8 / capacity_bps`，与后端无关。`StaticNetworkObserver` 对 flowSim 同样可用。

## 7. 明确不支持的机制

| 机制 | 状态 | 消费者得到的语义 |
| --- | --- | --- |
| 包级队列、ECN、DCQCN、PFC | 不建模 | `queue_bytes=0`、`pfc_pause_count=0`；`metadata.queue_model="none"`、`pfc="not_modeled"`、`congestion_control="not_modeled"` |
| ACK / 重传 / 乱序 | 不建模 | 完成=目的地收齐字节；`completion_semantics="all_bytes_serialized_at_destination_no_ack"` |
| 严格优先级 / 分类 0–3 调度 | 不支持，配置阶段拒绝 | 打开 `runtime.network_priority_enabled` 会 `ConfigError`；关闭时等权分配，没有暗中优待 EP |
| 冻结原生日历重放（`submit_frozen_event`） | 不支持 | 调用即 `BackendError`；`frozen_network_replay` 规格不能用 flowSim |
| 每包/每微秒级尾延迟 | 不产生 | 不能把流级完成时刻当成 ACK 或包级延迟 |
| 链路级丢包、错序、重传风暴 | 不产生 | 只能用于机制筛选，不能作为拥塞控制结论 |

因此：**流级用于筛选、包级用于复核**。同一策略在流级显示无收益，不能据此否定依赖 DCQCN/ECN/PFC
或微突发机制的结论；反过来，流级显示收益也必须用 ns-3 在同一输入上复核。

## 8. 已知近似与边界

1. 路由按 `(seed, src, dst, flow_id)` 哈希选下一跳，与 ns-3 的流元组哈希不同，可能存在上联分配差异。
2. 完成时间不含逐跳存储转发与排队；小流（KB 级）的相对误差主要来自传播延迟近似。
3. 同刻完成的流在 1 ns 内依次回调（补丁 #4 已把上游的“重付一个传播延迟”降为 1 ns）。
4. 速率只在新流加入、流完成和查询时重算；两次事件之间速率恒定（流级模型的固有离散化）。
5. ns-3 的 `packet_payload_bytes`、`switch_buffer_bytes`、`congestion_control` 对 flowSim 无效果，
   但仍写入 metadata 的 `*_requested` 字段，便于对照时看出“请求了但未生效”。
6. 上游未声明许可证，见 `third_party/flowsim/LICENSE-NOTICE.md`。

## 9. 复现与排错

- 构建日志：`joint_scheduling/artifacts/backend-flowsim/current/build.log`。
- 运行日志（stderr）：每次运行的 `network/flowsim.log`。
- 路由表：每次运行的 `network/flowsim_routes.json`。
- 后端身份：`network/backend_metadata.json`、评估目录 `reproducibility.json` 的 `backend` 字段、
  以及 `events.jsonl` 的 `metadata.backend`。
- 协议自检：`joint_scheduling/artifacts/backend-flowsim/current/build/joint-scheduling-flowsim --protocol-version` → `JSB_PROTOCOL 4`。
- 单元测试：`./joint_scheduling/jsim backend build-flowsim && python -m unittest joint_scheduling.backend.tests.test_flowsim -v`。
