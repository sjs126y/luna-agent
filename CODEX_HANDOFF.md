# Codex 交接记录

更新时间：2026-07-03 19:00 CST

## 当前状态

- 当前分支：`feature/cli-frontend-refactor`（从 `codex/terminal-cli` 派生）
- 这一轮把终端前端从「反复修边线/光标」的自定义 `prompt_toolkit.Application` 方案，重构成基于 `PromptSession` 的标准姿势，并补齐了一批 UX
- 最终目标不变：可长期使用的 CLI 版 Agent UI，风格接近 Hermes / Claude Code / Codex CLI
- 事件流（`conversation/events.py`）和渲染层（`cli_shell.py`）保持分离，方便以后抽给 desktop

## 本轮完成的内容（feature/cli-frontend-refactor）

### 架构

- `llm_end` 事件现在自带 `model` / `context_window`（`agent/loop.py`），renderer 不再反向掏 `agent._provider` 三层私有属性 —— desktop 复用的前提
- `events.py` 增加 `assistant_delta` 事件类型，为将来 streaming 逐字渲染留口子（当前 DeepSeek 不支持 SSE，未强推）

### 输入层：`_FramedPrompt` → `PromptSession`

- 砍掉自定义 `Application` 和那条反复修的满宽橙色下边线
- 换来官方推荐姿势 + 三样白送的能力：
  - ↑↓ 输入历史，**持久化**到 `data/cli_history.txt`（跨重启保留）
  - slash 命令 Tab 补全，带一行中文说明（menu 的 `display_meta`），并动态包含已注册 skill
  - **Ctrl+J 换行**，Enter 提交（旧的 `"""` 多行模式移除）
- **换行键教训**：最初用 Alt+Enter，但很多终端把它绑成最大化/全屏窗口，按键到不了 CLI。改用 Ctrl+J（`c-j`），终端层不拦截

### 修掉的生产 bug

- 原来 `chat` 走 `run_cli_shell_sync` 时传了 `input_fn`，导致 `_uses_prompt_toolkit()` 恒为 False —— 生产环境其实一直走裸 `input()`，prompt_toolkit 功夫没生效。现在 `run_cli_shell_sync` 走 `asyncio.run(run_cli_shell(...))` 不传 `input_fn`，真实终端确实走 PromptSession
- 用户输入回显：prompt_toolkit 提交的行天然留在 scrollback；非终端/管道/测试路径由 renderer 显式 `user_message` 回显，两条路径行为一致

### 渲染层 UX

- **AI 回复走 Markdown**：`rich.Markdown` + cyan 圆角 Panel，代码块高亮、超宽行折行（不再手画 box + 硬缩进）
- **「思考中」spinner**：非流式模型等待时用 `rich console.status`（dots），`llm_start` 起「思考中…」，`tool_start` 切成「调用工具 X…」，任何打印前自动停（`_print` 包一层先 `_stop_spinner`）。默认开；非终端 / `--quiet-events` / verbose / `spinner=False` 下自动关，避免污染输出
- **confirm/clarify 高亮**：这两个工具的输出不再混在普通 trace 里，渲染成醒目的黄框「需要确认 — 请回复 yes/no」/「需要澄清 — 请回复你的答案」。不拦截输入（保持工具原设计：用户下一句话即答案）
- **工具状态配色**：区分 success（绿）/ denied（黄，权限）/ interrupted·skipped（灰 grey62）/ error（红），点和结果摘要用对应颜色

## 关键文件

- `src/personal_agent/cli_shell.py` — renderer + CliShell + SlashCompleter + PromptSession
- `src/personal_agent/cli_chat.py` — CliChatRuntime（复用 ConversationService）
- `src/personal_agent/conversation/events.py` — 事件模型
- `src/personal_agent/agent/loop.py` — 事件产出点（llm_end 带 model/context_window）
- `tests/test_cli_shell.py` — 24 个测试覆盖新行为

## 已验证

```bash
python -m compileall -q src/personal_agent   # OK
uv run pytest -q                             # 473 passed
```

## 注意事项

- 测试会改 `src/personal_agent/skills/builtin/.usage.json` 的运行计数，这是副作用，提交前 `git checkout` 还原
- 自动化测试碰不到真正的 PromptSession 交互路径（走 `input_fn` 绕开），UI 回归只能真机肉眼验收
- 提交信息带 `[codex]` 或规范的 `feat/fix(cli):` 前缀
- 用户偏好：不要重型 TUI（Textual）；工具用无框 trace，只有 AI 回复用框；事件流和渲染层保持分离

## 接下来建议

真机 `uv run personal-agent chat` 验收本轮改动：

- Ctrl+J 换行是否在你的终端生效（这次应该不被抢）
- ↑↓ 历史跨重启保留、`/` Tab 补全（含 skill）
- 「思考中」spinner 在等待 DeepSeek 时是否顺眼
- confirm/clarify 黄框是否够显眼
- AI 回复的 markdown 观感（代码块、长行）

后续可选方向（本轮未做）：

- 底层 streaming 逐字渲染（等切到支持 SSE 的 provider，`assistant_delta` 口子已留）
- thinking/bash 长输出的 `ctrl+o` 展开
- `--simple` 旧 REPL（`cli_chat.py::repl`）是否还保留
- 给终端界面加主题开关（如 `cli.theme = "minimal" | "hermes"`）
