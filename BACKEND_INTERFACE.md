# Backend Interface Contract

更新时间：2026-07-06

本文给前端线使用，描述当前后端已经稳定提供的事件、命令和工具确认语义。后续 desktop/web/TUI 对接时优先看本文；历史需求文档已归档到 `docs/archive/`。

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

前端/桌面端可以通过稳定入口读取协议 schema：

```bash
personal-agent protocol schema --json
```

Python 层入口：

```python
from personal_agent.conversation import frontend_protocol_schema

schema = frontend_protocol_schema()
```

后端源码契约在：

- `src/personal_agent/conversation/events.py`
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

### `compression`

历史消息被压缩。

常见字段：

- `pre_message_count: integer`
- `post_message_count: integer`

### `llm_start`

一次模型请求开始。

常见字段：

- `api_calls: integer`
- `message_count: integer`
- `tool_count: integer`
- `model: string`

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
- `tool_call_count: integer`
- `finish_reason: string`
- `model: string`
- `context_window: integer`

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
- `execution_mode: string`，内部 profile，如 `standard` / `trusted`
- `permission_decision: string`，`allow` / `ask` / `deny`
- `reason_code: string`
- `required_allow: string`
- `decision_message: string`
- `grant_matched: string`
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
- `cwd: string`
- `timeout_seconds: number`
- `method: string`
- `process_label: string`

确认 UI 建议优先读：

- `display_name`
- `risk_level`
- `risk_summary`
- `default_action`
- `available_actions`
- `input_preview`
- `affected_paths`

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
- `guard_stage: string`
- `guard_reason_code: string`
- `permission_category: string`
- `permission_decision: string`
- `required_allow: string`
- `execution_mode: string`
- `grant_matched: string`
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
- `cwd: string`
- `timeout_seconds: number`
- `method: string`
- `process_label: string`

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
- `recoverable: boolean`

### `stop`

当前 turn 被停止或中断。

常见字段：

- `reason: string`，如 `user` / `interrupt` / `timeout` / `shutdown`
- `message: string`
- `stopped_tools: integer`
- `stopped_agents: integer`

### `error`

后端运行错误。

必需字段：

- `error: string`

常见字段：

- `category: string`，如 `llm` / `runtime` / `tool`
- `recoverable: boolean`
- `detail_id: string`

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

## 3. Inline Tool Confirmation

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
- `"always"`：放行，并加入当前 agent 的 `_destructive_allowed`，本轮后续同类工具不再询问。
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

## 4. Execution Mode

当前用户入口是：

```text
/mode
/mode list
/mode show
/mode set <mode>
```

用户可见四档：

- `Read Only`
- `Ask First`
- `Edit Freely`
- `Full Auto`

内部 profile 映射：

- `Read Only` -> `guarded`
- `Ask First` -> `standard`
- `Edit Freely` -> `trusted`
- `Full Auto` -> `sovereign`

兼容旧别名：

- `normal` -> `Ask First`
- `acceptEdits` -> `Edit Freely`
- `auto` -> `Full Auto`

切换 mode 会清空当前 agent 的临时 `/allow` grants，避免高权限残留。前端可通过 runtime 的 `current_execution_mode()` 读取当前显示文案。

## 5. Chat Slash Commands

CLI chat、inline TUI 和 Gateway 共享 slash command runtime。Typer CLI (`personal-agent ...`) 仍是独立入口，不和 slash command registry 合并。

当前 registry 版本：

```text
SLASH_COMMAND_REGISTRY_VERSION = 1
```

可发现入口：

- `/commands`
- `/commands json`
- `/commands <name>`
- `/help`

核心命令：

- `/new`
- `/session [current|list|switch <name>|rename <name>|delete [name]]`
- `/usage`
- `/export`
- `/allow [write|bash|background|network|destructive|all]`
- `/mode [list|show|set <mode>]`
- `/permissions [list|grants]`
- `/stop`
- `/agents [list [limit]|show <run_id>|clear]`
- `/memory [list|search <query>|show <id>|delete <id>|doctor]`
- `/tools [list|show <name>]`
- `/protocol [schema]`

插件命令仍使用插件系统自己的 command registry，并按 scope 暴露：

- `slash`
- `cli`
- `both`

