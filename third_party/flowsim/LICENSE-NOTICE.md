# flowSim 许可证与来源说明

本文件是 `third_party/manifest.json` 中 `flowsim` 组件的 `license_file`。它**不是**许可证文本，
而是一份诚实的缺口说明：上游仓库没有提供许可证文件。

## 事实

- 上游仓库：<https://github.com/netiken/m4>
- 上游提交：`95489830743c4ac2a97a050c86c7c2f32f264b04`
- 上游路径：`flowsim/`
- 该仓库根目录没有 `LICENSE`、`COPYING` 或等价文件；GitHub API 返回的 `license` 字段为 `null`。
- 部分上游源码头部的版权声明写着 “This source code is licensed under the MIT license found in the
  LICENSE file in the root directory of this source tree.”，但该 LICENSE 文件在仓库中并不存在。

## 本仓库的处理

- 按 `third_party/flowsim/SOURCE_MANIFEST.json` 逐文件 sha256 保存上游原始字节，不做改写。
- 在 `third_party/flowsim/README.md`、`third_party/manifest.json`、后端 metadata 和实验报告中
  都标注“上游未声明许可证”，不把该组件称作 MIT 许可软件。
- 该组件仅用于本研究内部的流级网络仿真实验；在重新分发或对外发布前，需要向上游作者确认许可条款。
- 本说明不构成法律意见。

## 与 MIT 参考实现的关系

上游 `flowsim/` 的拓扑/事件队列代码风格与 astra-sim 社区实现相近，但不能据此推断许可证：
`third_party/astra_sim` 是另一份独立引入的 MIT 代码，二者不互相替代，也不能用前者为后者背书。
