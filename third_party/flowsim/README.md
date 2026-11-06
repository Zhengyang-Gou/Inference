# flowSim (vendored from netiken/m4)

流级（fluid / 最大最小公平）网络仿真核心，用作 `joint_scheduling` 的可选快速端到端网络后端。
本目录只保存**适配必要的上游源码**，不含训练代码、数据集、checkpoint 或 m4 学习模型。

## 来源

- 上游仓库：<https://github.com/netiken/m4>
- 上游提交：`95489830743c4ac2a97a050c86c7c2f32f264b04`（main，2026-06-19）
- 上游路径：`flowsim/`
- 内容哈希：见 `SOURCE_MANIFEST.json`（逐文件 sha256，格式 `inference-vendored-source-v1`），
  摘要同时固定在 `joint_scheduling/backend/flowsim.lock.json`。
- 引入方式：普通源码文件，不使用 submodule；未在上游目录内构建。

`flowsim/` 在 m4 中是独立目录，不依赖 SimAI 训练栈。它引用的 submodule
`inference/rapidyaml`（YAML 解析）只用于上游 `main.cpp`/`convert.py` 的命令行工具，
本适配不读取 YAML，也不使用这些脚本，因此未引入（见“排除文件”）。

## 许可证

上游仓库根目录**没有** `LICENSE` 文件，GitHub API 报告的 `license` 为 `null`。
本目录保留的上游源码文件头部保留原始版权声明，其中 `Topology.h` 声明
“licensed under the MIT license found in the LICENSE file in the root directory of this source tree”，
但该 LICENSE 文件在上游仓库中缺失。因此：

- 本仓库按“仅用于本研究的第三方参考源码”记录该依赖；
- 不对外声称 flowSim 的许可证已被核实为 MIT，重新分发前须向上游确认；
- 该声明与 `third_party/manifest.json` 中的 `license` 字段一致，不构成法律意见。

## 与上游的差异

本目录保存的是**上游原始字节**（`SOURCE_MANIFEST.json` 逐文件校验）。所有为在线适配、
单一时间权威和延迟模型所需的改动都以补丁形式在隔离构建目录内施加：
`joint_scheduling/backend/flow_patch.py`。补丁内容、原因和可审计的匹配断言写在该文件中，
并进入 `flowsim.lock.json` 的 `patch_sha256`。

## 排除文件（未引入）

上游 `flowsim/` 中以下文件本适配不使用，未引入：

| 文件 | 原因 |
| --- | --- |
| `main.cpp` | 离线批处理入口（读取 `.npy` 到达时间/流大小，整段跑完再写结果）；本适配改为在线提交/推进的 driver。 |
| `npy.hpp` | 仅被 `main.cpp` 用于读写 `.npy`。 |
| `TopologyBuilder.cpp/.h` | 从上游 fat-tree 文本构建拓扑；本仓库拓扑来自 `joint_scheduling/topology.export_backend_topology`，由 driver 直接 `Topology::connect`。 |
| `Makefile`、`run.sh`、`compare.py`、`convert.py`、`plot.py`、`scan.py`、`test.py` | 上游离线批处理/绘图脚本。 |

保留的核心（14 个文件）：`Type.h`、`Chunk.*`、`Device.*`、`Link.h`、`Event.*`、`EventList.*`、
`EventQueue.*`、`Topology.*`。它们提供：流级 chunk、逐方向链路容量、设备图、
事件队列（可取消、同时间戳有序）、渐进填充（progressive filling）带宽分配与完成回调。

## 模型要点与限制

- 速率单位：GB/s（`Bandwidth`），时间单位 ns，流大小 Bytes；`bytes / (GB/s)` 数值上等于 ns。
- 分配规则：每条有向链路上对活跃流做最大最小公平（progressive filling，逐轮固定瓶颈链路）。
  上游没有优先级、队列、ECN/PFC、重传或 ACK；链路 `latency` 字段在上游**未被使用**
  （补丁把它作为路径传播延迟加到完成时刻，并在结果中标注）。
- 路由：上游接受固定 `Route`；ECMP 由 driver 按 `(seed, src, dst, flow_id)` 确定性哈希选择，
  并写出路径映射，见 `joint_scheduling/backend/FLOWSIM.md`。
- 完成语义：字节全部完成传输（serialized）时刻，不是 ACK；无重传/乱序。
