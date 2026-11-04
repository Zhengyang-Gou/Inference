# 批量实验运行

代码按职责分为 `planning.py`（规格解析与预检）、`parameters.py`（允许的参数与覆盖）、`comparison.py`（配对审计）、`runner.py`（冻结输入与汇总）、`execution.py`（单次运行）、`parallel.py`（有界进程调度）及 `contention.py`（固定流量诊断）。`plan_suite` 和 `run_suite` 的原调用入口保留；预检不加载请求执行器或网络客户端。

运行器串行执行参数矩阵，复用现有ns-3 仿真 基线与评估模块。每个运行拥有独立输入、网络进程和结果目录，观测按配置启用。

当前 EP/KV 主研究使用 [research 配置](../configs/research/README.md)，本页其余旧示例用于功能与接口验证。

`audit-ep` 提供独立的逐 token 路由/字节参考及可选的实际流依赖核验，使用方法、参考 gate 约束与模型差异见 [EP 流量核验](../backend/EP_FLOW_AUDIT.md)。核验通过不修改当前实验语义。

`experiment_type` 缺省为 `end_to_end`，明确设置为 `frozen_network_replay` 时运行正式竞争诊断：一个已审计的固定输入 case/seed，端到端运行后重放 both / EP only / KV only，再输出比较和 `report.md`。该类型要求节点内 TP、EP 开启、逐层立即 KV、原生网络优先级关闭和 1 ms 采样，并检查端点对及端口重用以保持重放身份。示例是 `configs/research/contention_diagnostic.json`。

所有端到端运行保存 `reproducibility.json`（配置/请求/profile 哈希、源码树及后端身份、模型估算状态）。suite 在 `inputs/source/` 冻结一次源码，各次通过相对路径引用，移动结果时保留整套目录；单次 baseline 也独立冻结源码。`runs.jsonl/csv` 增加实验类型、源码摘要和后端 fingerprint；每套输出 `report.md`。详细文件职责见 [研究系统](../RESEARCH_SYSTEM.md)。

## 配对策略与启动前审计

同一策略需要同时切换多个配置字段时，使用 `cases`，不要用独立字段的笛卡尔积生成无效组合：

```json
"cases": [
  {"id": "mlu_priority_off", "overrides": {}},
  {"id": "mlu_priority_on", "overrides": {"runtime.network_priority_enabled": true}}
]
```

`cases` 与 `sweep` 互斥；case 名须为唯一的小写字母开头、最多 64 字符的字母/数字/下划线/连字符。两种方式均限制最多 10,000 次运行，原有 `sweep` 接口和失败记录规则保持兼容。可选 `common_overrides` 在所有 case 之前统一覆盖基础配置，例如短程验证共用的截止时间和统计窗口；它只接受已实现的配置参数，不修改请求输入。

可选 `comparison` 合约包含四项：

- `purpose`：`kv_policy`、`network_priority` 或 `validation`。
- `allowed_changes`：case 唯一允许覆盖的配置字段。
- `fixed`：必须满足的配置值，包括通过 `common_overrides` 修改后的值。
- `input_sha256`：固定的 `profile` 和 `workload` 两个 SHA-256；要求使用固定请求文件。

预检加载每组有效配置，检查基础配置/各组及所有组间的真实差异，核验输入身份、完整请求和合批的 profile 覆盖、EP 兼容约束。`--dry-run` 输出审计结果，实际执行会在顶层写入 `comparison_audit.json`。审计还记录共同配置摘要、模型估算状态、当前 EP team 语义、SLO 来源和统计条件；随后冻结的输入字节必须与审计哈希一致。隐藏的带宽/批量差异和非法组合在创建输出目录、启动网络后端前失败。

没有声明 `comparison` 的旧规格仍按原规则执行，不能仅凭它们成功运行就认定各组适合公平对照。主研究的完整合约见 `configs/research/kv_policy.json` 和 `network_priority.json`。

## 通用参数矩阵

在 Inference 根目录执行：

```bash
./inference run-experiments --spec joint_scheduling/configs/experiments_example.json --output joint_scheduling/artifacts/suite-example --dry-run
./inference run-experiments --spec joint_scheduling/configs/experiments_example.json --output joint_scheduling/artifacts/suite-example
```

示例为 2 种 NIC 带宽 × 2 种请求到达率 × 2 个 seed，共 8 次运行。`--dry-run` 不创建目录或启动后端。`--plot` 为每次运行生成评估图和状态时间线。输出目录已存在时拒绝覆盖。

规格示例：

```json
{
  "schema_version": 1,
  "base_config": "baseline_smoke.json",
  "workload_spec": "workload_example.json",
  "seeds": [7, 19],
  "sweep": {
    "topology.nic_bandwidth_gbps": [100, 200],
    "workload.arrival.rate_rps": [150, 300]
  }
}
```

