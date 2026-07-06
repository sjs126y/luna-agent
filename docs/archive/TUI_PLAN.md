# TUI 重做规划 (CC/Codex 风格行内滚动)

> 交付对象:执行此计划的模型 (Opus 4.6 / GPT-5.5)。
> 本文假定你**没读过**这个仓库,所有关键事实都写在文里。动手前请通读一遍,并按「Phase 0」先做技术验证。

---

## 0. 背景与目标

### 现状 (已确认)
- 后端已经把 UI 和 core **解耦干净**了。核心是 `src/personal_agent/conversation/events.py` 里的事件层:
  - `ConversationEventSink` — 抽象协议 (async `emit(event)` + `wants_deltas` 流式开关)。
  - `ConversationEvent` — 纯 dataclass:`type` / `message` / `data`。
  - 14 种事件类型:`turn_start` `llm_start` `assistant_delta` `thinking_delta` `llm_end`
    `assistant_message` `tool_start` `tool_decision` `tool_end` `retry` `compression` `stop` `error` `turn_end`。
  - `assistant_delta` / `thinking_delta` 是高频 token 增量,只有 sink 把 `wants_deltas=True` 时才发。
- agent 循环 (`src/personal_agent/agent/loop.py::run_conversation`) 通过 `emit_event` / `emit_delta` 产生这些事件。
- `ConversationService.run_turn_events(..., event_sink=...)` 把 sink 一路传到 loop。  
  **如果只做行内滚动 renderer,后端几乎不用动;但若要做 renderer 抽象、执行模式切换、行内确认,仍会有边界增强型改动。**
- 现在的 CLI (`src/personal_agent/cli_shell.py`) 有一个 `TerminalRenderer(ConversationEventSink)`,
  它用 `rich.Live` 做流式 + `prompt_toolkit` 做输入/overlay。**这两个库抢终端 → 就是"效果不满意"的根因。**

### 目标
写一个**新的** `ConversationEventSink` 实现,做出 CC/Codex 风格的**行内滚动**体验:
- 已完成内容进终端 scrollback (终端原生滚动/复制/选择都能用)。
- 只有"当前活跃区"(正在流式的回复 + 工具调用 trace + 输入框 + 状态栏)在底部原地重绘。
- 退出后终端**无残留但保留完整对话记录** (scrollback 里)。

### 硬约束
- **不改** `events.py`、`agent/loop.py`、`conversation/service.py` 的对外契约。新 TUI 只是又一个 sink。
- 新旧渲染器**并存**。`CliShell` 构造时可注入 renderer (见 `cli_shell.py:809` `renderer=...`),
  通过 CLI flag / config 选择,默认仍是旧的 `TerminalRenderer`,稳定后再切默认。
- 遵守仓库 `CLAUDE.md` 约定:先开分支 `feature/tui-inline`,小步 commit,commit 前缀 `[claude code]`,
  提交前跑 `python -m compileall -q src/personal_agent` + `uv run pytest -q`。

---

## 1. 技术选型 (已定,别再纠结)

### 用 prompt_toolkit 独占终端,不用 Textual

**理由**(执行者请勿推翻):
- 用户明确要 **CC/Codex 风格的行内滚动**:保留终端 scrollback,底部钉输入框,输出往上正常滚。
- Textual 默认是**全屏 alternate-screen** 应用(像 htop,退出清屏,滚动是 app 内自绘),
  与"行内滚动"是相反的模型。用 Textual 硬做行内会一直和框架逆着走。
- CC / Codex / aider 这类工具的行内体验,Python 里最干净的实现就是 **prompt_toolkit 独占终端**。
- 现在"不满意"的根因是 `rich.Live` + `prompt_toolkit` **两个库抢终端控制权**。
  解法是**让 prompt_toolkit 成为唯一的终端主人**:流式内容也画进 prompt_toolkit 的 layout,
  而不是让 rich 另开一个 `Live` 去抢屏。`rich` 只降级为"把文本渲染成带 ANSI 的字符串"的工具
  (`Console(file=StringIO)` 或 `rich.markdown` 渲染成 `FormattedText`),不再直接控制终端。

