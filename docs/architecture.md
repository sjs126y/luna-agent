<div align="center">

<h1>架构说明</h1>

<p><strong>一次请求如何经过会话、模型、工具、记忆，最终可靠地回到用户</strong></p>

<p>
  <img src="https://img.shields.io/badge/status-current-2EA44F" alt="Current architecture">
  <img src="https://img.shields.io/badge/runtime-asyncio-0A84FF" alt="asyncio runtime">
  <img src="https://img.shields.io/badge/design-lightweight-555555" alt="Lightweight design">
</p>

<p>
  <a href="../README.md">项目首页</a> ·
  <a href="README.md">文档中心</a> ·
  <a href="capabilities-and-boundaries.md">功能全景</a> ·
  <a href="plugins.md">插件</a>
</p>

</div>

---

> **阅读建议**：先看“统一 Conversation Runtime”和“分层总览”。需要修改某个子系统时，再展开对应层级；维护检查和三条关键流转放在文档末尾。

## 统一 Conversation Runtime

```text
Gateway / TUI / CLI / Cron / Active Plugin
                    |
                    v
        ConversationCoordinator
          |-- per-session conversation queue
          |-- real-time control channel (/stop, /steer)
          `-- slash command execution lanes
                    |
                    v
          ConversationService (one turn)
                    |
                    v
          DeliveryService -> DeliveryPlanner -> SQLite multipart Outbox -> Adapter.send_message/send_artifact
