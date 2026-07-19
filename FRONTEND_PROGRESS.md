<div align="center">

<h1>Frontend Progress</h1>

<p><strong>Inline TUI 的交互、视觉与后端契约消费状态</strong></p>

<p>
  <img src="https://img.shields.io/badge/TUI-inline-0A84FF" alt="Inline TUI">
  <img src="https://img.shields.io/badge/security%20v4-merged-2EA44F" alt="Security v4 merged">
  <img src="https://img.shields.io/badge/updated-2026--07--19-555555" alt="Updated 2026-07-19">
</p>

<p>
  <a href="README.md">项目首页</a> ·
  <a href="docs/README.md">文档中心</a> ·
  <a href="BACKEND_INTERFACE.md">后端接口</a> ·
  <a href="FRONTEND_INTERFACE_REQUIREMENTS.md">前端需求</a>
</p>

</div>

---

本文给下一位前端 Codex 接手用，记录 inline TUI 当前进度、已接后端接口、用户偏好和下一步准备做但尚未开始的前端微调。后端接口权威文档仍以 `BACKEND_INTERFACE.md` 为准；前端给后端的需求仍写在 `FRONTEND_INTERFACE_REQUIREMENTS.md`。

## 当前主干同步与范围

- Security v4 前端实现 `b87c524` 已通过 `7b1afd0` 合并进 `main`；独立前端 worktree 仍保留 `feature/frontend-security-v4` 作为历史工作线，不代表主干尚未合并。
- 当前待合并分支：`refactor/luna-agent-rename`；运行时改名提交为 `7b798ca`，配置迁移为 `38c48c6`，文档迁移为 `f71a507`。
- 前端主要范围：`src/luna_agent/tui/`
- 相关测试：`tests/test_tui_app.py`、`tests/test_tui_layout.py`、`tests/test_tui_renderer.py`
- 视觉/交互记录：`docs/frontend_decisions.md`
- 前端只处理 CLI/TUI/desktop-web 侧内容；后端接口权威仍是 `BACKEND_INTERFACE.md`。

## 2026-07-19：Luna Agent 命名同步

- TUI 源码随宿主主包迁移到 `src/luna_agent/tui/`，内部 import 已全部切换为 `luna_agent.*`。
- 正式启动入口改为 `luna-agent chat`；旧 `personal-agent` 命令仍可用，但不再写入新文档和前端提示。
- Banner、Doctor、帮助文本和文档展示名统一为 `Luna Agent`。
- `ConversationEvent` 与前后端 payload 没有结构变化，协议版本保持 v1，不要求前端实现兼容分支。
- 当前完整后端回归为 `1171 passed, 1 warning`，唯一 warning 来自飞书 SDK。

## 已完成进度

### Inline TUI 主体

- 已有 inline TUI 输入区、状态行、上下文 meter、流式回复预览、工具运行活跃区。
- `luna-agent chat` 默认启动 inline TUI；classic `TerminalRenderer` 和 `--simple` 旧 REPL 已移除。
- 用户消息已增加底色/左侧强调，提高和助手输出的对比度；多行历史消息每行都会保持左侧蓝色强调条。
- 输入框已有低调背景和左侧提示符；多行输入/折行会保持同一左侧蓝色强调条；输入 `/` 时隐藏底部快捷键，并把命令区域放在输入框下方。
- 状态栏显示当前执行模式、模型、真正的 context usage，以及最近一轮模型 input/output token。
- 顶部 context meter 优先读 `llm_start` / `llm_end` 的 `context_used_tokens`、`context_window`、`context_percent`；不会再用 `input_tokens + output_tokens` 伪装成上下文占用。
- `input_tokens` / `output_tokens` 只作为最近一轮模型消耗显示，例如 `↓213 | ↑34`。
- cache usage 和 activity 不再放在顶部 meter line；它们保留为结构化状态/详情数据。

### Slash Command UI

