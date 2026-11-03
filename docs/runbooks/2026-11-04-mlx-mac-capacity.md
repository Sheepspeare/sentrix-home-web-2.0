# Sentrix MLX 并发验收

本实验基于上游 `main@d3485537b02e30e0ee3e7cc6dc4b450c2f38f7db`，迁入 PR #7 的嵌入推理解锁、Qdrant 冷启动保护、冻结库恢复和性能统计。数据库事务及状态锁保留。正式成绩以随报告发布的原始记录哈希和逐轮终态为准；资格测试、取消和失败的初跑不计入通过档。

## 固定环境

- Apple M4，16 GB 统一内存，macOS 26.6；Sentrix、SQLite、Qdrant、BGE-M3、Chinese-CLIP 和 Qwen 全部在同一台 Mac 运行。
- 模型从 ModelScope `lmstudio-community/Qwen3.5-4B-MLX-4bit` 下载。权重 SHA256：`52dc943eaf6093a2313271a7a0abc36d127b2a16609a0a7c54ee1b4e4ed06cb8`。逐文件版本和 SHA256 随实验清单保留。
- 原始模型目录只读保留。`scripts/benchmarks/prepare_mlx_model.py` 生成写时复制目录，仅将嵌套的图像处理器类名调整为 Transformers 支持的 `Qwen2VLImageProcessor`；权重、词表和数值预处理参数不变。
- 正式引擎为 [vllm-mlx](https://github.com/waybarrios/vllm-mlx) 0.6.0，mlx-vlm 0.7.6，Transformers 5.19.0，Python 3.12.9。先尝试的 vLLM Metal 0.32.0 未通过这台机器上 32K KV 缓存容量检查；相关失败日志保留，未计作正式结果。
- BGE-M3、CLIP 在 CPU 上运行。源机与 Mac 的模型权重、词表及预处理身份一致；真实嵌入数值兼容检查另行记录。解除推理锁改变实现哈希，因此不能声称实现身份或向量逐位相同。
- 云端 Judge 为 doubao-seed-2.1-lite。QA 与 Judge 独立异步调度，`judge_backpressure=false`。评分队列不降低 QA 请求数。

## 模型启动与契约

在隔离的引擎环境中安装上述固定版本，以派生模型目录启动：

```sh
export MLX_BUFFER_CACHE_LIMIT=512mb
export SENTRIX_MLX_CONTEXT_TOKENS=32768
export SENTRIX_MLX_EVENTS="$EXPERIMENT_ROOT/evidence/model-events.jsonl"
python scripts/benchmarks/serve_mlx.py serve "$DERIVED_MODEL" \
  --mllm --continuous-batching --max-num-seqs 16 \
  --prefill-batch-size 4 --completion-batch-size 16 \
  --mllm-prefill-step-size 512 --chunked-prefill-tokens 512 \
  --disable-prefix-cache --max-tokens 32768 --max-request-tokens 32768 \
  --served-model-name qwen35-4b-mlx-q4 --host 127.0.0.1 --port 18100 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder \
  --reasoning-parser qwen3 --enable-metrics \
  --default-chat-template-kwargs '{"enable_thinking":false}' --timeout 900
```

`serve_mlx.py` 有严格版本检查，并补齐三项基准所需契约：

1. `/tokenize` 使用引擎实际聊天模板、工具、结构化提示和图像展开计算输入长度。Sentrix 按剩余窗口分配输出预算，不按字符估算，不静默裁剪输入。窗口不足必须显式失败，统一增大窗口并重新验收后才能用于可比轮次。
2. 每请求独立的 MLX 随机状态固定为 42，避免其他并发请求消耗其随机序列。保留上游 top-p、min-p、top-k 的处理顺序。
3. vllm-mlx 0.6.0 的分块生成路径把未包含当前 token 的历史传给约束处理器，会触发 JSON grammar 错误并中止同批请求。适配器补上当前 token，与普通生成路径一致；不改变请求历史，不关闭结构化约束。

固定采样：temperature 0.7、top_p 0.8、top_k 20、min_p 0、presence_penalty 1.5、seed 42、关闭思考。Agent 最多 30 个模型步骤及 30 次工具调用。模型服务超时、输出截断和未知结果分别保留，不通过自动重发消除原始失败。Agent 步数与规划器/工具内部的 HTTP 请求数分别统计。

## 数据、恢复与轮次

完整 487 题文件 SHA256：`73fa4512dde072fb0df9cf45ff3ebd192192db606f9764054bde002dcbf0be47`，含 561 轮对话。固定 64 题含 69 轮对话，SHA256：`4f6d95b2673f3aa01a9dff7a6675293eaff45d5935c8418d8f31dbb810293e5b`。`mlx_capacity.py freeze-subset` 按整条 JSONL 的固定哈希排序选择，再恢复原文件顺序；保留多轮对话。

每一轮均使用 `memory_snapshot.py restore` 从同一验收快照恢复独立目录，并核对文件清单、迁移、媒体、向量和身份记录。macOS 使用 APFS 写时复制；绝不让两轮共用可写 SQLite 或 Qdrant 目录。该快照是历史验收的组合记忆库，不代表原始素材已全部成功入库。

每轮重启引擎以清除模型缓存，采用相同的 4 个文本/合成图片请求预热，然后提交唯一的 PhotoBench Run。提交意图先落盘，响应及 run_id 随后记录。恢复观察只查询已有 run_id；未知提交不得重放。C4、C8、C12、C16 各运行同一组 64 题两轮，最高通过档另开独立库运行完整 487 题。

通过条件包括完整 QA 和 Judge 终态、无执行/模型错误以及完整监控证据。答案得分独立报告；错误、取消和补跑不覆盖原始记录。最高通过档要求两轮都通过；吞吐最佳档按两轮总题数除以两轮 QA 总时长计算。

## 统计与页面

`mlx_capacity.py` 记录 CPU、GPU、统一内存、交换存量及换入换出计数、模型队列与活动数。`serve_mlx.py` 另从真实 token 生成批次记录实际批次大小、请求终态和首 token 时间。客户端提交 16 个请求不等于模型实际同时执行 16 路。

QA 和 Judge 时间、吞吐、完成与失败分开统计。QA 资源汇总只覆盖 QA 阶段；原始采样保留评分阶段。首 token 时间从调度器接收请求起算，包含模型队列等待，不包含 HTTP 请求之前的输入预处理。缺失值显示“未采集”，不替换为零。

PhotoBench 的“并发实验”页读取 `capacity-report.json`，展示逐档比较、487 题总览及资源曲线。公开截图只包含这些汇总，不包含私人照片、问题、答案正文、文件访问地址或凭据。复核页面与原始汇总一致后再截图。

## 服务恢复

接管前保存旧 MiniCPM LaunchAgent、启动脚本和进程状态。以 `launchctl bootout` 正常停止旧服务，保留权重。实验结束后按记录的 PID 和命令行核验归属，停止新推理、嵌入和评测进程，关闭监控；再用保存的 LaunchAgent 配置执行 `launchctl bootstrap`，验证启动状态和健康端点。不得仅杀旧进程造成服务被自动拉起，也不停止其他应用。

## 编排脚本

`launch_mlx_engine.py`、`prepare_mlx_round.py`、`run_mlx_matrix.py` 是本实验的可审阅编排入口，端口与目录布局固定。运行前准备以下隔离目录；原始数据和含认证信息的配置不提交仓库：

- `repo/`：本分支代码及 Python 3.11 环境 `.venv/`，构建后的 PhotoBench 位于 `apps/photobench/web/dist/`。
- `envs/vllm-mlx/`：固定版本的引擎环境；`models/`：原始及派生模型、BGE、CLIP。
- `source200/snapshots/accepted-baseline-v3/`、`source200/runtime/memory-baseline-identity.json`、`source200/datasets/album3-max-video10/`：已验收冻结材料。名称是实验目录约定，不会连接源机。
- `private/photobench-config/`：有效的 PhotoBench 配置，其中 `runtime_connection.json` 冻结产品设置；Judge 凭据只在这个私有目录内。
- `evidence/source-code-manifest.json`：`revision` 与 `files` 路径→SHA256 映射；`engine-qualification.json`、`window-seed-qualification.json`、`preflight-result.json`：真实资格测试及预跑结果。编排器先检查这些证据。
- `runtime/`、`rounds/`、`results/`、`evidence/`：进程、独立轮次、原始结果和采样记录。嵌入侧车先启动在 18101/18103，并通过健康和数值身份验收。

```sh
export SENTRIX_MLX_EXPERIMENT_ROOT=/absolute/path/to/isolated-experiment
export SENTRIX_MLX_SERIES=unique-series-name
cd "$SENTRIX_MLX_EXPERIMENT_ROOT/repo"
.venv/bin/python -m scripts.benchmarks.run_mlx_matrix
```

编排器用文件锁排除重复启动。已知运行的观察可恢复；处于提交中但没有唯一 run_id 的状态必须先对照 PhotoBench 记录，禁止再次 POST。失败轮次保留后，修复与复测使用新的轮次/系列身份，不能把旧结果改成通过。资格测试、生成请求或运行提交发生未知结果时均不自动重发。

规划器的结构化响应还将 `evidence_type` 限定为 `EvidenceType` 注册表枚举。原 schema 只要求字符串，4B 模型可输出属于 `value_type` 的 `media_set`，导致 JSON 合法却被业务解析器拒绝。新增约束与既有提示词、校验器保持一致；未增加别名，也未接受未知证据类型。旧失败及对应多轮记录保留，用原失败请求的真实生成验证修复后再建立新系列。
