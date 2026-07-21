<div align="center">

<h1>Backend Interface Contract</h1>

<p><strong>TUI、未来 Desktop/Web 与后端 Runtime 之间的稳定契约</strong></p>

<p>
  <img src="https://img.shields.io/badge/protocol-v1-0A84FF" alt="Protocol v1">
  <img src="https://img.shields.io/badge/events-stable-2EA44F" alt="Events stable">
  <img src="https://img.shields.io/badge/compatibility-text%20preserved-2EA44F" alt="Text compatibility preserved">
</p>

<p>
  <a href="README.md">项目首页</a> ·
  <a href="docs/README.md">文档中心</a> ·
  <a href="FRONTEND_INTERFACE_REQUIREMENTS.md">前端需求</a> ·
  <a href="docs/architecture.md">架构</a>
</p>

</div>

---

本文给前端线使用，描述当前后端已经稳定提供的事件、命令和工具确认语义。后续 desktop/web/TUI 对接时优先看本文；当前状态见 `docs/README.md`，阶段背景见 `PROJECT_EVOLUTION.md`。

> 2026-07-19 命名迁移：宿主 Python 包改为 `luna_agent`，CLI 改为 `luna-agent`，内置 Memory provider 改为 `luna`。本次没有修改 `ConversationEvent`、`SubmissionRequest`、`SubmissionOutcome`、Artifact、Delivery 或确认 payload，`protocol_version` 仍为 `1`，前端不需要做数据迁移。仓库内 TUI 的源码 import 已同步到 `luna_agent.*`。

## 快速导航

