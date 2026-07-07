# 前端规划（Frontend Roadmap）

更新时间：2026-07-03 CST
适用分支：`main`（CLI 前端重构 + 流式/thinking/Ctrl+O 已并入）

> 归档说明：本文是早期前端路线图，仅供背景参考；当前权威状态以 `FRONTEND_PROGRESS.md`、`BACKEND_INTERFACE.md` 和 `src/personal_agent/tui/README.md` 为准。

这份文档回答一个问题：**CLI 前端已经能用了，接下来往哪走。**

结论先行：短期最该投入的不是加 UI 花样，而是把「事件流 ↔ 渲染层」这条已经分好的缝彻底焊死，让 CLI 只是它的第一个消费者。架构成本（事件/渲染分离）已经付了，还没兑现收益——把地基做完才兑现。

---

## 0. 现状盘点（我们站在哪）

### 已经做对的事

- **事件流与渲染层分离**：`conversation/events.py` 产出结构化事件，`cli_shell.py::TerminalRenderer` 消费并渲染。业务逻辑（`agent/loop.py`、`tools/executor.py`）只管 `emit_event`，不关心怎么显示。
- **平台安全闸**：`wants_deltas` 让高频 delta 事件只发给 opt-in 的渲染器；飞书/Telegram/微信走 `run_turn`（无 event_sink），零开销。
- **EventRecorder 双职责**：收集事件（供落盘/回放）+ 转发给下游渲染器；delta 不入库避免堆积。
- **CLI 体验基本成型**：PromptSession 输入（历史/补全/Ctrl+J 换行）、rich Markdown 回复、spinner、工具 trace 配色、流式逐字、thinking 折叠、Ctrl+O 全屏 pager。

### 当前事件协议（实际字段，来自代码）

| 事件 | 触发点 | 关键字段 |
|---|---|---|
| `turn_start` | loop 开始 | `turn_id`, `user_message`, `message_count` |
| `llm_start` | 每次调模型前 | `api_calls`, `message_count`, `tool_count` |
| `assistant_delta` | 流式文本增量 | `chunk`（仅 `wants_deltas`） |
| `thinking_delta` | 流式思考增量 | `chunk`（仅 `wants_deltas`） |
| `llm_end` | 模型返回 | `input_tokens`, `output_tokens`, `tool_call_count`, `model`, `context_window`, `api_calls` |
| `assistant_message` | 一段完整回复 | `message`（文本） |
| `tool_start` | 工具开始 | `tool_name`, `tool_use_id`, `input_summary` |
| `tool_end` | 工具结束 | `tool_name`, `tool_use_id`, `status`, `category`, `error`, `duration`, `input_summary`, `output_summary`, `full_output` |
| `retry` | 各类重试 | `category`, `attempt`, `error`（视场景） |
| `compression` | 历史压缩 | `pre_message_count`, `post_message_count` |
| `stop` | 用户中断 | — |
| `error` | 模型调用失败 | `error` |
| `turn_end` | loop 结束 | `status`, `completed`, `final_response` |

> ⚠️ 这张表现在是「靠读代码得到的约定」，不是「被固化的契约」。第一阶段的核心工作就是把它变成契约。

---

## 一、地基层（优先做，回报最大）

目标：把前端从「一个 CLI 程序」变成「一个可挂多种前端的事件运行时」。

### 1.1 事件协议固化 + 版本化

**问题**：事件字段散在各处 `emit_event(...)` 调用里，靠约定维系。一旦 desktop/web 前端要复用，这就是 API 契约，但现在没人保证 `tool_end` 一定带 `duration`、`llm_end` 一定带 `model`。

**做法**：
- 为每个事件类型定义明确 schema（用 dataclass 或 TypedDict），字段必有/可选、含义、类型都写清楚。
- 加 `protocol_version` 常量；事件序列化时带上，让前后端能独立演进。
- 加一个「协议一致性测试」：断言每个 `emit_event` 调用点发出的字段符合 schema（可用轻量运行时校验，或至少一个覆盖所有事件类型的测试）。

**收益**：现在改协议成本最低；等三个前端都依赖它了再改就是伤筋动骨。

**验收**：新增 `conversation/protocol.py`（或在 events.py 内）定义 schema；测试覆盖全部事件类型的字段契约。

