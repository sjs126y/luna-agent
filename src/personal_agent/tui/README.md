<div align="center">

<h1>Inline TUI</h1>

<p><strong>保留终端 Scrollback 的事件驱动交互界面</strong></p>

<p>
  <img src="https://img.shields.io/badge/prompt--toolkit-inline-0A84FF" alt="prompt-toolkit inline">
  <img src="https://img.shields.io/badge/streaming-supported-2EA44F" alt="Streaming supported">
  <img src="https://img.shields.io/badge/confirm-interactive-7C3AED" alt="Interactive confirmation">
</p>

<p>
  <a href="../../../README.md">项目首页</a> ·
  <a href="../../../docs/README.md">文档中心</a> ·
  <a href="../../../FRONTEND_PROGRESS.md">前端进度</a>
</p>

</div>

---

CC/Codex 风格的行内滚动渲染器。当前后端事件契约见根目录 `BACKEND_INTERFACE.md`，前端进度见 `FRONTEND_PROGRESS.md`。

## 当前状态

- `personal-agent chat` 默认启用 inline TUI；`--ui inline` 可显式指定。
- `InlineTuiApp` 负责 prompt_toolkit 应用、输入框、快捷键、历史、命令补全和打印队列。
- `InlineRenderer` 只消费事件并更新 `UIState`；定稿内容通过 app 的 `print_above` 回调进入 scrollback。
- 快捷键：`Enter` 发送、`Ctrl+J` 换行、`Ctrl+O` 展开最近长输出、`Ctrl+C` 停止/清空/二次退出、`Shift+Tab` 循环执行模式。
- slash 命令仍走 runtime 后端命令入口；TUI 不重新实现命令逻辑。
- inline tool confirmation 已接入后端 `confirm=` 回调，app 会传入 `confirm_tool` 并消费结构化确认字段。

## 前端协作边界

- 本包可独立打磨布局、颜色、输入行为、快捷键和真实终端体验。
- 不在 TUI 层改变 conversation event 字段、权限语义、slash 命令语义或工具执行策略。
- 需要后端新增能力时，先在交接文档写清命令/API、事件字段、错误/取消边界。

## 技术边界

技术路线使用 **prompt_toolkit 独占终端**。生产实现采用以下 API 组合：

- `Application(full_screen=False, mouse_support=False)` —— 非 alternate-screen,
  让终端保留 scrollback、原生滚轮/选择可用。
- 底部活跃区 = `HSplit([ConditionalContainer(活跃区), 状态栏, 输入区])`。
  - 活跃区用 `Window(FormattedTextControl(callable), wrap_lines=True)`,
    高度动态;空闲时用 `ConditionalContainer(filter=Condition(...))` 收起到 0 行。
  - 流式内容累加进 state,`app.invalidate()` 触发原地重绘。
- 定稿内容(用户消息、完成的回复、工具 trace)用 `run_in_terminal(lambda: print(...))`
  打印到 Application **上方**,进入正常 scrollback。
- `ConditionalContainer` 的 `filter` 必须是 `Condition(lambda: ...)`,不能直接传 lambda。

当前稳定行为：
1. 输入框始终钉底(满屏时因分隔线上偏一行,预期内)。
2. 流式回复在活跃区原地重绘,不逐行下滚。
3. 定稿回复进 scrollback,滚轮可上翻查看历史。
4. Ctrl+C 中断流式;空闲时连续两次 Ctrl+C 退出,无全屏残留,记录留在 scrollback。

## 备选方案 (未采用,留档)
若上述在某些终端失效,退路是 `patch_stdout` 模式或 `print_formatted_text` +
最小 Application。当前无需使用。
