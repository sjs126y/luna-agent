# MCP Runtime Design

状态：Accepted

## 目标

Lumora 将 MCP 从一次性 stdio 工具加载器演进为长期运行的外部能力 runtime，同时保留现有工具权限、安全、审计和渐进式披露语义。

## 决策

- 使用官方 MCP Python SDK 的稳定 v1.x 协议与 transport 实现，依赖约束为 `mcp>=1.27,<2`。
- Lumora 自己管理 server 生命周期、重连、工具快照、Registry 同步、健康状态和安全策略。
- SDK 类型只存在于 MCP connection adapter 内部；Manager、runtime 和工具层使用 Lumora 自己的模型。
- 每台 server 使用独立 `MCPServerRuntime`，单台故障不得影响其他 server。
- stdio 与 Streamable HTTP 实现同一个 connection contract。
- 旧配置没有 `transport` 且包含 `command` 时继续按 stdio 解释。
- 工具名继续使用 `mcp__{server}__{tool}`，并继续经过 `ToolExecutor`、permission 和 audit。
- 短暂断线时保留工具归属并标记 unavailable；server 被禁用、删除或确认移除工具时才注销。
- 工具列表变化通过完整快照 diff 同步，刷新失败保留最后一次成功快照。
- MCP 结果保留结构化 content，并提供当前工具协议可消费的文本 fallback。

## Connection Contract

```python
class MCPConnection(Protocol):
    async def connect(self) -> MCPServerInfo: ...
    async def list_tools(self) -> list[MCPToolSpec]: ...
    async def call_tool(self, name: str, arguments: dict) -> MCPCallResult: ...
    async def close(self) -> None: ...
```

SDK session、transport context manager 和 notification callback 均由 connection adapter 封装，其他模块不得直接依赖 SDK 类型。

## 兼容契约

- 保留 `MCPManager.start()`、`stop()`、`health_snapshot()`、`total_tools` 和 `client_names`。
- 保留现有 stdio `name`、`command`、`args`、`env`、`enabled` 配置。
- 保留 doctor 中 server 连接、工具数量、错误和 stderr 摘要字段。
- Registry generation 变化后，现有 Agent 继续自动刷新工具视图。

## 失败语义

- 工具调用不无限等待重连；连接不可用时返回稳定的 temporarily-unavailable 结果并唤醒后台恢复。
- 网络中断和 stdio 子进程退出进入自动重连。
- 配置错误和确定的认证错误停止自动快速重试，并在 health 中给出原因。
- runtime `stop()` 必须取消连接、通知、刷新和重连任务，且重复调用安全。

## 暂不包含

- MCP server 模式或多 Agent 控制平面。
- 通用插件热重载。
- 默认开放 sampling 或 elicitation。
- 完整 OAuth 用户交互。
- 每台 server 单独创建线程。
- 平台出站媒体发送。

## 验证标准

- 旧 stdio 配置与工具命名保持兼容。
- 单台 server 故障和恢复不影响其他 server。
- `tools/list_changed` 能增删和更新工具，失败时不丢失旧快照。
- Streamable HTTP 可以连接、调用、断线恢复和安全关闭。
- MCP 工具仍经过权限与审计链路。
- 应用关闭后没有遗留子进程、网络 session、后台任务或 Registry 工具。
