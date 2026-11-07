# 本次提交、同步和验证命令

所有本机命令均由 Windows PowerShell 发起，未调用 wsl.exe。

1. 检查本地 Git status、HEAD、remote、真实内容 diff 与 cached diff；读取本地及服务器 AGENTS.md。
2. 服务器预检：
   ssh m0nesy 'cd /home/s223/m0nesy/Inference && git fetch origin && git pull --ff-only'
   执行前另核对工作区干净；结果 Already up to date。
3. 更新远端执行文档，git diff --check；以 core.filemode=false / core.autocrlf=false 暂时参数检查并暂存实际内容，保留原索引可执行位。
4. 本地显式提交：
   git commit -m "chore: finalize ns3-only baseline and move execution to Linux server"
   实际 Git 命令携带 safe.directory/core.filemode/core.autocrlf 参数；提交文件清单见 logs/commit-paths.txt。
5. 创建并验证 bundle：
   git bundle create code.bundle main ^91cf1e2a16b8ff59b595619a7d0c02c4dd5df8dc
   git bundle verify code.bundle
6. 按源 manifest 核对五个输入哈希，复制到本次迁移的独立暂存目录，生成 TRANSFER.json，打包 frozen-inputs.tar.gz。
7. 用 Windows scp 将上述两个文件传到 /home/s223/m0nesy/Inference/outputs/migrations/2026-11-07/server-migration-201527/；服务器校验包 SHA256。
8. 服务器代码更新：
   git bundle verify /home/s223/m0nesy/Inference/outputs/migrations/2026-11-07/server-migration-201527/code.bundle
   git fetch /home/s223/m0nesy/Inference/outputs/migrations/2026-11-07/server-migration-201527/code.bundle main
   git merge --ff-only FETCH_HEAD
   各步骤先校验原HEAD、FETCH_HEAD与工作区；日志见 bundle-verify.txt、bundle-fetch.txt、code-fast-forward.txt。
9. 创建原先不存在的 artifacts/inputs/T001-baseline-freeze/ 并解包输入，未覆盖现有文件。
10. 服务器验证：
    /home/s223/miniconda3/envs/simai/bin/python /home/s223/m0nesy/Inference/outputs/migrations/2026-11-07/server-migration-201527/validate_migration.py
    脚本为所有项目子命令显式设置 CONDA_EXE=/home/s223/miniconda3/bin/conda、PYTHONPATH=/home/s223/m0nesy/Inference；逐条 argv/退出码/墙钟在 commands.json。每个命令有硬超时，遇失败立即停止，未运行完整回归。
11. 取回本轮 validation-summary、命令和测试日志到本机文档目录供查看，保存同一份本次迁移报告到服务器。
