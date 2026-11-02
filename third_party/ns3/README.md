# Inference 网络后端依赖

这里保存 `aliyun/ns-3-alibabacloud` 的普通源码文件，没有 Git 子模块。来源版本见 [组件清单](../manifest.json)，原许可见 [LICENSE](LICENSE)。

`simulation/` 保留当前网络后端的完整依赖闭包，以及后续拓扑、流量监测、并行仿真所需模块；具体保留范围见 [目录说明](../README.md) 和组件清单中的 active_modules / future_modules。无线、蜂窝等无关模块、绑定生成器和通用示例已裁去，相关模块的模型实现、文档与测试仍保留。

当前基线只编译 core、network、internet、point-to-point 及其依赖，使用自有的 `joint_scheduling/backend/native/driver.cc`，不执行原 SimAI 应用入口。裁剪不修改保留的网络算法源码。

在 Inference 根目录运行：

```bash
./inference backend build --jobs 2
./inference backend doctor
```

`SOURCE_MANIFEST.json` 和 `joint_scheduling/backend/backend.lock.json` 校验源码内容。编译发生在 `joint_scheduling/artifacts/backend/` 的隔离副本中。修改源码后必须审查并同步更新清单和内容锁，再跑基线回归。

2026-10-28：联合绑定高开销压力触发了上游 PFC Resume 将优先级作为 QP 索引的空指针错误。`joint_scheduling/backend/build.py` 在隔离副本上应用精确、有指纹校验的恢复发送修补；本目录原始网络源码及 SOURCE_MANIFEST 不变。适配器同时补齐交换机 PFC 控制地址。修补与真实 incast 回归见 backend 说明和 binding 验证报告。
