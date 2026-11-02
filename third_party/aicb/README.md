# Inference 的推理 workload 工具

从 AICB 保留以下推理相关部分，来源版本见 [组件清单](../manifest.json)，原版权声明见 [License](License)：

- `workload_generator/SimAI_inference_workload_generator.py` 与 `Vidur_workload_generator.py`。
- DeepSeek、Qwen3 MoE、Qwen3 Next 的符号模型和工作量描述。
- `scripts/inference_configs/` 以及离线数据解析公共工具。

训练入口、训练模型、安装包、示例结果和宣传资料已经移除。推理入口中未使用的训练计算函数 `get_comp_out` 及其 import 也已删除。

这些 CPU 工具只读取已有的离线计算代价，通过 `--cost-file` 指定输入。可选依赖见 `requirements.txt`。它们尚未接入主请求基线；正式研究使用本仓库 JSON profile 及其覆盖检查。

现有 CPU 基线读取 `joint_scheduling/backend/fixtures/` 所示的 JSON 性能和负载格式，不直接读取这里生成的旧版 SimAI/Vidur workload。