### desktop 复用问题(消除疑虑)
desktop 版复用的是**事件流那一层** —— 到时候另写一个
`JsonStreamSink(ConversationEventSink)`,`emit()` 里把 `ConversationEvent` (纯 dataclass) 
`asdict` 成 JSON 经 WebSocket 推给前端。**这跟 TUI 用什么库完全无关。**
所以放弃 Textual **不损失任何 desktop 路线**。desktop 阶段直接复用现有 `serve` gateway。

### 依赖
- `prompt_toolkit>=3.0.52` — 已在 `pyproject.toml`,无需新增。
- `rich>=15.0` — 已在,仅用作 markdown→字符串渲染,不再做 `Live`。
- **不新增** Textual 等重依赖。

### 行内滚动的核心机制 (关键技术点)
prompt_toolkit 的 `Application` 默认走 alternate screen。要做行内效果,用它的
**`patch_stdout` + 非 full-screen `Application`** 或更直接的
**`print_formatted_text` 打印已完成内容 + 一个只占底部几行的 `Application` 做活跃区**。

推荐实现路径(Phase 0 需验证):
- 底部活跃区 = 一个 `Application(full_screen=False)`,layout 只含:
  流式回复区 (`Window` + `FormattedTextControl`,动态高度) + 状态栏 (`Window`, 1 行) + 输入区 (`TextArea`)。
- 已完成的内容(定稿的回复、工具 trace)用 `run_in_terminal()` / `print_formatted_text()`
  **打印到 Application 上方**,进入正常 scrollback。
- 这正是 prompt_toolkit 官方支持的 "print above the prompt" 模式,CC/aider 都是这个路子。

---

## 2. 架构:先抽 Renderer,再上新 TUI

新建 `src/personal_agent/tui/` 包:

```
src/personal_agent/tui/
  __init__.py
  app.py            # InlineTuiApp: prompt_toolkit Application 装配 + 事件循环
  renderer.py       # InlineRenderer(ConversationEventSink): emit() 映射到 app 状态
  layout.py         # 底部活跃区 layout (流式区 + 状态栏 + 输入区)
  markdown.py       # rich.Markdown -> prompt_toolkit FormattedText 的转换
  state.py          # 一个可观察的 UI 状态 dataclass (当前流式文本/工具列表/状态栏字段)
  theme.py          # 颜色/样式集中管理
```

### 数据流
```
run_conversation (loop.py, 不改)
   │ emit_event / emit_delta
   ▼
Renderer(ConversationEventSink)         ← 抽象基类,统一做 event → 语义方法分派
   ▼
InlineRenderer(Renderer)                ← 新写,只关心 UI state 和重绘
   │ 更新 state.py 里的 UIState
   ▼
InlineTuiApp                            ← 监听 state 变化,invalidate() 触发重绘
   ├─ 活跃区 (底部,原地重绘)
   └─ run_in_terminal(print) 把定稿内容推进 scrollback
```

**关键分工**:
- `Renderer.emit()` 统一把 `ConversationEvent` 分派成语义回调 (`on_turn_start` / `on_tool_end` / `on_error` …)。
- `InlineRenderer` 里**绝不直接操作终端**,只改 `UIState` 然后调 `app.invalidate()`。
所有实际绘制都由 prompt_toolkit 的渲染循环统一做。这就是消除"两库抢终端"的根本手段。

