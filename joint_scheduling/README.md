# Inference 的 P/D 联合绑定与 KV 调度模块

这里集中维护 Inference 自有的调度、前缀缓存、执行后端、观测和评估模块，以及统一配置和后续接入接口，项目环境使用 Python 3.12。本目录直接位于 Inference 仓库根目录；第三方源码位于根目录的 `third_party/`，不是 Git 子模块。

推荐从 [研究系统说明](RESEARCH_SYSTEM.md) 和 [研究配置](configs/research/README.md) 开始。`backend/execution/` 按职责组合运行时；旧命令和 `backend.runtime` 的计算 profile 导入保持兼容。

代码的依赖方向、配置与输入归属、命令扩展方式及兼容入口见 [模块边界](ARCHITECTURE.md)。配置、负载和实验预检可独立使用，执行组件负责初始化各自状态。

**已实现拓扑后端导出、物理对象容量账本、ns-3 仿真 时钟/网络适配器、简单请求基线及 evaluation 的统计与绘图。已实现 schema 2 逐层 KV 就绪与分块立即传输、动态网络观测、集合通信保护和 SLO 门控。`run-baseline` 支持候选剪枝＋增量贪心或轮询绑定，可与前缀缓存、FIFO/Chunked/合批 Prefill 和 Decode 连续批处理组合；预测采用有界近似，不复刻完整 Vidur/Sarathi 批处理；计算参数需要显式提供并验证覆盖范围。**

实验性 [EP 接入](backend/EXPERT_PARALLEL.md) 可使跨节点 collective 使用 EP dispatch/combine、TP 保持节点内，支持共享专家计算队列和五种 KV 模式；支持轮询和 P 轮询/D 均衡随机的静态绑定，EP 联合选择与绑定预测会明确拒绝。

## 目录

```text
joint_scheduling/
├── workload/              # 可复现的请求到达、长度和 SLO 生成
├── observation/           # 事件驱动状态、延迟可见性与时间积分统计
├── experiments/           # 参数矩阵、多 seed 执行与失败隔离
├── configs/
│   └── default.json        # 各模块共用的实验配置与功能开关
├── topology/              # 物理拓扑、P/D 到 GPU 的映射、检查用拓扑导出
├── binding/               # 窗口式联合绑定配置、整体方案预测与策略接口
├── kv_scheduler/          # 层就绪 KV 块、启动许可与调度接口
├── feedback/              # 统一时钟、网络提交和依赖完成接口
├── reservations/          # 物理对象级预留与生命周期接口
├── prefix_cache/          # 前缀身份、P/D 复用与有界 LRU 缓存
├── evaluation/            # 事件记录、统计、CSV/JSON 导出、离线绘图
├── backend/               # 固定版本构建、ns-3 适配器、简单基线与回归
├── bootstrap.sh           # 创建或更新 Conda 环境 simai
├── jsim                   # 固定项目环境下的统一入口
├── check.sh               # 全部测试与分组网络仿真 smoke
├── config.py              # 统一加载与跨模块检查
├── validation.py          # 公共字段校验
├── cli/                   # 按职责分组的命令参数和处理函数
└── __main__.py            # 兼容的统一命令入口
```

模块之间通过接口共享同一资源状态和时间基准。拓扑只描述物理资源与既定部署，实例绑定与 KV 启动是后续策略的决策。

## 配置与使用

在 Inference 仓库根目录执行。首次使用统一入口前先运行 `./joint_scheduling/bootstrap.sh` 创建 Conda 环境 `simai`；手动执行 Python 前先运行 `conda activate simai`，纯配置检查也可使用 `python -m joint_scheduling`：

```bash
./inference validate
./inference describe
./inference export-topology --output joint_scheduling/artifacts/topology.json
```

默认 export-topology 导出检查用 JSON 图；增加 `--format simai` 会导出实际网络后端使用的拓扑。两者都拒绝覆盖已有文件。后端端点采用服务器共享 NIC 聚合，详细语义见 backend/README.md。

复制 `configs/default.json` 为另一份实验配置后，可显式指定：

```bash
./inference --config joint_scheduling/configs/my_experiment.json validate
./inference --config joint_scheduling/configs/my_experiment.json describe
```

| 配置段 | 修改内容 |
| --- | --- |
| `topology` | Leaf/Spine 数量、节点与 GPU 数量、带宽时延，以及实例到 GPU 的部署映射 |
| `binding` | 窗口/预测开关、常规窗口、提前触发、最大等待、决策开销与有限预测范围、联合选择开关、候选与工作预算 |
| `kv_scheduler` | KV 策略名称、块大小上限、并发、在途字节额度及网络安全余量 |
| `feedback` | 时间权威、网络与计算后端、计算性能数据路径、观测延迟与调度开销 |
| `reservations` | 默认最大输出预留、安全余量、共享前缀计数与源端释放规则 |
| `prefix_cache` | 缓存总开关、P/D 独立复用、每实例缓存预算与 LRU 淘汰开关 |
| `evaluation` | 测量窗口、时间桶、默认 SLO、分位数、输出位置、图表与格式 |
| `runtime` | 基线入口的种子、负载、KV 传输模式、终止时间、拥塞控制、MTU、缓存、RPC 超时和提交批次 |
| `expert_parallel` | 可选实验性 EP 开关及同角色 TP 实例分组，默认关闭，需额外 EP profile |

