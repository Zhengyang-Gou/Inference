# Inference 的集合通信参考模型

这里保存 SimCCL 的普通源码，包含 NCCL mock 版本、集合通信流量与依赖生成逻辑。来源版本见 [组件清单](../manifest.json)，原许可见 [LICENSE](LICENSE)。

当前请求基线在 `joint_scheduling/backend/runner.py` 中使用独立的 ring AllReduce 流量规则，尚未调用 SimCCL。这里的代码用于后续集合通信模型扩展。

可在 Inference 根目录独立编译，生成文件放入忽略目录：

```bash
./scripts/simai-python -m cmake -S third_party/simccl/src -B joint_scheduling/artifacts/simccl
./scripts/simai-python -m cmake --build joint_scheduling/artifacts/simccl --parallel 2
```

公共 ASTRA 类型从相邻的 `third_party/astra_sim/` 引入，不需要旧 SimAI 目录或子模块。