- inline TUI 已消费后端 slash command registry。
- 一级命令、children、静态参数候选、动态参数候选都已经接入。
- 已支持连续选择，例如：

```text
/mode
-> /mode set
-> Ask First
-> /mode set Ask First
```

- 菜单打开时：
  - `Up/Down` 在菜单内移动选择。
  - `Enter` 只插入当前候选，不直接执行。
  - parent command 在第一层显示为 `/session ›`、`/mode ›`；按 `Enter` 进入下一层，再展示 `/session list` 或 `/mode set` 等子项。
  - 叶子命令仍在第一层直接展示；选中后下一次 `Enter` 执行。
  - 当命令完整且没有后续候选时，菜单收起；下一次 `Enter` 才执行命令。
- 已完成视觉细调：
  - 当前选中行有轻量 row highlight，不只依赖 `›` 标记。
  - 候选很多时 header 显示克制的位置提示，例如 `5/8`。
  - 无匹配候选时显示 `No matches`；动态候选加载中不会提前显示空结果。
  - `Esc` 在 confirm 中仍拒绝；在 slash 模式下关闭并清空命令输入；普通输入下清空输入。
- 已关闭 prompt_toolkit 内置补全浮层和 completer 输入改写，slash 菜单只走 inline TUI 自己的状态机，避免隐藏 completion state 抢 `Up/Down/Enter` 或改坏输入文本。

最近相关提交：

- `b3997af [codex] consume slash argument choices`
- `d07ec69 [codex] make slash menu keyboard selectable`

### Tool Results / Tool Runs

实时工具结果：

- 前端消费 `tool_start`、`tool_decision`、`tool_end` 事件。
- 优先读结构化字段，例如 `display_name`、`input_summary`、`output_summary`、`full_output`、`risk_level`、`command_preview`、`url_preview`、`affected_paths`。
- 实时工具 trace 和 `/tool-runs show` 共享摘要优先级：`command_preview`、`url_preview`、`affected_paths`、`process_label`、known JSON args、`input_preview` / `input_summary`。
- 工具摘要已改用短英文标签，例如 `Cmd`、`URL`、`Path`、`Process`、`Query`，减少 raw JSON 和突兀中文标签。
- 有 `full_output` / `error` / `output_summary` 时，会设置 `state.last_expandable`，用户可用 `Ctrl+O` 展开。
- `Ctrl+O` 展开标题已改为短标题，例如 `read #7`；展开块有轻量边界，避免和普通 assistant 输出混在一起。

历史工具查询：

- `/tool-runs`、`/tool-runs summary`、`/tool-runs show <id>` 已接入。
- 前端不直接调用 `ConversationQueryService`，而是消费 `CommandResult.kind == "tool_runs"` 的结构化 `payload`。
- `/tool-runs show <id>` 若有完整输出，也接到 `Ctrl+O` 展开；展示顺序已和实时 trace 的重点字段对齐。

### Activity Runtime UI

- 已合入后端 Activity Runtime 接口实现和 `BACKEND_INTERFACE.md` 第 8 节。
- `/activity` 已接入 `CommandResult.kind == "activity"` payload。
- TUI 会把 activity payload 渲染成结构化面板：
  - 顶部 summary：active 总数、attention、最长运行、Gateway / 子 agent / 后台进程数量。
  - `Gateway` 列表：session、platform、status、duration、stop/attention 状态、pending steer 数量。
  - `Sub agents` 列表：role、run id、status、duration、quota token、tool counts、task preview。
  - `Processes` 列表：pid/status/duration/command/cwd。
- `/activity processes <id>` 等详情 payload 会显示单个对象详情；进程详情里的 stdout/stderr、子 agent result 会接到 `Ctrl+O` 展开。
- Activity 不再占用顶部 meter line；运行情况只在 `/activity` 输出和详情里展示。
- Slash 菜单已加入 `/activity` 和二级项 `/activity agents|processes|gateway`，并支持后端动态 provider：`activity_agents`、`activity_processes`、`activity_gateway`。

### Confirm UI

