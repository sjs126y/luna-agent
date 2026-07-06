# Backend Progress

更新时间：2026-07-07 00:45 CST

## 交接定位

这个文档只记录后端线进度，给后续接手后端的 Codex 使用。前端 TUI / desktop / prompt_toolkit 真实终端问题交给前端线处理；后端线只负责事件、接口、agent runtime、工具执行、权限、配置、平台适配、provider / transport 等基础能力。

当前工作分支：`feature/backend-provider-cache`

权威接口文档：

- `BACKEND_INTERFACE.md`：前端消费后端事件、slash commands、tool metadata、tool runs 等接口的主文档。
- `FRONTEND_INTERFACE_REQUIREMENTS.md`：前端提出的后端字段/接口需求入口。
- `CODEX_HANDOFF.md`：总交接文档，记录前后端分工和整体状态。

## 当前后端状态

后端主干能力已经比较完整，最近已完成并验证的方向包括：

- Execution Mode v3：四档模式已经稳定，对应权限、沙箱、工具类别和确认行为。
- Tool execution / permission pipeline：工具执行门控已经统一到 executor 路径，权限只负责自己的决策层，不再和其他阻断逻辑混在一起。
- Tool decision metadata：`tool_decision` / `tool_end` 已带前端确认 UI 所需字段，包括展示名、风险摘要、默认动作、可选动作、路径/命令/URL 预览等。
- Event protocol：事件有 `protocol_version`，`retry` / `error` / `stop` / `tool_decision` / `tool_end` 等事件结构化。
- Tool truth / turn report：`AgentTurnReport` 能记录工具真实调用、retry、错误、口头声称工具调用但实际未调用等信息。
- Tool runs：工具执行结果已持久化，并提供 `/tool-runs` 与 `ConversationQueryService` 查询。
- Turn reports：每轮 `AgentTurnReport` 已进入持久化审计链路，可和 tool runs 通过 `turn_id/session_key` 关联。
- Activity runtime：已提供统一结构化接口，覆盖子 agent、后台进程和 gateway agent，并支持 `/activity`、结构化 `CommandResult.kind="activity"`、runtime/query API、slash metadata 和动态候选。
- Usage / context：`llm_start` / `llm_end` 已区分“最近一次 API token 消耗”和“当前上下文占用估算”；`/usage` 已修正工具计数文案，避免把活跃 turn 内部计数显示成会话统计。
- Slash commands v2：chat / inline TUI / gateway 共用 slash command registry，`/commands`、`/tools`、`/permissions`、`/protocol`、`/mode` 等支持结构化 `CommandResult`。
- Doctor diagnostics：runtime health 已能展示 commands、query、execution、doctor 配置/运行时状态。
- Config registry：配置整理已进入可用状态，新增配置通过 registry/field 描述，不再散落硬编码。
- Platform adapter base：平台消息基类和 media attachment v1 已打底，但平台线暂时不要继续激进推进，避免牵动底层架构。

最近一次记录的全量测试结果：turn report 阶段 `668 passed`。

## 当前分工约定

- 后端 Codex 不主动改 `src/personal_agent/tui/`。
- 如用户明确要求修后端事件字段的本地 TUI/CLI 消费，可做最小兼容改动，并同步接口文档。
- 前端 Codex 如果需要字段或接口，应通过 `FRONTEND_INTERFACE_REQUIREMENTS.md` 明确写出小需求。
- 后端接口变更必须同步 `BACKEND_INTERFACE.md`。
- `CLAUDE.md` 不处理。
- 测试可能修改 `src/personal_agent/skills/builtin/.usage.json`，提交前要检查并恢复非意图改动。

## 已完成方向：Provider / Transport Cache

状态：v1/v2/v3 已完成并提交。

已完成：

- `ProviderProfile` 已增加 cache capability：`cache_strategy`, `supports_cache_usage`, `cache_usage_fields`, `cacheable_blocks`。
- `BaseTransport` 已增加 cache diagnostics、usage normalization、request hash 与 `LLMRequestPlan` 支持。
- Anthropic explicit cache 策略已优化：保留 stable system cache marker，不再默认标记最后一条动态 message。
- DeepSeek / OpenAI-compatible transport 保持 prefix-cache 友好布局，不添加非标准 cache 字段，并解析 provider cache usage。
- agent loop 已能向支持的 transport 传入 request plan。
- `llm_end`、runtime health、doctor、`BACKEND_INTERFACE.md` 已暴露 cache usage / diagnostics。

