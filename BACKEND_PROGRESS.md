# Backend Progress

更新时间：2026-07-06 13:50 CST

## 交接定位

这个文档只记录后端线进度，给后续接手后端的 Codex 使用。前端 TUI / desktop / prompt_toolkit 真实终端问题交给前端线处理；后端线只负责事件、接口、agent runtime、工具执行、权限、配置、平台适配、provider / transport 等基础能力。

当前工作分支：`feature/backend-provider-cache`

权威接口文档：

- `BACKEND_INTERFACE.md`：前端消费后端事件、slash commands、tool metadata、tool runs 等接口的主文档。
- `FRONTEND_INTERFACE_REQUIREMENTS.md`：前端提出的后端字段/接口需求入口。
- `CODEX_HANDOFF.md`：总交接文档，记录前后端分工和整体状态。

## 当前后端状态

后端主干能力已经比较完整，最近已完成并验证的方向包括：

- Execution Mode v3：四档模式已经稳定，对应权限、沙箱、工具类别和确认行为。
- Tool execution / permission pipeline：工具执行门控已经统一到 executor 路径，权限只负责自己的决策层，不再和其他阻断逻辑混在一起。
- Tool decision metadata：`tool_decision` / `tool_end` 已带前端确认 UI 所需字段，包括展示名、风险摘要、默认动作、可选动作、路径/命令/URL 预览等。
- Event protocol：事件有 `protocol_version`，`retry` / `error` / `stop` / `tool_decision` / `tool_end` 等事件结构化。
- Tool truth / turn report：`AgentTurnReport` 能记录工具真实调用、retry、错误、口头声称工具调用但实际未调用等信息。
- Tool runs：工具执行结果已持久化，并提供 `/tool-runs` 与 `ConversationQueryService` 查询。
- Slash commands v2：chat / inline TUI / gateway 共用 slash command registry，`/commands`、`/tools`、`/permissions`、`/protocol`、`/mode` 等支持结构化 `CommandResult`。
- Doctor diagnostics：runtime health 已能展示 commands、query、execution、doctor 配置/运行时状态。
- Config registry：配置整理已进入可用状态，新增配置通过 registry/field 描述，不再散落硬编码。
- Platform adapter base：平台消息基类和 media attachment v1 已打底，但平台线暂时不要继续激进推进，避免牵动底层架构。

最近一次记录的全量测试结果：`703 passed`。

## 当前分工约定

- 后端 Codex 不主动改 `src/personal_agent/tui/`。
- 前端 Codex 如果需要字段或接口，应通过 `FRONTEND_INTERFACE_REQUIREMENTS.md` 明确写出小需求。
- 后端接口变更必须同步 `BACKEND_INTERFACE.md`。
- `CLAUDE.md` 不处理。
- 测试可能修改 `src/personal_agent/skills/builtin/.usage.json`，提交前要检查并恢复非意图改动。

## 当前待推进方向：Provider / Transport Cache

用户最新关注点：provider 和 transport 对缓存命中率支持不够明显。当前架构本身方向是对的：

1. 根据 `api_mode` 选择 transport。
2. transport 内部持有具体 `ProviderProfile`。
3. transport 根据 provider 构建最终请求参数。

问题不是这条链路错了，而是 provider / transport 目前缺少“缓存能力表达”和“缓存诊断”。

### 2026-07-06 v1 实施进度

状态：已完成实现，待全量回归。

已完成：

