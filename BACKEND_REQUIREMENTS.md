# 后端需求 — Inline TUI Phase 4（工具确认）+ 前端已预留接口

本文件列出 **inline TUI 前端已经做好、但需要后端配合** 的点。前端侧代码已在
`feature/tui-inline` 分支合入并通过测试（`uv run pytest -q` 628 passed）；下面每个需求
都标注了前端当前的「占位/降级」行为，后端补齐后即可自动接上，无需再改前端。

约定：
- 前端 = `src/personal_agent/tui/`（`app.py` / `layout.py` / `renderer.py` / `state.py` / `theme.py`）。
- 「confirm 回调」= 一个 `async def confirm(decision) -> str` 的可调用对象，前端已实现为
  `InlineTuiApp.confirm_tool`，返回值是 `"allow"` / `"deny"` / `"always"` 三者之一。
- 现状：前端调用 `runtime.run_message_events(...)` 时，**会用 `inspect.signature` 探测**
  对方是否接受 `confirm` 关键字参数。不接受就自动降级为不传（当前所有后端都走降级路径，
  行为与改动前完全一致）。所以后端可以分步做，任何一步没做完都不会让前端报错。

---

## 需求 1：`run_message_events` / `run_turn_events` 透传 `confirm` 回调

**目标**：让前端传入的 confirm 回调能一路传到工具执行处。

**当前调用链（后端现有代码）**：

```
InlineTuiApp._run_turn
  └─ CliRuntime.run_message_events(text, *, event_sink)        # cli_chat.py:119
       └─ ConversationService.run_turn_events(key, source, text, *, event_sink)  # service.py:62
            └─ run_conversation(agent, ctx, *, event_sink)      # agent/loop.py:24
                 └─ execute_tool_calls(tool_calls, messages, *, agent, hooks, event_sink)  # tools/executor.py:84
                      └─ execute_tool_call_result(tc, *, agent, hooks, event_sink)
```

**要做的**：在这条链的每一层新增一个 keyword-only 参数 `confirm=None`，逐层透传，
最终传到 `execute_tool_call_result`。签名建议：

```python
Confirm = Callable[[ToolDecision], Awaitable[str]]  # 返回 "allow" | "deny" | "always"

async def run_message_events(self, text, *, event_sink=None, confirm=None): ...
async def run_turn_events(self, session_key, source, text, *, event_sink=None, confirm=None): ...
async def run_conversation(agent, ctx, *, event_sink=None, confirm=None): ...
async def execute_tool_calls(tool_calls, messages, *, agent, hooks, event_sink, confirm=None): ...
```

**注意**：
- 必须是 **keyword-only + 默认 None**，这样其它调用方（Gateway / 单轮 CLI / 测试）不受影响。
- `run_turn_events` 里已有 `_accepts_event_sink(run_conversation)` 的探测模式，`confirm`
  可以照抄一个 `_accepts_confirm(...)`，或直接统一加参数。
- 前端探测逻辑见 `app.py::_runtime_accepts_confirm`：只要 `run_message_events` 的签名里
  出现 `confirm` 或 `**kwargs`，前端就会开始传回调。所以 **需求 1 一旦做完，前端立即生效**。

---

## 需求 2：工具执行前调用 confirm，并按返回值决定放行/拒绝

**目标**：destructive / 需要授权的工具在真正执行前，先问 confirm 回调。

**位置**：`tools/executor.py`，就在现有 guard 判定（`tool_decision_from_guard`）之后、
真正 dispatch 工具之前。现有逻辑已经能算出一个 `ToolDecision`（含 `permission_category`、
`required_allow`、`permission_decision` 等字段），confirm 应该只在「按现有规则本来要 ask /
被拦下」的情况下触发，**不要对每个工具都问**。

**建议语义**：

- 只有当 `tool_decision` 表示「需要用户授权才能继续」（即当前会走拒绝/ask 分支）时，才调用
  `confirm(tool_decision)`。
