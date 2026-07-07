# Frontend Interface Requirements

更新时间：2026-07-07 10:25 CST

本文给后端线使用，只记录 inline TUI / future desktop-web 前端仍需要后端配合或需要继续固化的接口事项。已经完成并被前端消费的需求不再保留在待办区，避免重复实现或误判优先级。

## 当前状态

截至 2026-07-06，前端已接入并消费以下后端能力：

- `tool_decision` / `tool_end` 的确认展示字段：`display_name`, `execution_mode_label`, `risk_level`, `risk_summary`, `default_action`, `available_actions`, `input_summary`, `input_preview`, `affected_paths`, `command_preview`, `url_preview`, `host`, `cwd`, `timeout_seconds`, `method`, `process_label`。
- `confirm(decision)` 的 allow / deny / always 语义，以及由 `available_actions` 限定可用确认动作。
- `/stop` 打断 pending confirm 后的固定 `tool_end.status/category/error` 收口。
- `retry` / `stop` / `error` 的增强状态字段：`max_attempts`, `recoverable`, `reason`, `stopped_tools`, `stopped_agents`, `category`, `detail_id`。
- 协议 schema 入口：`frontend_protocol_schema()` 和 `personal-agent protocol schema --json`。
- Slash command registry 和 `CommandResult v2`：inline TUI 补全使用 registry metadata，并能消费 `continue_text` 继续进入 inline event renderer。
- Slash command argument metadata：registry 已提供 `arguments`，支持 `choice` 和 `dynamic`。
- 静态参数候选：`/mode set <mode>` 和 `/allow <category>`。
- 动态参数候选入口：`slash_argument_choices(...)`，当前 provider 为 `tools` 和 `sessions`。
- Tool Runs 查询入口：`ConversationQueryService` 和 `/tool-runs [recent|summary|show <id>]`，返回 `CommandResult.kind="tool_runs"` 与结构化 payload。

## 已实现 / 归档需求

Activity Runtime 已由后端实现，并已被 inline TUI 消费。当前权威接口以 `BACKEND_INTERFACE.md` 的 Activity Runtime 章节为准；本节保留为需求来源和字段背景，不再表示待实现事项。

### P1：Activity 稳定结构化接口（已实现）

目标：inline TUI / future desktop-web 需要一个长期稳定的 Activity 接口，覆盖宏观总览和具体对象详情。这个接口不应只是临时 doctor 字段，而应成为后续一段时间内前端展示“系统正在做什么”的稳定数据契约。

Activity 覆盖三个层级不同的运行对象：

- `sub_agent`：主 agent 内部委派出去的子任务。
- `background_process`：工具层启动的后台进程。
- `gateway_agent`：平台 gateway 收到消息后启动的一次主 agent 处理流程；它不是子 agent。

前端长期展示模型：

- 状态栏显示轻量 badge，例如 `activity 3` 或 `activity 3 !`。
- `/activity` 打印 summary + 三类列表到 scrollback。
- `/activity agents <id>`、`/activity processes <id>`、`/activity gateway <id>` 打印详情到 scrollback。
- future desktop/web 可用同一 payload 做 activity drawer / detail panel。

后端应提供：

- slash command：`/activity [agents|processes|gateway] [id]`
- `CommandResult.kind="activity"` 的结构化 payload
- 等价 runtime/query API，供 future desktop/web 直接调用
- slash registry metadata 和动态候选，供 inline TUI 连续选择

#### `/activity` summary payload

`/activity` 返回完整 overview。列表应轻量，但足够让前端直接渲染，不再额外调用详情接口。

```json
{
  "summary": {
    "has_active_work": true,
    "active_total": 3,
    "attention_required": false,
    "longest_running_seconds": 34.6,
    "counts": {
      "sub_agents": {
        "active": 1,
        "recent": 12,
        "failed_recent": 1,
        "stop_requested": 0
      },
      "background_processes": {
        "total": 2,
        "running": 1,
        "done": 1,
        "killed": 0
      },
      "gateway_agents": {
        "running": 1,
        "stop_requested": 0
      }
    }
  },
  "sub_agents": {
    "active_runs": [],
    "recent_runs": []
  },
  "background_processes": {
    "items": []
  },
  "gateway_agents": {
    "running_agent_runs": []
  }
}
```

#### 列表 item 最小公共字段

每个列表 item 必须尽量保持同一公共外形，方便前端做统一 activity list：

- `id: string`
- `kind: "sub_agent" | "background_process" | "gateway_agent"`
- `status: string`
- `duration_seconds: number`
- `stop_requested: boolean`
- `error: string`
- `attention_required: boolean`

