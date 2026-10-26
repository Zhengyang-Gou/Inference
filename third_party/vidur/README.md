# Inference 的 Vidur 参考代码

保留原 SimAI 中的 Vidur 推理仿真实现，包括 P/D 分离、批处理、请求生成、性能预测、profiling 和指标代码。来源版本见 [组件清单](../manifest.json)，原许可见 [LICENSE](LICENSE)。

该代码用于后续接入和对照，尚未接入 `./inference run-baseline`。当前正式可运行流程见根目录 [README](../../README.md)，不需要安装这里的额外依赖。

已有本地路径修改按 Inference 布局保留：AICB 为相邻的 `third_party/aicb/`，配置中的根目录指向 Inference。原 `simai_simulation` / `simai_analytical` 预测路径仍需要单独构建的旧版模拟器，不会自动调用当前 ns-3 适配器。

`requirements.txt`、`environment*.yml`、`setup.py` 是这份参考实现自己的环境描述；其完整 GPU profiling 和旧版通信路径未纳入当前基线回归。
