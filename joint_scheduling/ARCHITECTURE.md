# 模块边界与扩展约定

执行语义、命令参数、配置字段及默认值保持兼容。主要依赖方向如下；配置与输入层不导入执行层：

```text
cli → 配置与输入工具 / 实验预检 / 运行器
experiments.runner → planning + parameters / backend.runner
backend.runner → 输入与 profile / execution
execution → 调度策略 / profile / 资源账本 / 网络客户端 → ns-3
配置、输入、profile → validation / 各模块配置类型
```

## 配置与输入

- `backend/settings.py` 管理运行配置与 KV 模式；`expert_config.py` 管理 EP 配置和部署约束。配置加载不导入执行器。
- `validation.py` 提供基础字段、数值与严格 JSON 校验；`workload/inputs.py` 负责请求文件、前缀身份和长度校验。
- `compute_profile.py` 与 `expert_profile.py` 只描述代价、路由和覆盖，不导入网络客户端或请求执行器。
- `backend.runtime`、`backend.inputs` 和 `backend.expert_parallel` 保留旧导入名称。新代码直接引用所属模块，避免通过兼容入口引入额外依赖。

## 命令与实验

- `__main__.py` 只保留入口，`cli/` 中每类命令一起维护参数注册与处理函数。增加命令时在所属命令文件中实现，再在 `cli.build_parser` 注册。
- `experiments/parameters.py` 统一维护允许扫描的参数和覆盖操作；绑定与缓存的字段由各自配置 dataclass 提供。新增扫描参数应同时检查执行路径是否使用它。
- `experiments/planning.py` 负责规格解析和对照预检，`comparison.py` 负责公平性审计；两者不反向引用运行器。
- `experiments/runner.py` 负责冻结输入和汇总，`execution.py` 独立执行一次运行，`parallel.py` 通过有界的 spawn 进程池调度。所有任务共用只读冻结输入，各工作进程只写自己的运行目录，主进程独占实时日志和汇总。仅在实际执行时加载网络执行依赖。固定流量诊断的预检同样不加载执行器。

## 运行时

`ExecutionCoordinator` 按依赖顺序装配组件；实例队列、计算、网络、P/D 批次、预算、显存、请求及 KV 调度状态由其所属组件初始化。状态所有权清单及组件间旧方法转发保持兼容。

新状态应登记到所属组件的 `owned_fields`，初始化放到该组件的 `init_*_state` 中。新组件调用优先使用明确的所属组件接口；已有绑定、EP 和合批算法继续通过兼容端口工作，尚未全部改为强类型接口。

## 验证

`./joint_scheduling/check.sh` 执行完整回归。`test_module_boundaries.py` 在独立解释器中检查配置、负载、profile、命令参数及研究预检不会引入执行依赖，并验证旧导入与后端独立命令的兼容性。`test_execution_components.py` 用冻结哈希核对 Dense/MoE 的 token、请求、批次统计和最终资源。