`status` 应稳定在：

- `running`
- `completed`
- `failed`
- `stopped`
- `stopping`

时间字段应统一：

- `started_at` / `finished_at`：ISO string 或 unix seconds 二选一，由后端统一。
- `duration_seconds`：必须提供，前端主要显示它。

#### sub agent 列表字段

```json
{
  "id": "abc123",
  "kind": "sub_agent",
  "run_id": "abc123",
  "status": "running",
  "role": "reviewer",
  "task": "检查 provider cache 实现",
  "task_preview": "检查 provider cache 实现",
  "started_at": "2026-07-06T22:10:00Z",
  "finished_at": "",
  "duration_seconds": 18.4,
  "stop_requested": false,
  "attention_required": false,
  "usage": {
    "input_tokens": 1000,
    "output_tokens": 200
  },
  "quota": {
    "used_tokens": 1200,
    "max_tokens": 4096,
    "over_token_quota": false
  },
  "tool_counts": {
    "requested": 2,
    "executed": 1,
    "denied": 1
  },
  "result_preview": "",
  "error": ""
}
```

前端列表会优先展示：

- role
- status / duration
- task_preview
- usage / quota
- tool_counts
- result_preview / error

#### background process 列表字段

```json
{
  "id": "3",
  "kind": "background_process",
  "pid": 3,
  "status": "running",
  "command": "uv run pytest -q",
  "command_preview": "uv run pytest -q",
  "cwd": "<workspace>",
  "started_at": "2026-07-06T22:10:00Z",
  "finished_at": "",
  "duration_seconds": 23.1,
  "returncode": null,
  "stop_requested": false,
  "attention_required": false,
  "has_stdout": true,
  "has_stderr": false,
  "stdout_truncated": false,
  "stderr_truncated": false,
  "stdout_bytes": 1200,
  "stderr_bytes": 0,
  "output_preview": "tests/test_runtime.py ...",
  "error": ""
}
```

列表里只需要 preview / bytes / truncated；完整 stdout/stderr 放详情 payload，避免 summary 太重。

前端列表会优先展示：

- command_preview
- cwd
- status / duration / returncode
- output_preview
- stdout/stderr bytes and truncated flags

#### gateway agent 列表字段

```json
{
  "id": "telegram:c1:u1",
  "kind": "gateway_agent",
  "session_key": "telegram:c1:u1",
  "platform": "telegram",
  "chat_id": "c1",
  "user_id": "u1",
  "status": "running",
  "started_at": "2026-07-06T22:10:00Z",
  "finished_at": "",
  "duration_seconds": 34.6,
  "stop_requested": false,
  "attention_required": false,
  "error": ""
}
```

前端列表会优先展示：

- platform
- session_key
- status / duration
- stop_requested
- error

#### Detail payload

详情接口是稳定能力，不是临时补充。列表用于 overview，详情用于完整内容、日志、工具明细或错误排查。

```json
{
  "kind": "sub_agent",
  "id": "abc123",
  "run": {}
}
```

Sub agent detail 的 `run` 建议包含：

- `run_id`
- `status`
- `role`
- `task`
- `result`
- `tool_calls`
- `executed_tool_calls`
- `denied_tool_calls`
- `tool_results`
- `usage`
- `quota`
- `error_type`
- `error_message`

```json
{
  "kind": "background_process",
  "id": "3",
  "process": {}
}
```

Background process detail 的 `process` 建议包含：

- `pid`
- `status`
- `command`
- `cwd`
- `stdout`
- `stderr`
- `stdout_truncated`
- `stderr_truncated`
- `returncode`
- `error`

```json
{
  "kind": "gateway_agent",
  "id": "telegram:c1:u1",
  "gateway_run": {}
}
```

Gateway detail 可以和列表基本一致，但应保留稳定 detail 入口，方便后续扩展平台消息 metadata。

#### Slash command / dynamic candidates

建议 registry 暴露：

```text
/activity
/activity agents
/activity agents <id>
/activity processes
/activity processes <id>
/activity gateway
/activity gateway <id>
```

建议动态 provider：

- `activity_agents`：返回 active + recent sub agent ids。
- `activity_processes`：返回 running + recent process ids。
- `activity_gateway`：返回 running gateway session ids。

候选项仍使用现有 slash argument choice 外形：

```json
{
  "value": "abc123",
  "label": "abc123",
  "description": "reviewer · running · 18s",
  "append_space": false
}
```

#### 前端展示假设

