# Inference

Inference 是面向大模型 Prefill–Decode 分离推理的调度研究仓库，研究 P/D 实例联合绑定、KV 传输与集合通信竞争，以及请求级 SLO 达标完成吞吐。

当前可运行的是 **CPU 上的真实 ns-3 网络简单基线**：请求到达 → P 计算与集合通信 → 返回首 token → 整份 KV 交接 → D 输出后续 token → 导出指标与图表。已实现配置校验、拓扑、物理对象容量账本、统一时间与网络反馈、统计和绘图；**联合优化、逐层 KV 就绪与 SLO 启动门控尚未实现**。

这是独立维护的 Inference 项目。第三方源码以普通文件保存在 `third_party/`，构建不依赖嵌套 Git 仓库或 Git 子模块。第三方代码的来源和许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 目录与职责

```text
Inference/
├── inference                     # 项目统一命令入口
├── joint_scheduling/              # 自有配置、调度接口、后端、资源与评估代码
│   ├── configs/                   # 默认配置与可运行的 smoke 配置
│   ├── binding/                   # 联合绑定数据结构与策略接口
│   ├── kv_scheduler/              # KV 块与启动许可接口
│   ├── topology/                  # 物理拓扑和 P/D 到 GPU 的部署映射
│   ├── reservations/              # 物理对象、共享引用和读依赖账本
│   ├── feedback/                  # 统一时钟与依赖完成接口
│   ├── backend/                   # ns-3 适配器和简单请求基线
│   └── evaluation/                # 请求/token 轨迹、统计与绘图
├── third_party/                   # 保留来源及许可的第三方源码
│   ├── ns3/                       # 当前基线使用的网络后端
│   ├── vidur/                     # 保留的推理仿真参考实现
│   ├── aicb/                      # 保留的计算/通信 workload 生成工具
│   ├── simccl/                    # 保留的集合通信流量模型
│   ├── astra_sim/                 # 保留的计算/通信仿真框架
│   └── manifest.json              # 第三方组件来源与版本记录
├── THIRD_PARTY_NOTICES.md          # 第三方许可、来源与裁剪说明
├── scripts/check_repository.py     # 独立仓库结构与网络源码完整性检查
├── joint_scheduling_explanation.md # 研究方案与目标
├── assets/                        # 方案图
└── .github/workflows/inference.yml # 基线回归工作流
```

`third_party/vidur` 等保留组件尚未接入当前请求基线。当前执行器使用独立的轮询/FIFO 规则，不复刻完整 Vidur/Sarathi 批处理，也不支持连续批处理、PP/EP 或共享前缀命中加速。

外层 Git 仓库由本项目独立管理；`SimAI/`、嵌套 Git、上游子模块和上游工作流已移除。保留的第三方源码随本仓库一起维护，原版权和许可见上述说明。AICB 已裁去训练入口、训练模型、安装包和历史示例结果。

## 快速运行

在本仓库根目录执行。需要 Linux、Conda 和 C++ 编译器；无需 GPU、MPI 或管理员安装。项目统一使用名为 `simai` 的 Conda 环境，Python 固定为 3.12，CMake 版本固定。首次创建环境需要联网下载依赖。

```bash
./joint_scheduling/bootstrap.sh
conda activate simai
./inference backend build --jobs 2
./inference backend doctor
./inference --config joint_scheduling/configs/baseline_smoke.json run-baseline --output joint_scheduling/artifacts/my-first-run --plot
./joint_scheduling/check.sh
```

`baseline_smoke.json` 使用明确标记为 `synthetic` 的计算数据，仅验证反馈、网络竞争与统计管线，数值不能代表真实模型性能。默认配置没有计算性能文件和负载，运行时会要求补齐，不会自动退回合成数据。结果目录拒绝覆盖。

`bootstrap.sh` 创建或更新 `simai`，显式使用 conda-forge 安装 Python 3.12，再从项目 requirements 安装运行依赖；根目录 `environment.yml` 同步记录环境定义。`./inference` 和 `check.sh` 会自动调用 `simai`；手动运行 Python 时先执行 `conda activate simai`。

当前网络模型使用服务器共享全双工 NIC、ECMP、RDMA 与拥塞控制；节点内通信采用共享局部 FIFO 链路。计算时间来自离线采样，计算文件不得重复包含本后端显式模拟的通信时间。详见 [后端说明](joint_scheduling/backend/README.md)。

## 配置、评估与研究入口

```bash
./inference validate
./inference describe
./inference export-topology --output joint_scheduling/artifacts/topology.json
./inference record-demo --output joint_scheduling/artifacts/demo/events.jsonl
./inference evaluate --trace joint_scheduling/artifacts/demo/events.jsonl --output joint_scheduling/artifacts/demo/result --plot
```

配置字段与跨模块语义见 [模块说明](joint_scheduling/README.md)。统计分别保留 TTFT、P 到 D 的交接间隔、后续最大 ITL、端到端时延和达标完成吞吐；失败与未完成请求不会被当成完成样本。具体口径见 [评估说明](joint_scheduling/evaluation/README.md)。

建议先读 [研究方案](joint_scheduling_explanation.md)，再从 `joint_scheduling/backend/runner.py` 跟踪一条请求，结合 `reservations/ledger.py` 和 `evaluation/metrics.py` 理解资源生命周期与指标。策略名称 `joint_lexicographic`、`slo_slack` 目前表达预留接口；修改名称不会使优化算法生效。

## 第三方版本与构建校验

`third_party/manifest.json` 记录第三方组件的来源和版本。当前网络构建读取 `third_party/ns3/SOURCE_MANIFEST.json`，核对 vendored 源码的 SHA256，并与 `joint_scheduling/backend/backend.lock.json` 的内容锁匹配；上游提交号仅作为来源记录，不要求本地存在相应 Git 元数据。

`./scripts/simai-python scripts/check_repository.py` 可以在未安装项目依赖、未构建后端时检查目录完整性、嵌套 Git 残留与网络源码内容。`check.sh` 和 CI 也会执行此检查。克隆本仓库后无需执行 `git submodule update`。

后端在 `joint_scheduling/artifacts/backend/` 中建立隔离副本，记录工具链、构建指纹及二进制 SHA256。变更第三方网络源码时，应同步审查来源清单、内容锁和回归结果。`check.sh` 检查统计、资源、真实网络及端到端确定性；GitHub Actions 在推送或拉取请求中执行相同基线回归。