```

`ConversationCoordinator` 是应用层唯一提交边界。同一 `session_key` 的 Agent turn 串行，不同 session 并发；每轮开始时捕获 `TurnPolicySnapshot` 和 Capability lease，并登记 `ActiveTurnRegistry`。稳定 `request_id` 在有界幂等缓存中复用进行中或已完成结果，防止主动 runner 和入口重试重复运行模型。`ConversationService` 只处理 session/history、AgentLoop、持久化、memory review 和事件。

Gateway 负责平台连接、鉴权、入站 Hook、附件准备与请求规范化。新 Runtime 下 Adapter 只转发入站并执行平台单次发送，不再决定会话忙碌、steer 旁路或 Agent 排队。

Delivery 使用 `SessionDirectory` 从 session 解析平台目标，执行 `PreDelivery/PostDelivery`，并在发送前写入 Outbox。Gateway 启动时会用 SessionStore 中持久化的 `platform/chat_id/user_id` 恢复反向投递绑定，所以 Cron 和主动插件无需等待一条新平台入站。工具/MCP 产物先进入 ArtifactStore；普通本地文件可通过 `artifact_from_file` 显式复制进当前 turn；模型再用 `response_attach` 将产物选入 `OutboundMessage`。DeliveryPlanner 按平台能力生成 text/image/file/audio/video operation。AUTH、APPROVAL、SYSTEM 类型跳过插件可变 Hook；临时失败按分片后台重试，超时或部分内容已送达标记 ambiguous，已成功 part 不会重复发送。Conversation 成功但 Delivery 暂不可用时结果保持完成，Outbox 只重试消息，不重跑 Agent。重试次数由 `gateway.delivery_max_attempts` 控制。

这份文档对应 README 里的 `Runtime Flow`，用于帮助开发者理解 Luna Agent 内部怎么从“用户输入”流转到“模型调用、工具执行、持久化和观测”。

README 放项目展示和架构图；这里放更详细的分层职责、关键模块和边界。

## 分层总览

| 层级 | 核心问题 | 代表模块 |
| --- | --- | --- |
| 入口层 | 用户从哪里进来 | CLI / inline TUI / Gateway / 平台 adapter / 未来 Desktop/Web |
| 应用启动层 | 全局 runtime 怎么装配 | `create_app_runtime` / `create_agent_runtime` / settings / plugin manager |
| 协调层 | 消息属于哪个会话，走命令、控制还是 agent | `ConversationCoordinator` / `ActiveTurnRegistry` / `SessionDirectory` |
| 单轮会话层 | 历史、Agent turn 和持久化如何执行 | `ConversationService` / `SessionStore` / `ConversationCommandRuntime` |
| 输入标准化层 | 文本、平台消息和附件如何统一 | `ConversationInput` / `AttachmentRef` / `AttachmentStore` / `MultiAttachmentProcessor` |
| Agent 核心层 | 如何构建上下文并跑一轮 agent | `build_turn_context` / `run_conversation` / memory / skill / steer / turn report |
| LLM 层 | 供应商和协议如何适配 | `ProviderProfile` / transport registry / Chat Completions / Anthropic / Responses |
| 工具层 | 工具如何安全执行 | tool registry / executor / guard / permission / sandbox / audit / MCP tools |
| 扩展层 | 外部能力如何接入 | plugins / MCP servers / skills / workflows / sub-agents |
| 投递层 | 回复如何可靠到达目标平台 | `DeliveryService` / `DeliveryPlanner` / multipart Outbox / Adapter |
| 持久化与观测层 | 运行结果如何存储和查看 | SQLite / messages / tool_runs / turn_reports / memory / activity / doctor |

核心原则：

- 新入口构造 `SubmissionRequest` 并提交 `ConversationCoordinator`，不要直连 Agent Loop。
- 新 provider 补 `ProviderProfile` 和 transport，不要在 agent 核心里写供应商分支。
- 新工具走 tool registry、executor、permission、sandbox 和 audit。
- 新平台 adapter 只处理平台差异，不处理模型协议。
- 平台附件下载、附件内容处理、模型多模态格式转换是三件事。
- 新前端可消费字段必须同步 `BACKEND_INTERFACE.md`。

<details>
<summary><strong>入口层：CLI、TUI、Gateway 与未来 Desktop/Web</strong></summary>

## 入口层

入口层负责“用户怎么进来”和“结果怎么展示或发送出去”。

### CLI / inline TUI

CLI 和 inline TUI 是本地交互入口，主要负责：

- 命令行参数解析。
- 创建或复用 `AppRuntime`。
- 选择当前 session。
- 把普通输入和 `/` 命令统一提交给 `ConversationCoordinator`。
- 渲染流式输出、thinking、工具确认、context meter 和最终回复。

关键边界：

- 不直接拼 LLM 请求。
- 不直接执行工具。
- 不绕过 permission / sandbox。
- 不单独维护一套会话语义。

### Gateway / 平台 adapter

Gateway 是平台入口，平台 adapter 负责 Telegram、飞书、QQ、微信等平台差异。

Gateway 负责：

- 加载平台插件。
- 创建和连接 adapter。
- 管理平台连接状态和重连。
- 做平台用户鉴权。
- 将平台来源绑定到逻辑 session key。
- 处理平台侧异步工具确认。
- 授权通过后触发 adapter 准备附件。
- 构造 `SubmissionRequest` 并提交 Coordinator。

平台 adapter 负责：

- 连接平台。
- 收消息。
- 发消息。
- 把平台消息解析为统一事件。
- 把平台图片、文件、语音等解析为 attachment ref。
- 在平台支持时下载附件或获取下载 URL。
- 按平台最大长度编码和分段逻辑消息，但不负责失败重试。

关键边界：

- Gateway 不负责 provider 选择。
- Gateway 不负责 LLM wire protocol。
- Gateway 不负责附件内容理解。
- adapter 不负责 agent loop。

### 未来 Desktop/Web

未来 Desktop/Web 应作为新的入口层，而不是新建另一套智能核心。

推荐接入方式：

- 构造 `ConversationInput`。
- 使用事件流接口消费 `assistant_delta`、`thinking_delta`、`tool_*`、`turn_*` 等事件。
- 工具确认、activity、usage、tool runs、turn reports 复用后端结构。
- 文件上传后转成 `AttachmentRef`，交给后端附件和多模态链路。

</details>

<details>
<summary><strong>应用启动层：Settings、AppRuntime 与 PluginManager</strong></summary>

## 应用启动层

应用启动层负责“全局 runtime 有哪些能力，以及每个会话 agent 如何装配”。

### Settings / ConfigLoader

配置来源主要是：

- `.env`：secret、LLM provider、平台 token、API key。
- `config.yaml`：运行行为、storage、plugins、memory、sandbox、MCP、auth、session、execution、multimodal。
- profile：`init --profile local/server/bot/telegram/feishu/wechat` 生成初始配置。

配置系统的职责是把分散配置收敛为 `Settings`，让后续模块不用自己读取环境变量或 YAML。

### create_app_runtime

`create_app_runtime` 是应用级启动入口，创建 `AppRuntime`。

它负责装配：

- settings。
- data dir / system dir。
- plugin manager。
- sandbox。
- audit。
- MCP manager。
- SQLite database。
- compression chain。
- session store。
- memory manager。
- memory review service。
- conversation service。
- boot report。

`AppRuntime` 还负责：

- `create_gateway()`。
- `start_gateway()`。
- `stop_gateway()`。
- `close()`。
- `health_snapshot()`。

### create_agent_runtime

`create_agent_runtime` 是会话 agent 的装配入口。

它负责：

- 根据 `settings.llm_provider` 获取 `ProviderProfile`。
- 通过 Provider capability 统一解析 `LLM_API_MODE`、协议来源和模型限制。
- 从 transport registry 获取 transport。
- 创建 compressor。
- 调用 `init_agent()`。
- 将共享 `HookManager` 注入 agent；插件生命周期回调仍由 PluginManager 单独调用。
- 初始化 workflow engine 的 LLM call 和工具列表。

`ConversationService` 会按 `session_key` 缓存 agent，避免每一轮都重新创建。

### Plugin Runtime

`PluginManager` 是发现、诊断与控制 facade；generation 生命周期由插件 runtime、`CandidateCatalog`、`CapabilitySnapshot` 和版本化 installer 协作完成：

- `PluginRuntimeContext` 通过 `ctx.register.*` 收集当前 generation 的能力。
- `CapabilityMapper` 将既有 manager 注册项映射为稳定 binding。
- `SnapshotBuilder` 校验冲突并生成不可变路由；`CapabilityStore` 原子发布 revision。
- Coordinator 为 Turn 获取 lease，Tool/Skill/Workflow/Command/Hook 使用同一能力视图。
- `tool_search`、`tool_describe` 和嵌套 `tool_call` 同样读取该 lease，不能穿透到新 generation 的全局 Registry。
- 新 generation 发布后旧实例进入 DRAINING，最后一个 lease 释放后才停止 Hook、MCP 与后台任务。
- installer 使用 staging 与不可变 package digest 支持安装、更新、回滚和延迟卸载。

插件可以提供：

- tools。
- platforms。
- memory providers。
- workflows。
- skills。
- MCP servers。
- commands。
- LLM transport 与正式生命周期 Hook。
- 受 capability 限制的 conversation submit / notification 端口。
- 隔离 storage 与绑定 runtime instance 的 task 端口。

</details>

<details>
<summary><strong>会话层：Coordinator、ConversationService 与命令通道</strong></summary>

## 会话层

会话层负责“这条输入属于哪个会话、如何排队、要不要执行命令、是否进入 agent、结果如何保存与投递”。

### ConversationCoordinator

`ConversationCoordinator` 是 CLI、TUI、Gateway、Cron 和主动插件共享的应用入口。它负责同 session 串行、跨 session 并发、命令执行通道、`/stop`/`/steer` 实时控制、每轮策略/能力快照，以及根据 `ResponseMode` 返回或交给 Delivery。`request_id` 同时承担一次运行生命周期内的幂等键：重复提交返回原 handle/outcome，不再次排队；相同 ID 对应不同 session、owner、response mode 或输入摘要时直接拒绝。缓存有界，不替代业务插件自身的持久 checkpoint。

### ConversationService

`ConversationService` 是单轮对话执行边界，由 Coordinator 调用。

核心职责：

- 根据 `session_key` 找到或创建 session。
- 读取历史消息。
- 获取或创建 cached agent。
- 处理结构化输入。
- 调用多模态处理。
- 构建 turn context。
- 调用 agent loop。
- 保存 transcript。
- 记录 turn report。
- 记录 tool runs。
- 提供 query service。

入口方法：

- `run_turn(session_key, source, text)`：兼容纯文本。
- `run_turn_events(...)`：纯文本 + 事件。
- `run_turn_input(session_key, user_input)`：结构化输入。
- `run_turn_input_events(...)`：结构化输入 + 事件。

### SessionStore

`SessionStore` 负责会话和消息历史：

- `get_or_create`。
- `load_history`。
- `save_transcript`。
- compressed session。
- session rename / delete。
- session expire。

除了消息历史，SessionStore 还持久化 `session_key` 对应的 platform、chat ID、user ID 和 chat type。Gateway 启动时把这些元数据恢复到内存 `SessionDirectory`；逻辑 session key 不需要也不应该通过字符串拆分来猜真实平台目标。

Gateway 和 CLI 不直接维护历史消息，而是通过 `ConversationService` 使用 `SessionStore`。

### ConversationCommandRuntime

Slash command 共用 command registry，但不同入口需要不同 runtime。

`ConversationCommandRuntime` 提供命令执行所需能力：

- 当前 session key。
- 当前 source。
- settings。
- plugin manager。
- conversation service。
- session 切换、删除、重命名。
- pending confirmation 状态。
- stop agents。
- plugin command kwargs。

Gateway 会有自己的 command runtime，因为它要处理平台 session 和 pending confirmation；命令的并发与 barrier 语义由 Coordinator 统一决定。

### Slash command path

以 `/` 开头的输入先进入 slash command path。

命令可能有三种结果：

- 直接返回文本，例如 `/permissions`、`/mode`、`/activity`。
- 返回结构化 `CommandResult`，给前端渲染。
- 设置 `continue_text`，把命令转换为普通消息继续进入 agent。

### Normal message path

普通消息路径是：

```text
entry
  -> ConversationCoordinator
  -> ConversationService
  -> SessionStore
  -> get_or_create_agent
  -> MultiAttachmentProcessor
  -> build_turn_context
  -> run_conversation
  -> save transcript
  -> persist turn report / tool runs
  -> return ConversationTurnResult