- `summary.attention_required` 由后端计算，前端不自行猜测。
- `summary.active_total` 是状态栏 badge 的主要数字。
- `summary.has_active_work=false` 时，前端显示 `activity idle`。
- `kind` 字段用于前端做统一 activity list 和 detail 路由。
- `summary.counts` 用于 overview，具体列表数据以各类 lists 为准。
- `attention_required=true` 时前端会显示 `!` 或更高亮的状态，但不自行推断原因。

## 已提供：Slash command 参数候选 metadata

当前 inline TUI 已能消费 slash command registry 的一级命令和 children。后端现在也把“参数阶段候选”放进 registry / runtime 接口，前端可以做连续选择：

```text
/mode
→ /mode set
→ Read Only / Ask First / Edit Freely / Full Auto
→ /mode set Ask First
```

任何 command / child spec 都可以声明可选的 `arguments` metadata：

```python
{
    "name": "set",
    "usage": "/mode set <mode>",
    "summary": "切换执行模式",
    "arguments": [
        {
            "name": "mode",
            "kind": "choice",  # choice | dynamic
            "choices": [
                {"value": "Read Only", "label": "Read Only", "description": "只读"},
                {"value": "Ask First", "label": "Ask First", "description": "执行前确认"},
                {"value": "Edit Freely", "label": "Edit Freely", "description": "可编辑"},
                {"value": "Full Auto", "label": "Full Auto", "description": "全自动"},
            ],
        }
    ],
}
```

已提供静态候选：

- `/mode set <mode>`: `Read Only`, `Ask First`, `Edit Freely`, `Full Auto`
- `/allow <category>`: `write`, `bash`, `background`, `network`, `destructive`, `all`

已提供动态候选入口。registry 暴露 `kind="dynamic"` 和 `provider`，runtime 提供统一查询入口：

```python
async def slash_argument_choices(
    runtime,
    provider: str,
    *,
    command: str,
    args: tuple[str, ...] = (),
    query: str = "",
    limit: int = 20,
) -> list[dict]: ...
```

候选 dict 建议至少包含：

```python
{
    "value": "run_abc123",          # 插入到命令行里的真实值
    "label": "run_abc123",          # 菜单显示文本；可等于 value
    "description": "python-expert · running",  # 可为空
    "append_space": False,          # 选中后是否追加空格，默认 False
}
```

当前动态 provider：

- `/session switch <name>` / `/session delete [name]`: session names
- `/tools show <name>`: tool names

后续适合补充的动态 provider：

- `/agents show <run_id>`: recent agent run ids
- `/memory show <id>` / `/memory delete <id>`: memory ids

前端消费语义：

- 当输入已经完整匹配某个 command / child，且下一个参数有候选时，继续显示同一个 slash menu。
- Enter 在菜单打开时只插入当前候选，不立即执行命令。
- 当命令已经完整且没有后续候选时，菜单收起；下一次 Enter 执行命令。
- `value` 是实际插入文本，`label` / `description` 只用于展示。

## P2：更专门的预览字段

当前前端已经消费通用预览字段，并能按 bash / network / path 做基础展示。后续如果要把写文件、补丁、网络请求做得更细，可以继续补以下可选字段：

- `write` / `edit`: `diff_summary`, `diff_preview`
- `bash`: future shell-specific safety summary if needed
- `network`: request body/header preview if future tools expose it

这些字段都应允许为空；前端会按存在字段渐进展示。

## 前端自行待办

以下不需要后端接口先行：

- 暂缓：在当前 turn 内做更完整的多工具结果列表和展开 UI。用户感兴趣，但之前尝试失败过，先不作为当前优先项。
- 已有基础策略：长工具输出在摘要行提示 `Ctrl+O 展开`，完整输出作为新的展开块打印到当前 scrollback 位置。后续只调整阈值、样式和复制体验。
- 继续调整确认面板的视觉密度、风险层级和键盘提示。
- 保持工具 trace 简洁化，避免 raw JSON、过度拟人化描述和过多中文标签。
- 已有基础策略：输入框增加低调背景和左侧强调；输入 `/` 时隐藏底部快捷键，并只在存在候选或二级命令时显示命令区域，候选来自后端 slash command registry。

当前不推进：

- 状态行中文化。

## 维护规则

- 需求被后端提供且前端已经消费后，从“当前活跃需求”移除。
- 已完成项只在“当前状态”里做简短归档，不保留字段级实现清单。
- 如果一个需求被明确暂缓，标注“暂缓”，避免被当作当前阻塞项。
