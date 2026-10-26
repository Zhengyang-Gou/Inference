# 第三方源码与项目维护

Inference 由 Zhengyang-Gou 独立维护，主仓库为 `Zhengyang-Gou/Inference`。项目自己的入口、调度接口、配置、网络适配、资源账本及评估代码位于根目录和 `joint_scheduling/`。

`third_party/` 是随 Inference 一起管理的普通源码文件，不包含上游 Git 历史、远端、子模块或上游 CI。组件来自原 SimAI 源码树中与推理有关的部分；版本记录见 [组件清单](third_party/manifest.json)。其中的原始版权、许可证和作者声明继续保留。

| 本地目录 | 来源与版本 | 许可 | 用途 |
| --- | --- | --- | --- |
| `third_party/ns3` | aliyun/ns-3-alibabacloud，`3e0c7c1bfbbe9f77890ddcf5e5b9c79fc6dd7437` | [GPL-2.0](third_party/ns3/LICENSE)，具体文件沿用各自声明 | 当前基线实际使用的网络仿真后端 |
| `third_party/vidur` | aliyun/SimAI，`a57d765c5478f319c5f5ab6fdc53f9cc7f461743` 中的 `vidur-alibabacloud/` | [MIT](third_party/vidur/LICENSE) | P/D 分离、批处理和性能预测参考 |
| `third_party/aicb` | aliyun/aicb，`23eec3c48ca2d2d93dd888a4c7b22ab4421e782f` | [原始声明](third_party/aicb/License)、[Apache-2.0 全文](LICENSES/SimAI-Apache-2.0.txt) | 推理 workload 与计算采样工具 |
| `third_party/simccl` | aliyun/SimCCL，`fd7cd57d16f9bd42e3ccb70911c977e18ec294b9` | [Apache-2.0](third_party/simccl/LICENSE) | 集合通信流量与依赖模型 |
| `third_party/astra_sim` | 上述 SimAI 版本中的 `astra-sim-alibabacloud/` | [MIT](third_party/astra_sim/LICENSE) | SimCCL 公共类型及原计算/通信模型参考 |

原 SimAI 顶层的 Apache-2.0 许可另存于 `LICENSES/SimAI-Apache-2.0.txt`，不替代各组件自己的许可。本次整理不为项目自有代码新增开源授权。

## 本地整理与修改

- 移除 `SimAI/` 外壳、原 Git 数据、子模块声明、上游工作流、宣传图片、安装包、训练启动器和原示例结果。
- 保留已有的 Vidur、SimCCL 和 ASTRA 本地修改，并将相关默认路径调整为 Inference 的 `third_party/` 布局。
- AICB 仅保留推理生成入口、模型、配置及其公共依赖；移除不再使用的训练计算入口及训练模型。
- ns-3 保留完整仿真依赖源码；移除上游 Git 忽略文件和误收录的编辑器临时文件后，同步更新源码清单和内容锁。网络算法源码不因本次拆分而改写。
- 当前正式运行入口是 `./inference`。Vidur、AICB、SimCCL、ASTRA 的原执行路径没有接入当前请求基线；保留它们用于后续研究，并不表示这些路径已经过完整集成验证。

依赖更新由本仓库审查并提交源码及清单，不需要重新关联上游仓库或初始化 Git 子模块。