```

</details>

<details>
<summary><strong>输入标准化层：文本、附件与多模态处理</strong></summary>

## 输入标准化层

输入标准化层负责把“纯文本、平台消息、附件、未来桌面端上传”变成 agent 能消费的统一结构。

### Text

纯文本仍然是最基础输入。旧入口可以继续调用 `run_turn()`，内部会转换成 `ConversationInput`。

### ConversationInput

`ConversationInput` 是统一输入结构，包含：

- text。
- source。
- attachments。
- metadata。

Gateway 的 envelope 和未来 Desktop/Web 都应该优先构造这个结构。

### Attachment refs

`AttachmentRef` 描述附件引用，而不是直接描述附件内容。

它可以包含：

- kind：image / audio / video / file。
- name。
- mime type。
- size。
- URL。
- local path。
- platform file id。
- metadata。

### Platform message parts

平台 adapter 会把平台私有消息拆成标准 message parts。

例如：

- 微信图片。
- QQ 文件。
- 飞书 image key。
- Telegram document / voice / video。

这些平台差异不应该泄漏到 agent loop。

### AttachmentStore

`AttachmentStore` 负责本地附件缓存：

- 下载结果入库。
- sha256 去重。
- 保存 metadata。
- 读取本地文件。
- 保存派生文本或图片描述缓存。

它像附件层的本地存储，不负责 OCR、总结或语义理解。

### MultiAttachmentProcessor

`MultiAttachmentProcessor` 负责按配置处理附件：

- `native`：生成模型原生多模态 block。
- `text`：文本化附件，例如文本文件、PDF、docx、图片描述、OCR。
- `auto`：优先可用能力，不可用时降级。
- `off`：不下载、不缓存、不处理，只生成提示。
- 失败时生成 notice，不让 turn 直接崩掉。

关键边界：

- 下载是平台 adapter / AttachmentStore 的事。
- 内容处理是 multimodal 的事。
- provider 能力只影响最终 native/text/notice 策略，不决定平台是否能下载。
- transport 只负责把处理后的内容转换成 provider wire format。

</details>

<details>
<summary><strong>Agent 核心层：上下文、记忆、Skill、Loop 与 Steer</strong></summary>

## Agent 核心层

Agent 核心层负责“一轮 agent 怎么组织上下文、调用模型、执行工具、处理 steer、产出报告”。

### build_turn_context

`build_turn_context` 把本轮输入和历史整理成 agent 可执行上下文。

它关注：

- session history。
- 当前用户输入。
- system prompt。
- 工具摘要。
- memory system text。
- skill summary。
- compression 状态。
- turn id。
- context usage 估算。

### Memory Prefetch

内部记忆与外部记忆使用不同的上下文策略：

- internal Markdown 全量进入 system prompt，每个缓存 Agent 固定一个 revision snapshot，仅在配置的 turn 边界采用新 revision，以保护 prompt cache。
- external memory 按当前消息检索并作为临时上下文注入，不写入会话历史。
- AppRuntime 持有异步 review queue/worker，checkpoint、observation、buffer 和历史保存在 `data/memory/memory.db`。
- `ExternalMemoryRouter` 从插件 registry 选择 Luna Agent 或 Mem0；不可用时切换核心 fallback。
- Luna Agent 使用百炼 OpenAI-compatible embedding、Qdrant 语义检索和 SQLite FTS5/BM25，并以 RRF 融合结果。

知识文档 ingest/RAG 不属于记忆系统，后续使用独立插件。记忆结果会影响 prompt，但不应该改变工具执行和权限边界。

### Skill Injection

Skill 用于沉淀提示、脚本和领域流程。

注入通常分为：

- skill summary。
- 命中后的具体 skill 内容。
- 工具形式的 skill 查询或读取。

Skill 影响模型上下文，不直接绕过工具执行。

### Request planning

这里的 request planning 指 agent 在进入 transport 前形成请求所需信息：

- system prompt。
- tools schema。
- message history。
- multimodal content blocks。
- cache diagnostics 所需 hash。
- usage/context 估算输入。
- provider 能力限制下的降级信息。

当前它不是必须独立成一个大对象，但概念上是 Agent 核心层和 LLM transport 的交界。

### run_conversation

`run_conversation` 是 agent 主循环。

它负责：

- 发起 LLM 调用。
- 接收文本、thinking、tool call。
- 处理 retry。
- 执行工具。
- 把工具结果写回消息。
- 检查 context overflow。
- 消费 steer。
- 产出 final response。
- 产出 turn report。

### ActiveTurnRegistry

`ActiveTurnRegistry` 属于 Coordinator，按 `session_key` 和 `turn_id` 管理运行中的任务与 steer。`/stop`、`/steer` 走独立控制通道，不等待普通会话队列；agent loop 在下一步循环消费 steer，把它作为高优先级用户修正注入。

### Turn report recorder

Turn report 记录本轮发生了什么：

- turn id。
- status。
- LLM usage。
- context usage。
- tool calls。
- retry。
- errors。
- steer。
- tool truth。

它解决“模型说调用了工具，但实际上没有调用”的可观测性问题。

</details>

<details>
<summary><strong>LLM 层：Provider、Transport、Cache 与 Usage</strong></summary>

## LLM 层

LLM 层负责“不同 provider 和 wire protocol 怎么统一接入”。

### ProviderProfile

`ProviderProfile` 描述 provider 能力和策略：

- provider name。
- base URL。
- model。
- max tokens。
- API mode。
- cache strategy。
- usage field mapping。
- multimodal support。
- 请求能力和 wire protocol 描述；不开放 LLM request/response 改写 Hook。

ProviderProfile 不直接发 HTTP，它给 transport 提供能力描述。

### Transport registry

Transport registry 按 API mode 选择 transport。

常见模式：

- `chat_completions`。
- `anthropic_messages`。
- `responses`。
- `codex_responses`。

`LLM_API_MODE=auto` 优先采用 provider 默认协议：OpenAI 使用 Responses，Anthropic/DeepSeek 使用 Anthropic Messages，OpenRouter/xAI 使用 Chat Completions；未知 provider 才使用明确的 base URL 线索。主 Agent、Memory LLM、插件 LLM port 和视觉 LLM 共用同一解析结果；特殊中转协议仍建议显式配置。

### Anthropic Messages

Anthropic transport 负责 Anthropic Messages 格式：

- system。
- messages。
- tools。
- content blocks。
- cache_control。
- Anthropic usage 解析。

### Chat Completions

Chat Completions transport 负责 OpenAI-compatible 协议：

- DeepSeek。
- OpenAI-compatible 中转站。
- 常规 `/chat/completions`。
- tool calls。
- stream delta。
- image_url mixed content。

### Responses / Codex Responses

Responses transport 负责 Responses wire API。

Codex Responses 是面向 Codex/Ahoo 这类中转站的兼容路径，通常使用根 base URL 和 responses wire API。

### Cache diagnostics

缓存诊断记录：

- provider cache capability。
- system hash。
- tools hash。
- message prefix hash。
- cache read tokens。
- cache write tokens。
- cache hit/miss tokens。
- hit rate。

目标是区分：

- 请求前缀变了。
- provider 没命中。
- provider 命中了但之前没解析出来。

### Usage normalization

Usage 会归一成前端和 doctor 能理解的字段：

- input tokens。
- output tokens。
- context used tokens。
- context window。
- context percent。
- cache usage。

`context_used_tokens` 表示当前上下文占用，不等同于最近一次 API 的 input tokens。

Provider capability catalog 同时记录模型硬上下文、输出上限、数据来源与校验日期。`ProviderProfile.context_window` 保持为 Agent 实际使用的有效窗口：OpenAI 未显式配置时默认 256K，其他已知 provider 使用模型硬上限，未知模型使用 256K fallback；显式上下文和输出配置都会被已知硬上限裁剪。OpenRouter 仅在静态 catalog 未命中时查询远端模型元数据，并以短超时和本地 TTL 缓存降级。

压缩阈值默认是有效窗口的 90%，同时必须为本轮输出额度和 `max(4096, context * 1%)` 留出空间。压缩器、`/usage` 与事件诊断共用同一个阈值函数。

</details>

<details>
<summary><strong>工具层：Registry、Executor、Security、MCP 与进程</strong></summary>

## 工具层

工具层负责“模型请求工具时，如何安全、可审计地执行”。

### Tool registry

Tool registry 汇总：

- 内置工具。
- 插件工具。
- MCP 工具。
- workflow 工具。
- delegate/sub-agent 工具。

模型只能调用 registry 暴露的工具。

Registry 不会把全部 schema 塞进每一轮。19 个日常核心工具直接暴露，其中 `list_directory`、`file_info`、`glob`、`grep` 分别承担单层浏览、元数据检查、路径搜索和内容搜索；calculator、task、workflow、worktree、多 Agent 组合、附件工具、MCP 和插件工具继续注册，但通过 `tool_search / tool_describe / tool_call` 按需发现。延迟工具仍走同一个 Executor 和安全链路。完整边界见 [Core Agent Tools](core-tools.md)。

文件搜索使用线程化的有界扫描内核：进入目录前剪枝，达到结果、条目或时间预算立即停止。文件读取支持行窗口；写入采用原子替换；Bash 持续排空子进程管道但只保留有限输出，避免单个工具冻结 Gateway 或耗尽内存。

### Tool executor

Tool executor 是统一执行入口。

它负责：

- 根据 tool name 找到实现。
- 构造执行上下文。
- 调用 execution guard。
- 执行工具。
- 处理异常。
- 截断过长输出。
- 发出 tool events。
- 记录 tool run。

### Execution guard

Execution guard 是执行前门控。

它整合：

- 工具风险。
- 权限类别。
- permission policy。
- path safety。
- URL safety。
- destructive precheck。
- sandbox。

### Permission manager

Security evaluator 根据以下信息做决策：

- execution mode 对应的 filesystem/network profile。
- `permissions.tool_approval` 的工具审批策略。
- 当前 session 内存中的工具/资源限时授权。
- Gateway/CLI/TUI 确认结果。

决策结果：

- allow。
- ask。
- deny。

安全上下文只在具体工具确认中扩权，授权对象是精确工具、路径或 network host；类别级 `/allow` 及旧运行时兼容层已经删除。

### Sandbox

Sandbox 是硬边界：

- roots。
- blocked patterns。
- bash work dir。
- bash path restrict。
- bash network restrict。
- process mount plan：系统运行目录、`cwd`、显式 read/write resource 与 blocked mask 分层映射。
- file write size limit。
- secret/path precheck。

权限允许不代表可以越过 sandbox。Bash/后台进程的 resource resolver 先生成精确文件系统需求，Tool Pipeline 完成审批后才构造平台启动计划：Linux/WSL 使用 Bubblewrap mount plan，原生 Windows 使用一次性 Shell Broker、AppContainer ACL/capability 与 Job Object；子进程看不到未声明的宿主路径。MCP stdio 使用同一进程后端抽象，但暂时保留宿主只读兼容策略，避免破坏现有 server 运行依赖。

### Audit

Audit 记录工具决策和执行结果，辅助回看：

- 谁请求了工具。
- 工具参数摘要。
- 权限决策。
- 是否被拒绝。
- 是否失败。
- 是否截断。

### MCP tools

MCP server 暴露的工具进入同一套 tool registry 和 executor。

这意味着 MCP 工具也要经过：

- permission。
- sandbox/precheck。
- audit。
- tool runs。

### Background process tools

后台进程工具用于长任务：

- start。
- list。
- read。
- wait。
- kill。
- clear。

它们进入 activity runtime，前端可以查看后台任务状态。

</details>

<details>
<summary><strong>扩展层：Plugin、MCP、Skill、Workflow 与 Sub-agent</strong></summary>

## 扩展层

扩展层负责“后续能力怎么接进来，而不破坏核心 runtime”。

### Plugins

插件是主要扩展机制。

插件可以提供：

- tools。
- platforms。
- workflows。
- memory providers。
- skills。
- MCP servers。
- hooks。
- commands。
- capability-bound conversation submit / notification ports。

### Builtin / user plugins

内置插件和用户插件都走 plugin manifest 和 manager。

区别只是来源不同：

- builtin plugins 随项目发布。
- user plugins 放在用户插件目录。

### MCP servers

MCP 更适合作为外部工具服务接入。

用户需要安装和配置 MCP server；Luna Agent 负责启动、连接、列工具、执行工具和纳入权限链路。

MCP 不属于核心 Runtime 的启动屏障。插件、Hook 和 Sandbox 完成注册后，MCP Manager 调度每个 server 的长期后台 task 并立即返回；Gateway 可以先上线。server 首次连接成功后通过 Tool Registrar 更新 Registry，当前 turn 保持原工具快照，缓存 Agent 从下一轮开始看到新工具。断线时已知工具保留在目录中但标记为不可用，Runtime 独立退避重连；单个 MCP 启动失败不会阻塞其他 server 或 Gateway。

### Skills

Skill 更适合沉淀：

- prompt。
- 脚本。
- 工作方法。
- 项目经验。
- 领域流程。

Skill 影响 agent 上下文或提供辅助工具，不替代 permission/sandbox。

### Workflows

Workflow 是结构化流程层，适合固定步骤任务。

它可以复用 LLM call、工具列表和 runtime 能力。

### Sub-agents

Sub-agent / delegate 用于把任务拆给子 agent。

它们需要受控：

- 并发。
- token。
- tool call。
- activity 状态。
- turn report。

</details>

<details>
<summary><strong>投递层：Binding、Outbox 与平台 Adapter</strong></summary>

## 投递层

投递层把逻辑 `session_key` 转换为真实平台目标，并保证发送失败不会污染 Conversation 语义。

```text
SessionStore (persistent platform/chat metadata)
        -> SessionDirectory.restore()
        -> SessionBinding(session_key -> SessionSource)
        -> PlatformDirectory(platform -> connected adapter)
        -> DeliveryPlanner
        -> multipart Outbox
        -> adapter.send_message / send_artifact
