# 远端 Linux 执行约定

默认仓库：/home/s223/m0nesy/Inference，分支 main。
GitHub：https://github.com/Zhengyang-Gou/Inference.git。
Windows SSH 别名：m0nesy，实际登录用户 s223，地址121.48.163.223，端口22。
本机 SSH 配置为 C:\Users\33199\.ssh\config；不将密钥写入仓库或同步包。

## 执行命令

从 Windows PowerShell 发起：

```powershell
ssh m0nesy 'cd /home/s223/m0nesy/Inference && git status --short'
ssh m0nesy 'cd /home/s223/m0nesy/Inference && export CONDA_EXE=/home/s223/miniconda3/bin/conda && ./inference backend doctor'
```

远端交互 shell：

```bash
source /home/s223/miniconda3/etc/profile.d/conda.sh
conda activate simai
cd /home/s223/m0nesy/Inference
```

非交互使用项目入口 ./inference、./scripts/simai-python，显式设置 CONDA_EXE。不要依赖 SSH 非交互 shell 自动加载 Conda；不要把本机 Windows 路径传给远端程序。

## 更新与同步

1. 先阅读远端 AGENTS.md，保存 git status、HEAD、执行环境及当前任务的原始命令日志。
2. 需要更新时，先确认未提交修改，再 git fetch origin / git pull --ff-only。工作区有修改或无法快进时报告差异，不覆盖。
3. 本地未提交修改先分辨真实内容、权限位/换行噪声、输入和生成产物。提交仅在用户明确要求时进行；推送需要相应授权。
4. 可以用 Git bundle 将已提交代码传到干净的远端，再 fetch bundle、merge --ff-only，使两端持有同一 commit。这样不会推送 GitHub。
5. 输入按逐文件SHA256清单定向传输，目标已存在时先比较，不覆盖未知文件。不复制旧实验结果、Conda环境、缓存或编译二进制。
6. 当前迁移仅保留 T001 冻结输入至 artifacts/inputs/T001-baseline-freeze/；TRANSFER.json 记录来源及哈希。原manifest中的WSL路径只作来源说明。新任务必须在服务器重新检查实际输入、配置路径和后端身份。

## 验证和归档

开发、测试和实验以本轮服务器实际执行为准。旧WSL实验、验收记录或复制过来的配置不构成服务器运行证据。

任务在 outputs/agents/YYYY-MM-DD/Tnnn-short-name/；草稿在对应 work/agents/ 路径。日期用 Asia/Shanghai，T003 已在本地提交但尚待指挥审核；后续编号不能从 T001 重新开始。旧任务原文和原始结果留在历史位置，不改写为服务器结果。

本次是用户直接要求的提交和迁移，不发布新任务书；迁移命令与检查归档在 outputs/migrations/2026-11-07/ 的独立目录。

必要检查可包括：配置 validate、backend doctor、与改动相关的定向测试、一次小规模 smoke。未要求时不跑完整 check.sh 或长时间全量矩阵。记录检查范围、退出码、日志、输入哈希、源码HEAD和后端身份。

项目当前运行 CPU 离散事件仿真；RTX A5000 的存在不使这些结果成为真实GPU推理性能。配置中的估算计算profile、合成路由、真实请求来源和人工SLO仍分别说明。