`/commands` 和 `/help` 会按当前 runtime scope 合并展示可见插件命令。

### CommandResult v2

`handle_slash_command(...)` 现在返回兼容文本和结构化结果：

- `handled: bool`
- `response: str | None`：CLI/Gateway 的兼容展示文本。
- `continue_text: str | None`：技能命令继续进入普通 agent 消息。
- `payload: dict | None`：前端/TUI 可消费的结构化数据。
- `kind: str`：例如 `text`, `commands`, `tools`, `permissions`, `protocol`, `mode`, `command_error`。
- `error: str | None`
- `suggestions: list[str] | None`

已提供结构化 payload 的命令：

- `/commands`、`/commands json`、`/commands <name>`
- `/tools list`、`/tools show <name>`
- `/tool-runs recent`、`/tool-runs summary`、`/tool-runs show <id>`
- `/permissions list`、`/permissions grants`
- `/protocol`、`/protocol schema`
- `/mode list`、`/mode show`、`/mode set <mode>`

`/commands json` 的 command metadata 包含：

- `name`
- `summary`
- `usage`
- `category`
- `aliases`
- `available_in`
- `mutates_state`
- `requires_agent`
- `arguments`
- `children`

`arguments` metadata：

- `name`
- `kind`: `choice` 或 `dynamic`
- `choices`: 静态候选列表。
- `provider`: 动态候选 provider。
- `required`

候选项字段：

- `value`
- `label`
- `description`
- `append_space`

当前静态候选：

- `/mode set <mode>`: `Read Only`, `Ask First`, `Edit Freely`, `Full Auto`
- `/allow <category>`: `write`, `bash`, `background`, `network`, `destructive`, `all`

当前动态 provider：

- `tools`: 用于 `/tools show <name>`。
- `sessions`: 用于 `/session switch <name>` 和 `/session delete [name]`。

动态候选查询入口：

```python
await slash_argument_choices(
    runtime,
    provider,
    command="tools",
    args=("show",),
    query="rea",
    limit=20,
)
```

未知命令或子命令会尽量返回 `suggestions`。完全未知且可能是技能命令的输入仍保持原有 fallback，不强行拦截。

## 6. Tool Runs / Tool Truth

后端已持久化工具运行结果，并提供只读查询 facade 与 slash command 查询入口。

当前能力：

- `Database.save_tool_runs(...)`
- `Database.recent_tool_runs(...)`
- `Database.get_tool_run(...)`
- `Database.tool_run_summary(...)`
- `SessionStore` 有对应代理。
- `ConversationService` 从 `tool_end` 事件自动记录 tool runs。
- `ConversationQueryService` 提供只读查询 facade。
- runtime health / doctor 会显示 tool run 摘要。

`/tool-runs` slash command：

- `/tool-runs` 或 `/tool-runs recent`
- `/tool-runs recent --all --limit 20`
- `/tool-runs summary`
- `/tool-runs summary --all`
- `/tool-runs show <id>`

`/tool-runs` 返回 `CommandResult.kind="tool_runs"`，文本 `response` 给 CLI/Gateway 展示，结构化 `payload` 给前端/TUI 使用。

recent payload：

- `action`
- `scope`
- `session_key`
- `limit`
- `items`

summary payload：

- `inspected`
- `tool_counts`
- `status_counts`
- `category_counts`
- `denied`
- `failed`
- `timeouts`
- `truncated`

detail payload 的 `tool_run` 包含：

- `id`
- `session_id`
- `session_key`
- `turn_id`
- `tool_use_id`
- `tool_name`
- `status`
- `category`
- `duration`
- `input_summary`
- `output_summary`
- `full_output`
- `output_truncated`
- `error`
- `permission_category`
- `permission_decision`
- `execution_mode`
- `created_at`

后续如果前端需要更复杂 UI 查询，再补分页游标和按 `status` / `tool_name` / `permission_category` 过滤。

## 7. Compatibility Notes

- 前端不要依赖事件字段顺序。
- `message` 是给人看的摘要，机器逻辑优先读 `data`。
- 未列为必需的字段都应按可缺省处理。
- delta 事件不会被 `EventRecorder` 存储，但会转发给 opt-in renderer。
- 当前协议是 v1；破坏性字段变更必须提升 `protocol_version`。