- `ProviderProfile` 增加 cache capability：`cache_strategy`, `supports_cache_usage`, `cache_usage_fields`, `cacheable_blocks`。
- `BaseTransport` 增加 cache diagnostics 与 usage normalization 基础方法。
- Anthropic / ChatCompletions parse 阶段归一化 provider cache usage。
- transport `call(...)` 记录最近一次 request cache diagnostics。
- `llm_end` 事件增加 cache usage 与 `cache_diagnostics` 可选字段。
- `AgentTurnReport` / `ConversationService` / runtime health 聚合最近 cache usage。
- `doctor --section runtime --json` 暴露 `runtime.llm_cache`。
- doctor 文本输出增加 LLM Cache 摘要和 runtime detail。
- `BACKEND_INTERFACE.md` 已同步新增事件字段和 runtime doctor 字段。
- 新增 `tests/test_transport_cache.py`，扩展 event/runtime/CLI/agent loop 测试。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_transport_cache.py tests/test_transport_streaming.py tests/test_event_protocol.py tests/test_agent_loop.py tests/test_runtime.py tests/test_cli.py -q
```

结果：`75 passed`。

下一步：

- 跑 `uv run pytest -q` 全量回归。
- 如果全量通过，进入 v2：调整 provider-aware transport cache 策略，尤其 Anthropic 最后一条动态 message 的 `cache_control`。

### 2026-07-06 v2 实施进度

状态：已完成实现，待回归。

已完成：

- Anthropic transport 保留 system prompt `cache_control`。
- Anthropic transport 不再默认给最后一条动态 message 添加 `cache_control`。
- Anthropic / ChatCompletions request body 中 tools 按 name 稳定排序。
- ChatCompletions 仍不添加任何非标准 cache 请求字段。
- 新增 v2 行为测试，覆盖 Anthropic marker 位置和 ChatCompletions 工具排序。

接口影响：

- 无新增前端/doctor 字段；`BACKEND_INTERFACE.md` 无需为 v2 追加新接口。

### 当前代码事实

- `ProviderProfile` 目前主要是连接参数：`name`、`base_url`、`api_key`、`model`、`max_tokens`、`context_window`、`request_hook`、`response_hook`、`extra_headers`。
- `AnthropicMessagesTransport` 已经有 `cache_control`，但现在会给 system 和最后一条 message 加 cache 标记。system 合理，最后一条 message 通常是动态内容，可能不利于命中率。
- `ChatCompletionsTransport` 对 DeepSeek / OpenAI / OpenRouter 基本是同一套 OpenAI-compatible 请求构建，没有 provider-aware cache policy。
- `_cached_system_prompt` 已经缓存，工具摘要也按名称排序。用户确认：memory system text 是启动快照，本轮运行内不会频繁变化。
- `_build_api_messages` 会把 skill summaries、skill injection、memory prefetch 插到 message 头部。这对模型理解可能合理，但对 prefix cache 是否最优还需要诊断。

### 最终目标

把 provider / transport 从“协议发送器”升级成：

**Provider 能力描述 + Transport 请求规划 + Cache 诊断归一化**

目标不是推翻 `api_mode -> transport -> provider` 架构，而是在现有链路里补齐缓存策略。

理想形态：

- `ProviderProfile` 能表达 provider cache capability。
- `BaseTransport` 提供缓存诊断/请求规划相关基础接口。
- transport 根据 provider 策略构建请求：
  - Anthropic：显式 cache，稳定块加 `cache_control`。
  - DeepSeek / OpenAI-compatible：prefix cache 友好布局，不加非标准字段。
  - OpenRouter：先保守处理，避免误加目标模型不支持的字段。
- response usage 统一归一化：
  - `cache_hit_tokens`
  - `cache_miss_tokens`
  - `cache_write_tokens`
  - `cache_read_tokens`
  - `cache_hit_rate`
- doctor / runtime 能展示 cache 诊断：
  - provider cache strategy
  - system hash
  - tools hash
  - stable prefix hash
  - 最近一次 cache usage

## 建议版本拆分

### v1：缓存能力与诊断基础

目标：先能判断问题，不急着大改请求行为。

计划：

- 给 `ProviderProfile` 增加轻量 cache capability 字段，例如：
  - `cache_strategy`: `none | prefix | explicit`
  - `supports_cache_usage`
  - `cache_usage_fields`
- 给 `BaseTransport` 增加默认缓存诊断方法，例如：
  - `cache_strategy`
  - `cache_diagnostics(body)`
  - `normalize_usage(raw_usage)`
- 在 transport parse 阶段归一化 provider cache usage 字段。
- 对请求稳定部分生成 hash：
  - system hash
  - tools hash
  - message prefix hash
- 先不改变大多数请求结构，避免影响稳定性。
- 增加单测覆盖 provider capability、usage 归一化和 hash 稳定性。

### v2：Transport 缓存策略优化

目标：在已有诊断基础上优化实际请求。

计划：

- Anthropic:
  - 保留 system cache marker。
  - 避免默认给最后一条动态 message 加 `cache_control`。
  - 评估是否将工具 schema 作为稳定 cache block 处理。
- ChatCompletions:
  - DeepSeek 走 prefix-cache 友好布局。
  - OpenAI-compatible 不加 provider 不支持的字段。
  - OpenRouter 保守处理，只做 usage 解析和稳定排序。
- 增加测试确保同 provider、同 system、同 tools 时稳定 hash 不变。

### v3：请求规划层

目标：让 agent 和 transport 之间有清晰中间层，而不是只传散落的 `messages/system/tools`。

计划：

- 引入轻量 `LLMRequestPlan` 或同等结构。
- 将请求拆为：
  - stable system
  - stable tools
  - stable context
  - dynamic injections
  - history
  - current user
- transport 按 provider capability 将这些块拼成最终 request。
- doctor 输出 request plan 摘要，方便排查缓存命中率。

## 下一个后端 Codex 建议入口

优先做 v1。原因：

- 当前最缺的是“判断能力”，不是立刻重排所有请求。
- v1 风险最低，对前端无破坏性。
- v1 做完后，可以区分：
  - provider 实际没命中；
  - provider 命中了但后端没解析 usage；
  - 请求稳定前缀本身在变；
  - Anthropic cache marker 放置不合理。

建议先读这些文件：

- `src/personal_agent/llm/provider.py`
- `src/personal_agent/llm/base.py`
- `src/personal_agent/plugins/builtin/llm/builtin/anthropic.py`
- `src/personal_agent/plugins/builtin/llm/builtin/chat_completions.py`
- `src/personal_agent/agent/agent.py`
- `src/personal_agent/agent/loop.py`
- `src/personal_agent/agent/context.py`
- `tests/test_transport_streaming.py`
- `tests/test_agent_loop.py`

建议新增/扩展测试：

- `tests/test_provider_cache.py` 或 `tests/test_transport_cache.py`
- Anthropic usage cache fields 解析。
- ChatCompletions provider-specific usage cache fields 解析。
- 同 system/tools 下 stable hash 保持不变。
- 动态 user message 变化时 system/tools hash 不变。

## 验证命令

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_transport_streaming.py -q
uv run pytest tests/test_agent_loop.py -q
uv run pytest -q
```
