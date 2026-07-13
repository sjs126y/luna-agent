# Lumora TODO

更新时间：2026-07-13

## 当前执行顺序

1. 先检查并修正 Execution Mode 与权限模型。
2. Mode 语义稳定后，再进行 MCP 安全收口。

MCP 安全策略依赖 Mode 对“允许、询问、拒绝”的定义。Mode 的具体问题尚未在本文件中定性，后续应结合实际行为、日志和预期语义单独分析，避免先在 MCP 层重复实现一套权限规则。

## P0：Execution Mode 与权限模型

状态：待优先分析。

当前只确认它是 MCP 安全改造的前置项，不在信息不足时猜测具体故障。后续需要先明确：

1. 各 Mode 对工具调用的默认权限和交互语义。
2. Mode、`/allow`、工具风险等级和用户确认之间的优先级。
3. 权限在单次调用、单轮对话、会话和全局范围内的生效边界。
4. 被拒绝、需要确认、超时和执行失败时，Agent 主循环应如何继续。
5. 内置工具、插件工具和 MCP 工具是否统一进入同一条权限与审计链路。

## P1：MCP 安全收口

状态：暂缓，依赖 P0；当前不修改 MCP 行为。

### 已确认风险

1. MCP 工具当前统一注册为 `is_parallel_safe=True`、`is_destructive=False`。远端写操作可能因此被错误分类、并行执行或进入非破坏性工具的自动重试。
2. stdio MCP 会在启动时以宿主用户权限执行配置中的命令，目前没有独立的进程沙箱和安装/首次启动确认。未来若接入插件市场或一键安装，远端插件可借此扩大到宿主机命令执行风险。
3. Streamable HTTP MCP 允许连接任意 `http` / `https` 地址，尚无私网地址、回环地址、DNS 重绑定等 SSRF 边界。HTTP 重定向已关闭，但还不足以形成完整防护。
4. MCP server 提供的工具描述、输入 schema 和结果都属于不可信内容，可能包含提示词注入或超大载荷。目前尚未统一限制工具数量、分页次数、schema 大小、结构化结果大小和二进制 artifact 大小。
5. stdio 环境变量会先过滤继承环境，再合并 server 显式配置的 `env`。显式配置仍可能把敏感变量注入第三方进程，需要由 Settings 和用户授权边界统一管理。
6. 官方 MCP tool annotations 目前未进入 Lumora 工具元数据。即便后续接入，`readOnlyHint`、`destructiveHint`、`idempotentHint` 和 `openWorldHint` 也只能作为不可信提示，不能直接替代本地策略。

### 当前已有保护

1. MCP 工具调用会经过统一 executor、hooks 和 audit 链路。
2. 已有调用超时、单轮工具调用上限、server 命名空间和 server 故障隔离。
3. HTTP header secret 已由 Settings 解析后显式注入，connection 不再主动读取 `.env`。
4. HTTP transport 不跟随重定向。
5. server 故障不会直接拖垮其他 MCP server。

### 后续设计约束

1. 在 Mode/权限模型之上增加 MCP 工具策略，不在 MCP 子系统中另造一套互相冲突的授权语义。
2. 未受信任的 MCP 工具默认按“需确认、不可并行、最多执行一次”处理；本地配置是最终权威。
3. 支持 per-server 与 per-tool 策略覆盖；server annotations 只提供初始提示，不能放宽本地安全配置。
4. stdio 首次启动应展示精确 command、args、工作目录和环境变量名称，并要求明确授权；插件市场来源还应考虑版本固定和进程沙箱。
5. HTTP transport 默认要求 HTTPS；回环、私网和其他敏感地址必须由用户显式允许，并防止解析后地址绕过。
6. 对 tools/list 分页、工具数量、schema、文本结果、structured content 和二进制内容设置明确上限，在完整载荷进入上下文或持久化之前截断或拒绝。
7. 所有 secret 只通过 Settings 的显式配置边界传入，不允许插件或 MCP connection 自行扫描进程环境和 `.env`。
8. 安全策略完成后补充权限矩阵、危险工具确认、并发/重试、stdio 启动授权、SSRF 和超大结果等测试。

### 参考资料

- [MCP Tools specification](https://modelcontextprotocol.io/specification/latest/server/tools)
- [MCP Security Best Practices](https://modelcontextprotocol.io/specification/latest/basic/security_best_practices)

## 本阶段明确不做

1. 不立即改动 MCP 注册、连接或执行逻辑。
2. 不在 Mode 语义明确前增加临时安全开关。
3. 不清理现有 MCP 配置或已安装 server。
4. 不改动用户的 `config.yaml` 和本机 secret。
