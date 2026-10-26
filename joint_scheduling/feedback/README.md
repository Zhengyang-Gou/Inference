# 计算与网络反馈闭环的接入边界

本目录定义 Inference 的公共接口；实际适配器是 `joint_scheduling/backend/client.py` 的 `Ns3Backend`，基线执行器是 `joint_scheduling/backend/runner.py`。路径均相对于 Inference 仓库根目录。构建、运行与模型边界见 [后端说明](../backend/README.md)。

## 时间与依赖

- `SimulationClock` 由 ns-3 提供唯一仿真时间，Python 侧不再独立推进另一条时间轴。
- GPU 计算使用离线 profiling 数据；该数据只包含计算部分，显式模拟的网络通信不得重复计时。
- 当前简单基线按计算步骤推进，Prefill 完成后整份 KV 就绪；相关集合通信完成后才推进依赖它的计算。
- KV 与集合通信使用同一个网络实例和同一套资源，新基线使用实际网络完成事件，不调用原 P/D 固定交接延迟路径。
- 源端读取依赖解除与目标端 KV 可读是两种事件。不能未经验证就将 `SendComplete` 或 `QpComplete` 同时解释为这两者。
- 当前网络完成采用发送端已确认全部字节 ACK 的保守语义，此时目标可用且源端读依赖已解除，可能晚于目标最早可读时间。
- P 返回首 token、D 返回后续 token 及请求完成事件已经接入 `evaluation.TraceRecorder`。

## 现有代码入口

| 位置 | 用途 |
| --- | --- |
| `third_party/vidur/vidur/events/batch_end_event.py` | 保留的上游 P/D 交接延迟与 Decode 接入逻辑，尚未接入当前请求基线 |
| `third_party/vidur/vidur/events/batch_stage_end_event.py` | 上游 PP stage 结束事件；**它不是 Transformer 单层 KV 就绪事件** |
| `third_party/ns3/simulation/src/point-to-point/model/rdma-driver.{h,cc}` | 网络提交接口及完成回调入口 |
| `third_party/simccl/` | 保留的集合通信点对点流量与依赖模型，尚未接入当前请求基线 |

先校准单请求、单流和单次集合通信，再验证并发竞争的反馈；调度器可见的是带观测延迟的当前状态，不包括未来真实事件。

新入口是显式的简单请求基线，不复刻完整 Vidur/Sarathi 批处理。后续接入原执行器时应复用这个网络实例与时间轴，避免同时使用两套互不感知的通信模型。逐层 KV 和优化策略尚未实现。

第三方源码以普通文件随本仓库保存，来源见根目录 `third_party/manifest.json`。网络构建根据 `third_party/ns3/SOURCE_MANIFEST.json` 和后端内容锁校验 SHA256，无需初始化 Git 子模块或保留嵌套 Git 仓库。
