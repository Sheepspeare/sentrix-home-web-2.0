# .100 服务器目录约定

2026-11-24起，项目管理根为`/home/realmagic/xy`。新建内容使用以下稳定路径，顶层项目名称不再附日期；日期用于版本、实验和审计记录。

| 路径（相对xy） | 用途 |
|---|---|
| `benchmarks/atm-bench/` | ATM-Bench题库、产品数据、结果与运行环境 |
| `benchmarks/photobench/` | PhotoBench题库、历史结果与产品实例 |
| `benchmarks/photobench/instances/default/` | 原8772/8191实例的完整运行目录 |
| `benchmarks/atm-bench/instances/doubao/product-data/` | 8294当前独立数据库与媒体 |
| `benchmarks/atm-bench/instances/doubao-legacy/` | 原`/var/tmp`云端运行目录，含网关脚本和历史产品库 |
| `benchmarks/atm-bench/datasets/sources/` | 完整图视频与邮件题包，11088文件 |
| `benchmarks/atm-bench/results/` | 8872历史答卷、评分、轨迹 |
| `benchmarks/atm-bench/results-cloud/` | 8874历史答卷、评分、轨迹 |
| `benchmarks/atm-bench/experiments/atm1013/releases/` | ATM1013产品/测评发布副本，保留版本标识 |
| `benchmarks/atm-bench/experiments/atm1013/operations/` | 已有恢复、补评、导出与比较操作及回执 |
| `benchmarks/atm-bench/experiments/atm1013/state/` | 活动任务映射、冻结配置及历史状态回执 |
| `benchmarks/atm-bench/experiments/atm1013/scripts/` | 该实验原有顶层脚本 |
| `benchmarks/atm-bench/experiments/atm1013/artifacts/` | 该实验原有压缩发布包 |
| `releases/` | 主产品的已提交发布版本与共用Python环境 |
| `deploy/` | 配套配置和发布记录 |
| `diagnostics/` | 网卡取证、维护工具和维护回执 |
| `archive/deploy/` | 较早的部署与回滚材料 |

PhotoBench default实例原68个散落文件已按scripts/logs/artifacts/audit/initial-deployment/config分类。

用户目录散落的20份旧日志、维护脚本和报告归入`/home/realmagic/maintenance/{logs,scripts,reports}/`。其他研究项目和他人目录维持原位置。

## 历史路径与数据边界

旧ATM、typed PhotoBench、deployment_outputs及云端运行根保留目录符号链接。旧ATM1013目录指向`xy/.legacy/atm1013`路径映射，逐项连接新的分类位置。历史JSON、数据库中的原件绝对路径、审计回执及旧脚本可继续查到原数据；它们不作为新建文件入口。不能删除这些路径入口，否则历史媒体和旧导出可能无法解析。

本次合并的是目录组织。不同产品实例的SQLite、Qdrant、相册和评分仍独立；冻结答卷、评分与模型条件保持原值。未改写历史run/评分/GT，也不重新执行原恢复或发布脚本。`operations/`和实验`scripts/`中的一次性写操作仅供追溯，后续操作仍应新建已提交工具。

## 迁移工具与验证

工具位于`deploy/maintenance/server100-layout/`，`plan.json`明确列出每个源目录与目标目录。`inventory.py`只读盘点；`layout.py inspect`核空闲、路径与Linux原子迁移能力；`apply`执行一次，已有intent即拒绝重放；`verify`只读复核迁移后的文件身份和服务；`finish.py`完成源题包与default文件分类；`accept.py`进行最终验收并记录split-commit当前SHA。apply/finish已执行，不可重放。

同分区采用Linux原子交换，让旧入口连续指向新目录。跨分区云端目录先复制，在确认空闲后只停8294/8874及两网关；离线增量复制与逐文件checksum比较通过后替换旧入口并删除已核对的旧副本，随后恢复原四服务。其他服务PID保持。配置原件备份保留在服务器`xy/diagnostics/directory-layout/config-backups/`，权限0600，不复制到本机。

配置与服务路径更新后`daemon-reload`。没有重启的服务继续用已有进程，其旧环境路径通过目录入口解析到同一文件；下次正常启动会读取新路径。8100主模型未重启。8294恢复时因原已停服的WeMM8102无法通过启动校验，曾自动重启34次；已启动既有WeMM（GPU0约5.6GiB）并恢复8294，重启次数稳定34。仅有服务启动/健康合成探测，无正式Agent/Judge/补跑或导出。

最终142563份身份中142547完全不变、16项已解释、无未知差异；跨分区两批checksum无差异。38份split-commit保持原inode/大小/mtime，并记录当前SHA；未采集迁移前SHA。8872/8874的349源码和三份静态页面SHA一致，三测评空闲、三产品live健康。

最终验收与当前状态见产品项目`audit/20261124-server100-directory-layout/README.md`及T006。不要只按本文推断迁移已执行。

2026-11-24后续静态更新：8872/8874前端导出筛选已发布61e4f1ee产物，ATM显示0–1量表；后端仍56ea57aa+100-main-merge，无重启。静态发布回执在`xy/deploy/20261124-export-score-scale/61e4f1ee/`。旧hash资源保留供已打开页面加载，前端当前文件以该回执为准。
