# 小鹿运行时测试手册

本文只给小鹿使用。小鹿不需要运行 pytest、读取源码、执行 shell，也不需要修改配置。测试方式是：在当前私聊中调用已经暴露的只读工具，观察结果，并把异常按格式汇报给用户。

## 一、测试边界

小鹿可以做：

- 查看 live runtime、插件、平台、会话、记忆、审计和结构化日志的安全摘要。
- 发起一轮普通对话，然后检查这一轮是否被正确记录。
- 用关联字段查询同一轮的日志、审计和 Tool Run。
- 检查配置是否已配置、来源是否正确、格式是否有效。

小鹿暂时不做：

- 不执行 CLI、pytest、shell 或 Python。
- 不读取 `.env`、token、Cookie、密钥和原始日志文件。
- 不修改配置，不安装/卸载/重载插件，不做环境 GC，不执行记忆迁移 `--apply`。
- 不模拟其他用户、群聊或未配置平台，因此不测试“非 owner 是否被拒绝”。
- 不主动制造 Worker 崩溃、平台断线或热重载失败；只检查当前状态和已有错误记录。

所有测试均为只读，除“发送一条普通测试消息”外不改变系统状态。发送消息前应使用用户当前私聊会话，不要向其他平台或其他用户发送。

## 二、工具清单

| 工具 | 调用方式 | 用途 |
| --- | --- | --- |
| `runtime_inspect` | 无参数 | 查看当前 live runtime 总体摘要 |
| `conversation_inspect` | `action`、`session_key`、`limit`、`item_id` | 查看本轮、Turn Report、Tool Run |
| `plugin_inspect` | `action`、`plugin_key` | 查看插件和 Worker/generation 状态 |
| `platform_inspect` | `action`、`platform` | 查看平台连接状态 |
| `config_inspect` | `action`、`key` | 查看脱敏配置状态 |
| `memory_inspect` | 无参数 | 查看 Memory provider、索引和 review 状态 |
| `audit_inspect` | `event`、`tool`、`trace_id`、`session_key`、`limit` | 查询有限审计记录 |
| `logs_query` | `level`、`logger_name`、`trace_id`、`limit` | 查询有限结构化日志 |

工具返回 `reason_code` 或错误时，先记录原样字段，再继续其他只读查询；不要通过读取文件绕过工具。

## 三、标准测试流程

### 1. Runtime 基线

调用：

```json
{"tool": "runtime_inspect"}
```

检查：

- `source` 必须是 `live`。
- 当前 `schema_version` 为 `2`；插件 runtime 中的 Worker 数应与 `plugin_inspect` 一致。
- `captured_at` 应接近当前时间。
- `status` 为 `healthy` 或有明确 `warnings` 的 `degraded`。
- `runtime.core_ready` 为真。
- MCP、插件、conversation、delivery、memory 摘要之间不能出现明显矛盾。

如果返回 `core_not_ready`、`boot_failed` 或 `status=failed`，停止后续结论，先汇报 Runtime 基线失败。

### 2. 配置和平台观察

依次调用：

```json
{"tool": "config_inspect", "action": "summary"}
{"tool": "config_inspect", "action": "field", "key": "auth.enabled"}
{"tool": "config_inspect", "action": "field", "key": "auth.owner_ids"}
{"tool": "platform_inspect", "action": "list"}
```

检查：

- 配置 summary 没有未解释的 error。
- `auth.enabled`、owner 平台数量和当前项目约定一致。
- 配置字段只显示脱敏状态、来源和有效性，不出现 token 或完整 secret。
- `auth.owner_ids` 只能显示已配置平台和数量，不显示实际 owner ID。
- 平台状态区分 `connected`、`disabled`、`error` 和“未配置”，不能把未配置说成运行故障。

不能由小鹿验证的认证项：非 owner、群聊、未授权平台的拒绝顺序。小鹿只报告当前配置和当前会话实际表现，不推断未测试结论。

### 3. 插件状态

调用：

```json
{"tool": "plugin_inspect", "action": "list"}
```

从列表中挑选用户关心的插件，再调用：

```json
{"tool": "plugin_inspect", "action": "info", "plugin_key": "<plugin_key>"}
```

检查：

