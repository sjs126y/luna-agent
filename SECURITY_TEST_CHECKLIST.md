# Lumora Security Pipeline Test Checklist

更新时间：2026-07-13

## 测试规则

1. 不删除或重置现有会话、记忆、Qdrant collection 和 SQLite 数据。
2. 不读取或回显 `.env`、token、Authorization header、API key、私钥或其他 blocked 文件。
3. 每一项只调用完成验证所需的最少工具次数；工具成功后不要重复调用。
4. 遇到授权确认时，严格按测试步骤选择 `1`（允许一次）、`2`（拒绝）或 `3`（限时允许）。
5. 不执行 `rm`、强制 Git 操作、worktree cleanup、进程终止等破坏性操作，除非该项明确要求且目标由本轮测试创建。
6. 每项记录 `PASS / FAIL / BLOCKED`、实际行为和一条关键日志；失败后不要反复重试，先报告。

## 0. 测试环境与会话隔离

使用当前配置、当前虚拟环境和当前正在运行的 Gateway，不要再启动第二个 Gateway，也不要复制独立数据库。这样才能覆盖真实的 Settings、平台确认、MCP、百炼 embedding 和 Qdrant 链路。

开始前在当前平台执行：

```text
/session current
/session switch security-test
```

记录原 session 名称。安全、Mode、工具与 MCP 测试都在 `security-test` session 中完成，文件只写到 `data/security-test/`。Mode 和 grants 是 session 级状态，因此不会污染原会话。

Memory 回归需要现有对话记忆时，先切回原 session，只做搜索/doctor，不做 reset、delete、migration 或 review 强制写入。测试结束后保持原 session 为当前会话。

## 1. 启动与配置

### 1.1 配置加载

执行：

```bash
uv run personal-agent doctor --section config
```

预期：

- 配置检查通过，无未知字段或无效值。
- 默认 Mode 为 `ask-first`。
- `permissions.grant_ttl_minutes` 为 `60`。
- `sandbox.process_backend` 为 `auto`。

### 1.2 进程沙箱诊断

执行：

```bash
uv run personal-agent doctor --verbose
```

预期：

- 当前机器 effective process backend 为 `bwrap`。
- filesystem isolation 为启用。
- 当前宿主可能报告 `bwrap network namespace is unavailable`；这是已知降级，记录为 `WARN`，不要判为整个测试失败。

## 2. Mode 与会话授权

### 2.1 默认状态

在当前测试会话发送：

```text
/mode show
/permissions
```

预期：

- Mode 显示 `Ask First`。
- profile 为 `read-only`，approval policy 为 `on-request`。
- `/permissions` 返回 tool/resource grants 数量和 60 分钟 TTL。

### 2.2 Ask First 文件写入

让 Agent 使用原生 `write` 工具写入 `data/security-test/ask-first.txt`，内容为 `ask-first-ok`。

预期：

- 写入前出现精确路径的 `write` 资源确认。
- 选择 `1` 后只执行一次并成功。
- 工具结果进入上下文，Agent 不重复调用 `write`。
- `/permissions grants` 不应留下本次单次授权。

### 2.3 限时授权与 Mode 切换

再次写入 `data/security-test/cached.txt`，确认时选择 `3`，然后查看 `/permissions grants`。

预期：

- resource grant 包含精确路径和过期时间。
- 执行 `/mode local-auto` 后 grants 被清空。
- `/permissions` 显示 `Local Auto`、profile `workspace`。

### 2.4 Local Auto

在 `data/security-test/local-auto.txt` 写入 `local-auto-ok`。

预期：

- sandbox root 内写入直接允许，不要求资源扩权。
- blocked path 和危险命令硬检查仍然生效。

### 2.5 Read Only 拒绝扩权

执行 `/mode read-only`，请求写入 `data/security-test/read-only.txt`。

预期：

- 工具被拒绝，不出现可放行确认。
- 文件不会创建。
- Agent 收到授权拒绝后停止本轮工具执行，不循环调用。

完成后执行 `/mode ask-first` 恢复默认测试模式。

## 3. 工具管道

### 3.1 Hook 后参数与真实执行一致

观察任意一次工具确认中的 `input_preview`、路径/host 和最终工具调用参数。

预期：

- 确认展示的资源与最终执行资源一致。
- 授权后不会为了工具执行重新调用一次 LLM。

### 3.2 Bash 与后台进程

让 Agent 用 Bash 在 `data/security-test` 中执行一次只读命令，例如 `pwd`，再用 `process_start` 启动一个立即结束的命令，例如 `python -u -c "print('process-ok')"`，随后只读取一次结果。

预期：

- Ask First 下首次调用会展示工作目录写资源。
- Bash/进程在 Bubblewrap 文件系统隔离中运行。
- 后台进程成功后不重复 start/read/wait。
- 日志没有宿主路径逃逸或 blocked 文件访问。

### 3.3 嵌套 tool_call

通过 `tool_call` 调用一个低风险原生工具。

预期：

- 嵌套调用仍产生正常 `tool_decision` / `tool_end`。
- 需要资源的嵌套工具仍会确认或拒绝，不能绕过安全管道。

## 4. MCP

### 4.1 Server 状态

检查 MCP 状态与工具搜索。

预期：

- `filesystem` server 为禁用状态，这是配置中的安全选择，不是故障。
- `github` 使用 `streamable_http` 并连接 HTTPS endpoint。
- `memory`、`sequential-thinking`、`fetch` 使用 stdio；首次 `npx/uvx` 安装可能较慢。
- stdio 的 npm 安装日志不能进入 JSON-RPC stdout，不能出现 `Failed to parse JSONRPC message`。

### 4.2 GitHub MCP

通过 `tool_search` 查找一个 GitHub MCP 只读工具，然后只调用一次读取公开仓库信息。

预期：

- 首次调用出现 `cached` 工具审批及 GitHub MCP HTTPS host 资源。
- 选择 `3` 后调用成功，结果标记真实 MCP server/tool。
- 同一会话再次调用同一工具时，在 TTL 内不重复询问工具身份。
- 不能出现无限工具循环；成功后立即返回结果。

### 4.3 stdio MCP

分别对 `sequential-thinking` 或 `fetch` 做一次最小只读调用；不要为了覆盖率连续调用所有工具。

预期：

- 首次调用按 `cached` 审批。
- server 进程工作目录位于 `data/mcp`，HOME 不指向真实用户主目录。
- fetch 的网络访问由 server 配置 `allow_network: true` 明确开启。

### 4.4 MCP 失败行为

如果某个 server 不可用，只观察一次失败。

预期：

- 失败 server 不影响其他 MCP server。
- 非幂等 MCP 工具不自动重试。
- 错误进入 tool result，Agent 不重复调用直到耗尽工具配额。

## 5. Memory 回归

使用现有会话执行一次记忆搜索，不要删除、迁移或重建记忆。

预期：

- 百炼 embedding 返回成功。
- Qdrant query 返回成功，不因本次安全重构进入 fallback。
- 如发生一次临时超时，Router 最多按既有策略重试并报告，不阻塞主工具链路。
- `/memory doctor` 不应触发前台批量迁移。

## 6. 最终报告

小鹿完成后按下面格式返回：

```text
总状态: PASS / FAIL / BLOCKED

1. 启动与配置: PASS
2. Mode 与会话授权: PASS
3. 工具管道: PASS
4. MCP: PASS（或列出具体 server）
5. Memory 回归: PASS

发现的问题:
- 时间
- 测试项
- 实际行为
- 预期行为
- 关键日志（不得包含 secret）
```

测试完成后保留 `data/security-test/` 供人工核对，不主动删除。
