# 助手本机语音输入

`/assistant` 的“问一句”支持点击“语音输入”、允许麦克风、讲话、点击“停止并识别”。
识别文字追加到现有草稿，手动点击“停止并识别”后自动发送，服务器接受请求后清空草稿。
单次最长 60 秒；到时自动停止只填回文字，需用户确认后发送。发送失败或状态未确认时保留草稿，
不自动重试，请先检查会话是否已有该问题。取消、离开页面、切换会话/相册会释放麦克风。

## 准备资源

在仓库根执行：

```sh
npm --prefix apps/web run speech:prepare
make web-dev
```

也可使用已经下载好的官方压缩包（仍强制校验 SHA-256）：

```sh
npm --prefix apps/web run speech:prepare -- /absolute/path/to/sherpa-onnx-wasm-simd-1.12.20-vad-asr-zh_en_ja_ko_cantonese-sense_voice_small.tar.bz2
```

生产构建前同样运行准备命令，再 `npm --prefix apps/web run build`；Vite 将
`public/speech/` 复制到 `dist/speech/`，随前端一起部署。权重与第三方构建文件不进入 Git。
测试/CI 不需要下载模型，真实推理验证必须另外准备资源。

来源与 SHA-256 固定在 `apps/web/speech-assets.lock.json`。使用官方
[sherpa-onnx v1.12.20 发布包](https://github.com/k2-fsa/sherpa-onnx/releases/tag/v1.12.20)，
JS binding、WASM、Emscripten data 必须来自同一包，不混用 master 或另一版本。
压缩包 165,723,573 字节；浏览器加载的 data 为 240,193,589 字节，WASM 约 11.5 MB。
首次加载可能较慢，界面显示进度。同一助手组件存活期间复用已初始化的 Worker 和模型，
再次录音、回答忙碌状态切换不重新加载。刷新页面或模型被释放后仍需初始化；
资源是否从磁盘缓存读取由浏览器 HTTP 缓存策略决定，磁盘缓存不能代替内存中的模型实例。
本机内存需容纳 WASM 初始 512 MiB 堆、模型文件和推理工作区，低内存设备需实测。

## 运行边界

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

全新克隆先安装前端依赖 `npm --prefix apps/web ci`，语音模型资源按上文准备。
前端代理 `/api`、`/control` 到原来的后端地址；手机打开 Vite 输出的 HTTPS Network 地址。
第一次切换 HTTPS 时先停止原来的 HTTP Vite 进程，4274 被占用时会明确报错。
构建不运行证书安装，Vitest 配置也明确排除 mkcert 插件。

手机必须安装并信任根 CA。本配置使用插件默认目录，根证书为
`~/.vite-plugin-mkcert/rootCA.pem`（插件显式以该目录作为 CAROOT）。
iPhone 安装描述文件后，在“设置 → 通用 → 关于本机 → 证书信任设置”开启完全信任；
Android 在系统安全设置安装 CA 证书，然后使用 Chrome 访问。
只传 `rootCA.pem`，不要传根 CA 私钥。插件不能远程替手机安装信任。

官方说明：https://github.com/liuweiGL/vite-plugin-mkcert#mobile-devices

- 模型是 SenseVoice-Small INT8（2024-07-17 导出），引擎是 sherpa-onnx WebAssembly SIMD，
  `numThreads=1`、CPU provider、自动语种、逆文本规范化。依据
  [官方 SenseVoice 文档](https://k2-fsa.github.io/sherpa/onnx/sense-voice/)。
- AudioWorklet 采集单声道 Float32 PCM；将实际采样率交给 sherpa-onnx，由其重采样到模型的 16 kHz。
  不把 MediaRecorder 的 WebM/MP4 当作 WAV，不使用浏览器 SpeechRecognition 或云端 ASR。
- 只向同源站点读取静态模型资源。录音经 transferable buffer 交给 Worker，音频不上传、
  不落盘、不写日志。手动停止识别后自动发送的文本、以及用户点击发送的草稿走既有问答 API。
- 需要 HTTPS 或 `http://localhost:4274`，以及 AudioWorklet、Web Worker、WASM SIMD。
  局域网明文 HTTP 地址通常无法使用麦克风；界面保留文字输入并说明原因。
- 不使用 SharedArrayBuffer，不要求全站 COOP/COEP。识别在 Worker 执行，取消通过终止
  Worker 中断同步解码。加载超时 300 秒、识别超时 120 秒；失败保留草稿并允许重试。
- 已就绪识别器在助手组件内复用，取消录音或切换会话时保留；组件卸载时释放。
  若正在加载或解码，取消/切换会话会终止 Worker，下次需重新初始化；解码异常同样重建。
  权限拒绝、录音太短、空识别结果不销毁健康模型。只承诺模型加载完成后的本机推理；
  未引入 Service Worker，不承诺整个网站断网后仍能重载和调用问答服务。

## 验证与排障

```sh
cd apps/web
npx vitest run src/features/assistant/speech
npx tsc -b --force
npm run build
```

浏览器验收：检查“语音输入”按钮；首次加载后允许麦克风；录入一句话并停止，确认自动发送且只发送一次、成功后草稿清空；连续录音三次确认不再加载模型。
等待 60 秒确认只填回草稿；再验证发送失败保留文字、取消、拒绝权限、空音频、切换会话、移动端宽度。
浏览器网络面板应只有同源静态模型 GET，不应出现录音上传请求。
本机原始麦克风和目标手机的识别准确率需分别实测，模拟音频不能代替真人验收。

资源 404 或返回 HTML：运行准备命令并确认部署包含 `dist/speech/sensevoice-1.12.20/`。
初始化失败：检查 WASM SIMD 支持、浏览器可用内存，以及四个资源是否来自同一锁定版本。
权限拒绝：在网站设置中恢复麦克风权限，再点击“语音输入”。

上游许可与来源：
[sherpa-onnx Apache-2.0](https://github.com/k2-fsa/sherpa-onnx/blob/v1.12.20/LICENSE)、
[SenseVoice 项目及模型许可说明](https://github.com/FunAudioLLM/SenseVoice#license)。
发布第三方模型产物时保留其对应许可与来源，模型许可与运行时许可分别核对。
