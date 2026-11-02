# EP 下按 MLU 分级的 KV SLO 调度

`kv_scheduler.policy="slo_mlu"` 固定 P/D 去向，为 P→D KV 计算期限，按 MLU 晋升，并与 EP 分阶段通信共享有界发送额度。
这是 [Nuska v2 §4.3–4.5](https://arxiv.org/html/2603.17456v2) 的思想适配，不是完整复现：没有新增远程前缀拉取阶段、RLI/RED 全部算法或请求剪除。原 `slo_slack` 和 FIFO 保留作对照。

## 运行

```bash
./inference backend build --jobs 4
./inference --config joint_scheduling/configs/ep_mlu_smoke.json run-baseline --output joint_scheduling/artifacts/ep-mlu-example
./inference --config joint_scheduling/configs/ep_mlu_pressure.json run-baseline --output joint_scheduling/artifacts/ep-mlu-pressure
./inference run-experiments --spec joint_scheduling/configs/ep_mlu_comparison.json --output joint_scheduling/artifacts/ep-mlu-comparison
./inference run-experiments --spec joint_scheduling/configs/ep_mlu_fifo_comparison.json --output joint_scheduling/artifacts/ep-mlu-fifo
```

示例均为 synthetic。压力样本在 0.6 秒注入 24 个请求（40 rps，最后到达 0.575 秒），混合 8/12/16 输入 token、4 输出 token 和三组 TTFT/ITL 阈值；回放至 3 秒。这是控制逻辑验证，不是硬件容量测量。
`cluster_128gpu_tp4_ep8_mlu.json` 提供 16 节点×8 GPU、P/D 各 64 GPU、节点内 TP4 和跨节点 EP8 的空输入模板，需要另配目标模型 profile/workload。

要求 `layer_ready_slo`、`slo_priority_enabled=true`，并关闭联合绑定和绑定预测；轮询窗口可用。
独立的 `runtime.network_priority_enabled` 开关用于比较同一软件调度在真实优先级出队或原轮询下的行为。

## 截止时间与 MLU

当前首 token 在 P 返回，因此交接约束对应第一次 ITL。KV 最晚交付为 `t1 + ITL_SLO - estimated_post_KV_decode_wait - estimated_decode_step`。
P 未结束时使用预测 t1 并限制到 TTFT 截止；返回后使用真实 t1，不能用当前时刻后移锚点。
D 的等待估计包含整个 EP 组的当前任务与已提交 FIFO 任务，覆盖计划剩余计算和本地 TP。扣除可与当前传输重叠的等待，同时保留一轮可见 Decode 竞争者；只使用声明输出上限，不读取真实 EOS。

对每条有向资源 e 计算 `m[r,e] = remaining_bytes[r,e] / ((KV_deadline[r] - now - guard) * effective_capacity[e])`，再取 `m[r] = max_e m[r,e]`。
guard 包含 `mlu_deadline_guard_us`、观测延迟和调度开销。剩余字节扣除 D 命中、完整 ACK 和延迟可见的部分 ACK，仍包含未产出需求；只有就绪块可启动。
有效容量扣除安全余量和观测的其他流量，EP/KV 在仲裁与窗口中计费，避免把自身限速吞吐当作容量下降。
ECMP 对最短路径有向端口并集保守计费，不使用实际选路的未来结果。
每次决策也记录链路累计最低需求和超载资源；单请求 MLU≤1 不足以保证多请求均可调度。

## 等级与消息边界

| 逻辑等级 | 物理数据队列 | 用途 |
| --- | --- | --- |
| 0 | 1 | 达到紧急阈值且仍可行的 KV |
| 1 | 2 | 阻塞计算的 collective，包括 EP dispatch/combine |
| 2 | 3 | 中等紧迫 KV |
| 3 | 4 | 宽松 KV 与受限 best effort |

ACK/控制继续使用优先队列 0。默认 KV 晋升阈值是 0.125、0.5，属于本适配器的实验参数。
可行请求等级只升不降，同级按 MLU、截止和稳定就绪顺序仲裁。后续未发送块继承请求等级，已启动消息及其分片不改等级。
截止已过、预计生产太晚、TTFT 已不可达标或 MLU>1 的请求保留为 best effort，不晋升无限紧迫度，也不丢弃数据。
存在 EP 或可行请求竞争时，best effort 使用 `mlu_best_effort_fraction` 积累额度（默认 2%，桶至少容纳一整块）；共享链路没有这些竞争者时可借用空闲带宽。两种情况均服从普通 KV 的 EP 排空保护、总令牌与硬上限，不保证饱和 EP 下无饥饿。
层/阶段/网络/完成事件以及控制周期触发重评估，晋升只影响尚未发送的完整消息。

## EP 阶段与额度

MLU 模式分别登记预计/实际 dispatch、预计/实际 combine 的端点、字节工作量、开始时刻和孤立服务时间。当前 batch 最后一段 dense 计算开始后，用其计划结束时间与本地 TP 估计下一次 dispatch。
专家计算期间只登记预计 combine：guard 范围外可利用计算空档；FIFO 开始和专家完成信号修正其时刻。
EP 资源预留按阶段 byte demand/估计服务时间计算，不以固定 70% 贯穿整个调用；旧 `slo_slack` 保留整调用预留。

每资源维护总容量、普通 KV、best-effort 令牌。启动需满足全局并发、全局在途、每资源硬上限和带宽流水窗口。
普通 KV 额外服从 EP 预留及预计排空额度；紧急可行 KV 可绕过软保护，但不能绕过总令牌、硬上限或 Decode 准入名额。
`fixed` 固定带宽流水窗口，仍执行阶段排空保护。`collective_protection_enabled=false` 关闭软件 EP 预留；网络优先级是独立开关，完全无保护对照需同时关闭。
完成仅释放在途量，不返还发送令牌。低等级在途消息不取消，后续包可以在网络出队时让位。

## 原生网络

RPC 版本 4 保留 SEND 的逻辑等级，初始化携带优先级开关。
启用后，主机在未暂停、窗口可用、拥塞控制允许发送的 QP 中选最高等级，同级轮询；交换机数据队列按等级出队。控制/ACK 优先不变。
关闭后所有数据回到原队列 3，使用原主机/交换机轮询。ECMP、ACK、DCQCN、PFC 继续执行。节点内链路仍是非抢占 FIFO，不模拟 NVSwitch 优先级。

`backend/priority_patch.py` 只修改隔离构建；vendored ns-3 与 SHA256 清单不变。覆盖脚本、适配器和协议参与构建指纹，旧二进制必须重建。

## 观测与边界

- debug 的 `kv_scheduling_decision` 记录 MLU、各链路需求、期限、D/专家等待、等级、原因、超载资源和全部额度。
- `observation/kv_scheduler_summary.json` 始终保存晋升数、各等级字节、best effort、超载决策、阻塞原因与剩余状态；请求结束后清理等级历史。
- `observation/network_summary.json` 的 `data_priority_bytes[0..3]` 是包含包头/重传的实际发送字节；关闭优先级时数据均在槽 2（物理队列 3）。
- `result/summary.json` 包含策略累计统计。essential/debug 不改变执行，只改变记录量。

测试分别构造同源 NIC 和不同源汇聚交换机瓶颈，比较开关下高等级短流和低等级长流的真实完成时间。
其他验证覆盖分级、单链路瓶颈、累计超载、单调晋升、延迟反馈、硬额度、专家等待、EOS 隔离、前缀命中、Decode 准入、截止资源与记录等价。
截止预算、EP 时刻和服务时间仍为局部启发式估计，Decode 沿用 dense step 后接 MoE 的实验模型；MLU 不是全部 SLO 的硬保证。