配置校验会拒绝未知字段、重复 JSON 键、非有限数值、重复 GPU 部署、缺少 P/D 任一角色，以及明显矛盾的跨模块额度。未填写字段使用默认值；五个基础配置段和部署列表必须存在，evaluation/runtime/prefix_cache 可省略以兼容已有配置。功能开关总表见根目录 README；前缀缓存默认关闭，详细格式见 [前缀缓存说明](prefix_cache/README.md)。

计算性能数据路径相对于配置文件所在目录解析，也可使用绝对路径。空路径允许检查目录与配置结构；`validate` 不检查性能文件内容，`run-baseline` 会加载并校验其格式、采样范围及 TP。真实研究应提供匹配目标硬件和模型的数据。

## 初始配置的含义

- 示例为 2 Leaf、2 Spine、每个 Leaf 下 4 节点、每节点 2 GPU，共 16 GPU。
- GPU ID 从 0 连续编号；`node_id = gpu_id // gpus_per_node`，`leaf_id = node_id // nodes_per_leaf`。
- 每个机架暂对应一个 Leaf，每台服务器暂抽象为一个共享的全双工网卡；各方向需分别核算容量。
- `p0/d0` 分别使用第一个机架的两对节点，`p1/d1` 对应第二个机架，各实例的 GPU 互不重叠。
- 这个部署允许比较同 Leaf 与跨 Leaf 的 P/D 绑定，并为跨节点集合通信与 KV 的共享资源竞争提供条件。
- 参数是功能示例值；正式仿真使用 research 配置。检查图不展开 GPU 内部互联；后端通过可配置的共享局部 FIFO 链路表示它。

## 单位与资源语义

- JSON 字段名显式标注 `ms/us`、`Gbps`、`GiB`、`bytes`；公共事件接口统一使用秒和字节。
- `Gbps` 按十进制换算为字节每秒，`GiB` 按 `2**30` 换算为字节。
- `gpu_memory_gib` 是物理容量；最终资源账本需先扣除模型参数、固定缓冲及安全余量，再核算占用和预留。
- 输出 token 预留值是默认上限；运行时若请求声明自己的最大输出长度，应依据其实际允许上限预留。
- 最小归一化 SLO 余量必须考虑所有适用指标，交接间隔与后续最大 ITL 使用请求的 ITL 阈值；整体方案预测应纳入已有请求的影响。
- `PlanPredictor` 接收完整临时绑定方案，避免把孤立请求的分数当作固定分数相加。
- KV 门控控制启动许可，底层网络决定实际速率。已经启动的传输继续执行，源端依赖解除后才能释放 KV。
- 共享前缀按物理对象与内存域去重；预留转为实际占用时等量转换，避免重复计数。
- 当前仅暴露已确认存在的 ECMP 配置名；自适应路由和理想多路径对照应在有对应后端实现后再添加。

## 运行分组网络仿真基线

```bash
./joint_scheduling/bootstrap.sh
./inference backend build --jobs 2
./inference backend doctor
./inference --config joint_scheduling/configs/baseline_smoke.json run-baseline --output joint_scheduling/artifacts/my-run --plot
./joint_scheduling/check.sh
```

示例计算数据明确标记为 synthetic，分组网络仿真基线能够验证反馈和统计，但其数值不代表真实模型性能。现已支持显式逐层 Prefill 与 KV 分块立即传输，也支持动态带宽额度、集合通信保护与 SLO 门控；可在同一后端继续研究联合绑定。模型边界、输入格式与版本维护步骤见 backend/README.md。

## 统计与绘图

```bash
./inference record-demo --output joint_scheduling/artifacts/demo/events.jsonl
./inference evaluate --trace joint_scheduling/artifacts/demo/events.jsonl --output joint_scheduling/artifacts/demo/result
./inference plot --results joint_scheduling/artifacts/demo/result --output joint_scheduling/artifacts/demo/figures
```

绘图需要在对应 Python 环境安装 `joint_scheduling/evaluation/requirements-plot.txt`。demo 是明确标记的合成数据，不是策略效果实验。精确指标口径、未完成请求处理、接口与多策略比较见 `evaluation/README.md`。

第三方组件的来源和版本记录在根目录 `third_party/manifest.json`。当前网络后端使用 `third_party/ns3/SOURCE_MANIFEST.json` 与 `backend/backend.lock.json` 校验 vendored 源码 SHA256，再在隔离副本中构建；无需初始化子模块或保留上游 Git 元数据。`third_party/vidur`、`aicb`、`simccl` 和 `astra_sim` 是保留的第三方组件，尚未接入当前请求基线。绑定窗口和整体方案预测已接入，有界联合优化已接入，见 [绑定模块说明](binding/README.md)。已实现的 KV 策略由 runtime.kv_transfer_mode 显式选择，SLO 算法见 kv_scheduler/README.md。反馈接口见 feedback/README.md。

## 实验基础功能

负载、观测和批量运行已接入统一 CLI。用 `generate-workload --spec ... --output ...` 创建真实基线可读的请求文件；`run-baseline` 自动生成 observation/，`--plot` 同时生成状态时间线；`run-experiments --spec ... --output ...` 默认串行执行参数矩阵，增加 `--jobs N` 可并行运行独立 case/seed，并导出 runs.csv、comparison.csv 与 summary.json。详情见 workload/README.md、observation/README.md 和 experiments/README.md。