- 前端已实现 `confirm_tool(decision)` 回调。
- 已消费后端确认字段：`display_name`、`execution_mode_label`、`risk_level`、`risk_summary`、`default_action`、`available_actions`、`input_preview`、`affected_paths`、`command_preview`、`url_preview` 等。
- Confirm 面板现在是可选择 action row：
  - `Up/Down` 在纵向动作列表中移动。
  - `Enter` 执行当前选中动作。
  - 快捷键保留为辅助：`a` / `y` allow once，`Esc` / `n` deny，`Shift+A` always。
- 面板已压缩成短标签风格：`Risk`、`Cmd`、`Path`、`URL`、`Process`、`Input`；动作区改为纵向编号列表，形如 `1> Allow once`、`2> Deny`、`3> Always 24h`。
- 仅展示 `available_actions` 允许的动作；`default_action` 只影响初始选中项和默认标记，`none` 时不标默认动作。
- `allow_always` 会按后端 `temporary_grant_ttl_seconds` 显示 TTL，例如 `Always 24h`。
- `1/2/3` 可直接选择对应 confirm action；`Enter` 执行当前 `›` 选中项，`Left/Right` 仍保留为兼容辅助移动。

## 已接后端能力

当前前端已消费：

- `CommandResult v2`: `kind`、`payload`、`continue_text`、`error`、`suggestions`
- Slash command metadata registry
- Slash argument metadata：`choice` / `dynamic`
- 动态参数入口：`slash_argument_choices(...)`
- 当前动态 provider：`tools`、`sessions`
- Activity 动态 provider：`activity_agents`、`activity_processes`、`activity_gateway`
- Tool Runs 查询：`/tool-runs ...` + `kind="tool_runs"` payload
- Activity Runtime 查询：`/activity ...` + `kind="activity"` payload
- Tool confirmation fields
- Runtime steer 事件：`steer_consumed`
- LLM context fields：`llm_start` / `llm_end` 的 `context_used_tokens`、`context_remaining_tokens`、`context_percent`、`context_budget`
- LLM turn usage fields：`input_tokens`、`output_tokens`
- LLM cache usage fields：`cache_hit_tokens`、`cache_miss_tokens`、`cache_write_tokens`、`cache_read_tokens`、`cache_hit_rate`（当前不在顶部常驻展示）
- `retry` / `stop` / `error` 增强字段
- Security v4：移除 `/allow`，只保留 `/deny all`，消费四档稳定 Mode、结构化
  `/permissions`、精确工具/资源授权和 `requested_resources`
- Doctor diagnostics 目前仅用于联调判断，TUI 未做 UI 消费；不是当前必做项。

## 用户偏好

- 不要过度拟人化工具 trace；工具行要简洁、信息化。
- 少用突兀中文标签，尤其是工具/状态类 UI；必要时用短英文标签。
- 确认框要清楚但不要啰嗦，重点展示风险、默认动作和关键预览。
- 长输出展开可以做，但完整输出打印在当前 scrollback 位置，不尝试回到截断处插入。
- 状态行中文化没必要。
- 顶部 meter 只放 context usage 和最近一轮模型 token；不要放 cache diagnostics 或 activity badge。
- 多工具结果列表 / Ctrl+O 选择展开：用户感兴趣，但之前尝试失败过，暂缓，不作为当前优先项。

## 最近完成

<details>
<summary><strong>展开按日期记录的前端完成历史</strong></summary>

### 2026-07-18 主干接口同步

- Conversation Runtime 已在后端统一 CLI/TUI、Gateway、Cron 和插件 submit；TUI 仍通过既有 event/command 接口工作，不需要接触 Adapter、Coordinator 或 Delivery 内部对象。
- 后端新增 `artifact_available`、`response_artifact_selected`、`ConversationTurnResult.outbound_message` 和 Delivery 状态契约。当前 TUI 可继续消费通用工具事件，但尚未实现附件缩略图、Artifact 列表或 multipart Delivery 专用面板。
- 新增 `artifact_from_file` 与 `response_attach` 属于 Agent 工具链，不要求前端传递本地路径；前端只能展示安全的 `artifact_id`、kind、filename、MIME 和 size 摘要。
- 主干全量回归为 `1050 passed, 1 warning`。本节只同步接口状态，没有宣称新增前端 UI 已完成。

