# Frontend Interface Requirements

更新时间：2026-07-06

本文给后端线使用，描述 inline TUI / future desktop-web 前端为了做更完整体验还需要的接口能力。当前后端已提供能力见 `BACKEND_INTERFACE.md`；本文只列前端希望补齐或固化的部分。

## 0. 前端目标

短期目标不是增加花哨 UI，而是让用户在 inline TUI 里清楚知道：

- 当前 turn 处于 thinking / tool / retry / stopped / failed 哪个状态。
- 工具为什么需要确认、风险是什么、默认动作是什么。
- 确认后是本次允许、始终允许、还是拒绝。
- 长输出、工具结果、后端审计之间可以稳定关联。

## 1. Tool Confirmation 展示字段

当前前端最少能从 `confirm(decision)` 读到：

- `tool_name`
- `permission_category`
- `required_allow`
- `reason_code`
- `decision_message`

这足够做 `[y/n/a]`，但不足以做更好的确认面板。希望后端在传给 `confirm` 的 decision 对象，以及相关 `tool_decision` / `tool_end` 事件里稳定提供以下字段。

### 1.1 必需字段

- `tool_use_id: string`
- `tool_name: string`
- `display_name: string`
- `permission_category: string`
- `permission_decision: "allow" | "ask" | "deny"`
- `execution_mode: string`
- `execution_mode_label: string`
- `required_allow: string`
- `decision_message: string`
- `reason_code: string`

说明：

- `execution_mode` 保留内部 profile，例如 `standard` / `trusted`。
- `execution_mode_label` 给前端直接显示，例如 `Ask First` / `Edit Freely`。
- 如果后端暂时不想新增 `execution_mode_label`，前端可以本地映射，但长期建议后端提供，避免多前端重复维护映射。

### 1.2 风险与默认动作

用于把确认 UI 从裸 `y/n/a` 升级成明确的确认面板：

- `risk_level: "low" | "medium" | "high"`
- `risk_summary: string`
- `default_action: "allow" | "deny" | "none"`
- `available_actions: list[string]`

建议含义：

- `default_action` 决定 Enter 的行为。用户倾向是默认操作更方便，但前端必须明确展示默认项。
- `available_actions` 至少支持 `allow_once` / `allow_always` / `deny`。
- 如果后端策略认为某类工具不应 Enter 默认允许，则返回 `default_action="none"` 或 `"deny"`。

前端期望展示形态：

```text
需要确认
bash · shell command
风险: 将执行 shell 命令

Enter 允许本次   A 始终允许 bash   Esc 拒绝
```

### 1.3 输入预览

用于展示“到底要做什么”，避免用户只看到工具名。

通用字段：

- `input_summary: string`
- `input_preview: string`
- `affected_paths: list[string]`

按类别建议：

- `write` / `edit`: `affected_paths`, `diff_summary`, `diff_preview`
- `bash`: `command_preview`, `cwd`, `timeout_seconds`
- `network`: `url_preview`, `host`, `method`
- `background`: `process_label`, `command_preview`

字段都应允许为空；前端会按存在字段渐进展示。

## 2. Confirmation 交互语义

当前后端 confirm 返回值是：

- `"allow"`
- `"deny"`
- `"always"`

这个可以继续保留。为了 UI 文案更清晰，前端建议在文档层固定别名语义：

- `"allow"` = allow once / 本次允许。
- `"always"` = allow always for current agent/session grant scope。
- `"deny"` = deny this tool call。

希望后端明确：

- `"always"` 的有效范围：当前 agent、当前 session、当前 turn，还是直到模式切换。
- `/mode` 切换后会清空哪些 grants。
- `/stop` 打断 pending confirm 后，最终 `tool_end.status/category/error` 的固定取值。

## 3. Event Stream UI 需求

inline TUI 会消费 `BACKEND_INTERFACE.md` 里的事件，但希望后端保证以下字段稳定，方便前端显示。

### 3.1 `retry`

用于显示轻量提示，避免用户误以为卡住。

希望字段：

- `category: string`
- `attempt: integer`
- `max_attempts: integer`
- `error: string`
- `tool_name: string`
- `tool_names: string`
- `recoverable: boolean`

前端展示：

```text
↻ 模型空回复，准备重试 · 1/2
```

### 3.2 `error`

用于进入 scrollback 的明确错误行。

希望字段：

- `error: string`
- `category: string`
- `recoverable: boolean`
- `detail_id: string`

`detail_id` 可用于后续从日志/doctor/tool run 查询完整详情。

### 3.3 `stop`

用于用户按 Ctrl+C 或 `/stop` 后的可见反馈。

希望字段：

- `reason: "user" | "interrupt" | "timeout" | "shutdown"`
- `message: string`
- `stopped_tools: integer`
- `stopped_agents: integer`

### 3.4 `compression`

用于显示上下文被压缩。

当前字段已够用：

- `pre_message_count`
- `post_message_count`

可选增强：

- `token_before`
- `token_after`
- `reason`

## 4. Tool Runs 查询接口

后端已经持久化 tool runs。前端后续需要一个 runtime 层查询接口，不直接碰 Database。

建议接口：

```python
async def recent_tool_runs(
    *,
    session_key: str | None = None,
    turn_id: str | None = None,
    limit: int = 20,
    status: str | None = None,
    tool_name: str | None = None,
    permission_category: str | None = None,
    include_output: bool = False,
) -> list[dict]: ...

async def get_tool_run(
    run_id: str,
    *,
    include_output: bool = True,
) -> dict | None: ...
```

前端用途：

- Ctrl+O 展开最近工具完整输出。
- future desktop 的工具运行历史面板。
- 从 denied/error 工具跳到完整审计详情。

## 5. Schema / Versioning

后端文档里已经提到 `protocol_version`、`EVENT_SCHEMAS`、`event_protocol_schema()`。前端希望补一个可调用 runtime/CLI 接口，方便 desktop/web 启动时校验协议。

建议：

```python
def frontend_protocol_schema() -> dict: ...
```

或 CLI：

```bash
personal-agent protocol schema --json
```

前端用途：

- UI 启动时确认协议版本。
- desktop/web 自动生成类型。
- 测试里对事件字段做契约校验。

## 6. 前端兼容策略

前端会按以下方式兼容旧后端：

- 缺失 `execution_mode_label` 时，本地从 profile 映射。
- 缺失风险字段时，只显示 tool name + permission category。
- 缺失 preview 字段时，退回 `input_summary`。
- 缺失 `default_action` 时，前端不把 Enter 绑定为 allow，除非产品明确决定默认 allow。
- 未知事件字段忽略，未知事件类型只记录不渲染。

## 7. 优先级

P0：

- confirm decision 增加 `tool_use_id`, `display_name`, `execution_mode_label`, `default_action`, `available_actions`, `risk_summary`, `input_preview`。
- 固化 `/stop` 打断 confirm 后的 `tool_end` 字段。

P1：

- `retry/error/stop` 增加用于 UI 展示的稳定字段。
- runtime 层 tool runs 查询接口。

P2：

- 协议 schema CLI/API。
- diff preview / affected paths / network host 等按工具类别细化字段。