### 1.2 渲染层抽象成接口

**问题**：`TerminalRenderer` 直接 implements `ConversationEventSink`（一个 `emit(event)` 的大 dispatch）。写第二个前端时，得照抄这套 dispatch。

**做法**：
- 定义 `Renderer` 抽象基类，把 `emit` 的大 if-else 拆成语义方法：`on_turn_start` / `on_assistant_delta` / `on_thinking_delta` / `on_assistant_message` / `on_tool_start` / `on_tool_end` / `on_retry` / `on_error` / `on_turn_end` …
- 基类的 `emit` 负责把 event 分派到对应方法（一次实现，所有子类复用）；子类只覆写关心的方法。
- `TerminalRenderer` 重构成 `Renderer` 的一个实现，行为不变。

**收益**：写 desktop/web 前端 = 实现一个新 `Renderer`，不碰事件层。这正是 handoff 里「方便抽给 desktop」的落地方式。

**验收**：`TerminalRenderer` 继承 `Renderer`；现有 491 个测试全过（行为不变）；新增一个「mock Renderer 收到分派回调」的测试证明基类分派可用。

> 1.1 和 1.2 做完，地基就成了。后续所有前端工作都建立在这两块上。

---

## 二、体验层（CLI 本身打磨，随时可做，不阻塞）

按性价比排序：

### 2.1 可中断/可继续（高）
- 流式态下 Ctrl+C 优雅停当前轮。现有 `_request_stop` / `stop_agents` 逻辑在，但**流式渲染进行中**的中断路径要专门测（Live 未 finalize 时被打断，屏幕状态是否干净）。

### 2.2 错误呈现分级（中）
- 现在 `retry` / 工具失败 / 模型 `error` 都可能走红框。应分轻重：重试是暗色一行（`retry` 已经是），真错误（`error`）才红框，工具失败（`tool_end` status=error）走 trace 行内红色。梳理一遍确保一致。

### 2.3 会话切换视觉反馈（中）
- `/session switch` 后清屏 + 重放最近几轮摘要，让人知道「我在哪个会话」。依赖 EventRecorder 的历史回放能力。

### 2.4 主题开关（低）
- handoff 提过的 `cli.theme = "minimal" | "hermes"`。**优先级最低**——先把内容对了再谈皮肤。做的话应建立在 1.2 的 Renderer 抽象上（主题 = Renderer 的样式配置）。

---

## 三、扩展层（想清楚方向再动）

### 3.1 desktop / web 前端
- 如果这是真目标，第一层两件事是前提。地基做完，这里近似「换个 Renderer + 一个事件传输通道（WebSocket/SSE）」。
- 传输层：把 `ConversationEvent` 序列化（1.1 的 `protocol_version` 在此发挥作用）通过 WebSocket 推给前端，前端实现一个 JS 版 Renderer。

### 3.2 OpenAI-format 的 thinking/reasoning
- 当前只有 Anthropic-format 路径解析 `thinking`。各家 reasoning 字段不统一，等真用到带 reasoning 的模型再接。

### 3.3 多模态渲染
- 图片/文件预览。看实际需求，不确定就先不做。

---

## 已知限制与债务

- **Ctrl+O 单 slot**：`_last_expandable` 只存最近一个可展开输出，一轮多工具时旧的被覆盖。用户已确认「够用」。若要做「展开任意第 N 个」：改成 list + 编号，Ctrl+O 弹选择列表或用 `/expand n`。标为实验性。
- **交互路径测不到**：自动化测试走 `input_fn` 绕开真实 PromptSession/pager，UI 回归只能真机验收。已有一个 import 冒烟测试防「run() 依赖的模块级名字被误删」这类低级回归。
- **事件协议是约定非契约**：见 1.1，这是第一阶段要还的债。

---

## 推荐路线

1. **先做地基（1.1 + 1.2）** —— 兑现已付出的架构成本，且现在改协议最便宜。
2. **体验层按需插入** —— 2.1（中断）最该先做，其余随手可做，不阻塞。
3. **扩展层等方向明确** —— desktop/web 是不是真目标？明确了再动 3.1。

原则不变（沿用 handoff）：不上重型 TUI（Textual）；工具用无框 trace，只有 AI 回复用框；事件流与渲染层保持分离。
