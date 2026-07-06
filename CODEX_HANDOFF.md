# Codex 交接记录

更新时间：2026-07-06 11:35 CST

## 当前状态

- 当前分支：`feature/backend-tool-truth`
- 当前 HEAD：`b381ce9 [codex] serialize confirmed tool prompts`
- 当前协作分工：
  - **后端线**：由本 Codex 继续负责，范围包括 agent loop、tool executor、permissions / execution mode、config、database、gateway、runtime diagnostics、后端测试与文档。
  - **前端线**：另一个 Codex 负责，范围包括 inline TUI / classic CLI UI / desktop/web UI 的布局、交互、视觉和真实终端验收。
- 当前结论：
  - Tool truth / tool result / turn report / execution mode / inline confirm 后端链路已经阶段性完成。
  - inline TUI 仍有真实终端体验问题，后续由前端线处理；后端线只在接口或事件契约需要时配合。

## 最近完成内容

### Tool Truth / Tool Runs

- `AgentTurnReport` 与 tool truth 汇总已接入单轮执行结果。
- `ConversationService` 记录最近 turn report 与 tool truth 摘要。
- 新增 tool run 结果持久化：
  - `tool_runs` 表。
  - `Database.save_tool_runs()` / `recent_tool_runs()` / `get_tool_run()` / `tool_run_summary()`。
  - `SessionStore` 代理查询。
  - `ConversationService` 从 `tool_end` 事件落库。
- runtime health / doctor 可看到 tool run 摘要。
- 相关提交：
  - `60f2663 [codex] add tool truth turn reporting`
  - `28bb5a6 [codex] expose tool truth summaries`
  - `303562f [codex] persist tool run results`

### Inline Tool Confirmation 后端链路

- `confirm=None` 已从前端 runtime 一路透传到 executor：
  - `CliChatRuntime.run_message_events`
  - `ConversationService.run_turn_events`
  - `agent.loop.run_conversation`
  - `tools.executor.execute_tool_calls`
  - `execute_tool_call_result`
- executor 只在 `permission_required + ask` 时调用 confirm。
- confirm 返回语义：
  - `"deny"`：拒绝工具。
  - `"allow"`：临时授权当前工具，执行后撤销 grant。
  - `"always"`：持久加入当前 agent 的 `_destructive_allowed`。
- hard precheck、unknown tool、runtime guard deny、已有 grant、policy allow 不会触发 confirm。
- 需求 4 已完成：pending confirm 被 `/stop` 中断时不会卡死，后端取消确认等待并返回 denied。
- 需要确认的 parallel-safe 工具现在会退出并发 batch，按顺序弹确认，避免前端单一 `_confirm_future` 被覆盖。
- 相关提交：
  - `91db4a9 [codex] wire inline tool confirmations`
  - `5957564 [codex] interrupt pending tool confirmations`
  - `b381ce9 [codex] serialize confirmed tool prompts`

### Execution Mode v3 + `/mode`

- execution mode 已从旧的 `/allow` preset 切到真正的 `ExecutionPolicy` profile。
- 用户可见模式：
  - `Read Only` -> `guarded`
  - `Ask First` -> `standard`
  - `Edit Freely` -> `trusted`
  - `Full Auto` -> `sovereign`
- `/mode` 支持短语、slug、旧别名：
  - `normal` -> `Ask First`
  - `acceptEdits` -> `Edit Freely`
  - `auto` -> `Full Auto`
- 切换 mode 时会清空 `_destructive_allowed`，避免高权限残留。
- `ConversationCommandRuntime.current_execution_mode()` 返回前端可显示的模式标签。
- 相关提交：
  - `ca11bb9 [codex] wire mode command to execution policy`

### Runtime / Diagnostics

- BootReport 与 AgentTurnReport 已阶段性完成：
  - 启动成功/失败路径结构化。
  - `AppRuntime.health_snapshot()` 暴露 boot / turns / tool_runs 摘要。
  - doctor runtime section 能显示 boot steps、turn summary、tool run summary。
- 这部分来自上一阶段主线，当前仍可用。
- 相关提交：
  - `f496791 [codex] add runtime boot report`
  - `f34bc25 [codex] report runtime boot failures`
  - `c70ee9b [codex] add agent turn reports`
  - `48d4777 [codex] expose recent turn reports`

## 前后端边界

### 后端线继续负责

- tool executor 行为、权限判定、sandbox / precheck、confirm 语义。
- execution mode profile、`/mode` 命令、permission grants。
- conversation events 的字段契约、tool result / turn report / tool runs 落库。
- gateway / platform adapter 的后端接口。
- config registry / runtime health / doctor。
- 后端需求文档与测试维护。

### 前端线继续负责

- `src/personal_agent/tui/` 的布局、颜色、输入框、快捷键、真实终端视觉验收。
- inline TUI 的 prompt_toolkit 细节，包括 TextArea prompt、height、scrollback 体验。
- classic CLI / future desktop/web 的渲染体验。
- 前端 roadmap 与 UI 截图验收。

### 联调规则

- 前端需要新增后端能力时，先写清：
  - 命令/API 名称。
  - 输入输出字段。
  - 事件类型与字段。
  - 错误/取消/权限边界。
- 后端实现后补测试并提交。
- 前端视觉问题不再由后端线直接改，除非确认是后端事件/状态数据错误。

## 当前已知前端问题

- inline TUI 仍存在真实终端布局细节争议：
  - 输入框与 hint/meter 是否足够贴近。
  - prompt_toolkit TextArea 高度、prompt 渲染和真实输入显示需要前端线继续打磨。
- 已知可用修复现状：
  - prompt 使用 native formatted-text tuple，不要用 `ANSI(...)`。
  - `dont_extend_height=True` 已用于避免输入区吞掉剩余高度。
  - 需要确认的工具后端已串行化，前端不应再遇到多个 confirm 同时覆盖 `_confirm_future` 的卡死。

## 已验证

最近一次全量验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest -q
```

结果：

```text
648 passed
```

## 注意事项

- 测试会修改 `src/personal_agent/skills/builtin/.usage.json`，提交前必须恢复。
- 当前分支混有 Claude 的前端 TUI 提交和 Codex 的后端提交；后续建议前后端分支拆开，减少互相覆盖。
- 后端 confirm 不做自动 timeout；目前只处理用户 deny/allow/always 与 `/stop` 中断。
- `tool_runs` 已持久化，但 turn report 仍主要是内存 ring buffer，不要默认当作长期审计存储。

## 后续建议

1. 后端线：
   - 事件协议 schema / version 固化。
   - tool decision / tool result / audit log 的统一关联。
   - 按前端需求补 gateway/desktop 所需接口。
2. 前端线：
   - 专门修 inline TUI 输入面板与真实终端体验。
   - 梳理 TUI 视觉规范，避免多模型反复改同一块布局。
   - 后续 desktop/web 前先固化事件消费接口。

## 最近提交

```text
b381ce9 [codex] serialize confirmed tool prompts
5957564 [codex] interrupt pending tool confirmations
546acd6 [claude code] fix vanishing TextArea input: native tuple prompt + revert weight=0 height
483dd13 [codex] fix inline tui prompt rendering
3589f1c [codex] compact inline tui input panel
ca11bb9 [codex] wire mode command to execution policy
91db4a9 [codex] wire inline tool confirmations
303562f [codex] persist tool run results
28bb5a6 [codex] expose tool truth summaries
60f2663 [codex] add tool truth turn reporting
```