### emit() 事件映射表 (逐事件说明该做什么)
| 事件 | InlineRenderer 该做的事 |
|------|------------------------|
| `turn_start` | 清空本轮活跃区 state;记 `turn_started_at` |
| `llm_start` | 状态栏切"思考中…" spinner;记 `llm_started_at` |
| `thinking_delta` | 累加 `state.thinking`;`app.invalidate()` (活跃区显示"💭 思考中 N 字") |
| `assistant_delta` | 累加 `state.stream_text`;`app.invalidate()` (活跃区流式显示,纯文本或轻量 md) |
| `assistant_message` | 用 `markdown.py` 把完整回复渲成 FormattedText,`run_in_terminal` 打印进 scrollback;清空活跃区流式 state |
| `llm_end` | 更新状态栏字段:`model` / `input_tokens` / `output_tokens` / `api_calls` / `context_window` (都在 `event.data` 里) |
| `tool_start` | 活跃区加一条工具 trace (name + 参数摘要);状态栏 spinner "调用工具 X…" |
| `tool_decision` | 若需人工确认 (destructive),弹确认行 (见 §3 权限模式) |
| `tool_end` | 该 trace 标记完成 (✓/✗ + 耗时);长输出存 expandable,提示可展开 |
| `retry` | 活跃区插一条黄色 "重试: …" |
| `compression` | 活跃区插一条紫色 "压缩: …" |
| `stop` | 活跃区插 "已停止";结束流式 |
| `error` | 红色错误行,`run_in_terminal` 打印 |
| `turn_end` | 结束 spinner;把残留流式定稿;活跃区回到"等待输入"态 |

> 数据来源已确认:`llm_end` 的 `event.data` 含 `input_tokens/output_tokens/api_calls/model/context_window`
> (见 `agent/loop.py`)。完整用量(上下文预算分解、压缩阈值)走 `/usage` 命令的 `build_context_budget`
> (见 `src/personal_agent/commands/runtime.py:170`),TUI 的 `/usage` 直接复用现有命令返回的文本即可。

---

## 3. 界面布局 (CC/Codex 风格,已和用户确认)

### 整体:行内滚动
```
  [已完成的对话内容,进入终端 scrollback,可原生上下滚/选择/复制]
  你: 帮我重构那个函数
  Personal Agent: 好的,我看一下……(定稿回复,markdown 渲染)
    ⚙ read_file src/foo.py            ✓ 0.3s
    ⚙ edit_file src/foo.py            ✓ 0.5s  (Ctrl+O 展开 diff)
  ─────────────────────────────────────────────   ← 活跃区上边界
  Personal Agent: 正在流式输出的回复……▌            ← 活跃区:流式回复(原地重绘)
  ⚙ 调用工具 run_tests…  ⠋                          ← 活跃区:当前工具 spinner
  ─────────────────────────────────────────────
  › 输入框________________________________________  ← 输入区 (prompt_toolkit TextArea)
  auto · deepseek-chat · 12.3k/128k (9%) · ⏎ 发送  Ctrl+J 换行  Ctrl+C 停止  ← 状态栏
```

- 活跃区在**底部固定几行**,内容流式时原地重绘;定稿后 `run_in_terminal` 打印到上方,进 scrollback。
- 空闲时活跃区收起,只留输入框 + 状态栏。

### 状态栏字段 (对接后端)
一行,dim 样式,字段间用 ` · ` 分隔。从左到右:
1. **执行模式** — `auto` / `acceptEdits` / `normal` 等 (见 §4,当前先固定显示 `normal`)。
2. **model** — 来自 `llm_end` 事件的 `model` 字段,或启动时 `settings.llm_model`。
3. **context 用量** — `used/limit (percent%)`,来自 `llm_end` 的 `context_window` + 累计 tokens;
   或定期调用 `build_context_budget` 拿精确值 (成本较高,建议只在 turn_end 后刷新)。
4. **快捷键提示** — `⏎ 发送 · Ctrl+J 换行 · Ctrl+C 停止 · Ctrl+O 展开 · /help`。

> 换行键:仓库已知约定 **Ctrl+J**(Alt+Enter 被终端抢占,见项目 memory)。沿用。

