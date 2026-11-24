# PhotoBench

稳定根目录：`/home/realmagic/xy/benchmarks/photobench`。

- `datasets/`：原typed题包。
- `instances/default/`：原8772/8191实例的完整数据和历史结果。
- `instances/default/data/`：该产品实例数据库、媒体与派生产物。
- `instances/default/results/`：原测评历史；`results-chat/`为独立聊天记录。
- `instances/default/source153-switch/catalog/`：该实例实际使用的题库入口。
- `instances/default/{scripts,logs,artifacts,audit}/`：旧脚本、日志、归档材料和验收报告。
- 根目录的`code/`、`config/`、`results/`、`scripts/`、`artifacts/`：原typed准备环境，独立保留。

8772/8191进程保持运行，版本与数据库逻辑保持。旧`deployment_outputs`入口用于历史路径解析；新文件使用本页路径。

完整目录约定见`../../diagnostics/directory-layout/server100-directory-layout.md`。
