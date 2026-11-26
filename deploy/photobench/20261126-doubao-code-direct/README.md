# 原豆包代码与相册的Flash直连对照

用户2026-11-26明确要求新增两个实例，回到上周Doubao Lite实际Agent代码，分别20模型/30工具和100模型/600工具，各16答题并发，直接请求云端；先同16代表子集，按真实错误调整。该授权取代候选冻结限制，仅适用于本次独立实验。

产品源码精确重建11/20实际部署：ffec2be的334个源文件，叠加9363d4b中会话删除相关3文件；与历史部署334文件全部逐SHA一致。相册复用当时Doubao媒体记忆及6742封邮件，18张记忆表全量指纹保护，0新建库/embedding。保留16K上下文、2048默认输出和历史Planner/推理/工具策略。评测调度器使用30c67e74已提交版本，支持UUID优先与16题检查点，不改变Agent决策。历史Doubao74.41%使用其他裁判，分数不能直接当同Judge模型对照。

transport.py在本实验进程内为直达云端的HTTP请求附鉴权，转换原生enable_thinking=false并记录压缩原包。无HTTP监听、队列、槽位、重试或Agent业务修改。system reasoning/Planner开启，模型thinking关闭。

独立8301/8881及8302/8882；/home仅约7.7GiB，新两份SQLite与Qdrant放专属/dev/shm目录，媒体只读复用，答卷/轨迹及HTTP原包落/home并在验收时回传本机。原全量、候选与119high保持。运行状态和wait以产品audit/doubao-code-direct下intent/receipt为准，不重放已有启动。