### 2026-07-14 Security v4 前端适配

- Slash 菜单和测试不再依赖已删除的 `/allow`；`/deny all` 是当前 session
  工具/资源限时授权的清理入口。
- `CommandResult.kind="mode"` 会直接使用 `payload.current.label` 同步状态栏，Mode
  固定为 `Read Only`、`Ask First`、`Local Auto`、`Full Auto`。
- `/permissions` 已消费 `security`、`tool_grants`、`resource_grants`、
  `temporary_grant_ttl_seconds` 和 `pending_confirmation`，以紧凑结构化列表展示。
- Confirm UI 已消费 `tool_approval_mode` 和 `requested_resources`；存在精确资源时，
  优先展示资源的 kind/access/resource，不再依赖旧类别授权说明。
- Always 动作的 TTL 继续完全来自后端 `temporary_grant_ttl_seconds`。

阶段提交：

- `bc79e5f Adapt TUI mode handling to security v4`
- `365613d Render security v4 permissions in TUI`

已验证：

```bash
uv run pytest tests/test_tui_app.py tests/test_tui_layout.py tests/test_tui_renderer.py -q
uv run pytest tests/test_tui_app.py tests/test_tui_layout.py tests/test_tui_renderer.py tests/test_commands.py tests/test_cli_chat.py tests/test_gateway_commands.py -q
PYTHONPYCACHEPREFIX=/tmp/luna-agent-frontend-pycache python -m compileall -q src/luna_agent/tui
git diff --check
```

结果：TUI `101 passed`；前端与 slash command 集成回归 `167 passed`；语法与 diff 检查通过。

### 2026-07-08 02:20 CST

- 接入后端 Runtime Steer 消费事件：`steer_consumed` 会在 scrollback 打印轻量 `steer applied` 提示，表示修正已真正注入当前 turn。
- 修复运行中无法发送 `/steer <text>` 的问题：普通输入仍会保留草稿，只有 `/steer` 会在 turn running 时走 slash command 通道提交。
- `llm_start` 现在会提前刷新 model/context meter；`input_tokens` / `output_tokens` 仍只由 `llm_end` 更新，保持“最近一轮实际模型消耗”的语义。
- `/activity` gateway overview 会显示 `steer N`；gateway detail 会显示 `active_turn_id` 和 `pending_steers`。
- 调整 confirm 面板视觉：去掉背景色块和横向 action row，改为无大面积底色的纵向编号列表；`Up/Down` 是主要移动方式，`Enter` 执行当前选中项，`1/2/3` 可直接选择；`allow_always` 文案按后端临时授权 TTL 显示。

已验证：

```bash
uv run pytest tests/test_tui_app.py tests/test_tui_layout.py tests/test_tui_renderer.py -q
PYTHONPYCACHEPREFIX=/tmp/luna-agent-pycache python -m compileall -q src/luna_agent/tui
git diff --check
```

结果：`92 passed`。普通 `python -m compileall -q src/luna_agent/tui` 在当前 worktree 会因为已有 `__pycache__` 写入报 `Read-only file system`，已用 `PYTHONPYCACHEPREFIX=/tmp/luna-agent-pycache` 完成等价语法验证。

### 2026-07-07 10:55 CST

- 收敛聊天入口：inline TUI 成为唯一正式交互终端 UI。
- 删除 classic `TerminalRenderer`、`CliShell` 和对应测试；删除 `--simple` 旧 REPL。
- 保留 `luna-agent chat --once` 作为脚本/单轮入口。
- 全量验证结果：`708 passed`。

### 2026-07-06 20:40 CST