- 回调返回：
  - `"allow"`  → 本次放行，执行该工具。
  - `"deny"`   → 不执行，产出一个 `status="denied"` 的 ToolExecutionResult（复用现有 denied 路径）。
  - `"always"` → 放行，且把对应 category 加进 `agent._destructive_allowed`（等价于用户敲了
    `/allow <category>`），本轮后续同类工具不再询问。
- 若 `confirm is None`（没有前端接入，如 Gateway / 单轮），保持**现有行为**（按 guard 结果直接
  ask/deny），不得因为新参数改变旧路径。

**并发注意**：`execute_tool_calls` 会把相邻的 parallel-safe 工具用 `asyncio.gather` 并发跑
（executor.py:104-126）。但 destructive 工具本来就是串行 barrier（`not entry.is_destructive`
才进并发批次），所以需要确认的工具天然是串行的，confirm 不会并发弹多个。**请保持这个前提**
——不要让需要 confirm 的工具进入并发批次，否则前端一次只能显示一个确认框会卡住。

---

## 需求 3：ToolDecision 暴露给 confirm 的展示字段

**目标**：前端确认框要显示「在请求执行什么」。

前端 `confirm_tool(decision)` 目前只读两个字段，且兼容 dict 和对象两种形态：

- `tool_name`     — 工具名（必需，用于「允许执行 X?」）。
- `permission_category` — 权限类别（可选，如 `write` / `bash`，显示成「允许执行 X（write）?」）。

**要做的**：确保传给 confirm 的 `decision` 对象上有 `tool_name` 和 `permission_category`
两个可读属性（现有 `ToolDecision` 基本已具备，确认即可）。如果还能顺带给出以下字段，前端后续
可以把确认框做得更详细（非必需，先有上面两个就能跑）：

- `input_summary` — 工具入参摘要（如 `write_file: data/foo.txt`）。
- `required_allow` — 放行需要的授权名，用于 `"always"` 时准确加哪个 category。

---

## 需求 4（可选，未来）：确认超时 / 中断的后端语义

**目标**：定义「用户一直不回答」或「turn 被 /stop 打断」时 confirm 的行为。

前端当前：确认框弹出后会一直等用户按键；用户按 `Ctrl+C` 会 resolve 成 `"deny"`。但如果后端在
等待期间被 `request_stop` 中断，需要约定：

- 后端中断一个正在 await confirm 的工具时，应视同 `"deny"` 处理并走正常的 interrupted/denied 落盘。
- 建议 confirm 的 await 点也纳入现有的 `_interrupt_requested` 轮询范围，避免卡死。

此项不阻塞需求 1–3，可最后做。

---

## 需求 5：`/mode` 真正切换 execution policy profile（Phase 3 执行模式，后端大功能）

**背景 — 现在有两条独立的「模式」轴，别混淆**：

1. **`_destructive_allowed`（授权集合）** — 每轮的临时授权，`/allow write`、`/allow all` 写进
   `agent._destructive_allowed`。**前端现有的 `/mode normal|acceptEdits|auto` 就是叠在这条轴上的
   便捷标签**（normal 清空、acceptEdits 授权 write、auto 授权 all）。这条已经能用，不需要后端做。
2. **`ExecutionPolicy.mode`（策略 profile）** — `guarded|standard|trusted|sovereign`，在
   `execution.py::resolve_execution_policy(settings)` 里根据 config.yaml `execution.mode`
   **在 agent 创建时解析一次**，决定每个工具 category 的 `allow|deny|ask`（见 `MODE_PROFILES`）。
   **目前没有运行时切换它的通道** —— 改了只能重启。

**这条需求 = 让用户在会话中动态切换第 2 条轴（policy profile）**，这是你说的「后端一个大功能」。

**要做的（后端）**：

