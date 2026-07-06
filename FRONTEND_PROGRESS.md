# Frontend Progress

更新时间：2026-07-06 22:41 CST

本文给下一位前端 Codex 接手用，记录 inline TUI 当前进度、已接后端接口、用户偏好和下一步准备做但尚未开始的前端微调。后端接口权威文档仍以 `BACKEND_INTERFACE.md` 为准；前端给后端的需求仍写在 `FRONTEND_INTERFACE_REQUIREMENTS.md`。

## 当前分支与范围

- 当前分支：`feature/frontend-tui-polish`
- 前端主要范围：`src/personal_agent/tui/`
- 相关测试：`tests/test_tui_app.py`、`tests/test_tui_layout.py`、`tests/test_tui_renderer.py`
- 视觉/交互记录：`docs/frontend_decisions.md`
- 注意：当前工作区可能存在后端线文件改动，例如 `CODEX_HANDOFF.md`、`BACKEND_PROGRESS.md`。前端 Codex 不要误提交这些文件，除非用户明确要求。

## 已完成进度

### Inline TUI 主体

- 已有 inline TUI 输入区、状态行、上下文 meter、流式回复预览、工具运行活跃区。
- 用户消息已增加底色/左侧强调，提高和助手输出的对比度。
- 输入框已有低调背景和左侧提示符；输入 `/` 时隐藏底部快捷键，并把命令区域放在输入框下方。
- 状态栏显示当前执行模式、模型、context usage 和后端 `llm_end` cache usage 摘要。

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

### Confirm UI

- 前端已实现 `confirm_tool(decision)` 回调。
- 已消费后端确认字段：`display_name`、`execution_mode_label`、`risk_level`、`risk_summary`、`default_action`、`available_actions`、`input_preview`、`affected_paths`、`command_preview`、`url_preview` 等。
- Confirm 面板现在是可选择 action row：
  - `Left/Right` 在动作间移动。
  - `Enter` 执行当前选中动作。
  - 快捷键保留为辅助：`a` / `y` allow once，`Esc` / `n` deny，`Shift+A` always。
- 面板已压缩成短标签风格：`Risk`、`Cmd`、`Path`、`URL`、`Process`、`Input`。
- 仅展示 `available_actions` 允许的动作；`default_action` 只影响初始选中项和默认标记，`none` 时不标默认动作。

## 已接后端能力

当前前端已消费：

- `CommandResult v2`: `kind`、`payload`、`continue_text`、`error`、`suggestions`
- Slash command metadata registry
- Slash argument metadata：`choice` / `dynamic`
- 动态参数入口：`slash_argument_choices(...)`
- 当前动态 provider：`tools`、`sessions`
- Tool Runs 查询：`/tool-runs ...` + `kind="tool_runs"` payload
- Tool confirmation fields
- LLM cache usage fields：`cache_hit_tokens`、`cache_miss_tokens`、`cache_write_tokens`、`cache_read_tokens`、`cache_hit_rate`
- `retry` / `stop` / `error` 增强字段
- Doctor diagnostics 目前仅用于联调判断，TUI 未做 UI 消费；不是当前必做项。

## 用户偏好

- 不要过度拟人化工具 trace；工具行要简洁、信息化。
- 少用突兀中文标签，尤其是工具/状态类 UI；必要时用短英文标签。
- 确认框要清楚但不要啰嗦，重点展示风险、默认动作和关键预览。
- 长输出展开可以做，但完整输出打印在当前 scrollback 位置，不尝试回到截断处插入。
- 状态行中文化没必要。
- 多工具结果列表 / Ctrl+O 选择展开：用户感兴趣，但之前尝试失败过，暂缓，不作为当前优先项。

## 最近完成

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

### 2026-07-06 22:41 CST

- 读取后端 worktree 的 `BACKEND_INTERFACE.md`，接入新增 `llm_end` cache usage 字段。
- 状态栏 context meter 后会低调显示 cache 摘要，例如 `cache 42% r12.3k w800`；没有 cache 字段时不显示。
- 暂不把 turn reports / doctor cache diagnostics 做成普通 TUI UI，它们更适合后续明确查询入口后再接。

已验证：

```bash
uv run pytest tests/test_tui_app.py tests/test_tui_layout.py tests/test_tui_renderer.py -q
python -m compileall -q src/personal_agent/tui
git diff --check
uv run pytest tests/test_commands.py tests/test_cli_chat.py tests/test_gateway_commands.py -q
```

结果：TUI tests `79 passed`，command/gateway tests `55 passed`。

## 不建议现在做

- 不建议现在做完整 tool result browser。
- 不建议现在做 full-screen 子 agent 切换 UI，除非后端先提供结构化 `agent_runs` payload。
- 不建议把 doctor diagnostics 做成普通用户 UI；它更适合联调和排错。

## 验证建议

前端改动后至少跑：

```bash
uv run pytest tests/test_tui_app.py tests/test_tui_layout.py tests/test_tui_renderer.py -q
python -m compileall -q src/personal_agent/tui
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

## 提交注意

- 用户要求“自己改的文件自己提交”。
- 提交时精确暂存前端文件，避免带入后端线或用户未提交文件。
- 当前常用提交风格：`[codex] ...`
