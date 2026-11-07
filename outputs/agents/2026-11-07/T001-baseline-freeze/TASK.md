# T001：冻结第一阶段唯一基线条件

- 执行角色：执行 agent；当前聊天为指挥 agent，负责审核。
- 仓库：`/home/m0nesy/Postgraduate/Inference`
- Windows 入口：`\\wsl.localhost\Ubuntu\home\m0nesy\Postgraduate\Inference`
- 唯一任务目录：`/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T001-baseline-freeze`
- 草稿目录：`/home/m0nesy/Postgraduate/Inference/work/agents/2026-11-07/T001-baseline-freeze`
- 前置任务/报告：无。本任务不继承或恢复已撤销的旧任务。

## 原始需求与本轮问题

用户已确定第一阶段：固定每个请求的 P/D 绑定，研究 KV 与 EP 竞争，以及 KV 调度对请求性能的影响；以 SLO 达标完成吞吐为主，同时观察 TTFT、交接间隔、后续 ITL、失败和未完成请求；使用一套固定基础配置。

本轮只回答：后续基础验收和算法对照应共同使用哪一份明确、可复现的基线条件？

本轮是条件核对、冻结与静态预检，不是基础链路仿真验收，也不是算法测试。

## 交付目标

生成一份完整的基线条件表、一组可加载的冻结配置和输入，以及机器可核对的身份清单。任务完成后，指挥应能直接审阅参数、来源、绑定、SLO和开关，无需从旧聊天拼接信息。

## 范围与固定条件

以当前 `joint_scheduling/configs/research/base.json` 为唯一配置来源，在本任务目录生成副本。正式源码、正式配置、profile、原始数据不修改。以下是本轮冻结要求；若实际文件存在实质矛盾，按停止条件报告，不悄悄换模型或改输入。

| 类别 | 本轮要求 |
| --- | --- |
| 模型 | 现有 DeepSeek-V3/H800 TP1、EP16/16 的16K估算profile；61层，前三层Dense、后58层MoE；标明估算和合成路由边界 |
| 规模与部署 | 4节点×8GPU＝32GPU，P16/D16，TP1；P/D分别一个EP16组；保留现有placement |
| 网络 | ns-3；每GPU400Gbps独立NIC；Leaf/Spine拓扑、上联、延迟、拥塞控制和packet参数沿用base的实际值并完整列出；节点内保留gpu_ports、每GPU TX/RX各160GB/s |
| Prefill | chunk上限512；每实例批次token预算2048、请求上限16 |
| Decode | 每实例batch上限64，逐层61层执行；不能替换成旧aggregate fixture |
| 绑定 | balanced_random，binding.seed=7；P轮询、D按轮均衡随机；为所有请求冻结同一绑定计划，跨策略复用 |
| 主负载 | data/azure/conv_original.json的完整Azure2023对话输入；预期19366请求，保留请求ID、配对长度、源顺序和原始相对到达时间，逐字节复制 |
| 样本处理 | 本轮不筛选、截短、循环、压缩时间或改变到达率，不生成新的压力样本；完整输入作为来源冻结不代表获准全量运行，后续有界功能样本另行明确规则 |
| SLO | 默认TTFT=1.0秒、ITL=0.1秒；明确属于人工实验假设，审计所有请求是否有覆盖默认值的字段 |
| 随机性与窗口 | runtime.seed=7；沿用base的统计起止、预热和排空时限，在条件表写出精确数值与半开区间语义；不在本轮调窗口找收益 |
| 观测 | 网络观测开启，周期1ms、延迟100μs；状态观测开启，用于后续排队与资源分析；recording.mode=essential；说明观测不是优化策略 |
| 模拟调度开销 | 保留10μs配置假设，标明不是实测CPU耗时；同时写明立即发送路径不经过受控KV决策，不能把未生效参数写成实际延迟 |

负载和profile来源分别为：
- `/home/m0nesy/Postgraduate/Inference/data/azure/conv_original.json`
- `/home/m0nesy/Postgraduate/Inference/joint_scheduling/profiles/deepseek_v3_h800_tp1_ep16_ep16_estimated.json`

正式输入保持完整；下一轮基础功能验收可以在另行指定的有界样本上进行。本轮不得把完整输入冻结理解成可以启动约58分钟仿真时间的全量包级运行。Azure2024不在本轮范围。

### 基础验收模式的开关

在冻结副本中明确设置：

```json
{
  "binding.window_enabled": false,
  "binding.prediction_enabled": false,
  "binding.joint_selection_enabled": false,
  "binding.decision_overhead_us": 0,
  "prefix_cache.enabled": false,
  "runtime.kv_transfer_mode": "layer_ready_immediate",
  "kv_scheduler.policy": "ready_immediate",
  "kv_scheduler.slo_priority_enabled": false,
  "kv_scheduler.collective_protection_enabled": false,
  "kv_scheduler.decode_admission_limit": 0,
  "runtime.network_priority_enabled": false
}
```

以上为字段路径说明，实际配置必须使用仓库要求的嵌套JSON结构。