### 快捷键 (prompt_toolkit KeyBindings)
| 键 | 行为 |
|----|------|
| `Enter` | 发送当前输入 |
| `Ctrl+J` | 输入框内换行 |
| `Ctrl+C` | 若正在跑:请求停止 (`agent._interrupt_requested=True`,见 `commands/runtime.py:400`);空闲时:清空输入/退出确认 |
| `Ctrl+O` | 展开上一条可展开输出 (工具长输出 / thinking 全文) — 复用旧逻辑 `_last_expandable` |
| `Ctrl+D` | 空输入时退出 |
| `↑/↓` | 历史命令 (prompt_toolkit `FileHistory`,复用旧的 history 文件路径) |

### slash 命令
所有 `/xxx` 命令**已由后端统一处理** (`commands/runtime.py::handle_slash_command`)。
TUI 只需:输入以 `/` 开头 → 调 `runtime.run_...` 走命令路径 → 把返回文本 `run_in_terminal` 打印。
**不要**在 TUI 里重新实现命令逻辑。输入框可加 `/` 命令自动补全 (prompt_toolkit `Completer`,复用旧 `cli_shell.py` 里的 Completer)。

---

## 4. 执行模式 (automode / acceptEdits) — 前后端一起做

用户要的是**类似 CC 的执行模式**:`normal`(每个危险操作都确认)/ `acceptEdits`(自动接受文件编辑)/
`auto`(全自动,不确认)。

### 后端现状 (已核查代码,重要 — 别按老印象来)
后端**已经有一套完整的执行策略/权限机制**,不是从零开始:
- `agent._execution_policy` 已存在,带 `mode` 字段和 `permission_for(category)` / `explain_permission(category)`
  (见 `src/personal_agent/tools/execution_guard.py:222 check_permission`)。**这就是执行模式的现成载体。**
- `agent._destructive_allowed` 是 grant 集合 (`{"write","bash","network","destructive","all"}`),
  `/allow` 命令往里加 (见 `commands/runtime.py:238 _allow`)。
- 每个工具执行前已经走 `evaluate_execution_guards` → `check_permission`,并**已经发 `tool_decision` 事件**
  (`tools/executor.py:268`),事件带 `permission_category` / `permission_decision` / `execution_mode` /
  `required_allow` / `grant_matched` 字段,前端能直接读来显示。
- **当前确认行为 = 同步拒绝,不是暂停等待**:destructive 工具若策略判定 `ask` 且无对应 grant,
  `check_permission` 直接返回 `allowed=False`(execution_guard.py:258),工具被 denied,
  错误信息回给模型提示用户 `/allow`。它**不会阻塞 loop 等用户按键**。

### 结论:automode/acceptEdits 拆成两块,难度差很大

**(A) mode 切换 + 状态栏显示 —— 容易,Phase 3 就能顺带做:**
1. 加 slash 命令 `/mode [normal|acceptEdits|auto]`,在 `handle_slash_command` 里处理,
   映射到 `_execution_policy.mode` + 对应的 `_destructive_allowed` grant:
   - `normal` → 清空 grant(destructive 工具触发 ask)。
   - `acceptEdits` → 加 `write` grant(文件编辑自动过,bash/network 仍 ask)。
   - `auto` → 加 `all` grant(等价 `/allow all`)。
   **复用现有 `_allow` / `_execution_policy` 语义,别另起一套权限系统。**
2. 前端状态栏读 `tool_decision` / `llm_end` 里的 `execution_mode` 显示当前 mode;
   `/mode` 命令或 `Shift+Tab` 循环切换。
3. 这一块**不需要改 loop/executor 的控制流**,纯粹是加命令 + 读字段。

