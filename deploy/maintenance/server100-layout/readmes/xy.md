# .100 项目目录

项目数据统一入口：[benchmarks](benchmarks/)。

- [ATM-Bench](benchmarks/atm-bench/)：题库、8872/8874结果、数据库、模型及ATM1013实验。
- [PhotoBench](benchmarks/photobench/)：题库与8772/8191实例数据。
- `releases/`：主产品发布版本和共用Python环境。
- `deploy/`：配套配置及发布材料。
- `diagnostics/`：网卡取证和维护工具；本次整理回执在`diagnostics/directory-layout/`。
- `archive/deploy/`：较早的部署、回滚材料。
- `shared/`：共享运行资源。

用户目录的旧日志、维护脚本、报告已归入`~/maintenance/`。
带日期的旧目录入口是符号链接，用于读取历史绝对路径；新建内容使用本页列出的目录。
`experiments/atm1013-20261118`连接隐藏的路径映射，正式分类位置在ATM目录下。

具体说明：[目录约定](diagnostics/directory-layout/server100-directory-layout.md)。其他研究项目和他人的目录保持原位。