基础模式保留逐层/逐chunk KV生产和就绪即发，不启用FIFO/MLU门控；不是整份KV等到Prefill结束才发。保留EP计算/通信、显存准入、源读保护、通信完成依赖和请求/token评估。其他KV额度参数即使存在，也须注明在当前无门控模式下是否生效。不同策略后续仅能改变明确指定的策略项，本轮不创建或运行策略矩阵。

### 指标口径

主指标为测量窗口内“完成且全部适用SLO通过”的请求数除以窗口时长。TTFT包含到达后的等待；handoff为首、第二个token的间隔；后续ITL单独统计。单token请求没有handoff或后续ITL，不能补0。

条件表应区分completion window与arrival cohort，以及失败、未完成和排空阶段完成的处理。不得仅使用已完成请求中的达标率代替主指标。

## 执行步骤

1. 阅读根AGENTS.md、本TASK及以下材料（路径均相对于已给定的仓库根目录）：joint_scheduling/configs/research/README.md、joint_scheduling/configs/research/base.json、data/azure/README.md、data/azure/manifest.json、joint_scheduling/evaluation/README.md、joint_scheduling/config.py、joint_scheduling/binding/static.py。不用旧实验结论补证据。
2. 保留本TASK原文，将STATUS.json更新为in_progress并填写执行者和时间；保存执行前Git状态到logs/baseline-status.txt。已有未提交修改属于执行前状态，不回退、不清理。
3. 审计当前配置的显式值与默认值、实际输入路径、profile覆盖范围和输入身份。统计请求数、时间范围、长度范围、最大Decode上下文、重复ID及请求级SLO覆盖情况；检查chunk/批量曲线的静态覆盖。本轮不测吞吐和网络利用率。
4. 在results/frozen/生成如下冻结文件：
   - config.json：完整有效的嵌套配置，按上述要求设置；profile/workload/evaluation输出使用相对本配置的路径，保证从冻结文件加载时不会引用旧任务目录。
   - compute_profile.json、workload.json：原文件逐字节复制，不修改内容。
   - binding_plan.json：用现有静态绑定实现生成所有请求的确定性P/D计划，记录计划哈希、种子和分配计数，不启动执行器。
   - manifest.json：记录原文件路径及SHA256、冻结文件SHA256、从原base到冻结config的实际字段差异、Git HEAD和执行前脏状态、模型假设及参数生效范围。不要声称本轮已经完成源码/二进制运行归档。
5. 仅执行静态验证：使用项目入口加载并validate冻结config；用项目环境核对profile/workload及绑定计划的身份、覆盖和一致性。命令、目录、退出码与关键输出写入logs/。配置摘要可作辅助，但最终绑定策略以实际配置和binding_plan为准。
6. 写results/baseline.md、results/input-audit.json及REPORT.md。报告逐项回答下述验收，不把静态检查写成已跑通仿真。状态改为submitted后停止。

## 交付与验收

| 编号 | 通过标准 | 证据 |
| --- | --- | --- |
| A1 | 唯一条件表完整列出配置、负载、绑定、SLO、窗口、指标、开关及模型边界 | results/baseline.md |
| A2 | 冻结配置能加载并通过结构/约束校验；差异仅为本任务指定的基础开关、观测与可搬移路径 | results/frozen/config.json、manifest.json、logs/ |
| A3 | 请求/profile逐字节保持来源，身份可核对；输入范围和所有请求级SLO覆盖已审计；覆盖不足如实列出 | results/input-audit.json、冻结文件SHA256 |
| A4 | 19366请求有完整确定性P/D绑定；相同输入、种子重新生成一致，不按KV策略或网络状态重绑 | results/frozen/binding_plan.json、核对日志 |
| A5 | 清楚区分模型实际执行的字段与关闭后无效的字段；未更改EP、Chunked Prefill、连续Decode、容量与完成依赖 | 条件表、有效配置与必要源码引用 |
| A6 | 无源码/正式配置/原始数据修改；无端到端仿真、算法矩阵、扩容或新机制；报告与状态完整 | Git差异核对、REPORT.md、STATUS.json |

## 失败与停止条件

- 若实际输入身份与manifest不符、profile覆盖不足、SLO覆盖字段与规定冲突、配置校验失败，保存证据并明确列出缺口；不删除请求、不改长度、不换synthetic fixture、不调阈值来通过。
- 普通字段展开、路径处理、脚本与报告组织自行完成，不反复请求用户决定。
- 遇到需要改源码、换模型、换输入、扩大规模或改变上述冻结要求的情况，停止依赖该决定的工作，以blocked/failed如实提交；不自行扩展为修复任务。
- 禁止run-baseline、backend smoke、run-experiments实际运行和完整check.sh；不构建后端、不安装新依赖、不做MLU计时或收益评估。必要验证仅限本任务静态预检。
- 不提交或推送，不启动其他agent，不恢复已撤销的旧任务，不继续下一轮基础运行验收。

## 回报给指挥

按仓库coordination/templates/REPORT.md填写本目录REPORT.md，最后回复：
`/home/m0nesy/Postgraduate/Inference/outputs/agents/2026-11-07/T001-baseline-freeze/REPORT.md`
以及一句话结论、实际静态检查结果、未完成项和需要指挥决定事项。执行完成不等于审核通过；等待指挥写REVIEW.md。
