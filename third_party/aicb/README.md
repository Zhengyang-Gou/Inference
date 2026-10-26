# Inference 的推理 workload 工具

从 AICB 保留以下推理相关部分，来源版本见 [组件清单](../manifest.json)，原版权声明见 [License](License)：

- `workload_generator/SimAI_inference_workload_generator.py` 与 `Vidur_workload_generator.py`。
- DeepSeek、Qwen3 MoE、Qwen3 Next 推理模型及 AIOB 采样实现。
- `scripts/inference_workload_with_aiob.sh`、`scripts/inference_configs/` 以及所需公共工具。

训练入口、训练模型、安装包、示例结果和宣传资料已经移除。推理入口中未使用的训练计算函数 `get_comp_out` 及其 import 也已删除。

这些工具用于后续 profiling 和 workload 研究，尚未接入当前请求基线。GPU 采样需要该实现自己的 PyTorch、vLLM、DeepGEMM 等环境；根目录 bootstrap 不安装这些可选依赖。

现有 CPU 基线读取 `joint_scheduling/backend/fixtures/` 所示的 JSON 性能和负载格式，不直接读取这里生成的旧版 SimAI/Vidur workload。
