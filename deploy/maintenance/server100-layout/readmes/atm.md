# ATM-Bench

稳定根目录：`/home/realmagic/xy/benchmarks/atm-bench`。

| 目录 | 内容 |
|---|---|
| `datasets/` | 完整输入、媒体子集和小测包；完整版本在`sources/` |
| `product-data/` | 8291原产品数据库与媒体 |
| `instances/doubao/product-data/` | 8294当前独立数据库与媒体 |
| `instances/doubao-legacy/` | 原云端运行目录、网关脚本、日志及历史产品库 |
| `results/` | 8872全部历史答卷、评分、轨迹 |
| `results-cloud/` | 8874全部历史答卷、评分、轨迹 |
| `experiments/atm1013/releases/` | ATM1013产品与测评代码发布副本 |
| `experiments/atm1013/operations/` | 补做、恢复、导出、比较操作与回执 |
| `experiments/atm1013/state/` | 任务映射、冻结配置、历史状态 |
| `experiments/atm1013/scripts/` | 该实验原有顶层脚本 |
| `experiments/atm1013/{logs,artifacts,cache}/` | 该实验日志、归档包、缓存 |
| `code/` | 初始代码树，含历史现场修改，继续保留 |
| `config/`、`runtime/` | 初始配置与运行回执 |
| `logs/`、`audit/`、`scripts/`、`artifacts/` | 初始环境日志、审计、脚本、归档材料 |
| `models/`、`.venv/`、`tmp/` | 模型、Python环境与工作缓存 |

测评入口8872/8874，产品入口8291/8294。当前测评代码为`experiments/atm1013/releases/main-56ea57aa`，在线版本为`56ea57aa+100-main-merge`；前端导出筛选标签已于11/24随61e4f1ee静态更新（ATM QS 0–1），后端版本保持；发布回执在`/home/realmagic/xy/deploy/20261124-export-score-scale/61e4f1ee/`。产品代码保持各自既有版本。本次整理目录；8294/8874及网关停启后，为恢复8294的启动依赖已启用既有WeMM8102（GPU0约5.6GiB）。主模型保持，无正式测评任务。

旧绝对路径通过符号链接继续解析，数据库中的旧媒体路径也继续有效。原正式答卷、评分、GT和失败历史不改写。已消费的恢复、部署、评分脚本不重放。

初始环境说明已原文存入`audit/initial-environment-20261110.md`；其中版本、PID和资源状态属于当时记录。当前目录约定见`../../diagnostics/directory-layout/server100-directory-layout.md`。
