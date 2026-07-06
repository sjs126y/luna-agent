# Frontend Interface Requirements

更新时间：2026-07-06

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

## 当前活跃需求

暂无 P0/P1 阻塞项。

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