**(B) CC 那种行内 y/n 确认 —— 需要改执行管道,放 Phase 4:**
- 现状是同步拒绝。要做成"暂停弹确认",需让 `execute_tool_calls` / `_execute_single`
  在"策略判定 ask 且无 grant"这一点(execution_guard.py:258 那个分支)**回调一个可选的 async confirm 函数**,
  由 TUI 提供,`await` 用户按键(y=本次允许 / n=拒绝 / a=本会话总是允许该 category → 加进 grant)再决定放行还是 deny。
- **改动方式(保持契约不破)**:给 `execute_tool_calls(..., confirm=None)` 加一个可选回调参数,
  一路透传到 `_execute_single`。`confirm=None`(平台路径、旧 CLI)→ 保持现在的同步拒绝行为,零影响;
  TUI 传 confirm → 走行内确认。这样**不改对外契约、不破坏平台路径**。
- confirm 回调签名建议:`async def confirm(decision: ToolDecision) -> Literal["allow","deny","always"]`。
  loop 侧在 `await` confirm 期间,活跃区弹 `⚠ 允许执行 edit src/foo.py? [y/n/a]`,输入区临时切成单键模式。
- **给执行者的边界提醒**:这是唯一触碰工具执行管道的改动,必须保留所有现有 audit / guard / checkpoint 逻辑,
  且作为独立 commit;改完 `uv run pytest -q` 必须全绿(executor / guard 相关测试不能挂)。

### 前端 (TUI) 配合
- 状态栏第 1 段显示当前执行模式 (读 `tool_decision` / `llm_end` 的 `execution_mode` 字段)。
- 加快捷键 `Shift+Tab` 循环切换 mode (CC 的习惯),或直接用 `/mode` 命令。
- (A) mode 显示 + `/mode` 切换 → 归入 **Phase 3**,不需要动执行管道。
- (B) `tool_decision` 行内 y/n 确认 → 归入 **Phase 4**,需要给 `execute_tool_calls` 加 `confirm` 回调 (见上)。

> **已核查**:owner 确认应支持,代码核实结果 = 后端已有 `_execution_policy` + `tool_decision` 事件,
> 但当前是**同步拒绝**而非暂停等待。所以 (A) 立刻可做,(B) 需加一个可选 confirm 回调 (不破坏契约)。

---

## 5. 分阶段实施 (每阶段独立可验证 + 独立 commit)

> 总原则:每个 Phase 结束都要能跑、能 demo、能回退。旧 `TerminalRenderer` 全程不动、保持默认。

### Phase 0 — 技术验证 (spike,不进主干或单独分支)
**目的**:证明 "prompt_toolkit 独占终端 + 行内滚动 + 底部活跃区" 这条路走得通,别在没验证前写一堆代码。
1. 写一个 ~100 行的独立脚本 `scripts/spike_inline.py`:
   - 一个 `Application(full_screen=False)`,底部 3 行:动态文本区 + 状态栏 + 输入区。
   - 一个后台 async 任务模拟流式:每 50ms 往"回复"追加几个字,活跃区原地重绘。
   - 流式结束时用 `run_in_terminal` 把定稿回复打印到上方(验证它进 scrollback、能滚上去)。
   - 验证:输入框始终钉底、终端能原生上滚看历史、Ctrl+C 能中断流式、退出无残留。
2. **验收标准**:上述 4 点全部成立。若 `full_screen=False` 做不出理想效果,
   改试 `prompt_toolkit.shortcuts.ProgressBar` 的 patch_stdout 模式 / 或 `print_formatted_text` + 最小 Application。
   记录哪种 API 组合有效,写进 `tui/README.md`,后续 Phase 照此实现。
3. **不通过就停下来找 owner**,不要硬写。

### Phase 1 — Renderer 抽象 + 只读渲染骨架 (MVP)
**目的**:先把"第二个 renderer 会复制 TerminalRenderer dispatch"这个问题解决掉,再让新 TUI 接管一整轮展示。
1. 先抽一个通用 `Renderer(ConversationEventSink)` 基类:
   - `emit()` 统一做 event → 语义方法分派
   - 暴露 `on_turn_start` / `on_llm_start` / `on_assistant_delta` / `on_tool_start` / `on_tool_end` / `on_retry` / `on_error` / `on_turn_end`
   - 默认实现为空方法