所有输入路径相对于实验规格文件解析；base_config 内的 profile/workload 路径仍相对于 base_config。运行开始时冻结计算采样与固定负载/生成规格；每次配置使用冻结文件的绝对路径，避免子目录路径失效或实验途中原文件变化影响后续运行。

- 可以用 `workload` 指向固定请求文件，或使用 base_config 指定的固定请求文件；不能同时提供 workload 和 workload_spec。
- `profile` 可覆盖整个实验的计算采样文件。
- 默认每个 seed 同时用于网络和负载生成。相同 seed 在不同网络配置下生成相同负载；改变到达率时，请求长度和 SLO 也保持对应。可用固定 `workload_seed` 让不同网络 seed 使用同一份生成负载。
- sweep 按键名排序后做笛卡尔积；配置组合是 case，seed 是该组合的重复运行。seed 和同一参数的选项不允许重复；单个实验最多 10,000 次运行。
- 支持 topology 的直接字段、已实现的 runtime 参数、evaluation 的直接字段（不含 output_dir）、feedback.observation_delay_us、reservations.capacity_safety_fraction，以及 workload 生成规格的字段。
- 修改部署可用 `topology.placements` 提供整个数组；不支持通过点路径索引数组元素。改变 GPU 数与实例 TP 后仍需匹配 profile。
- 支持 binding.policy 与联合选择开关、候选数和预算扫描；`kv_scheduler.policy` 支持已实现的 `slo_slack` / `slo_mlu`，必须满足相应运行模式约束。调度开销及 KV 控制参数扫描要求至少包含一个受控 KV 模式。
- 支持 runtime.kv_transfer_mode 和 kv_scheduler.max_block_bytes 扫描；后者仅影响分块模式。kv_comparison_example.json 对同一份 schema 2 层模型执行整份、完成后分块、逐层立即三组对照，共 18 次。

输出包括：

```text
suite/
├── plan.json             # 输入规格、解析后的基础配置与完整运行计划
├── environment.json      # Python 解释器、版本与平台
├── execution.json        # 并发数、启动方式、结果顺序与 fail-fast 语义
├── inputs/               # 启动时冻结的计算采样、负载或生成规格
├── runs.jsonl            # 每次完成或失败后立即追加并刷新
├── runs.csv              # 每次运行的状态、参数、输入哈希与指标
├── comparison.csv        # 各配置跨 seed 的样本数、均值与样本标准差
├── summary.json          # 汇总及统计口径
└── runs/case-0000-seed-7/
    ├── config.json
    ├── workload.json
    ├── error.json        # 该次失败时记录异常和堆栈
    └── run/              # 正常 baseline 结果、network/ 和 observation/
```

某次失败时默认继续后续运行；只要出现失败，CLI 返回非零退出码 2，同时保留汇总。`--fail-fast` 停止提交新任务，尚未提交的条目标记为 skipped，已提交任务完成收尾。失败不当作零吞吐成功样本：失败数明确报告，指标均值只使用成功运行；无有效样本为 null，一个样本的样本标准差为 null。

arrived/failed/incomplete 按到达 cohort 统计，completed/吞吐按完成窗口统计，延迟来自到达 cohort 中已完成请求。不同窗口或负载的结果不能直接解释为算法收益。每次记录真正执行的轮询/联合绑定、FIFO Prefill 和 KV 规则；adaptive_comparison_example.json 比较逐层立即、保护下 FIFO 与保护下 SLO，使用不同到达时间和请求级阈值；生成器与示例计算数据均为 synthetic，当前批量实验用于机制和管线验证。

运行时长 wall_seconds 仅表示工具执行耗时，不是仿真时延。比较性能时使用 completion_throughput_rps、slo_goodput_rps 和各延迟指标。

前缀缓存对照见 `configs/prefix_cache_comparison_example.json`：缓存关闭/开启 × 三种逐层 KV 模式 × 两个 seed，共 12 次。支持扫描 `prefix_cache` 的实际配置字段、`kv_scheduler.slo_priority_enabled`、`kv_scheduler.collective_protection_enabled`、`kv_scheduler.inflight_budget_mode`、`feedback.network_observation_enabled` 和 `feedback.state_observation_enabled`。受控 KV 开关仍要求矩阵中包含受控模式。输入与前缀身份在各组冻结共享，`runs.csv` 和 `comparison.csv` 增加 prefix_hit_tokens、prefix_saved_kv_transfer_bytes、prefix_resident_bytes；命中 token 是 P/D 两侧累计查询命中的总和，驻留字节是结束时的缓存占用。

## 多进程运行

`--jobs` 指定最多同时执行的独立 case/seed 数，默认 `1` 保留串行方式。例如：

```bash
./inference run-experiments --spec joint_scheduling/configs/research/kv_policy.json --output joint_scheduling/artifacts/research/kv-policy-parallel --jobs 4
```

