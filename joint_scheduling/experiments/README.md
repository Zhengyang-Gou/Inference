# 批量实验运行

运行器串行执行参数矩阵，复用现有真实 ns-3 基线与评估模块。每个运行拥有独立输入、网络进程和结果目录，状态观测自动开启。

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
- 拒绝 binding.policy、kv_scheduler.policy、调度开销等当前未执行的策略参数扫描，避免给相同执行规则贴不同算法标签。

输出包括：

```text
suite/
├── plan.json             # 输入规格、解析后的基础配置与完整运行计划
├── environment.json      # Python 解释器、版本与平台
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

某次失败时默认继续后续运行；只要出现失败，CLI 返回非零退出码 2，同时保留汇总。`--fail-fast` 会停止后续执行并将剩余条目标记为 skipped。失败不当作零吞吐成功样本：失败数明确报告，指标均值只使用成功运行；无有效样本为 null，一个样本的样本标准差为 null。

arrived/failed/incomplete 按到达 cohort 统计，completed/吞吐按完成窗口统计，延迟来自到达 cohort 中已完成请求。不同窗口或负载的结果不能直接解释为算法收益。每次记录真正执行的轮询/FIFO/整份 KV 规则；生成器与示例计算数据均为 synthetic，当前批量实验用于机制和管线验证。

运行时长 wall_seconds 仅表示工具执行耗时，不是仿真时延。比较性能时使用 completion_throughput_rps、slo_goodput_rps 和各延迟指标。