- 完成 slash 菜单视觉细调：row highlight、候选位置提示、`No matches` 空状态、`Esc` 关闭 slash 菜单。
- 完成 tool trace / `/tool-runs show` 展示精简：统一摘要优先级、短英文标签、短展开标题、展开块边界。
- 完成 confirm 面板压缩：短标签、可用动作过滤、默认动作显式化、去掉冗余元信息。

### 2026-07-06 20:55 CST

- 修复真实终端里 slash completion 可能改坏输入的问题：inline TUI 不再把 prompt_toolkit completer 挂到输入框，`Enter` 不再读取隐藏 completion state。
- 明确 parent command 层级行为：第一层显示 `/session ›`、`/mode ›`，按 `Enter` 后进入子命令菜单；不会把 `/session list` 等全部摊到第一层。
- 增加回归测试覆盖 `/session` 进入子菜单、`/mode set` 完整文本保留，避免 `/mode` 被改成 `/mde` 这类问题回归。

### 2026-07-06 22:32 CST

- 将 confirm 面板从“快捷键提示”升级为可左右选择的 action row，更接近 CC/Codex 的决策控件。
- `Enter` 现在执行当前选中的 confirm action；`Left/Right` 移动选择；快捷键继续作为辅助操作。
- Confirm action 根据后端 `available_actions` 和 `default_action` 构建，只显示可用动作，并对默认项加轻量标记。

### 2026-07-06 22:41 CST（历史记录，当前已调整）

- 读取后端 worktree 的 `BACKEND_INTERFACE.md`，接入新增 `llm_end` cache usage 字段。
- 当时状态栏 context meter 后曾低调显示 cache 摘要，例如 `cache 42% r12.3k w800`；2026-07-07 已按用户偏好移除顶部常驻 cache summary。
- 暂不把 turn reports / doctor cache diagnostics 做成普通 TUI UI，它们更适合后续明确查询入口后再接。

### 2026-07-06 22:51 CST

- 修复多行用户消息和多行输入的左侧强调条：历史消息每行都保持蓝色左条，当前输入的续行/折行也保持同一左条。

### 2026-07-06 22:58 CST

- 在 `FRONTEND_INTERFACE_REQUIREMENTS.md` 记录 Activity Summary / Detail 结构化接口需求，供后端 Codex 跨 worktree 读取。
- 前端预期消费 `/activity` 的 `CommandResult.kind="activity"` payload，用于状态栏 `activity N` badge、`/activity` scrollback 列表和各类详情展示。

### 2026-07-06 23:02 CST

- 将 Activity 需求改为长期稳定接口契约：明确 `/activity [agents|processes|gateway] [id]`、summary/list/detail payload、公共 item 字段、动态候选 provider 和前端展示假设。

### 2026-07-06 23:48 CST

- 合入后端 `feature/backend-provider-cache` 的 Activity Runtime / LLM cache / turn report persistence 接口实现。
- 前端 TUI 消费 `/activity` 结构化 payload，新增 activity 总览、三类列表、详情输出和 `Ctrl+O` 展开。
- Slash registry 增加 `/activity` 二级命令和动态 id provider，`/a` 下现在会出现 `/activity`。
- Runtime health snapshot 同时保留前端 command/query/execution 诊断，并加入后端 `activity`、`llm_cache` 和 persisted turn report summary。

### 2026-07-07 00:56 CST

- 按后端最新语义修正顶部 meter：context meter 只读 `llm_end.context_used_tokens/context_window/context_percent`。
- 最近一轮模型消耗独立显示为 `↓<input> | ↑<output>`。
- 移除顶部常驻 cache summary 和 activity badge，避免把诊断/运行状态混进 context usage。
- `UIState` 保留 `context_budget`，后续若做 `/usage` 结构化 UI 或 context breakdown 可直接消费。

已验证：

