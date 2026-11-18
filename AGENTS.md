# Sentrix 与 PhotoBench 开发约定

- 默认中文、精炼客观。代码根目录为本文件所在目录。
- 11/19用户授权取消两路长尾后发布有限预算并续评；100模型/600工具（搜索100、其他500）源码与专项验证中，尚未激活；完整存储/节点懒加载待办，见 `docs/refactor/2026-11-18-lazy-read-and-reasoning-limits-plan.md`。
- 11/18 Marvell迁移候选5430262已放100并校验，autoconnect=false；待管理员导入，未切网，见T006与产品项目audit/20261118-marvell-migration-prep/。
- 11/18侧聊100独立30秒网卡/温度取证已启用（a2a41b9），Intel无传感器记null，日志约128MiB轮转；仅新增用户单元，不改网络/业务，见T006与产品项目audit/20261118-nic-thermal-recording/。
- 跨设备同步：先在本机开发分支完成修改、验证并提交，再传输该提交的代码或构建产物。
  同步前核对远端版本和未提交改动，同步后核对提交号或文件校验值，记录局部部署范围。
  服务器无需个人 GitHub 凭据。活动测评期间保持现场，后端切换须先确认对应产品和测评均空闲。
- 本机ATM小测已由10题扩充为16题，含六道涉邮件题；生成与验证见`docs/refactor/2026-11-18-atm-smoke-16q.md`，未发布100。
- 媒体原件不可原地改写。相册记录与处理结果独立，文件内容共享由存储层管理。
- 长期工作维护本索引及 `HANDOFF/`；产品项目总入口为
  `/Users/jzousz/Documents/ChatGPT/产品项目/AGENTS.md`，既有服务现场见其 T006 记录。

| ID | 任务方向 | 状态 | 最近更新 | 一句话范围 | 记录文档 |
|---|---|---|---|---|---|
| T006 | PhotoBench 2.0 跨平台集成 | 进行中 | 2026-11-18 | 两路测评已按用户授权取消、产品回合已停；有限推理预算本机验证中，发布后从原scope续评；完整懒加载待办 | [HANDOFF/T006-mainline-shared-media.md](HANDOFF/T006-mainline-shared-media.md) |
