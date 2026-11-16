# 助手本机后端语音输入

`/assistant` 的浏览器只负责录音，SenseVoice-Small 由运行 API 的本机后端执行。
手动点击“停止并识别”后，将识别文字追加到现有草稿并自动发送；
服务器接受提问后清空草稿，失败或状态未确认则保留文字，不自动重试。
60 秒超时只填回草稿，需用户确认后发送。

## 准备和启动

首次迁移安装后端依赖并准备模型：

```sh
.venv/bin/pip install -e "apps/api[dev]"
make speech-prepare
```

当前开发机已安装依赖和模型。之后在项目根目录的两个终端运行：

```sh
make dev
make web-dev
```

准备脚本将锁定的官方模型安装到 `SENTRIX_DATA_DIR/models/sensevoice-small`，
默认 `data/models/sensevoice-small`。不新增单独的模型路径开关。
模型为 2024-07-17 SenseVoice-Small INT8，Python sherpa-onnx 固定 1.12.40，
CPU provider、2 个推理线程、自动语种、逆文本规范化。
1.12.20 的浏览器 WASM 版本已移除；其 Python wheel 不适用于当前开发环境。

`scripts/speech-model.lock.json` 锁定来源、压缩包和逐文件 SHA-256；
已有文件通过校验后不再下载。模型、token、许可和官方中文测试音频保存在数据目录，不提交 Git。
离线准备可用：

```sh
.venv/bin/python scripts/prepare_speech_model.py /path/to/official-model.tar.bz2
```

API 启动生命周期中加载模型，每个 API 进程只保存一个实例；
请求创建独立识别 stream，共享模型，不进行请求时懒加载。
刷新页面、切换会话、取消录音都不会卸载模型。API 重启才重新加载，
关闭时等待已有解码结束后释放。多 worker 部署会各自加载一份模型，开发默认单进程。

模型缺失或加载失败时记录日志，文字功能仍可用。
状态端点明确返回不可用，识别请求返回 503；修复模型文件后重启后端。
API 启动不联网下载模型，生产部署也应先执行准备命令。

## 接口与资源边界

- `GET /api/v1/assistant/speech/status` 返回 `{available,message}`。
- `POST /api/v1/assistant/speech/transcribe?sample_rate=48000` 接收
  `application/octet-stream` 的单声道 little-endian float32 PCM，返回 `{text}`。
- AudioWorklet 保留实际采样率，后端接受 8–192 kHz、0.2–60 秒音频；
  校验帧对齐、时长、有限采样值和 [-1,1] 范围。sherpa-onnx 负责重采样。
  上传按采样率限制大小（最高约 46 MB），流式读取限时 30 秒。
- 识别在线程池执行，不阻塞事件循环；模型只允许一个同时解码的请求，
  其他请求返回 429，不排队、不自动重试。
- 浏览器状态查询限时 10 秒，识别请求限时 120 秒。取消或会话切换会中止
  HTTP 请求并丢弃晚到结果；已进入原生推理的请求不能强杀，会完成后释放并发槽。
  客户端超时不会卸载模型，后续请求可能短暂收到忙碌提示。
- 音频会从手机/浏览器传到运行 API 的本机，内存处理，不落盘、不写音频或转写日志、
  不发往云端 ASR。文本继续走原有问答 API。沿用同源会话、CSRF 与内部调用守卫。
- 浏览器不再下载模型或依赖 WASM SIMD、ASR Worker；仍需 HTTPS 和 AudioWorklet。
  旧浏览器资源准备命令、模型锁文件与识别 Worker 已删除，保留录音 AudioWorklet。
  从旧分支升级时可删除旧生成目录 `apps/web/public/speech/sensevoice-1.12.20`，
  避免旧大文件进入 Vite 构建产物。

### 手机局域网 HTTPS

Vite 使用 `vite-plugin-mkcert` 自动下载 mkcert、安装本机根 CA，并为 localhost 和
当前局域网 IP 生成证书。证书按插件默认位置保存在用户缓存目录，私钥不进入项目。
首次启动需要网络，安装系统信任可能要求输入系统管理员密码；之后复用证书。
无需手工创建 `.certs/dev.pem`，无需进入临时 worktree。

在当前项目根目录分别打开两个终端：

```sh
make dev       # 后端 http://127.0.0.1:8191
make web-dev   # 前端 https://localhost:4274（在另一个终端运行）
```

全新克隆先安装前端依赖 `npm --prefix apps/web ci`，后端模型资源按上文准备。
前端代理 `/api`、`/control` 到原来的后端地址；手机打开 Vite 输出的 HTTPS Network 地址。
第一次切换 HTTPS 时先停止原来的 HTTP Vite 进程，4274 被占用时会明确报错。
构建不运行证书安装，Vitest 配置也明确排除 mkcert 插件。

手机必须安装并信任根 CA。本配置使用插件默认目录，根证书为
`~/.vite-plugin-mkcert/rootCA.pem`（插件显式以该目录作为 CAROOT）。
iPhone 安装描述文件后，在“设置 → 通用 → 关于本机 → 证书信任设置”开启完全信任；
Android 在系统安全设置安装 CA 证书，然后使用 Chrome 访问。
只传 `rootCA.pem`，不要传根 CA 私钥。插件不能远程替手机安装信任。

官方说明：https://github.com/liuweiGL/vite-plugin-mkcert#mobile-devices


## 验证

```sh
.venv/bin/pytest apps/api/tests/unit/test_sensevoice.py apps/api/tests/contract/test_speech.py
.venv/bin/python scripts/run_real_tests.py apps/api/tests/integration/test_sensevoice_real.py
cd apps/web
npx vitest run src/features/assistant/speech
npx tsc -b --force
npm run build
```

真实模型专项使用准备脚本中的官方中文样例，通过应用启动与 HTTP 端点连续识别两次，
验证同一模型仅加载一次、结果一致、退出释放。它不替代真人麦克风或实体手机验收。
浏览器验收应检查手动停止自动发送、60 秒只填回草稿、取消丢弃结果、失败保留草稿，
网络面板无旧模型/WASM 下载，仅有本机状态查询和 PCM 上传。

官方来源与许可：
[模型下载与说明](https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html)、
[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)、
[SenseVoice](https://github.com/FunAudioLLM/SenseVoice#license)。