```

`SessionBinding` 是反向路由，不是聊天历史：它保存逻辑 session 对应的 platform、chat ID、user ID 和 chat type。正常平台入站会刷新绑定；Gateway 重启则从 SessionStore 恢复。Binding 存在但 Adapter 不在线，以及绑定暂时不可用，都属于可恢复的 `DEFERRED`；永久发送错误才把本次 Delivery 标记为失败。

Coordinator 把 Conversation 和 Delivery 分成两个结果层次。Agent 已经生成并持久化回复后，即使初次发送进入 `DEFERRED`，Submission 仍是 `COMPLETED`，具体投递状态位于 `payload.delivery_result`。Outbox 持有同一 `OutboundMessage` 并后台重试，不重新构造用户消息，也不再次调用 LLM。

Outbox 在逻辑消息下保存多个 operation。文本、图片和文件可以分别成功或失败；已经成功的 part 后续跳过。超时和部分成功属于 ambiguous，避免无法判断平台是否收件时盲目重复发送。

</details>

<details>
<summary><strong>持久化与观测层：SQLite、Reports、Activity 与 Doctor</strong></summary>

## 持久化与观测层

持久化与观测层负责“长期使用时如何知道发生了什么”。

### SQLite

SQLite 是本地状态库，默认在 `data/state.db`。

它保存：

- sessions。
- messages。
- tool runs。
- turn reports。
- query 数据。

### Messages

Messages 是会话历史来源。Agent 每轮读取当前物理 Session，完成后增量保存 transcript。

上下文达到阈值时，压缩器保留 token 预算内的真实用户原话和近期完整消息，生成一份
不设独立输出上限的 Handoff Summary。ConversationService 在继续当前 turn 前先持久化
replacement history，形成 `session-v1 -> session-v2` 物理链；工具循环中也可以在安全边界
创建 mid-turn checkpoint。旧物理 Session 不改写，窗口编号和压缩前后 token 单独记录在
SQLite `compression_checkpoints` 中。

### Tool runs

Tool runs 保存真实工具调用。

它用于：

- 前端工具历史。
- `/tool-runs`。
- tool truth。
- 审计。

### Turn reports

Turn reports 保存每轮 agent 摘要。

它用于：

- 判断是否真的调用工具。
- 关联 tool runs。
- 查看 usage。
- 查看 retry/error。
- 查看 steer 是否被消费。

### Memory

Memory 包括：

- 稳定的 internal Markdown snapshot。
- 动态 external provider 与核心 fallback。
- observation buffer、结构化 consolidation 和冲突状态。
- SQLite 权威记录、Qdrant 派生向量索引和 provider 历史。

Memory 是长期上下文能力，但不应该承载权限状态。

### Activity runtime

Activity runtime 汇总运行中状态：

- sub-agents。
- background processes。
- gateway agent。

它给 `/activity`、前端面板和 doctor 提供结构化数据。

### Doctor / runtime health

Doctor 和 health snapshot 用于启动诊断和运行排错。

覆盖：

- boot report。
- config diagnostics。
- plugin state。
- MCP state。
- Gateway state。
- tools state。
- execution mode。
- sandbox。
- cache/context usage。
- activity。
- hook registrations、超时、失败和阻止计数。

</details>

## 关键流转

### 普通 CLI/TUI 文本

```text
CLI/TUI
  -> AppRuntime
  -> ConversationCoordinator.submit(RETURN_ONLY)
  -> command/control lane or ordered turn queue
  -> ConversationService.run_turn_input
  -> SessionStore.load_history
  -> get_or_create_agent
  -> build_turn_context
  -> run_conversation
  -> LLM transport
  -> tool executor if needed
  -> save transcript
  -> persist tool_runs / turn_reports
  -> ConversationTurnResult
  -> render events/final response