验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_transport_cache.py tests/test_agent_loop.py tests/test_cli.py tests/test_runtime.py -q
uv run pytest -q
```

结果：`68 passed`，全量 `667 passed`。

## 已完成方向：Turn Report 持久化

目标：把内存态 `AgentTurnReport` 升级为可长期查询的后端审计链路，并和已落库的 `tool_runs` 通过 `turn_id/session_key` 关联。

### 2026-07-06 v1/v2/v3 实施进度

状态：已完成实现并通过全量回归。

已完成：

- SQLite 新增 `turn_reports` 表，常用查询字段列化，完整报告保存在 `report_json`。
- `Database` / `SessionStore` 增加 turn report 保存、最近查询、详情查询、摘要查询。
- `ConversationService.run_turn_events(...)` 在 turn 结束后持久化 turn report；落库失败只记录日志，不影响用户对话。
- 保留现有内存 ring buffer 行为，新增持久化查询入口：
  - `recent_persisted_turn_reports(...)`
  - `get_persisted_turn_report(...)`
  - `tool_runs_for_turn_report(report_id)`
  - `persisted_turn_report_summary()`
- `tool_runs` 查询支持 `turn_id` 过滤，便于和 turn report 关联。
- runtime health / doctor 增加 `runtime.turns.persisted` 摘要。
- `BACKEND_INTERFACE.md` 已同步 Turn Reports 和 doctor 字段。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_database.py tests/test_conversation_service.py tests/test_runtime.py tests/test_cli.py -q
uv run pytest -q
```

结果：针对性 `76 passed`，全量 `668 passed`。

下一步：

- 如前端需要历史详情 UI，可基于 `ConversationService.recent_persisted_turn_reports(...)` 和 `tool_runs_for_turn_report(...)` 接入。

## 当前推进方向：Activity 稳定结构化接口

状态：v1/v2/v3 已完成并提交。

目标：给 inline TUI / future desktop-web 提供稳定 Activity 接口，用于查看当前系统正在运行的子 agent、后台进程和 gateway agent。

已完成：

- v1：新增 `personal_agent.activity` 聚合层，统一 summary/list/detail 结构；后台进程工具增加 `process_snapshot(...)`、`process_detail(...)`、`process_choices(...)`；runtime health 增加 `activity`。
- v2：新增 `/activity [agents|processes|gateway] [id]`；`CommandResult` 增加可选 `kind` / `payload`；`ConversationCommandRuntime` 提供 `activity_snapshot(...)`、`activity_detail(...)`、`activity_choices(...)`。
- v3：补齐前端便捷字段 `task_preview`、`command_preview`、`has_stdout`、`has_stderr`、`stdout_bytes`、`stderr_bytes`、`output_preview`；新增 `slash_command_metadata(...)` 和 `slash_argument_choices(...)`；activity 动态 provider 为 `activity_agents`、`activity_processes`、`activity_gateway`。
- `BACKEND_INTERFACE.md` 已同步 Activity payload、detail payload、slash metadata 和动态候选契约。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_activity.py tests/test_commands.py tests/test_conversation_command_runtime.py tests/test_cli_shell.py -q
```

结果：`49 passed`。

下一步：

- 跑全量回归，确认没有影响旧命令和 CLI/TUI。
- 前端接入后如果需要 activity 历史分页或 gateway 已完成记录，再扩展持久化层；当前 gateway detail 只覆盖运行中的 gateway agent。

## 已完成方向：Usage / Context 语义修正

状态：已完成实现并通过聚焦验证。

目标：修正前端 context meter 和 `/usage` 工具计数的语义错位。

已完成：

- `llm_start` / `llm_end` 新增 `context_used_tokens`、`context_remaining_tokens`、`context_percent`、`context_budget`。
- `AgentTurnReport.llm` 同步记录最新 context 估算，历史详情和实时事件语义一致。
- `input_tokens` / `output_tokens` 保持为 provider 最近一次 API 调用消耗，不再建议前端用它们作为当前上下文占用。
- CLI shell / TUI 状态栏优先使用 `context_used_tokens`，没有新字段时回退旧逻辑。
- `/usage` 将“本轮工具调用”改为“最近一轮工具执行”和“单轮工具上限”，避免常见 `0 / 20` 误导。
- `BACKEND_INTERFACE.md` 已同步 context 字段和 `/usage` 语义。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_event_protocol.py tests/test_agent_loop.py tests/test_commands.py tests/test_conversation_service.py tests/test_cli_shell.py tests/test_tui_renderer.py tests/test_tui_layout.py -q
uv run pytest -q
```

结果：聚焦 `108 passed`，全量 `684 passed`。

## 后续可评估方向

- 真实 provider cache API 验证：用实际 provider 响应确认 cache usage 字段与命中率。
- 上下文压缩质量：优化长对话压缩后的任务状态、路径、工具结果保留。
- 工具失败恢复策略：改进工具错误、权限拒绝、格式错误后的模型恢复提示。
