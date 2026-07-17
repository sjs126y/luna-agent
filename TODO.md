# Lumora TODO

更新时间：2026-07-17

## Conversation Runtime 后续项

1. 插件安装、卸载与热加载仍需正式 `RuntimeSnapshot + lease + drain`；本轮只完成已加载插件的能力约束 submit/notification 端口。
2. 出站多模态仍需将 Agent/tool artifact 转成结构化 `OutboundMessage`，补平台 capability fallback、原生附件发送与逐分片投递状态。
3. 主动决策系统仍需候选事项、冷却、静默时间、优先级和反馈策略；Cron 目前只是正式的主动触发源，不等于主动决策。

已完成收尾：Adapter 旧队列、busy/旁路、发送重试和 Gateway 兼容 Agent 路径已经删除；会话顺序统一由 Coordinator 管理，发送重试统一由 Delivery Outbox 管理。

## 已完成：Execution Mode 与工具安全重构

状态：已完成实现，并通过真实 Gateway 场景复测。

1. 用户模式统一为 `Read Only`、`Ask First`、`Local Auto`、`Full Auto`，稳定 ID 分别为 `read-only`、`ask-first`、`local-auto`、`full-auto`。
2. Mode 由 filesystem/network permission profile 与 `on-request/never` approval policy 组合，不再依赖工具类别长期授权。
3. 每个 session 持有内存安全状态；切换 Mode、重置/删除会话或服务重启会清空授权。
4. 工具调用在 hooks 修改后冻结输入，再依次经过 hard precheck、工具/资源审批与 dispatch；审批后不会重新询问模型。
5. 工具审批支持 `auto/cached/prompt/deny`，工具与资源共享 `permissions.grant_ttl_minutes`。
6. 文件、网络、Bash、后台进程和 HTTP MCP 声明具体资源；`tool_call` 嵌套调用不能绕过统一 executor。
7. Bash/后台进程支持 `auto/bwrap/legacy` 后端；显式 `bwrap` 不可用时失败关闭，`doctor` 会报告自动降级。

## 已完成：MCP 安全收口

状态：已完成实现，并通过真实 GitHub MCP、sequential-thinking 与 Fetch stdio MCP 复测。

1. 未知 MCP 工具默认 `cached` 审批、不可并行、不可自动重试；支持 per-server 与 per-tool 本地覆盖。
2. stdio MCP 在应用 Runtime 中复用进程沙箱，默认写目录为 `data/mcp`，网络需 server 显式配置 `allow_network: true`。
3. HTTP MCP 默认要求 HTTPS；明文 HTTP 与私网目标分别需要 `allow_insecure_http`、`allow_private_network` 显式开启。
4. URL 检查覆盖 DNS 返回的全部地址，云元数据与 link-local 地址不可放行，HTTP redirect 继续关闭。
5. tools/list 分页、工具数量、schema、文本结果、structured content 和 artifact 都有进入上下文前的硬上限。
6. MCP header secret 与 stdio 显式环境变量继续只通过 Settings 边界注入。

## 后续安全项

1. Bubblewrap network namespace 在部分宿主环境不可用；当前会明确诊断并保留命令白名单边界，后续可评估 nftables/seccomp 或平台原生沙箱。
2. DNS 校验仍存在解析检查与实际连接之间的 TOCTOU 窗口；如需更强保证，应让 transport 连接已校验 IP 并校验 Host/TLS。
3. MCP 市场/自动安装尚未实现；未来需要版本固定、来源签名、安装预览与首次启用确认。
4. MCP tool annotations 尚未消费；未来只能作为收紧策略的提示，不能放宽本地配置。
