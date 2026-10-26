# Inference 的 ASTRA 参考与公共类型

保留原 SimAI 的计算/通信模型、SimCCL 使用的公共类型和原构建脚本。来源版本见 [组件清单](../manifest.json)，原许可见 [LICENSE](LICENSE)。

该组件未接入当前请求基线。正式网络后端通过根目录 `./inference backend build` 构建，不需要执行这里的原构建脚本。

`build/` 内的 CMake 与 shell 文件是源文件，应纳入主仓库；其中后续生成的 `build/*/build/` 和 `build/*/result/` 才是忽略目录。原构建脚本仍保留历史 backend 路径，仅作参考，未作为新的 Inference 构建入口。
