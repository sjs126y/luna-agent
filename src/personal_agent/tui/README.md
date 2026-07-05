# Inline TUI (CC/Codex-style)

CC/Codex 风格的行内滚动渲染器。设计与分阶段计划见仓库根目录 `TUI_PLAN.md`。

## Phase 0 结论 (已验证)

技术路线 **prompt_toolkit 独占终端** 已在真实终端验证通过,spike 脚本见
`scripts/spike_inline.py`。有效的 API 组合:

- `Application(full_screen=False, mouse_support=False)` —— 非 alternate-screen,
  让终端保留 scrollback、原生滚轮/选择可用。
- 底部活跃区 = `HSplit([ConditionalContainer(活跃区), 状态栏, 输入区])`。
  - 活跃区用 `Window(FormattedTextControl(callable), wrap_lines=True)`,
    高度动态;空闲时用 `ConditionalContainer(filter=Condition(...))` 收起到 0 行。
  - 流式内容累加进 state,`app.invalidate()` 触发原地重绘。
- 定稿内容(用户消息、完成的回复、工具 trace)用 `run_in_terminal(lambda: print(...))`
  打印到 Application **上方**,进入正常 scrollback。
- `ConditionalContainer` 的 `filter` 必须是 `Condition(lambda: ...)`,不能直接传 lambda。

验证过的 4 点(真实终端):
1. 输入框始终钉底(满屏时因分隔线上偏一行,预期内)。
2. 流式回复在活跃区原地重绘,不逐行下滚。
3. 定稿回复进 scrollback,滚轮可上翻查看历史。
4. Ctrl+C 中断流式;Ctrl+D 空行退出,无全屏残留,记录留在 scrollback。

## 备选方案 (未采用,留档)
若上述在某些终端失效,退路是 `patch_stdout` 模式或 `print_formatted_text` +
最小 Application。当前无需使用。