实际进程数不超过实验总数。多个任务使用 `spawn` 启动的独立 Python 工作进程，各次运行创建自己的 ns-3 后端和输出目录；单次仿真的时间轴、计算/通信顺序与策略不变。`run_suite(..., jobs=4)` 提供相同能力，`jobs` 是新增的仅限关键字参数。作为脚本调用进程版 API 时，将入口放在 `if __name__ == '__main__':` 保护中。

主进程先冻结 profile、负载/生成规格和源码，并为审计要求一致的对照生成一份绑定计划，再启动任务。工作进程只写自己的运行目录；主进程独占 `runs.jsonl`、CSV 和汇总的写入。实时日志和进度回调按完成顺序交付，最终 `runs.csv`、比较表及汇总按计划顺序排列。每条执行记录包含 `worker_pid`；`execution.json` 和 `summary.json.execution` 保存请求并发数、实际上限、启动方式及失败处理语义。

并行 `--fail-fast` 在观察到失败后停止提交新任务：已提交任务允许完成，尚未提交的任务记为 skipped，不创建其运行目录。普通异常和进程池/子进程异常均保留失败记录，不作为零吞吐成功样本。默认串行时仍在第一次失败后跳过后续运行。

`--dry-run --jobs 4` 只报告计划与执行设置，不启动工作进程或创建输出目录；`--jobs` 必须是正整数。固定流量诊断规格只有一个任务，端到端阶段和三次重放仍串行，实际并发数记录为 1。

并行任务会竞争宿主 CPU/内存，`wall_seconds` 和绑定的实际 CPU 计时可能变化；仿真时间使用原有显式开销。测量单任务控制开销时使用 `--jobs 1`。进程数属于执行设置，不参与策略参数扫描或改变仿真配置。

## 持续加压

每次运行默认继承 `recording.mode="essential"`，省略该配置也使用必要记录。配置、请求/token 时间、结果与失败信息保留；队列/在途量的配置窗口统计从在线积分写入 `observation/summary.json`，不用重新读取全量状态文件。需要逐事件排查时在 base config 中显式设置 `recording.mode="debug"`，详见 [实验记录规则](../RECORDING.md)。

绑定窗口/预测对照使用 `configs/binding_comparison_example.json`，窗口关/开 × 预测关/开 × 两个 seed 共 8 次。支持扫描 `binding` 的已实现字段及 `policy`；此旧规格未开启联合选择，实际端点记录为 round_robin。每次运行的 binding_summary.json 记录绑定等待、窗口触发和预测误差，诊断字段也进入评估 summary。

`sustained_pressure_comparison_example.json` 使用固定 1 秒到达时长，扫描 25/75/150/300 请求每秒、10/40/200 Gbps、立即/FIFO/SLO 三种模式与 seeds 7/19，共 72 次。请求输出为 64/128/256 token，三组共享相同输入和阈值，全部上下文处于显式 synthetic profile 的覆盖范围。

也可分开运行：`sustained_pressure_low_bandwidth_example.json` 选择 10 Gbps 的 25/75/150 档，共 18 次；`sustained_pressure_high_bandwidth_example.json` 选择 40/200 Gbps 的全部四档，共 48 次。低带宽已过载后可停止继续加档，但应完整保留每个选定档位的时长和 seed，并明确记录略过的档位。

预热区间为 [0,0.2)，测量区间为 [0.2,0.9)，请求持续到 1 秒；不包含停止到达后的排空阶段。除完成窗口吞吐外，使用 `evaluation/pressure.py` 统计 token 吞吐、起止未完成请求数、平均/峰值在途量、队列积分，以及到达 cohort 的 SLO 达标率上下界。截止时未完成的请求不能算成功：已违反 TTFT/ITL 的记为确定超限，其余标为未知。结束时剩余内存/在途量可非零，属于压力截止状态，不能误判为完成后的泄漏。

联合绑定对照使用 `joint_heterogeneous_comparison.json` 和 `joint_uniform_comparison.json`：相同 workload/profile/seed/开关，比较轮询和有界联合选择，并扫描 0/5/50/200/1000/5000 μs 显式仿真开销。新增列记录 binding 候选评分数、预算耗尽数与实际整轮墙钟 P50/P95/P99、包含快照构造的 P99；墙钟诊断不改变仿真。模型近似见 [绑定模块说明](../binding/README.md)；历史结果已清理，后续实验重新记录。

组合执行示例为 `configs/composed_execution_comparison.json`：相同小型 synthetic profile/负载，
开启前缀缓存及 P token 预算合批，比较轮询/联合 × D 批量 1/4 × seeds 7/19。
运行器支持扫描 `runtime.prefill_chunk_tokens`、`prefill_batch_token_budget`、`prefill_batch_size` 与 `decode_batch_size`；
组合必须满足相同的 profile 覆盖和配置约束，实际执行方式以每次 `effective_baseline` 为准。