- 提供一个运行时重解析/切换 execution policy 的入口，例如在 `CommandRuntime` 或
  `ConversationService` 上加 `async def set_execution_mode(mode: str) -> str`：
  - 校验 `mode in EXECUTION_MODES`（`config_registry.py:244`）。
  - 重新构造 `ExecutionPolicy`（`resolve_execution_policy` 目前吃 settings，可能要改成能直接吃
    一个 mode 字符串，或在 settings 上覆盖后重解析）。
  - 把新 policy 挂到当前会话的 agent 上（`agent._execution_policy`，见 `agent/agent.py:56`），
    并处理 agent LRU 缓存里已存在的实例（要么直接改属性，要么让缓存失效重建）。
  - 决定是否持久化：是只影响本会话，还是写回 config.yaml `execution.mode`。建议**只影响本会话**，
    避免污染全局配置；跨会话仍读 config。
- 决定 `guarded/standard/trusted/sovereign` 四档要不要跟前端的 `normal/acceptEdits/auto` 合并成
  一套词汇，还是并存两个概念。**这是产品决策，需要你和 gpt 定**：
  - 方案 A：前端 `/mode` 继续管授权集合（轻量），另开一个 `/policy guarded|standard|...` 管
    execution profile（重量）。两者正交。
  - 方案 B：把 `/mode` 的三档直接映射到 execution profile 的四档（比如 normal→standard、
    auto→sovereign），废掉授权集合那套。改动更大，语义要重新对齐。

**前端配合（等后端定了我再做，改动很小）**：

- 状态栏已经在显示 `exec_mode`（现在显示的是授权集合推断出的 normal/acceptEdits/auto）。
  后端确定 policy 切换入口和词汇后，我把状态栏改成读真正的 `execution_policy.mode`，
  并把 `/mode` 或新 `/policy` 命令、`Shift+Tab` 循环接到后端的 `set_execution_mode` 上。
- `conversation/command_runtime.py::current_execution_mode`（已存在）可以扩展成返回真正的
  policy mode；前端 `_refresh_mode` 已经在每轮/每命令后调它，接上即可。

**验收**：`/policy trusted`（或定好的命令）后，一个原本在 `standard` 下需要 ask 的 write 工具，
在 `trusted` 下按 profile 直接放行；`/policy guarded` 后 bash 被 deny；状态栏实时反映当前 profile；
重启后回到 config.yaml 的默认档。

---

## 验收（后端做完后一起过）

1. `uv run pytest -q` 全绿（前端已加的确认相关测试见 `tests/test_tui_app.py`、
   `tests/test_tui_layout.py`，它们只测前端，不依赖后端）。
2. `personal-agent chat --ui inline`，让模型调用一个 destructive 工具（如写文件），
   确认底部活跃区弹出 `⚠ 允许执行 write_file（write）?  [y/n/a]`，且：
   - `y` / Enter → 工具执行；
   - `n` / Ctrl+C → 工具被拒绝，模型收到 denied 结果；
   - `a` → 执行且本轮后续同类工具不再询问。
3. Gateway（`serve`）与单轮（`chat --once`）路径行为不变（confirm 为 None，走旧逻辑）。

---

## 附：前端已就绪、无需后端改动的部分（供对照）

- `theme.py` 集中配色；markdown 代码块高亮 / 表格 / 列表对齐。
- 活跃区高度上限（12 行）+ 超长流式截尾 + 超多工具折叠。
- `--ui inline|classic` flag 与 config.yaml `agent.ui`（默认 classic）。
- `/mode [normal|acceptEdits|auto]` 命令 + 状态栏显示 + `Shift+Tab` 循环切换。
  **注意**：现版 `/mode` 只叠在 `_destructive_allowed` 授权集合上（轻量、已能用），
  **没有**接 `_execution_policy` 的 profile 枚举 `guarded/standard/trusted/sovereign`。
  让它真正翻转 execution policy profile 是后端大功能，已单列为**需求 5**。
- 确认框 UI、`confirm_tool` 回调、y/n/a 与 Ctrl+C 键绑定、runtime 签名探测降级。
