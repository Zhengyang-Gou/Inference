# Inference 网络后端依赖

这里保存 `aliyun/ns-3-alibabacloud` 的普通源码文件，没有 Git 子模块。来源版本见 [组件清单](../manifest.json)，原许可见 [LICENSE](LICENSE)。

`simulation/` 保留完整 ns-3 仿真源码依赖。当前基线只编译 core、network、internet、point-to-point 及其依赖，使用自有的 `joint_scheduling/backend/native/driver.cc`，不执行原 SimAI 应用入口。

在 Inference 根目录运行：

```bash
./inference backend build --jobs 2
./inference backend doctor
```

`SOURCE_MANIFEST.json` 和 `joint_scheduling/backend/backend.lock.json` 校验源码内容。编译发生在 `joint_scheduling/artifacts/backend/` 的隔离副本中。修改源码后必须审查并同步更新清单和内容锁，再跑基线回归。
