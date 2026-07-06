# Frontend Progress

更新时间：2026-07-06 17:05 CST

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
- 状态栏显示当前执行模式、模型和 context usage。

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
  - 当命令完整且没有后续候选时，菜单收起；下一次 `Enter` 才执行命令。
- 已关闭 prompt_toolkit 内置补全浮层，避免隐藏 completion state 抢 `Up/Down/Enter`。

最近相关提交：

- `b3997af [codex] consume slash argument choices`
- `d07ec69 [codex] make slash menu keyboard selectable`

### Tool Results / Tool Runs

实时工具结果：

- 前端消费 `tool_start`、`tool_decision`、`tool_end` 事件。
- 优先读结构化字段，例如 `display_name`、`input_summary`、`output_summary`、`full_output`、`risk_level`、`command_preview`、`url_preview`、`affected_paths`。
- 有 `full_output` / `error` / `output_summary` 时，会设置 `state.last_expandable`，用户可用 `Ctrl+O` 展开。

历史工具查询：

- `/tool-runs`、`/tool-runs summary`、`/tool-runs show <id>` 已接入。
- 前端不直接调用 `ConversationQueryService`，而是消费 `CommandResult.kind == "tool_runs"` 的结构化 `payload`。
- `/tool-runs show <id>` 若有完整输出，也接到 `Ctrl+O` 展开。

### Confirm UI

- 前端已实现 `confirm_tool(decision)` 回调。
- 已消费后端确认字段：`display_name`、`execution_mode_label`、`risk_level`、`risk_summary`、`default_action`、`available_actions`、`input_preview`、`affected_paths`、`command_preview`、`url_preview` 等。
- 键位：
  - `Enter` 根据 `default_action` 执行默认动作。
  - `Esc` / `n` 拒绝。
  - `a` allow once。
  - `Shift+A` allow always。

## 已接后端能力

当前前端已消费：

- `CommandResult v2`: `kind`、`payload`、`continue_text`、`error`、`suggestions`
- Slash command metadata registry
- Slash argument metadata：`choice` / `dynamic`
- 动态参数入口：`slash_argument_choices(...)`
- 当前动态 provider：`tools`、`sessions`
- Tool Runs 查询：`/tool-runs ...` + `kind="tool_runs"` payload
- Tool confirmation fields
- `retry` / `stop` / `error` 增强字段
- Doctor diagnostics 目前仅用于联调判断，TUI 未做 UI 消费；不是当前必做项。

## 用户偏好

- 不要过度拟人化工具 trace；工具行要简洁、信息化。
- 少用突兀中文标签，尤其是工具/状态类 UI；必要时用短英文标签。
- 确认框要清楚但不要啰嗦，重点展示风险、默认动作和关键预览。
- 长输出展开可以做，但完整输出打印在当前 scrollback 位置，不尝试回到截断处插入。
- 状态行中文化没必要。
- 多工具结果列表 / Ctrl+O 选择展开：用户感兴趣，但之前尝试失败过，暂缓，不作为当前优先项。

## 准备做但尚未开始

用户刚同意 1、2、3 可以一起做，但随后要求先写本文档，因此以下事项尚未实现。下一位 Codex 接手前应先确认用户是否仍要继续这些微调。

### 1. Slash 菜单视觉细调

目标：在现有可选择菜单基础上做更清楚、更克制的视觉层级。

可做项：

- 当前选中行增加轻量 row highlight，而不只是 `›` 标记。
- `Esc` 在 slash 菜单打开时关闭/清空菜单状态，避免和确认框冲突。
- 候选很多时增加轻量位置提示，例如 `2/5` 或 `more`，但不要做得太重。
- 空结果可以显示简短 `No matches`，避免用户误判 UI 没响应。

### 2. Tool trace / Tool Runs 展示精简

目标：实时工具 trace 和 `/tool-runs show` 的展示更一致、更少 raw 感。

可做项：

- 统一实时 tool trace 与 `/tool-runs show` 的字段顺序。
- 对常见工具做更好的摘要优先级：`command_preview`、`url_preview`、`affected_paths`、`process_label` 优先于 raw JSON。
- `Ctrl+O` 展开标题改得更短，例如 `read #7`、`web_search #12`。
- 长输出展开块增加轻量边界，避免和普通 assistant 输出混在一起。

### 3. Confirm 面板压缩

目标：安全确认更像产品 UI，减少冗余文案。

可做项：

- 改成短标签风格：`Risk`、`Cmd`、`Path`、`URL`、`Enter`、`Esc`。
- 风险行、详情行、动作行分层更清楚。
- `available_actions` 不包含的动作不显示。
- `default_action` 更明显，例如 `Enter allow once` / `Enter deny`。
- 保持面板高度克制，避免挤压输入区。

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
