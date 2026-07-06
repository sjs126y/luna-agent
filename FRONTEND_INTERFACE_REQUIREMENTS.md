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

## 当前活跃需求

暂无 P0 阻塞项。

## P1：Tool Runs 查询入口（暂缓）

当前 inline TUI 已能消费事件流里的工具结果摘要和确认结果。短期内如果后端不做工具结果持久化，前端可以先不开发历史工具结果面板。

后续如果要做 Ctrl+O 展开最近工具完整输出、future desktop 工具运行历史、或从 denied/error 跳转到完整审计详情，前端仍需要一个 runtime 层查询入口，不直接访问 Database。

建议接口形态：

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
- 保持工具 trace 简洁化，避免 raw JSON 和过度拟人化描述。
- 已有基础策略：输入框增加低调背景和左侧强调；输入 `/` 时预留固定命令区域，输入框上移，后续命令内容等后端能力稳定后再细化。

当前不推进：

- 状态行中文化。

## 维护规则

- 需求被后端提供且前端已经消费后，从“当前活跃需求”移除。
- 已完成项只在“当前状态”里做简短归档，不保留字段级实现清单。
- 如果一个需求被明确暂缓，标注“暂缓”，避免被当作当前阻塞项。