2. 让旧 `TerminalRenderer` 继承 `Renderer`,行为不变。
3. 建 `src/personal_agent/tui/` 包 (见 §2 结构)。
4. `state.py`:定义 `UIState` dataclass (stream_text / thinking / tool_traces / status_fields)。
5. `renderer.py`:`InlineRenderer(Renderer)`,`wants_deltas=True`,
   只覆写相关 `on_*` 方法,按 §2 映射表**只更新 state + app.invalidate()**。先只接:
   `turn_start / assistant_delta / assistant_message / llm_start / llm_end / tool_start / tool_end / turn_end`。
6. `app.py`:按 Phase 0 验证的 API 搭底部活跃区 + 状态栏 + 输入区;后台起 turn 任务喂事件。
7. `markdown.py`:先用最朴素实现——`rich` 把 markdown 渲成带 ANSI 的字符串,再包成 `ANSI()` FormattedText。
8. 接入 `CliShell`:加一个 renderer 选择开关 (见 Phase 3),此阶段可临时硬编码用新 renderer 跑通。
9. **验收**:
   - `TerminalRenderer` 已迁到 `Renderer` 基类之上,行为不变
   - `uv run personal-agent chat` (临时切到新 renderer) 能完整跑一轮问答
   - 流式在底部原地刷、定稿进 scrollback、工具 trace 显示、状态栏显示 model+tokens
10. **commit**:
   - `[claude code] extract renderer event dispatch`
   - `[claude code] add inline TUI skeleton (read-only render)`

### Phase 2 — 输入交互 + 快捷键 + slash 命令
1. 输入区接 `Enter` 发送 / `Ctrl+J` 换行 / `↑↓` 历史 (复用旧 `FileHistory` 路径)。
2. `Ctrl+C` 中断:正在跑→`agent._interrupt_requested=True`;空闲→清输入。
3. `/` 命令走后端 `handle_slash_command`,返回文本 `run_in_terminal` 打印。复用旧 `Completer` 做补全。
4. `Ctrl+O` 展开:复用 `_last_expandable` 机制,长工具输出/thinking 全文。
5. **验收**:能连续多轮对话、能中断、能跑 `/help /usage /session /new` 等命令、能翻历史、能换行。
6. **commit**:`[claude code] add inline TUI input + keybindings + slash commands`。

### Phase 3 — 打磨 + 可切换 + 成为可选默认
1. `theme.py` 收拢配色;markdown 渲染优化 (代码块高亮、表格、列表对齐)。
2. 处理边界:窄终端换行、超长回复、超多工具 trace 的活跃区高度上限 (超过就滚动或折叠)。
3. **renderer 选择开关**:
   - CLI flag:`personal-agent chat --ui inline|classic` (`cli.py` 里加参数)。
   - 或 config.yaml:`agent.ui: inline`。
   - `CliShell` 按开关注入 `InlineRenderer` 或 `TerminalRenderer` (构造已支持 `renderer=` 注入)。
   - **默认仍是 classic**,inline 作为 opt-in;稳定后再评估切默认。
4. 补测试:
   - 参考 `tests/test_cli_shell.py`,给 `Renderer` 基类写事件分派测试
   - 给 `InlineRenderer` 写事件→state 的单元测试 (不需要真终端,断言 state 变化即可)
5. **执行模式 (A) — mode 显示 + 切换**(后端已就绪,归入本阶段):
   - 后端加 `/mode [normal|acceptEdits|auto]` 命令,映射到 `_execution_policy.mode` + `_destructive_allowed`
     (见 §4-A,复用 `_allow` 语义,独立 commit)。
   - 前端状态栏读 `execution_mode` 显示当前模式;`/mode` 或 `Shift+Tab` 切换。