```

### Gateway 平台消息

```text
platform adapter
  -> Gateway inbound normalization / auth / pending confirmation
  -> prepare_inbound_attachments
  -> SubmissionRequest(DELIVER)
  -> ConversationCoordinator
  -> command/control lane or ordered turn queue
  -> ConversationService.run_turn_input
  -> MultiAttachmentProcessor
  -> build_turn_context
  -> run_conversation
  -> OutboundMessage
  -> DeliveryService / multipart Outbox
  -> Adapter single send operation
```

### 工具调用

```text
model tool_call
  -> ToolRegistry
  -> ToolExecutor
  -> ExecutionGuard
  -> PermissionManager
  -> Sandbox / safety precheck
  -> tool implementation
  -> tool result
  -> events + tool_runs + turn_report + audit
```

### 多模态附件

```text
platform message part / desktop upload
  -> AttachmentRef
  -> adapter.prepare_inbound_attachments
  -> AttachmentStore
  -> ConversationInput
  -> MultiAttachmentProcessor
  -> native block / text fallback / notice
  -> Provider transport
```

## 维护清单

新增功能时按这个清单检查：

- 是否只是新入口（提交 Coordinator），还是单轮会话能力（进入 ConversationService）？
- 是否新增了前端可消费字段？如果是，更新 `BACKEND_INTERFACE.md`。
- 是否新增配置？如果是，更新 config registry、example、configuration docs 和 doctor。
- 是否新增工具？如果是，确保走 executor、permission、sandbox、audit。
- 是否新增 provider？如果是，补 `ProviderProfile`、transport、usage/cache 解析和测试。
- 是否新增平台消息类型？如果是，只在 adapter 层解析，不让平台私有结构进入 agent loop。
- 是否新增附件处理能力？如果是，放到 multimodal/attachment 层，不塞进 provider 或 gateway。
