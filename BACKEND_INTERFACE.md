# Backend Interface Contract

更新时间：2026-07-07

本文给前端线使用，描述当前后端已经稳定提供的事件、命令和工具确认语义。后续 desktop/web/TUI 对接时优先看本文；更详细的历史背景见 `CODEX_HANDOFF.md` 和 `BACKEND_REQUIREMENTS.md`。

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
- `attachments_count: integer`
- `attachment_kinds: list[string]`
- `multimodal_diagnostics: object`

`multimodal_diagnostics` 常见字段：

- `enabled: boolean`
- `attachments_count: integer`
- `attachment_kinds: list[string]`
- `status_counts: object`
- `effective_modes: object`
- `resolved_count: integer`
- `native_count: integer`
- `notice_count: integer`
- `failed_count: integer`

说明：后端不会在事件或 transcript 中返回图片 base64。前端只需要展示附件数量、类型和降级/失败摘要；具体附件缓存路径属于后端内部实现。

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
- `model: string`
- `context_window: integer`
- `context_used_tokens: integer`
- `context_remaining_tokens: integer`
- `context_percent: number`
- `context_budget: object`

字段语义：

- `input_tokens` / `output_tokens` 是 provider 返回的最近一次 API 调用实际消耗。
- `context_window` 是模型上下文窗口大小。
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

### `retry`

后端正在重试或要求模型恢复。

必需字段：

- `category: string`

常见字段：

- `attempt: integer`
- `error: string`
- `tool_name: string`
- `tool_names: string`

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

当前唯一用户入口是：

```text
/mode <mode>
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

## 5. Usage / Context Summary

`/usage` 返回人类可读文本，当前语义如下：

- `API 调用`、`输入 tokens`、`输出 tokens` 是当前 session 累计值，其中输入/输出来自 provider usage 报告。
- `上下文窗口 (估算)` 使用同一套 context budget 估算逻辑，展示当前历史、system prompt、tools schema、skill、memory 和 MCP tools 占用。
- `最近一轮工具执行` 是上一轮实际执行并记录到 agent runtime 的工具结果数量。
- `单轮工具上限` 是当前 agent 的工具调用上限配置。

注意：`最近一轮工具执行` 不等于活跃 turn 内部计数；前端如需结构化历史工具明细，应优先使用 Tool Runs / Turn Reports。

## 6. Tool Runs / Tool Truth

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

## 7. Turn Reports

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

完整 `report.llm` 除了 `input_tokens` / `output_tokens` / cache 字段外，也包含：

- `context_window`
- `context_used_tokens`
- `context_remaining_tokens`
- `context_percent`
- `context_budget`

关联语义：

- `turn_reports.turn_id` 与 `tool_runs.turn_id` 对齐。
- `session_key` 用于查询同一逻辑会话，包括发生压缩后的会话链。
- `session_id` 用于精确归属当前物理 session。
- `tool_runs_for_turn_report(report_id)` 会按 `session_key + turn_id` 返回该轮工具明细。

## 8. Runtime / Doctor Cache Diagnostics

`personal-agent doctor --section runtime --json` 的 `runtime.llm_cache` 会暴露 provider cache 能力和最近一次缓存 usage 摘要。

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

`personal-agent doctor --section runtime --json` 的 `runtime.turns.persisted` 会暴露持久化 turn report 摘要。

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

## 9. Activity Runtime Interface

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
- `gateway_agent`：`session_key`, `platform`, `chat_id`, `user_id`。

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

## 10. Compatibility Notes

- 前端不要依赖事件字段顺序。
- `message` 是给人看的摘要，机器逻辑优先读 `data`。
- 未列为必需的字段都应按可缺省处理。
- delta 事件不会被 `EventRecorder` 存储，但会转发给 opt-in renderer。
- 当前协议是 v1；破坏性字段变更必须提升 `protocol_version`。