6. **验收**:两种 UI 都能用、flag 切换生效、`/mode` 切换生效并反映在状态栏、`uv run pytest -q` 全绿。
7. **commit**:`[claude code] make inline TUI selectable via --ui flag + tests` /
   `[claude code] add /mode command (normal/acceptEdits/auto)`。

### Phase 4 — 行内工具确认 (CC 那种 y/n) [触碰执行管道,单独做]
1. 后端:给 `execute_tool_calls(..., confirm=None)` 加可选 async 回调,一路透传到 `_execute_single`;
   在 execution_guard.py:258 的 "ask 且无 grant" 分支,若 `confirm` 存在则 `await confirm(decision)`,
   按返回值 allow/deny/always 决定放行 (见 §4-B)。`confirm=None` 保持现同步拒绝行为。**独立 commit。**
2. 前端:`tool_decision` 需确认时,活跃区弹 `⚠ 允许执行 X? [y/n/a]`,输入区临时单键模式,回传结果。
3. **验收**:normal 模式下 destructive 工具弹确认、y/n/a 行为正确、平台路径 (无 confirm) 行为不变、
   executor/guard 相关测试全绿。
4. **commit**:`[claude code] add inline tool confirmation (confirm callback)` (后端) + 前端分开提。

---

## 6. 交付检查清单 (每个 Phase 都过一遍)
- [ ] `python -m compileall -q src/personal_agent` 通过。
- [ ] `uv run pytest -q` 全绿 (若动了会写 `.usage.json` 的测试,提交前还原它,见 CLAUDE.md)。
- [ ] 旧 `TerminalRenderer` 未被破坏,`--ui classic` 仍工作。
- [ ] `events.py` / `agent/loop.py` / `conversation/service.py` 的对外契约未改 (Phase 4 后端改动除外,且已确认)。
- [ ] commit 前缀 `[claude code]`,小步提交,在 `feature/tui-inline` 分支上。

## 7. 明确的"不要做"
- 不要引入 Textual 或其它全屏 TUI 框架。
- 不要让 `rich.Live` 和 prompt_toolkit 同时控制终端 (这是旧实现的病根)。
- 不要在 TUI 层重新实现 slash 命令 / 权限 / 会话逻辑,全部走后端现有接口。
- 不要在 Phase 1 里直接从 `ConversationEventSink` 再复制一个大 dispatch renderer;先抽 `Renderer` 基类。
- 不要改事件协议的类型或字段;缺字段先跟 owner 确认,别自行扩展 `events.py`。
- 不要一次性重写:旧渲染器全程保留,新的作为可选项并存。

## 8. 关键文件索引 (执行时按需读)
| 文件 | 作用 |
|------|------|
| `src/personal_agent/conversation/events.py` | 事件协议 + sink 基类 (要 implement 的接口) |
| `src/personal_agent/cli_shell.py` | 旧 `TerminalRenderer` (参照物) + `CliShell` (注入点在 ~809 行) |
| `src/personal_agent/agent/loop.py` | 事件从哪产生 (`llm_end` data 字段在 173-183 行;工具执行调用在 294 行) |
| `src/personal_agent/tools/executor.py` | 工具执行管道;`tool_decision` 事件在 268 行;confirm 回调要加在这里 (Phase 4) |
| `src/personal_agent/tools/execution_guard.py` | 权限判定;`check_permission` 在 222 行;"ask 且无 grant" 拒绝分支在 258 行 (Phase 4 确认钩子点) |
| `src/personal_agent/commands/runtime.py` | slash 命令统一入口 `handle_slash_command`;`/usage` 在 170 行;`/allow` 在 238 行;中断在 400 行 |
| `src/personal_agent/cli.py` | CLI 参数定义 (加 `--ui` flag 处) |
| `tests/test_cli_shell.py` | renderer 测试的参照 |
