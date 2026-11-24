# Server100 Qwen3.8 Flash build-only operation

2026-11-24 用户授权：8874/8294 将新模型设为主模型，构建 ATM 完整来源相册，媒体并发4；不运行 QA/Judge。

- `gateway.py`：沿用已验证的 Qwen loopback 适配器，模型/上下文由非秘密配置指定；密钥只读环境变量。保留消息和工具，关闭 thinking，并发上限4。
- `operate.py prepare`：核空闲、存储和新目录，备份旧配置，新建独立数据根和角色配置，仅切换8294/8874并启动18108网关。一次执行，出错先读现场。
- `operate.py start`：核实际模型/嵌入/4线程，单次提交 `mode=build`，保存 intent 与 receipt；不继承旧答卷。
- `operate.py status`：只读当前任务和阶段。

代码必须先本机验证提交，再同步核SHA。密钥从用户CSV通过SSH标准输入单独写入远端0600文件，不进入Git。产品代码由本机已提交源码归档，数据位于 `/var/tmp/sentrix-qwen38-flash-20261124/product-data`，统一实例根保留软链接。默认角色上下文128K、单次输出4096、图片长边1280；实验图开关保持。

当前仅媒体建库阶段，不以本次操作宣称预算/看图需求修复或评分提升。结果与回调记录见产品项目 `audit/20261124-qwen38-flash-build/`。
