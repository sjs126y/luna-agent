# 架构说明

> 2026-07-17：会话与发送主链路已经收尾。Adapter 旧队列/重试、Gateway busy state 和兼容 Agent 路径均已删除；所有入口以本节的新结构为准。

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
          DeliveryService -> SQLite Outbox -> Adapter.send_message
```

`ConversationCoordinator` 是应用层唯一提交边界。同一 `session_key` 的 Agent turn 串行，不同 session 并发；每轮开始时捕获 `TurnPolicySnapshot` 并登记 `ActiveTurnRegistry`。`ConversationService` 只处理 session/history、AgentLoop、持久化、memory review 和事件。

Gateway 负责平台连接、鉴权、入站 Hook、附件准备与请求规范化。新 Runtime 下 Adapter 只转发入站并执行平台单次发送，不再决定会话忙碌、steer 旁路或 Agent 排队。

Delivery 使用 `SessionDirectory` 从 session 解析平台目标，执行 `PreDelivery/PostDelivery`，并在发送前写入 Outbox。AUTH、APPROVAL、SYSTEM 类型跳过插件可变 Hook；临时失败后台重试，超时或部分分段已送达标记 ambiguous，Outbox 通过原子 claim 避免重复消费者。重试次数由 `gateway.delivery_max_attempts` 控制。

这份文档对应 README 里的 `Runtime Flow`，用于帮助开发者理解 Lumora 内部怎么从“用户输入”流转到“模型调用、工具执行、持久化和观测”。

README 放项目展示和架构图；这里放更详细的分层职责、关键模块和边界。

## 分层总览

| 层级 | 核心问题 | 代表模块 |
| --- | --- | --- |
| 入口层 | 用户从哪里进来 | CLI / inline TUI / Gateway / 平台 adapter / 未来 Desktop/Web |
| 应用启动层 | 全局 runtime 怎么装配 | `create_app_runtime` / `create_agent_runtime` / settings / plugin manager |
| 会话层 | 消息属于哪个会话，走命令还是 agent | `ConversationService` / `SessionStore` / `ConversationCommandRuntime` |
| 输入标准化层 | 文本、平台消息和附件如何统一 | `ConversationInput` / `AttachmentRef` / `AttachmentStore` / `MultiAttachmentProcessor` |
| Agent 核心层 | 如何构建上下文并跑一轮 agent | `build_turn_context` / `run_conversation` / memory / skill / steer / turn report |
| LLM 层 | 供应商和协议如何适配 | `ProviderProfile` / transport registry / Chat Completions / Anthropic / Responses |
| 工具层 | 工具如何安全执行 | tool registry / executor / guard / permission / sandbox / audit / MCP tools |
| 扩展层 | 外部能力如何接入 | plugins / MCP servers / skills / workflows / sub-agents |
| 持久化与观测层 | 运行结果如何存储和查看 | SQLite / messages / tool_runs / turn_reports / memory / activity / doctor |

核心原则：

- 新入口接 `ConversationService`，不要复制 agent loop。
- 新 provider 补 `ProviderProfile` 和 transport，不要在 agent 核心里写供应商分支。
- 新工具走 tool registry、executor、permission、sandbox 和 audit。
- 新平台 adapter 只处理平台差异，不处理模型协议。
- 平台附件下载、附件内容处理、模型多模态格式转换是三件事。
- 新前端可消费字段必须同步 `BACKEND_INTERFACE.md`。

## 入口层

入口层负责“用户怎么进来”和“结果怎么展示或发送出去”。

### CLI / inline TUI

CLI 和 inline TUI 是本地交互入口，主要负责：

- 命令行参数解析。
- 创建或复用 `AppRuntime`。
- 选择当前 session。
- 把普通输入交给 `ConversationService`。
- 把 `/` 开头输入交给 slash command path。
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
- 根据 `LLM_API_MODE` 或 provider/base URL 选择 API mode。
- 从 transport registry 获取 transport。
- 创建 compressor。
- 调用 `init_agent()`。
- 将共享 `HookManager` 注入 agent；插件生命周期回调仍由 PluginManager 单独调用。
- 初始化 workflow engine 的 LLM call 和工具列表。

`ConversationService` 会按 `session_key` 缓存 agent，避免每一轮都重新创建。

### PluginManager

PluginManager 负责插件生命周期：

- discover。
- load enabled。
- configure。
- list plugins。
- 转发正式 Hook 注册并清理 owner；调用少量宿主生命周期回调。

插件可以提供：

- tools。
- platforms。
- memory providers。
- workflows。
- skills。
- LLM transport 与正式生命周期 Hook。

## 会话层

会话层负责“这条输入属于哪个会话、如何排队、要不要执行命令、是否进入 agent、结果如何保存与投递”。

### ConversationCoordinator

`ConversationCoordinator` 是 CLI、TUI、Gateway、Cron 和主动插件共享的应用入口。它负责同 session 串行、跨 session 并发、命令执行通道、`/stop`/`/steer` 实时控制、每轮策略快照，以及根据 `ResponseMode` 返回或交给 Delivery。

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
- `ExternalMemoryRouter` 从插件 registry 选择 Lumora 或 Mem0；不可用时切换核心 fallback。
- Lumora 使用百炼 OpenAI-compatible embedding、Qdrant 语义检索和 SQLite FTS5/BM25，并以 RRF 融合结果。

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

`LLM_API_MODE=auto` 会尝试根据 provider/base URL 探测；中转站场景建议显式配置。

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
- file write size limit。
- secret/path precheck。

权限允许不代表可以越过 sandbox。

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
- hooks。

### Builtin / user plugins

内置插件和用户插件都走 plugin manifest 和 manager。

区别只是来源不同：

- builtin plugins 随项目发布。
- user plugins 放在用户插件目录。

### MCP servers

MCP 更适合作为外部工具服务接入。

用户需要安装和配置 MCP server；Lumora 负责启动、连接、列工具、执行工具和纳入权限链路。

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

Messages 是会话历史来源。

Agent 每轮会读取历史，完成后保存 transcript。压缩发生时会创建压缩后的 session 链路。

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

## 关键流转

### 普通 CLI/TUI 文本

```text
CLI/TUI
  -> AppRuntime
  -> ConversationService.run_turn
  -> SessionStore.load_history
  -> get_or_create_agent
  -> build_turn_context
  -> run_conversation
  -> LLM transport
  -> tool executor if needed
  -> save transcript
  -> persist tool_runs / turn_reports
  -> render events/final response
```

### Gateway 平台消息

```text
platform adapter
  -> Gateway._handle_message
  -> auth / slash command / pending confirmation / busy check
  -> prepare_inbound_attachments
  -> ConversationInput.from_envelope
  -> ConversationService.run_turn_input
  -> MultiAttachmentProcessor
  -> build_turn_context
  -> run_conversation
  -> final response
  -> adapter.send
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

- 是否应该接在入口层，还是应该进入 `ConversationService`？
- 是否新增了前端可消费字段？如果是，更新 `BACKEND_INTERFACE.md`。
- 是否新增配置？如果是，更新 config registry、example、configuration docs 和 doctor。
- 是否新增工具？如果是，确保走 executor、permission、sandbox、audit。
- 是否新增 provider？如果是，补 `ProviderProfile`、transport、usage/cache 解析和测试。
- 是否新增平台消息类型？如果是，只在 adapter 层解析，不让平台私有结构进入 agent loop。
- 是否新增附件处理能力？如果是，放到 multimodal/attachment 层，不塞进 provider 或 gateway。