| 前端正在做 | 直接查看 |
| --- | --- |
| 实时聊天与工具轨迹 | [Conversation Event Stream](#1-conversation-event-stream) / [Event Types](#2-event-types) |
| 文件上传与出站附件 | [Desktop Multimodal](#3-desktop-multimodal-input-reserved-interface) / [Artifact 与 Delivery](#出站-artifact-与-delivery) |
| 工具确认和安全模式 | [Inline Tool Confirmation](#4-inline-tool-confirmation) / [Execution Mode](#6-execution-mode) |
| `/stop`、`/steer` | [Runtime Steer](#5-runtime-steer) |
| Usage、Tool Runs、Activity | [Usage](#7-usage--context-summary) / [Tool Runs](#8-tool-runs--tool-truth) / [Activity](#11-activity-runtime-interface) |
| Memory、Plugin、MCP、QQ 状态 | [Memory](#12-memory-runtime) / [Plugin](#13-plugin-diagnostics) / [MCP](#14-mcp-runtime-diagnostics) / [QQ](#15-qq-runtime-diagnostics) |

> 字段级定义只在本文件维护。Progress 和 Requirements 文档只记录消费状态与未完成需求，不复制完整 Schema。

## 0. 统一提交边界

后端入口现统一提交 `SubmissionRequest` 到 `ConversationCoordinator`。TUI/CLI 使用 `ResponseMode.RETURN_ONLY`，因此继续直接消费 `ConversationTurnResult` 和本文件定义的事件流；Gateway 使用 `DELIVER`，最终 `OutboundMessage` 由 `DeliveryService` 投递，Adapter 不再返回响应字符串。

`SubmissionHandle` 会立即提供 accepted receipt，并可异步等待最终 `SubmissionOutcome`。Outcome 的 `kind` 为 `conversation`、`command` 或 `control`；`status` 为 `completed`、`failed`、`cancelled` 或 `rejected`。这套对象目前属于后端应用接口，前端事件协议版本仍为 `1`，现有事件字段没有破坏性变化。

多模态出站保持文本兼容：`ConversationTurnResult.final_response` 和 `SubmissionOutcome.response` 仍为普通字符串；结构化结果分别位于 `ConversationTurnResult.outbound_message` 和 `SubmissionOutcome.message`。`OutboundMessage.parts[]` 的媒体 part 使用宿主管理的 `artifact_id`，不暴露本地路径，也不复用平台 `file_id`。

- `/stop`、`/steer` 不等待当前对话队列，可实时响应。
- `/mode` 立即修改会话的下一轮策略；当前运行轮次保持启动时快照。
- `/new`、session rename/delete/switch 等操作与会话队列有序执行。
- Skill slash command 展开后作为普通 Agent 请求进入队列。

新增插件命令会通过现有 slash command metadata 自动暴露给前端：

- `/github-status`：GitHub MCP、仓库白名单与写操作策略。
- `/github-watch-status`：GitHub 主动监视状态、仓库、待处理事件和错误数。
- `/developer-docs-status`：Context7 插件配置摘要。
- `/browser-status`：Playwright 浏览器、域名和上传/脚本策略。
- `/reminders`、`/remind-cancel`：当前会话提醒查询与取消。
- `/feeds`：当前会话 Feed 订阅。
- `/inbox-status`：Inbox 文件处理、等待和失败状态。

新增 Skill 命令名为 `/repo-summary`、`/review-pr`、`/triage-issues`、`/release-notes`、`/library-docs`、`/upgrade-library`、`/compare-library-api`、`/inspect-web-page`、`/test-web-page`、`/operate-web-page`。Skill 执行仍表现为普通 conversation turn，不新增前端事件类型。

低频插件控制面新增 `plugin_inspect`、`plugin_build`、`plugin_manage` 三个可检索工具。它们继续使用现有 `tool_start/tool_decision/tool_end` 与确认 payload，不新增事件类型；前端按普通工具展示即可。`plugin_inspect(action="environments")` 返回环境保留/回收预览；`plugin_manage(action="environment_gc", apply=false)` 也是只读预览，`apply=true` 才删除。`tool_approval_mode` 由 action 解析：`validate=auto`，`package/enable/disable/reload/active_on/active_off/active_restart/active_run=cached`，`test/install/rollback/uninstall` 及实际执行的 `environment_gc=prompt`。当前 `approval_reviewer` 默认关闭；开启后只对符合风险上限的人工确认请求进行模型审核，失败回退既定策略。前端仍只需展示后端事件给出的最终模式。

## 1. Conversation Event Stream

所有实时前端消费同一种事件模型：

```json
{
  "protocol_version": 1,
  "type": "tool_end",
  "message": "工具 read success",
  "data": {}
}
```

- `protocol_version`：当前为 `1`。
- `type`：事件类型。
- `message`：给人看的摘要，可显示也可忽略。
- `data`：结构化字段，前端逻辑应主要依赖这里。
- `assistant_delta` / `thinking_delta` 是高频事件，只有 sink 设置 `wants_deltas=True` 才会收到。

后端源码契约在：

- `src/luna_agent/conversation/events.py`
- `EVENT_PROTOCOL_VERSION`
- `EVENT_SCHEMAS`
- `event_protocol_schema()`
- `ConversationEvent.as_dict()`

## 2. Event Types

### `turn_start`

一轮用户请求开始。

常见字段：

- `turn_id: string`
- `user_message: string`
- `message_count: integer`
- `was_compressed: boolean`
- `attachments_count: integer`
- `attachment_kinds: list[string]`
- `multimodal_diagnostics: object`

`multimodal_diagnostics` 常见字段：

- `enabled: boolean`
- `attachments_count: integer`
- `attachment_kinds: list[string]`
- `status_counts: object`
- `effective_modes: object`
- `reason_counts: object`
- `resolved_count: integer`
- `native_count: integer`
- `notice_count: integer`
- `failed_count: integer`
- `items: list[object]`

`multimodal_diagnostics.items[]` 是安全摘要，不包含图片 base64、完整 URL 或后端缓存路径。常见字段：

- `id: string`
- `kind: string`
- `name: string`
- `mime_type: string`
- `size: integer`
- `configured_mode: string`
- `effective_mode: string`
- `status: string`
- `reason: string`
- `has_notice: boolean`
- `resolved: boolean`
- `native: boolean`
- `has_local_path: boolean`
- `has_url: boolean`
- `has_platform_file_id: boolean`

说明：后端不会在事件或 transcript 中返回图片 base64。前端只需要展示附件数量、类型和降级/失败摘要；具体附件缓存路径属于后端内部实现。

### `compression`

历史消息被压缩。

常见字段：

- `trigger: "auto" | "mid_turn" | "overflow"`
- `pre_message_count: integer`
- `post_message_count: integer`
- `pre_tokens: integer`
- `post_tokens: integer`
- `summary_tokens: integer`
- `retained_user_tokens: integer`

压缩会先持久化为新的物理 Session checkpoint，再继续当前 turn。摘要采用完整交接格式，
后端不设置单独的摘要输出 token 上限。

### `steer_consumed`

运行中用户修正已被注入当前 turn 上下文。

常见字段：

- `count: integer`
- `steer_ids: list[string]`
- `text_preview: string`

语义：

- 该事件只表示 agent loop 已消费 `/steer` 入队内容，并把它作为新的 user message 追加到当前 turn。
- 用户刚发送 `/steer` 时，前端会先拿到 slash command 的文本回执；真正影响模型上下文时才会看到 `steer_consumed`。
- 如果修正在一次 LLM 最终答案返回时到达，后端会先保留那次 assistant 文本，再注入修正并继续下一次 LLM 调用，让模型按新要求重答。

### `llm_start`

一次模型请求开始。

常见字段：

- `api_calls: integer`
- `message_count: integer`
- `tool_count: integer`
- `provider: string`
- `model: string`
- `api_mode: string`
- `model_capability: object`
- `context_used_tokens: integer`
- `context_remaining_tokens: integer`
- `context_percent: number`
- `context_budget: object`

### `assistant_delta`

助手流式文本增量。仅发送给 `wants_deltas=True` 的 renderer。

必需字段：

- `chunk: string`

### `thinking_delta`

模型 reasoning / thinking 增量。仅发送给 `wants_deltas=True` 的 renderer。

必需字段：

- `chunk: string`

### `llm_end`

一次模型请求结束。

常见字段：

- `input_tokens: integer`
- `output_tokens: integer`
- `cache_hit_tokens: integer`
- `cache_miss_tokens: integer`
- `cache_write_tokens: integer`
- `cache_read_tokens: integer`
- `cache_hit_rate: number`
- `cache_diagnostics: object`
- `tool_call_count: integer`
- `finish_reason: string`
- `provider: string`
- `model: string`
- `api_mode: string`
- `model_capability: object`
- `context_window: integer`
- `context_used_tokens: integer`
- `context_remaining_tokens: integer`
- `context_percent: number`
- `context_budget: object`

字段语义：

- `input_tokens` / `output_tokens` 是 provider 返回的最近一次 API 调用实际消耗。
- `context_window` 是后端最终给 Agent 使用的有效上下文窗口，不一定等于模型硬上限。
- `model_capability` 提供 `model_context_limit`、`model_max_output_tokens`、`effective_context_window`、`effective_max_output_tokens`、`context_source`、`output_source`、`capability_source`、`capability_verified_at`、`context_clamped`、`output_clamped`、`api_mode` 和 `api_mode_source`。前端可将其用于诊断，不应自行重新推断模型限制。
- `context_used_tokens` / `context_remaining_tokens` / `context_percent` 是后端按当前请求体估算的上下文占用，前端 context meter 应优先使用这些字段。
- `context_budget` 是上下文估算明细，常见字段：
  - `system_prompt`
  - `history_messages`
  - `tools_schema`
  - `skills`
  - `memory_injections`
  - `mcp_tools`
  - `used`
  - `context_limit`
  - `remaining_context`
  - `percent`
  - `compression_threshold`
  - `over_compression_threshold`

`cache_diagnostics` 用于排查 provider prompt cache 命中率，当前常见字段：

- `cache_strategy: string`，`none` / `prefix` / `explicit`
- `system_hash: string`
- `tools_hash: string`
- `message_prefix_hash: string`
- `stable_prefix_hash: string`
- `dynamic_context_hash: string`
- `stable_block_count: integer`
- `dynamic_block_count: integer`
- `turn_tail_block_count: integer`
- `current_user_present: boolean`
- `source: string`
- `message_count: integer`
- `tool_count: integer`

### `assistant_message`

一段完整助手文本已定稿。正文在 `message` 字段，不在 `data`。

### `tool_start`

工具开始执行。

必需字段：

- `tool_name: string`
- `tool_use_id: string`

常见字段：

- `input_summary: string`

### `tool_decision`

工具执行前的 guard / permission 决策。前端做权限确认、工具 trace、审计展示时应优先读这个事件。

必需字段：

- `tool_name: string`
- `tool_use_id: string`

常见字段：

- `allowed: boolean`
- `stage: string`，如 `lookup` / `precheck` / `permission` / `runtime_guard` / `execution`
- `status: string`，如 `allowed` / `denied` / `error`
- `permission_category: string`，如 `write` / `bash` / `background` / `network`
- `execution_mode: string`，当前会话模式 ID，如 `read-only` / `ask-first` / `local-auto` / `full-auto`
- `permission_decision: string`，`allow` / `ask` / `deny`
- `reason_code: string`
- `required_allow: string`
- `decision_message: string`
- `grant_matched: string`
- `grant_scope: string` — 当前为 `cached` 或空字符串；前端不应再解释旧 `turn` / category grant
- `grant_expires_at: number` — 兼容持久化字段；精确授权过期时间以 `/permissions` 的 grant 条目为准
- `temporary_grant_ttl_seconds: integer`
- `tool_approval_mode: string`，`auto` / `cached` / `prompt` / `deny`
- `requested_resources: list[object]`，需要本次确认的最小资源集合；元素包含 `kind`、`resource`、`access`、`reason`
- `batch_items: list[object]`，同一模型响应中合并为一次确认的调用明细；普通单项决策为空列表
- `display_name: string`，给 UI 直接展示的工具名
- `execution_mode_label: string`，给 UI 直接展示的模式名，如 `Ask First`
- `risk_level: string`，`low` / `medium` / `high`
- `risk_summary: string`，给确认框展示的风险说明
- `default_action: string`，`allow` / `deny` / `none`
- `available_actions: list[string]`，如 `allow_once` / `allow_always` / `deny`
- `input_summary: string`，脱敏后的紧凑输入摘要
- `input_preview: string`，确认框优先展示的脱敏预览
- `affected_paths: list[string]`
- `command_preview: string`
- `url_preview: string`
- `host: string`

确认 UI 建议优先读：

- `display_name`
- `risk_level`
- `risk_summary`
- `default_action`
- `available_actions`
- `input_preview`
- `affected_paths`

### Bash / process_start 资源输入

`bash` 与 `process_start` 共享以下文件系统字段：

- `cwd: string`：进程工作目录，同时声明为 writable filesystem resource。
- `read_paths: list[string]`：额外现有文件/目录，只读挂载。
- `write_paths: list[string]`：额外现有文件/目录，可写挂载。

工具身份默认使用 `cached`；路径仍单独进入 `requested_resources[]`。前端确认框必须展示后端返回的精确路径与 access，不应把某个文件授权概括为整个目录授权。blocked path 会在确认前硬拒绝，不会出现在可批准列表。

`doctor --json` 的 `sandbox.process` 新增：

- `bash_effective_backend: "bwrap" | "legacy" | "unavailable"`
- `bash_filesystem_isolated: boolean`
- `bash_fail_closed: boolean`

这些字段用于区分 Bash 严格策略与 MCP stdio 的兼容后端；前端应按未知字段可忽略的方式消费。

### `tool_end`

工具结束、失败、拒绝或中断。

必需字段：

- `tool_name: string`
- `tool_use_id: string`

常见字段：

- `status: string`，`success` / `error` / `denied` / `timeout` / `interrupted` / `skipped`
- `category: string`
- `error: string`
- `duration: number`
- `input_summary: string`
- `output_summary: string`
- `full_output: string`
- `output_truncated: boolean`
- `artifact_count: integer`
- `artifacts: list[object]`。Runtime 管理的产物包含 `artifact_id`、`kind`、`filename`、`mime_type`、`size_bytes`、`source`、`delivery_eligible`；旧的纯内存结果仍只包含类型、编码大小和引用存在性。两者都不包含 base64、完整 URI 或本地路径
- `result_metadata: object`，MCP server、远端工具名和结构化内容存在性等安全元数据
- `count_as_tool: boolean`，透明路由包装器（当前为 `tool_call`）为 `false`；UI 统计实际工具次数时应排除，但仍可保留 trace
- `guard_stage: string`
- `guard_reason_code: string`
- `permission_category: string`
- `permission_decision: string`
- `required_allow: string`
- `execution_mode: string`
- `grant_matched: string`
- `tool_approval_mode: string`
- `requested_resources: list[object]`
- `display_name: string`
- `execution_mode_label: string`
- `risk_level: string`
- `risk_summary: string`
- `default_action: string`
- `available_actions: list[string]`
- `input_preview: string`
- `affected_paths: list[string]`
- `command_preview: string`
- `url_preview: string`
- `host: string`

### `artifact_available`

工具或 MCP 产物完成校验并进入 ArtifactStore。字段：

- `tool_name: string`
- `tool_use_id: string`
- `artifacts: list[object]`，只包含安全的 `StoredArtifactRef` 摘要

### `response_artifact_selected`

模型通过 `response_attach` 将当前 turn 的 Artifact 选入最终回复。字段：

- `artifact_ids: list[string]`
- `count: integer`

前端可以用这两个事件展示“产物可用”和“已附加”状态，但不能把 `artifact_id` 当成本地路径读取。

### `retry`

后端正在重试或要求模型恢复。

必需字段：

- `category: string`

常见字段：

- `attempt: integer`
- `max_attempts: integer`
- `error: string`
- `tool_name: string`
- `tool_names: string`

当同一轮内相同参数的工具调用已经成功执行 3 次，后端不再执行第 4 次请求，改为一次禁用工具的模型收尾调用。此时发送 `category="duplicate_tool_call"`、`attempt=1`、`max_attempts=1` 和对应 `tool_name`。该事件只表示运行时恢复；最终 `assistant_message` 是模型基于已有工具结果生成的正常回复，不包含重复调用诊断或原始结果转储。

当单轮工具调用总配额触发时，后端保留已有工具结果并进行一次禁用工具的模型收尾调用。此时发送 `category="tool_quota"`、`attempt=1`、`max_attempts=1`。前端可显示“正在整理结果”；该事件是可恢复状态，不应直接渲染为整轮失败。

### `stop`

当前 turn 被停止或中断。

### `error`

后端运行错误。

必需字段：

- `error: string`

### `turn_end`

一轮结束或会话保存完成。注意现在 agent loop 和 conversation service 都可能发 `turn_end`，字段会按阶段不同略有差异。

常见字段：

- `session_key: string`
- `status: string`
- `completed: boolean`
- `final_response: string`
- `api_calls: integer`
- `should_review_memory: boolean`
- `was_compressed: boolean`
- `context_overflow: boolean`
- `partial: boolean`，停止轮只持久化已确认完成的消息时为 `true`
- `messages_saved: integer`，本次停止轮实际保存的消息数

停止轮不会再统一退化成“用户消息 + 已停止”。后端会保留完整助手文本以及能够按 `tool_use.id` / `tool_result.tool_use_id` 配对的工具事务，丢弃没有结果的孤立工具调用。临时 memory/skill context 和强制收尾提示仍不进入 transcript。

## 3. Desktop Multimodal Input Reserved Interface

本节是给未来桌面端 / desktop-web 的预留接口说明。当前后端已经有内部结构化入口，但还没有正式 HTTP/WebSocket 外部服务；桌面端实现时应按这里的结构接入，不要走 CLI 文本输入模拟附件。

后端内部入口：

- `ConversationInput`
- `AttachmentRef`
- `SubmissionRequest`，携带 `session_key`、`ConversationInput` 和 `ResponseMode.RETURN_ONLY`
- `ConversationCoordinator.submit(request)`
- 事件返回仍然使用 `ConversationEvent.as_dict()`

桌面端推荐请求结构：

```json
{
  "session_key": "desktop:default:local",
  "source": {
    "platform": "desktop",
    "user_id": "local",
    "user_name": "Local User",
    "chat_id": "default",
    "chat_type": "dm"
  },
  "text": "帮我看看这张图",
  "attachments": [
    {
      "id": "local-1",
      "kind": "image",
      "name": "screenshot.png",
      "mime_type": "image/png",
      "size": 123456,
      "local_path": "/absolute/path/to/screenshot.png",
      "url": "",
      "platform_file_id": "",
      "metadata": {}
    }
  ]
}
```

`attachments[]` 字段语义：

- `id: string`：前端生成的临时 id，单条消息内稳定即可。
- `kind: string`：`image` / `audio` / `video` / `file`。
- `name: string`：展示文件名。
- `mime_type: string`：前端能判断就传；不能判断可留空，后端会尽量推断。
- `size: integer`：字节数；可用于前端提前提示过大文件。
- `local_path: string`：桌面端本机文件路径。后端会走 sandbox/path safety。
- `url: string`：远程附件 URL。后端会走 URL safety；桌面端本地文件优先用 `local_path`。
- `platform_file_id: string`：平台文件 id，桌面端通常不用。
- `metadata: object`：可放前端内部信息，但后端不依赖。

桌面端边界：

- 前端负责选文件、展示附件 chip、发送 `text + attachments`。
- 前端不负责判断 provider 是否支持图片。
- 前端不负责把图片转 base64。
- 前端不负责 OCR / ASR / 文件解析。
- 前端不直接调用 provider / transport。
- 后端根据 `multimodal.*` 配置和 provider 能力决定 `off` / `text` / `native` / `notice`。
- 后端根据 `attachments.*` 配置决定平台附件是否下载和缓存；provider 不参与下载决策。
- 文本类、PDF、docx 附件在 `text` 或 `auto -> text` 模式下会由后端抽取文本并加入本轮上下文。
- 文本抽取受 `multimodal.text_extract_max_chars` 和 `multimodal.text_extract_pdf_max_pages` 限制，超出会截断。
- 图片在 `text` fallback 模式下会进入统一图片文本化链路；配置 `multimodal.image_text_provider` 后可调用辅助 vision provider 生成文本描述。
- `multimodal.image_text_api_mode` 可控制图片文本化使用的 API 协议：`auto` / `chat_completions` / `anthropic_messages` / `responses` / `codex_responses`。Anthropic/DeepSeek 的 `auto` 会按 Anthropic Messages 请求，DeepSeek 官方根地址会规范化到 `/anthropic`，随后请求 `/anthropic/messages`；OpenAI-compatible 中转站应显式使用 `chat_completions`。Codex/Ahoo 这类 Responses 中转站建议显式使用 `codex_responses`，base URL 通常填根地址，后端会请求 `{base}/responses`。`codex_responses` 使用标准 Responses wire format，但额外强制 SSE、过滤中转站通道标记，并兼容其历史工具消息限制。
- 主 Agent 的 `LLM_API_MODE` 也支持 `responses` / `codex_responses`。使用 Codex/Ahoo 这类中转站时，推荐 `.env` 设为 `LLM_PROVIDER=openai`、`LLM_BASE_URL=https://api.ahooqq.cn`、`LLM_API_MODE=codex_responses`、`LLM_MODEL=<目标模型>`；`doctor` 会接受该配置。
- 主 Agent 的上下文窗口可通过 `.env` 的 `LLM_CONTEXT_WINDOW` 或 `config.yaml` 的 `llm.context_window` 显式配置；默认 `0` 表示自动解析。OpenAI provider 默认使用 256K 有效窗口，其他已知 provider 使用模型硬上限，未知模型使用 256K fallback；显式值不能超过已知硬上限。该值会影响 `llm_end.context_window`、`context_budget.context_limit`、`/usage` 和 turn report。
- vision fallback 的 API key / base URL 使用 `.env` 的 `IMAGE_TEXT_API_KEY` / `IMAGE_TEXT_BASE_URL`；前端不参与模型调用。
- 配置 `multimodal.ocr_endpoint` 后，后端可调用本地 OCR HTTP 服务；OCR 引擎不内置在主项目中。

桌面端事件消费：

- 发送后消费同一套 `ConversationEvent`。
- 附件处理状态优先读 `turn_start.data.multimodal_diagnostics`。
- 前端不应期待事件里出现图片 base64。
- provider 拒绝图片时，后端会自动纯文本重试一次，并通过 `retry.category == "multimodal_fallback"` 暴露。

CLI 说明：

- CLI 默认仍是纯文本输入。
- CLI 不建议开放图片 / 文件上传 UI。
- 用户在 CLI 里输入本机路径时，应让 agent 通过文件工具读取，而不是把 CLI 输入模拟成 attachment。

平台 adapter 当前保证：

- Telegram / Feishu / QQ / WeChat 会尽量把平台图片、音频、视频、文件解析为 `AttachmentRef`。
- `kind` 会统一到 `image` / `audio` / `video` / `file`。
- 能拿到的 `name` / `mime_type` / `size` / `url` / `platform_file_id` 会保留。
- 平台原始附件字段会放入 attachment metadata，供平台下载器复用。
- Gateway 授权通过且命令未被内部消费后，会调用来源 adapter 的附件准备方法。
- 如果配置允许，平台 adapter 会尝试把 `url` / `platform_file_id` 本地化到 `data/attachments/`。
- 本地化成功后，后端会把 `AttachmentRef.local_path` 更新为缓存文件路径。
- 本地化状态会写入 `AttachmentRef.metadata.attachment_resolve`。
- QQ adapter 已支持 OneBot 风格的 `get_image` / `get_record` / `get_file` / `get_group_file_url` 下载候选。
- WeChat adapter 已支持 iLink CDN 加密媒体下载，会使用 `aes_key` 解密后再进入缓存。
- WeChat adapter 会识别顶层或嵌套的 `encrypt_query_param` / `encrypted_query_param`，缺少 `aes_key` 时会稳定返回 `decrypt_key_unavailable`。

平台 adapter 当前不保证：

- 不保证 Feishu / Telegram 的 `platform_file_id` 已经实现真实下载器。
- 不保证所有 OneBot 实现都支持同一组文件下载 API。
- 不保证缺少微信 `cdn_url` / `encrypt_query_param` / `aes_key` 的媒体可以下载。
- 不保证下载失败的附件可以继续进入原生多模态处理。
- 不做 OCR / ASR / 视频抽帧。
- 不保证除文本类、PDF、docx 之外的文件可被文本抽取。
- 不负责判断 provider 是否支持原生多模态。

`metadata.attachment_resolve` 常见结构：

```json
{
  "status": "resolved",
  "reason": "cached",
  "sha256": "abc...",
  "local_path": "data/attachments/images/abc.png",
  "source_url": "",
  "size": 123456,
  "mime_type": "image/png"
}
```

失败或跳过时常见 `reason`：

- `mode_off`
- `multimodal_disabled`
- `resolve_inbound_disabled`
- `cache_disabled`
- `url_download_disabled`
- `platform_download_disabled`
- `platform_download_unavailable`
- `unsafe_url`
- `size_exceeded`
- `unsupported_file_type`
- `text_extract_unavailable`
- `text_extract_failed`
- `empty_description`
- `image_text_disabled`
- `image_text_describer_unavailable`
- `image_text_provider_not_supported`
- `image_text_failed`
- `image_text_empty`
- `ocr_endpoint_unavailable`
- `ocr_request_failed`
- `ocr_empty`
- `ocr_response_invalid`

可选本地 OCR 服务协议：

- `GET /health` 返回 `{"ok": true, "engine": "paddleocr"}`。
- `POST /ocr` 请求体为 `{"image_path": "...", "mime_type": "image/png", "language": "auto"}`。
- `POST /ocr` 成功返回 `{"ok": true, "text": "...", "confidence": 0.92, "blocks": [], "engine": "paddleocr"}`。
- OCR 服务由用户本地部署，后端只调用 HTTP 接口，不内置 OCR 引擎依赖。

### 出站 Artifact 与 Delivery

LLM 不返回结构化 JSON。工具/MCP 先产生 `ToolArtifact`，后端物化为当前 session/turn 的 `StoredArtifactRef`；模型需要把产物发给用户时调用 `response_attach({artifact_ids: [...]})`，随后照常返回最终文本。没有明确选择的产物不会自动发送。对于只返回 Markdown 本地文件链接的 stdio MCP，只有该 server 显式声明的受控 `artifact_roots` 内文件才会物化；前端仍只接收 `artifact_id` 和安全摘要，不接收 MCP 工作目录或 `file://` URI。通用 `resource`/`document` 会按可信 MIME 规范为 `image`、`audio`、`video` 或 `file`，前端和平台 adapter 应使用规范后的 `kind`。

普通工作区文件不会因 `write`、`edit` 或 `bash` 自动进入 ArtifactStore。新增核心工具 `artifact_from_file({path, filename?})`，在统一 filesystem read 权限与 sandbox 检查后复制文件内容，并通过现有 `tool_end.artifacts[]` / `artifact_available` 契约返回当前 turn 的 `artifact_id`。前端无需增加事件类型，也不能直接提交本地路径给 `response_attach`。

媒体 `MessagePart` 的稳定字段：

```json
{
  "type": "image",
  "artifact_id": "art_...",
  "name": "homepage.png",
  "mime_type": "image/png",
  "metadata": {"size_bytes": 58241}
}
```

Delivery 根据 `PlatformCapabilities` 生成 text/image/file/audio/video operation；不支持的媒体确定性降级为普通文件或明确的文字提示，绝不显示宿主本地路径。Outbox 持久化每个 operation 的状态，已成功 part 在重试和重启恢复后不会再次发送；不确定是否送达的 part 标记 `ambiguous`，不会盲目重试。

`PostDelivery` Hook payload 现包含：

- `partial: boolean`
- `degraded: boolean`
- `parts[]`: `index`、`kind`、`success`、`error`、`ambiguous`、`attempts`

`PreDeliveryOutcome.remove_artifacts(*artifact_ids)` 可以按稳定 ID 移除附件；后续 PreDelivery Hook 只会看到过滤后的 Artifact 摘要。Hook 不能注入文件路径或绕过 ArtifactStore。

平台当前出站能力：微信图片/视频/文件，Telegram 图片/文件/音频/视频，飞书图片/文件，QQ 图片/文件/音频/视频。QQ 出站媒体使用 `base64://` OneBot segment，不暴露宿主路径，也不要求 Windows NapCat 访问 WSL 文件系统。最终仍以 Adapter 的 `PlatformCapabilities` 为准。

## 4. Inline Tool Confirmation

前端可以在调用：

```python
runtime.run_message_events(text, event_sink=renderer, confirm=confirm_callback)
```

时传入：

```python
async def confirm_callback(decision) -> str:
    return "allow"  # or "deny" / "always"
```

后端语义：

- 只在 `permission_required + ask` 时调用 `confirm`。
- `"allow"`：本次临时放行，执行后撤销临时 grant。
- `"deny"`：不执行工具，返回 denied 工具结果。
- `"always"`：放行，并加入当前 agent/session 的限时精确授权；显示时长必须使用 `temporary_grant_ttl_seconds`，不能硬编码。
- `/stop` 中断 pending confirm 时，后端会取消等待并按固定 denied 结果收口：
  - `tool_end.status="denied"`
  - `tool_end.category="authorization"`
  - `tool_end.error="tool confirmation interrupted"`
- 需要 confirm 的工具不会并发确认；后端会串行化这些工具，避免前端单一确认框被覆盖。

前端确认框最少需要读：

- `decision.tool_name`
- `decision.display_name`
- `decision.permission_category`
- `decision.execution_mode_label`
- `decision.risk_summary`
- `decision.default_action`
- `decision.available_actions`
- `decision.input_preview`

## 5. Runtime Steer

运行中修正用于“模型还在处理上一条消息时，用户补一句方向修正”，例如：

```text
/steer 回答短一点，重点说结论
```

统一后端入口：

- Slash command：`/steer <text>`
- `CommandResult.kind`：默认 `text`
- 运行中成功回执：`已收到，会在当前任务下一步应用。（st_xxx）`
- 非运行中回执：`当前没有运行中的任务可修正。`
- 空参数回执：`用法: /steer <运行中修正内容>`

后端行为：

- `ConversationService` 每个 turn 分配 `turn_id`，并在 turn 生命周期内登记 active turn。
- `/steer` 会进入当前 session 的 `SteerManager` 队列，绑定当前 active `turn_id`。
- agent loop 在下一次循环边界消费队列，并追加一条 user message：
  - 第一行固定为 `[高优先级运行中用户指令]`
  - 后续会说明这些内容是用户在当前任务执行过程中追加的最新指令，优先级高于本轮较早请求。
  - 最后写入用户最新指令；多条会编号合并。
- 如果修正在 LLM 调用期间到达，最晚会在该次调用返回后、下一次模型调用前生效。
- turn 结束后未消费的 steer 会标记为 `expired`，不会污染下一轮。
- 单 session pending steer 默认最多 10 条；文本会限制长度，避免上下文污染。

Gateway / 平台行为：

- 微信/QQ/飞书/Telegram 等 gateway 平台在同 session 正在运行时，`/steer` 会像确认回复一样旁路 adapter 队列，不会被普通“上一条还在处理”队列挡住。
- `/stop` 仍然用于停止当前任务；`/steer` 只补充方向，不取消当前工具或模型调用。
- 普通非 slash 文本在 busy 时仍返回 `我正在处理你上一条消息，请稍候...`。

健康状态字段：

- `Gateway.health_snapshot().pending_steer_count: integer`
- `Gateway.health_snapshot().active_steer_sessions: list[string]`
- `Gateway.health_snapshot().steer: object`
- `Gateway.health_snapshot().running_agent_runs[].active_turn_id: string`
- `Gateway.health_snapshot().running_agent_runs[].pending_steers: integer`

`steer` session snapshot 常见形状：

```json
{
  "session_key": "telegram:c1:u1",
  "active_turn_id": "a1b2c3d4",
  "pending_count": 1,
  "pending_items": [
    {
      "id": "st_abc123",
      "turn_id": "a1b2c3d4",
      "status": "pending",
      "text_preview": "回答短一点"
    }
  ],
  "recent_items": []
}
```

前端接入建议：

- 运行中输入 `/steer <text>` 时，应走 slash command 通道，不要作为普通用户消息排队。
- 对桌面端/自定义 UI，可在当前 turn running 时提供一个“发送修正”输入框，底层仍调用 `/steer <text>`。
- 展示层可用 command 回执提示“已收到”，用 `steer_consumed` 或 health 中的 `pending_steers` 展示是否已应用。

## 6. Execution Mode

当前唯一用户入口是：

```text
/mode <mode>
```

用户可见四档：

- `Read Only`
- `Ask First`
- `Local Auto`
- `Full Auto`

稳定 mode ID 与 profile/policy 映射：

- `Read Only` -> `read-only` + `never`
- `Ask First` -> `read-only` + `on-request`
- `Local Auto` -> `workspace` + `on-request`；roots 内读写、普通网络和默认 cached 工具自动
- `Full Auto` -> `trusted` + `never`；默认 cached 工具自动，不提供交互扩权

切换 mode 会清空当前 session 的工具与资源 grants；重置/删除会话和服务重启也会清空。授权不会跨 session 或跨平台用户合并。前端可通过 runtime 的 `current_execution_mode()` 读取当前显示文案。

新安全上下文不接受 `/allow write` 这类类别级预授权。工具确认返回的授权由两部分组成：`tool_approval_mode` 控制工具身份是否需要确认，`requested_resources` 列出本次缺失的具体路径/host。允许一次只覆盖当前调用；限时允许使用全局 `permissions.grant_ttl_minutes`。

显式 `permissions.tool_approval.tools` / `mcp_servers` 覆盖始终优先。未显式覆盖时，`cached` 在 Ask First 中首次确认，在 Local Auto / Full Auto 中视为 `auto`。Local Auto 允许普通 network resource，但 Bash 独立网络开关、MCP URL 安全校验和插件 Hook 仍可收紧或阻止操作。

权限诊断中的 `security.tool_approval.cached_tools_auto` 表示当前 Mode 是否会把继承的默认 `cached` 解释为自动放行；前端应同时展示该字段与 `default_external`，不要只按静态配置推断实际行为。

新增命令：

- `/deny all`：撤销当前 session 的全部工具/资源限时授权。
- `/permissions` payload 提供 `security`、`tool_grants`、`resource_grants`、`temporary_grant_ttl_seconds` 和 `pending_confirmation`；旧类别 grant 字段已删除。
- `tool_decision` / `tool_end` 新增 `tool_approval_mode` 与 `requested_resources`，前端确认框应优先展示这些字段。
- 可选的 `approval_review` 事件表示模型审批器完成了一次审查。字段包括 `tool_name`、`tool_use_id`、`decision`（`allow_once` / `deny` / `ask_human`）、`reviewer_model`、`reviewer_latency_ms`、`reviewer_error` 和 `fallback`。该事件只在 `permissions.approval_reviewer.enabled=true` 且请求需要确认时出现；前端可将 `ask_human` 继续展示为普通确认，不应把模型判断视为绕过沙箱。

Gateway 异步确认：

- 微信/QQ/飞书/Telegram 等 gateway 平台遇到 `permission_required + ask` 时，会发送确认文本，不再立即 denied。
- 用户回复 `1` = 允许一次，`2` = 拒绝，`3` = 按 `permissions.grant_ttl_minutes` 限时允许。
- pending confirm 期间，非 `1/2/3` 普通文本会被消费并提示 `请回复 1、2 或 3；发送 /stop 可取消。`
- `/stop` 会取消 pending confirm，并让等待中的工具确认返回 `interrupted`。
- `Gateway.health_snapshot()` 新增 `pending_confirmations` 与 `pending_confirmation_count`；`/permissions` payload 的 `pending_confirmation` 会返回当前 session 的 pending 状态。

当一批工具调用全部因为新安全审批被拒绝时，后端会结束当前 turn，并提示用户在支持授权确认的入口重试，避免模型反复调用。对应 `tool_end` / Tool Runs / Turn Reports 仍记录真实 denied 结果。

## 7. Usage / Context Summary

`/usage` 返回人类可读文本，当前语义如下：

- `API 调用`、`输入 tokens`、`输出 tokens` 是当前 session 累计值，其中输入/输出来自 provider usage 报告。
- `上下文窗口 (估算)` 使用同一套 context budget 估算逻辑，展示当前历史、system prompt、tools schema、skill、memory 和 MCP tools 占用。
- `最近一轮工具执行` 是上一轮实际执行并记录到 agent runtime 的工具结果数量。
- `单轮工具上限` 是当前 agent 的工具调用上限配置。

注意：`最近一轮工具执行` 不等于活跃 turn 内部计数；前端如需结构化历史工具明细，应优先使用 Tool Runs / Turn Reports。

## 8. Tool Runs / Tool Truth

后端已持久化工具运行结果，供后续前端/desktop 查询使用。

当前能力：

- `Database.save_tool_runs(...)`
- `Database.recent_tool_runs(...)`
- `Database.get_tool_run(...)`
- `Database.tool_run_summary(...)`
- `SessionStore` 有对应代理。
- `ConversationService` 从 `tool_end` 事件自动记录 tool runs。
- runtime health / doctor 会显示 tool run 摘要。

`recent_tool_runs(...)` 当前支持按 `session_key` 和 `turn_id` 过滤，用于和持久化 turn report 关联。

后续如果前端需要 UI 查询接口，请先明确：

- 查询范围：当前 session / 最近全局 / 指定 turn。
- 分页参数。
- 是否需要 `full_output`。
- 是否需要按 `status` / `tool_name` / `permission_category` 过滤。

## 9. Turn Reports

后端会把每轮 `AgentTurnReport` 持久化到 SQLite，作为 turn 级审计记录。它记录一轮对话的整体状态、LLM/cache usage、工具调用汇总、retry、错误、tool truth 等信息。

当前能力：

- `Database.save_turn_report(...)`
- `Database.recent_turn_reports(limit=20, session_key=None, status=None)`
- `Database.get_turn_report(report_id)`
- `Database.turn_report_summary()`
- `SessionStore` 有对应代理。
- `ConversationService.recent_persisted_turn_reports(...)`
- `ConversationService.get_persisted_turn_report(...)`
- `ConversationService.tool_runs_for_turn_report(report_id)`
- `ConversationService.persisted_turn_report_summary()`

常见字段：

- `id: integer`
- `session_id: string`
- `session_key: string`
- `turn_id: string`
- `status: string`，`completed` / `failed` / `stopped` / `context_overflow`
- `completed: boolean`
- `duration: number`
- `error: string`
- `user_message_summary: string`
- `final_response_summary: string`
- `llm_calls: integer`
- `tool_calls: integer`
- `cache_hit_tokens: integer`
- `cache_miss_tokens: integer`
- `cache_write_tokens: integer`
- `cache_read_tokens: integer`
- `source: object`
- `report: object`，完整 `AgentTurnReport`
- `created_at: number`

停止轮的 `report.persistence` 常见字段：

- `partial: boolean`
- `messages_saved: integer`
- `tool_calls_saved: integer`
- `incomplete_tool_calls_dropped: integer`

完整 `report.llm` 除了 `input_tokens` / `output_tokens` / cache 字段外，也包含：

- `provider`
- `api_mode`
- `model_capability`
- `context_window`
- `context_used_tokens`
- `context_remaining_tokens`
- `context_percent`
- `context_budget`

完整 `report.steer` 记录本轮运行中修正摘要：

- `received: integer`
- `consumed: integer`
- `expired: integer`
- `pending: integer`
- `items: list[object]`

`items[]` 常见字段：

- `id: string`
- `session_key: string`
- `turn_id: string`
- `status: string`，`pending` / `consumed` / `expired`
- `text_preview: string`
- `created_at: number`
- `consumed_at: number`

关联语义：

- `turn_reports.turn_id` 与 `tool_runs.turn_id` 对齐。
- `session_key` 用于查询同一逻辑会话，包括发生压缩后的会话链。
- `session_id` 用于精确归属当前物理 session。
- `tool_runs_for_turn_report(report_id)` 会按 `session_key + turn_id` 返回该轮工具明细。

## 10. Runtime / Doctor Cache Diagnostics

`luna-agent doctor --section runtime --json` 的 `runtime.llm_cache` 会暴露 provider cache 能力和最近一次缓存 usage 摘要。

常见字段：

- `provider: string`
- `model: string`
- `strategy: string`，`none` / `prefix` / `explicit`
- `supports_usage: boolean`
- `usage_fields: object`
- `cacheable_blocks: list[string]`
- `last_usage: object`
- `last_diagnostics: object`
- `error: string`

`last_usage` 当前包含：

- `cache_hit_tokens`
- `cache_miss_tokens`
- `cache_write_tokens`
- `cache_read_tokens`
- `cache_hit_rate`

`last_diagnostics` 与 `llm_end.cache_diagnostics` 字段一致。

`luna-agent doctor --section runtime --json` 的 `runtime.turns.persisted` 会暴露持久化 turn report 摘要。

常见字段：

- `stored: integer`
- `last_id: integer`
- `last_turn_id: string`
- `last_session_key: string`
- `last_status: string`
- `last_error: string`
- `last_duration: number`
- `last_llm_calls: integer`
- `last_tool_calls: integer`
- `last_cache_hit_tokens: integer`
- `last_cache_miss_tokens: integer`
- `last_cache_write_tokens: integer`
- `last_cache_read_tokens: integer`

## 11. Activity Runtime Interface

后端已提供稳定 Activity 接口，用于前端展示“系统正在做什么”。Activity 覆盖：

- `sub_agent`：主 agent 委派的子任务。
- `background_process`：`process_start` 启动的后台进程。
- `gateway_agent`：gateway 平台消息触发的一次主 agent 处理流程。

入口：

- Slash command：`/activity [agents|processes|gateway] [id]`
- Command result：`CommandResult.kind == "activity"`，结构化数据在 `payload`。
- Runtime/query API：
  - `activity_snapshot(limit=20)`
  - `activity_detail(kind, id_)`
  - `activity_choices(provider, query="", limit=20)`
  - `slash_command_metadata()`
  - `slash_argument_choices(provider, command="", args=(), query="", limit=20)`

`/activity` overview payload：

```json
{
  "summary": {
    "has_active_work": true,
    "active_total": 3,
    "attention_required": false,
    "longest_running_seconds": 34.6,
    "counts": {
      "sub_agents": {"active": 1, "recent": 12, "failed_recent": 1, "stop_requested": 0},
      "background_processes": {"total": 2, "running": 1, "done": 1, "killed": 0},
      "gateway_agents": {"running": 1, "stop_requested": 0}
    }
  },
  "sub_agents": {"active_runs": [], "recent_runs": []},
  "background_processes": {"items": []},
  "gateway_agents": {"running_agent_runs": []}
}
```

列表 item 公共字段：

- `id: string`
- `kind: "sub_agent" | "background_process" | "gateway_agent"`
- `status: "running" | "completed" | "failed" | "stopped" | "stopping"`
- `started_at: string`
- `finished_at: string`
- `duration_seconds: number`
- `stop_requested: boolean`
- `error: string`
- `attention_required: boolean`

各类 item 还会提供前端常用字段：

- `sub_agent`：`run_id`, `role`, `task`, `task_preview`, `usage`, `quota`, `tool_counts`, `result_preview`。
- `background_process`：`pid`, `command`, `command_preview`, `cwd`, `returncode`, `has_stdout`, `has_stderr`, `stdout_bytes`, `stderr_bytes`, `output_preview`, `stdout_truncated`, `stderr_truncated`。
- `gateway_agent`：`session_key`, `platform`, `chat_id`, `user_id`, `active_turn_id`, `pending_steers`。

详情 payload：

```json
{"kind": "sub_agent", "id": "abc123", "run": {}}
{"kind": "background_process", "id": "3", "process": {}}
{"kind": "gateway_agent", "id": "telegram:c1:u1", "gateway_run": {}}
```

Slash metadata：

- `slash_command_metadata()` 中 `/activity` 声明 `result_kind="activity"`。
- `/activity` 的 `scope` 参数是 choice：`agents`, `processes`, `gateway`。
- `/activity agents [id]` 使用 dynamic provider `activity_agents`。
- `/activity processes [id]` 使用 dynamic provider `activity_processes`。
- `/activity gateway [id]` 使用 dynamic provider `activity_gateway`。

动态候选外形：

```json
{
  "value": "abc123",
  "label": "abc123",
  "description": "reviewer running",
  "append_space": false
}
```

## 12. Memory Runtime

Memory doctor/health payload 提供：

- `requested_provider: "luna" | "mem0" | "fallback" | "none"`
- `effective_provider: "luna" | "mem0" | "fallback" | "none"`
- `fallback_reason: string`
- `internal_revision: integer`
- `internal_profile: string`
- `buffer_pending: integer`
- `providers.internal.available: boolean`
- `providers.external.provider.available: boolean`
- `providers.external.last_primary_error: string`
- `providers.external.consecutive_failures: integer`
- `providers.external.last_probe_at: string`，ISO-8601 时间；尚未探测时为空。
- `providers.external.last_probe_status: "not_run" | "ok" | "error" | "skipped"`
- `scope: object`，当前诊断对应的 `user_id / session_key / profile / agent_id`。
- `migration.pending: integer`，当前 scope 待迁移 observation 数量。
- `migration.global_pending: integer`，所有 scope 待迁移 observation 数量。
- `migration.status_counts: object`
- `migration.global_status_counts: object`
- `index.pending: integer`，当前 scope 待写入 vector/keyword 派生索引的 memory 总数。
- `index.global_pending: integer`，所有 scope 待写入派生索引的 memory 总数。
- `index.status_counts: object`，按 `vector`、`keyword` 分组的状态计数。
- `index.global_status_counts: object`，所有 scope 的分组状态计数。
- `index.backends: object`，当前索引 Backend、fingerprint、generation 与更新时间。

Memory `search` / `list` 的每条外部记录额外提供：

- `provider: string`：兼容字段，表示该记录最初由哪个提供器写入。
- `source_provider: string`：明确的记录来源提供器，语义与兼容字段 `provider` 相同。
- `effective_provider: string`：当前 External Memory Router 实际使用的提供器。
- `target: "external"`

旧记录可以出现 `source_provider="fallback"` 且 `effective_provider="lumora"`；`lumora` 是改名前的持久化标识，读取时兼容为 `luna`，不表示当前路由已经降级。

Router 状态按 scope 隔离。幂等语义搜索遇到主提供器异常会重试一次；恢复时先真实 probe，成功后立即切回主提供器，不再用历史迁移阻塞前台请求。待迁移 observation 和待索引 memory 由现有 Memory Review worker 每次各处理一条，并逐条保存尝试次数、错误和完成状态。

`luna-agent memory doctor` 会执行真实 embedding + 外部向量存储探测，并在外部 provider 的 `components` 与 `fingerprints` 中报告 embedding、vector、keyword、fusion、reranker 状态；普通配置诊断仍只表示所选 Backend 的配置/依赖 readiness。

`luna-agent memory reindex --index all|vector|keyword` 会从 SQLite Archive 重建 Luna Agent 派生索引。Archive 始终是权威数据源，切换 embedding、vector 或 keyword Backend 不要求从旧索引服务导出数据。

Review worker payload 提供：

- `enabled: boolean`
- `workers: integer`
- `queue_size: integer`
- `submitted: integer`
- `completed: integer`
- `skipped: integer`
- `last_error: string`
- `maintenance_runs: integer`
- `migrations_completed: integer`
- `migrations_failed: integer`
- `indexes_completed: integer`
- `indexes_failed: integer`

`should_review_memory` 为兼容字段，不再用于触发 review；前端不要依据它调度任务。Review 由 AppRuntime worker 自动提交和持久化 checkpoint。

## 13. Plugin Diagnostics

`plugins list/info/doctor/validate --json` 的单插件诊断对象现在稳定提供：

- `management_schema_version: integer`，当前为 `1`
- `schema_version: integer`，当前为 `1`
- `source: "builtin" | "local" | "installed"`
- `kind: string`
- `tags: list[string]`
- `provides: list[string]`
- `registered: object`，各类能力计数
- `registered_items: object`，按 Tool、Skill、MCP Server、Hook 等分组的名称
- `runtime_state: "discovered" | "preparing" | "ready" | "active" | "draining" | "stopped" | "failed"`
- `runtime_backend: "in_process" | "worker"`，当前 generation 的执行后端
- `generation_id: string`
- `runtime_instance_id: string`
- `snapshot_revision: integer`
- `package_digest: string`
- `active_enabled: boolean`
- `active: object`，包含 `state`、`ready`、`started_at`、`ready_at`、`last_heartbeat`、`restart_count`、`quiescing`、`stop_requested`
- `active_error: string`
- `active_circuit_open: boolean`
- `active_resources: object`，主动 generation 声明的 Tool、MCP 和应用端口安全摘要
- `external_runtime: object`，外置 Worker 诊断；包含 `isolated`、`environment_id`、`environment_path`、`sandbox_backend`、`state`、`restart_count`、`failure_count`、`circuit_open`、`last_exit_at`、`next_retry_at`、`last_error` 和 `worker`
  - `worker` 常见字段为 `pid`、`running`、`returncode`、`last_error`、`stderr_tail`
- `worker_status: object`，Worker Supervisor 的安全摘要；字段为 `state`、`restart_count`、`failure_count`、`circuit_open`、`last_error`、`last_exit_at`、`next_retry_at`
- `active_status: object`，Active Supervisor 的安全摘要；字段为 `enabled`、`error`、`restart_count`、`failure_count`、`circuit_open`
- `boot_scope: object`，字段为 `sealed`、`pending_restart`、`capabilities`。Memory provider 在 boot scope seal 后不会被替换；deferred Platform 可在 Gateway 首次装配时加载，已有 Platform 路由之后不会被热重载替换。`pending_restart=true` 表示需要完整重启才能应用新绑定。
- 主动会话端口使用 SDK 的 `ActiveConversationIntent` 和 `ConversationStatus` 契约：
  - `ActiveConversationIntent`：`intent_id`、`session_key`、`kind`、`instruction`、`evidence`、`request_id`、`metadata`。
  - `ConversationStatus`：`session_key`、`busy`、`queued_count`、`last_user_at`、`last_assistant_at`、`recent_user_messages`（有界列表）。
  - `conversation.status(session_key)` 只读查询；`conversation.submit_intent(intent)` 进入 Coordinator，使用 `SubmissionOrigin.ACTIVE_PLUGIN`，不会创建伪造用户消息。
  - `intent_id`/`request_id` 应由插件稳定生成；相同 owner、origin 和请求身份在 SQLite Submission Ledger 中保持幂等。
- active runner 使用 `wait_for_wakeup(timeout)` 等待 timer、manual 或 internal 唤醒；`/plugins active <key> run` 只产生一次 manual 唤醒，不改变启用状态。
- `mcp: list[object]`，该插件注册的 MCP server 运行状态
- `installed_versions: list[object]`，字段为 `digest`、`version`、`source`、`path`、`active`、`status`
- `latest_operation: object`、`latest_event: object`

环境查询和 GC 返回同一报告结构：`dry_run`、`retained[]`、`removable[]`、`removed[]` 与 `bytes_reclaimable`。环境项包含 `plugin_key`、`environment_id`、`path`、`size_bytes`；保留项额外包含 `reasons[]`，常见值为 `installed_package`、`active_generation`、`active_process_lease`、`installed_manifest_unavailable` 或 `invalid_metadata`。前端必须把 `removable` 视为预览集合，只有 `removed` 表示本次实际删除。

插件配置位于 `plugins.config.<plugin-key>`。配置诊断会递归遮蔽键名中的 token、secret、password、authorization 和 api key；前端不应尝试从诊断 payload 恢复真实密钥。

`AppRuntime.health_snapshot()` 现在还提供 `hooks` 对象，供 doctor 或后端诊断页展示：

- `registered: integer`
- `owners: integer`
- `events: list[string]`
- `items: list[object]`

`items[]` 常见字段：`hook_id`、`owner`、`source`、`event_name`、`name`、`matcher`、`priority`、`timeout_seconds`、`execution_count`、`blocked_count`、`timeout_count`、`failure_count`、`last_duration_ms`、`last_error`。这些字段是诊断信息，不进入 Conversation Event Stream；前端应允许新增事件名和诊断字段。

`AppRuntime.health_snapshot().plugin_runtime` 提供热重载运行态：

- `current_revision: integer`
- `current_fingerprint: string`
- `current_bindings: integer`
- `active_leases: integer`
- `leases_by_revision: object`
- `retired_revisions: list[integer]`
- `runtime_counts: object`
- `install_revision: integer`
- `installed_packages: integer`
- `pending_removals: list[string]`
- `active_owner_running: boolean`
- `generation_coordinator: object`，字段为 `transition_count` 和 `last_transition`；后者包含 `plugin_key`、`runtime_instance_id`、`previous`、`current`、`reason`、`changed_at`
- `active_supervisor: object`，字段为 `owner_running`、`watch_task_count`
- `worker_supervisor: object`，字段为 `worker_count`、`running_count`、`recovery_task_count`、`environment_lease_count`、`stopping_count`
- `boot_scope: object`，字段为 `sealed`、`pending_restart_plugins`、`pending_restart_count`
- `active_plugins: list[object]`，稳定字段为 `key`、`enabled`、`state`、`ready`、`restart_count`、`circuit_open`、`error`

核心 slash command registry 提供 `/plugins` 及 `list/info/logs/versions/operations/operation/install/reload/enable/disable/active/rollback/uninstall` 子命令。`active` 用法为 `/plugins active <key> <on|off|restart|run>`；`run` 只发送一次 manual 唤醒。

- `logs <key>` payload：`management_schema_version`、`plugin_key`、`events[]`
- `versions <key>` payload：`management_schema_version`、`plugin_key`、`versions[]`
- `operations [key]` payload：`management_schema_version`、`plugin_key`、`operations[]`
- `operation <id>` payload：`management_schema_version`、`operation`

管理操作包含 `operation_id`、`action`、`stage`、`status`、`started_at`、`finished_at` 和安全错误摘要；同一个 plugin key 的修改操作串行执行。修改操作的回执 `kind="plugins"`，失败为 `kind="plugins_error"`；payload 包含 `operation_id`、`action`、`plugin_key`、`status`、`generation_id`、`runtime_instance_id`、`active_enabled`、`active` 和最新 `capabilities`。

## 14. MCP Runtime Diagnostics

`AppRuntime.health_snapshot()` 的 `mcp` 对象用于展示后台 MCP 就绪状态：

- `running: boolean`
- `configured_count: integer`
- `enabled_count: integer`
- `initializing: boolean`，至少一个已启用 server 尚未完成首次连接尝试
- `starting_count: integer`
- `connected_count: integer`
- `degraded_count: integer`，包含 degraded 和 reconnecting server
- `failed_count: integer`
- `total_tools: integer`
- `registered_tools: list[string]`
- `retired_runtime_count: integer`
- `servers: list[object]`

`servers[]` 提供 `runtime_instance_id`、`initial_attempt_done`、`initial_attempt_duration_seconds`、`last_shutdown_error` 和 `shutdown_timeout_count`。后两个字段只描述当前进程的关闭诊断，不表示下一轮连接失败。MCP 工具断线后仍可出现在 Tool Catalog，但 `available=false`，`unavailable_reason` 会说明 server 正在 starting、reconnecting、failed 或 stopped，并可附带最近错误。MCP 权威工具列表变化会发布 Capability Snapshot，Agent 在下一轮按投影 fingerprint 刷新；前端不要假定 `core_ready=true` 等于所有 MCP 已连接。

## 15. QQ Runtime Diagnostics

QQ/NapCat 平台在 Gateway health 的 `platforms[].adapter_health` 中额外提供：

- `ws_url: string`：当前 OneBot WebSocket Server 地址。
- `ws_connected: boolean`：WebSocket 入站通道是否实时连接。
- `action_transport: "http" | "websocket"`：当前 OneBot action 出站通道。
- `ws_reconnect_attempts: integer`：本进程 WebSocket 重连尝试次数。
- `pending_actions: integer`：等待 OneBot `echo` 响应的 action 数。
- `last_ws_event_at: string`：最后收到 WebSocket JSON 帧的本地 ISO 时间。
- `self_id: string`：最后事件报告的机器人 QQ 号。
- `companion: object`：NapCat 伴随进程状态。稳定字段包括 `mode`、`managed`、`owned`、`running`、`pid`、`starts`、`restarts`、`stop_on_shutdown`、`startup_timeout_seconds`、`last_started_at`、`last_exit_code`、`last_error` 和 `log_path`。

QQ 平台现要求 `.env` 配置 `QQ_BOT_WS_URL`。`QQ_BOT_BASE_URL` 为可选 HTTP action 通道；留空时后端通过 WebSocket 发送 action，不影响完整收发。

QQ 插件的 `plugins.config.platforms/qq.runtime.mode` 可为 `external` 或 `managed`。受管模式只执行用户明确配置的绝对可执行路径，不经过 shell；前端或诊断界面不应提供任意命令编辑与立即执行能力。

## 16. Codex Plugin Development Bridge

启用 `integrations/codex-bridge` 后，Codex Bridge 暴露以下 Agent 工具：

- `plugin_dev_create(plugin_id, description, brief?)`：在外部 development workspace 创建脚手架、插件简报和规范副本，并建立持久会话记录；重复调用同一 `plugin_id` 幂等返回已有会话。
- `plugin_dev_message(plugin_id, text)`：向该插件唯一 Codex Thread 提交一条消息；已有 Turn 时进入有界内存队列并立即返回 accepted/queued。
- `plugin_dev_list()`、`plugin_dev_status(plugin_id)`：查询会话、Thread、工作区和当前状态。
- `plugin_dev_events(plugin_id, limit=20, offset=0, order="desc", event_types?, detail="summary")`：分页查询持久事件。`limit` 最大 200；`order` 为 `asc|desc`；`detail` 为 `summary|full`。返回 `total`、`returned`、`has_more`、`next_offset` 和 `events[]`。
- `plugin_dev_cancel(plugin_id)`：中断当前 Turn，清理排队消息并标记会话 cancelled。
- `codex_approval_list(plugin_id?)`、`codex_approval_decide(request_id, decision)`：查看并允许一次或拒绝 App Server 请求；`decision` 为 `allow_once` 或 `deny`。

`plugin_dev_events` 的事件存储始终使用带时区的 UTC。返回给 Agent/前端的 `created_at` 是本机展示时区，`created_at_utc` 保留规范化 UTC 值；不要用展示字段进行跨时区排序或持久化。

同一插件发布 `plugin-development-workflow` Skill。前端或 Agent 可按名称发现并加载；加载只向当前模型上下文注入协作规则，不创建 Workspace、不启动 Codex Turn，也不改变会话状态。Skill 规定方案探索、方案讨论、最终方案、执行计划、用户授权、实现、审查、打包和安装的边界；底层仍统一使用上述通用工具。

Codex Bridge 只通过 `ActiveConversationIntent` 投递需要 Luna 处理的关键事件，不把事件写成普通用户输入。持久事件流仍包含 `turn_started`、`assistant_message`、`progress`、`request_user_input`、`approval_requested`、`turn_completed`、`error` 和 `process_restarted`；普通进度、Turn 开始及 `willRetry=true` 的自动重试不会创建 Conversation Turn。中间 `assistant_message` 会暂存，随 `turn_completed` 合并为一次最终通知；同一 Turn 的不可恢复错误和失败完成只通知一次。`active.sessions` 同时决定授权会话与通知目标；为空时只持久化事件，不主动唤醒会话。每个插件工作区保存一次只读 `LUNA_PLUGIN_DEVELOPMENT.md`，后续 Turn 不重复注入规范。

开发会话状态存储在插件私有 `development-sessions.json`，并公开最近一次有效 `model` 与 `model_provider`；Codex Thread 历史仍由 Codex `CODEX_HOME` 管理。App Server 使用 `config/read` 解析隔离配置，并在 `thread/start` / `thread/resume` 显式覆盖 model/provider；响应失配时失败关闭。重启只恢复 Thread，不静默恢复中断 Turn；未处理审批默认拒绝。旧 `mcp__codex__codex` MCP 与 Hook 注册继续兼容。

## 17. Compatibility Notes

- 前端不要依赖事件字段顺序。
- `message` 是给人看的摘要，机器逻辑优先读 `data`。
- 未列为必需的字段都应按可缺省处理。
- delta 事件不会被 `EventRecorder` 存储，但会转发给 opt-in renderer。
- 当前协议是 v1；破坏性字段变更必须提升 `protocol_version`。