```bash
uv run pytest tests/test_tui_app.py tests/test_tui_layout.py tests/test_tui_renderer.py -q
python -m compileall -q src/luna_agent/tui
git diff --check
uv run pytest tests/test_commands.py tests/test_cli_chat.py tests/test_gateway_commands.py -q
uv run pytest tests/test_commands.py tests/test_runtime.py tests/test_activity.py tests/test_conversation_command_runtime.py tests/test_cli.py -q
uv run pytest tests/test_agent_loop.py tests/test_event_protocol.py tests/test_tui_layout.py tests/test_tui_renderer.py tests/test_tui_app.py -q
```

结果：TUI tests `85 passed`；activity/runtime/commands/CLI tests `62 passed`；context/meter/event tests `107 passed`。

### 2026-07-08 15:20 CST

- 重新补回 followup TUI 改动，并按用户要求本次完成后立即提交。
- 启动时输出一次 Luna Agent ASCII banner 到普通 scrollback，不进入 active region，不改 delta/streaming renderer 链路。
- Ctrl+C 行为改为：运行中请求 stop；confirm 中拒绝；有输入时清空；空闲空输入时第一次显示短暂提示，第二次退出。Ctrl+D 不再作为空行退出入口。
- 运行中允许发送只读/控制 slash 命令：`/steer`、`/activity ...`、`/tool-runs ...`、`/usage`、`/agents`、`/session`，结果打印到 scrollback，不启动第二个模型 turn。
- 完整 slash 命令优先提交，避免 `/activity agents` 被动态 id 候选自动补成某个历史 run id。
- confirm 面板继续使用纵向编号动作，并截断长 input/command/url preview，避免长 JSON 把选项挤出可视区域。
- 工具运行/完成行增加间距，工具名加粗突出，提升连续工具调用的可读性。

已验证：

```bash
uv run pytest tests/test_tui_app.py tests/test_tui_layout.py tests/test_tui_renderer.py -q
```

结果：`98 passed`。

### 2026-07-08 15:34 CST

- 修复 Ctrl+C 二次退出提示不可见的问题：`UIState.has_active_region()` 现在会把 `Press Ctrl+C again to exit`、`cleared`、`stop requested` 这些短状态算作 active region 内容。
- 空闲空输入第一次 Ctrl+C 会在输入框上方短暂显示 `Press Ctrl+C again to exit`，第二次 Ctrl+C 才退出。

已验证：

```bash
uv run pytest tests/test_tui_app.py tests/test_tui_layout.py tests/test_tui_renderer.py -q
python -m compileall -q src/luna_agent/tui
git diff --check
```

结果：`98 passed`。

</details>

## 不建议现在做

- 不建议现在做完整 tool result browser。
- 不建议现在做 full-screen 子 agent 切换 UI，除非后端先提供结构化 `agent_runs` payload。
- 不建议把 doctor diagnostics 做成普通用户 UI；它更适合联调和排错。

## 验证建议

前端改动后至少跑：

```bash
uv run pytest tests/test_tui_app.py tests/test_tui_layout.py tests/test_tui_renderer.py -q
python -m compileall -q src/luna_agent/tui
git diff --check
```

如果改到 slash command registry 消费或 command output 格式化，可补跑：

```bash
uv run pytest tests/test_commands.py tests/test_cli_chat.py tests/test_gateway_commands.py -q
```

真实终端还需要手测：

- `/`、`/memory`、`/mode`、`/mode set` 的上下键和 `Enter` 行为。
- 小窗口下输入框、slash 菜单、meter 是否挤压。
- `Ctrl+C`、`Esc`、`Enter` 在普通输入、slash 菜单、confirm 面板三种状态下是否冲突。
- `/tool-runs`、`/tool-runs show <id>` 和 `Ctrl+O` 展开体验。
- `/activity`、`/activity agents`、`/activity processes`、`/activity gateway`，以及对应详情命令。

## 提交注意

- 用户要求“自己改的文件自己提交”。
- 提交时精确暂存前端文件，避免带入后端线或用户未提交文件。
- 当前常用提交风格：`[codex] ...`