- `status`、`runtime_state`、generation 和 Worker 状态一致。
- 外置插件显示 sandbox backend、environment、PID/Worker 状态和最近错误摘要。
- 主动插件显示 active runner 是否启用、最近心跳/事件/错误和目标 session。
- `markdown-structure-analyzer`、`document-converter`、`workspace-watch` 等已迁移插件版本和状态正确。
- 看到 `ERROR` 时，继续用 `info` 查看依赖、manifest、environment 和最近错误，不直接判定为代码故障。

不能由小鹿单独验证的项：主动重载、Worker 崩溃恢复、真实沙箱逃逸测试。小鹿只检查当前诊断中是否存在 recovering、unhealthy、restart_count、circuit breaker 或 sandbox 错误。

### 4. 普通对话链路

在当前私聊发送一条不会触发写工具的简单消息，例如“请回复测试成功”。收到回复后调用：

```json
{"tool": "conversation_inspect", "action": "recent_turns", "limit": 3}
{"tool": "conversation_inspect", "action": "tool_runs", "limit": 10}
```

检查：

- 最新 Turn 的 session key 是当前会话，不是其他会话。
- Turn 状态、完成标记、响应摘要和时间合理。
- 如果本轮没有工具调用，Tool Run 为空是正常结果。
- 如果本轮有工具调用，Tool Run 的状态、工具名、耗时和错误与事件中看到的结果一致。

如果需要查看单条持久化记录，使用返回的 `id`：

```json
{"tool": "conversation_inspect", "action": "turn", "item_id": <turn_report_id>}
{"tool": "conversation_inspect", "action": "tool_run", "item_id": <tool_run_id>}
```

### 5. 关联审计、日志和 Tool Run

从最近 Turn、Tool Run 或当前事件中取出已有的 `trace_id`、`turn_id` 或 `session_key`。只使用实际返回的值，不自行编造 ID。然后调用：

```json
{"tool": "audit_inspect", "trace_id": "<trace_id>", "limit": 20}
{"tool": "logs_query", "trace_id": "<trace_id>", "limit": 20}
{"tool": "conversation_inspect", "action": "tool_runs", "session_key": "<session_key>", "limit": 20}
```

检查：

- 相同 trace/session/turn 下的记录属于同一轮操作。
- 审计包含工具摘要、事件类型、成功状态和关联字段。
- 日志包含时间、级别、logger、消息和可用关联字段。
- 审计和日志中的敏感文本已脱敏。
- 没有记录不等于失败：可能是审计关闭、该操作没有工具调用或日志尚未产生；应结合 Runtime 状态判断。

### 6. Memory 观察

调用：

```json
{"tool": "memory_inspect"}
```

只检查健康摘要：

- 当前 effective provider 是否符合配置。
- external/builtin provider 是否 ready 或明确 degraded。
- pending migration、pending index、review 状态和最近错误是否持续增长。
- canonical owner 字段存在且没有按每个外部 user ID 无限分裂的迹象。

小鹿不读取记忆原文、不执行 owner scope 迁移、不修改索引；发现 pending 或 provider 错误时只汇报数量、状态和 reason，不自行修复。

## 四、失败判断

只有满足以下条件才报告为“运行故障”：

1. 工具返回明确 `ok=false`、`status=failed` 或结构化 `reason_code`；并且
2. 同一问题在第二个相关接口中得到印证，或连续两次查询仍存在；并且
3. 不是“未配置平台”“没有历史记录”“当前会话没有工具调用”等正常空状态。

单个 `ERROR`、一次空列表、一次超时或缺少可选平台配置，只能报告为“待观察”，不能直接下故障结论。

## 五、汇报格式

每次测试结束使用下面格式：

```text
测试范围：runtime / config / platform / plugin / conversation / memory / audit
结果：PASS / DEGRADED / FAIL

已检查：
- 调用的工具和 action：
- 关键状态：
- 是否与上次结果一致：

问题（没有则写“无”）：
- 现象：
- 预期：
- 实际：
- reason_code：
- trace_id / turn_id / session_key：
- 是否连续两次复现：

未测试项：
- 明确写出不能通过当前只读接口验证的内容，不要猜测。

安全确认：未读取或回显 secret；未执行写操作。
```

## 六、判断优先级

遇到问题时按这个顺序收集信息：

1. `runtime_inspect`
2. 对应领域的 inspect：plugin / conversation / platform / config / memory
3. 用已有关联 ID 查 `audit_inspect` 和 `logs_query`
4. 最后给出 PASS、DEGRADED 或 FAIL，以及证据字段

小鹿的任务是收集证据和判断状态，不是自行修改配置或修复系统。
