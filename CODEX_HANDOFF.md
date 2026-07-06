# Codex 交接记录

更新时间：2026-07-06 13:30 CST

## 当前状态

- 当前工作分支：`feature/frontend-tui-polish`
- 当前协作分工：
  - **后端线**：负责 agent loop、tool executor、permissions / execution mode、conversation events、runtime / doctor、gateway / platform adapter、后端测试与接口文档。
  - **前端线**：负责 `src/personal_agent/tui/`、classic CLI / future desktop-web 的布局、交互、视觉、真实终端验收。
- 前后端接口权威文档：`BACKEND_INTERFACE.md`。
- 前端对后端的小需求入口：`FRONTEND_INTERFACE_REQUIREMENTS.md`。
- 历史计划/需求文档已归档到 `docs/archive/`。

## 最近完成

### Event Protocol / Frontend Contract

- `ConversationEvent.as_dict()` 带 `protocol_version`。
- `EVENT_SCHEMAS` 覆盖所有事件类型。
- `frontend_protocol_schema()` 暴露 Python 层协议 schema。
- `personal-agent protocol schema --json` 暴露 CLI 层协议 schema。
- `retry` / `error` / `stop` 状态事件已结构化：
  - `retry`: `max_attempts`, `recoverable`
  - `error`: `category`, `recoverable`, `detail_id`
  - `stop`: `reason`, `message`, `stopped_tools`, `stopped_agents`

### Tool Decision / Tool End Metadata

- `tool_decision` 和 `tool_end` 已提供确认 UI 所需展示字段：
  - `display_name`
  - `execution_mode_label`
  - `risk_level`
  - `risk_summary`
  - `default_action`
  - `available_actions`
  - `input_summary`
  - `input_preview`
  - `affected_paths`
  - `command_preview`
  - `url_preview`
  - `host`
  - `cwd`
  - `timeout_seconds`
  - `method`
  - `process_label`
- `confirm(decision)` 对象与事件流字段保持一致。

### Tool Confirmation / Execution Mode

- `confirm=None` 已从 runtime 透传到 executor。
- executor 只在 `permission_required + ask` 时调用 confirm。
- confirm 返回语义：
  - `"allow"`：本次放行。
  - `"deny"`：拒绝本次工具。
  - `"always"`：加入当前 agent grant，后续同类工具不再询问。
- `/stop` 打断 pending confirm 时固定收口：
  - `tool_end.status="denied"`
  - `tool_end.category="authorization"`
  - `tool_end.error="tool confirmation interrupted"`
- 需要确认的工具会串行化，避免多个确认框并发覆盖。
- Execution Mode 四档：
  - `Read Only` -> `guarded`
  - `Ask First` -> `standard`
  - `Edit Freely` -> `trusted`
  - `Full Auto` -> `sovereign`

### Tool Truth / Tool Runs

- `AgentTurnReport` 汇总每轮 LLM、工具、retry、tool truth。
- `ConversationService` 记录最近 turn report 与 tool truth 摘要。
- `tool_runs` 已持久化，runtime / doctor 可看到摘要。
- 用户当前不急着推进 Tool Runs 查询 API，暂缓。

## 当前约定

- 后端接口变更必须同步 `BACKEND_INTERFACE.md`。
- 前端提出后端需求时，应尽量是字段/小接口级别，不直接要求重构后端流程。
- 前端视觉和 prompt_toolkit 真实终端问题归前端线。
- 后端线不主动编辑 TUI 文件，除非确认是事件/接口问题。
- `CLAUDE.md` 保留，不在本轮文档清理中处理。

## 已验证

最近后端提交前运行过：

```bash
python -m compileall -q src/personal_agent
uv run pytest -q
```

最近一次全量结果：`672 passed`。

## 注意事项

- 测试会修改 `src/personal_agent/skills/builtin/.usage.json`，提交前必须恢复。
- 当前分支仍混有前后端提交，提交时要精确暂存文件，避免把另一条线的改动混入。
- `PROVIDER_TRANSPORT_RETRY.md` 仍留在根目录，因为 `CLAUDE.md` 直接引用它，而本轮不处理 `CLAUDE.md`。
