# Sentrix 与 PhotoBench 开发约定

- 11/19 Q4测评OOM已修复47523cd并仅发布1008872，继承95题续62cc28；Qwen842598继续运行。两路新wake监控已核，旧超大事件通知失败已修复；完整懒加载仍未完成。见产品项目audit/20261119-q4-oom-recovery/README.md与T006，当前run/wait以主实验state.json为准。

- 默认中文、精炼客观。代码根目录为本文件所在目录。
- 11/19有限推理预算ffec2be已发布1008291/8294及8872/8874：模型100、工具600（搜索100/其他500）；Q4继承92续62b2fc，Qwen继承249续842598，两路真实新回合已核预算。Q4已移GPU0，原scope/有效分/失败史保留；完整节点懒加载仍本地草稿。见产品项目audit/20261118-reasoning-bounds-release/README.md，接T004/T006。
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
| T006 | PhotoBench 2.0 跨平台集成 | 进行中 | 2026-11-19 | Q4 OOM修复后继承95续评，两路异常监控已验证；完整懒加载待完成 | [HANDOFF/T006-mainline-shared-media.md](HANDOFF/T006-mainline-shared-media.md) |
