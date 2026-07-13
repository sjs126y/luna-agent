# Lumora 后续架构方向

更新时间：2026-07-12

## 1. Lumora：已有基础与真实缺口

Lumora 已经有完整的 Agent 基础：工具注册、工具集和渐进式工具披露；执行模式、权限、确认、沙箱与审计；平台 Gateway、会话路由、附件处理和压缩链；结构化多模态模型；Cron、后台进程、子 Agent、工作流和长期运行的 MCP runtime；以及可注册工具、平台、MCP、skill、工作流、hook 和命令的插件系统。

下一阶段不该重写这些系统，而是改善生命周期边界：

1. **已完成基础**：被动插件的作用域配置、Registry 归属、冲突检查和失败回滚；运行期热替换留待 Manager reconcile。
2. **已完成**：MCP transport、单 server runtime、连接恢复、动态工具快照和结构化结果。
3. **待推进**：完整的“入站媒体 -> 模型/工具 -> 出站媒体”路径。
4. **待推进**：内部 turn 分发、Outbox/Delivery 和真正的主动决策系统。
5. **已完成**：长期记忆与知识 RAG 已拆分；RAG 不再由 memory provider 承担。

## 2. Hermes 的工具与插件模型

Hermes 将工具分成三层：

- `ToolRegistry`：保存工具 schema、handler、toolset、`check_fn` 和元数据。
- `toolsets.py`：声明工具分组和组合方式。
- `model_tools.py`：计算当前哪些工具对模型可见，并生成 schema。

内建工具通过扫描 `tools/*.py` 顶层的 `registry.register(...)` 调用发现，再只导入相应模块。工具已注册不代表模型一定可见：`check_fn` 会在构建模型工具 schema 时决定可见性。

插件加载过程是：发现 manifest -> 判断来源、kind 和启用策略 -> 导入模块 -> 调用 `register(ctx)` -> 注册工具、hook、命令、中间件等。普通插件只需 `register(ctx)`；平台、memory provider、model provider 等替换型子系统应使用专用 contract。

插件工具应进入统一 ToolRegistry，因此继续受 toolset 过滤、权限、审计、分发和 hook 约束。好的宿主边界包括：延迟导入重型平台插件、明确内建工具覆盖策略、默认不向插件暴露原始模型凭据的 LLM facade，以及核心定义的 hook 点。

注册 hook 只是订阅已有核心事件。只有核心代码实际调用 `plugin_manager.invoke_hook("...")`，hook 才会运行；新增 hook 点仍需修改核心代码。普通插件加载不是可靠的进程内热重载模型；真正热替换需要版本代际、快照、租约和排空旧资源的设计。

## 3. MCP

状态：**核心 runtime 已完成（2026-07-12）**。

Hermes 使用外部 MCP server 的路径：配置 `mcp_servers` -> connect、initialize、list_tools -> 包装为 `mcp__server__tool` -> 注册到 ToolRegistry -> 刷新 Agent 工具快照 -> 模型工具调用 -> MCP `call_tool`。

它使用一个共享的 MCP event-loop 后台线程；每台 server 在这个 loop 内有自己的 asyncio task。HTTP/SSE 是异步网络 I/O；stdio 每台 server 会启动一个子进程，但不是一台 server 一个线程。

Hermes 处理的长连接能力包括：握手超时、带降级的 keepalive ping、重试/重连、熔断、`tools/list_changed` 动态刷新、OAuth、sampling 与 elicitation。

Lumora 当前实现：

```text
MCPManager
  -> 每台 server 一个 MCPServerRuntime
  -> SDK-backed MCPConnection
     -> stdio / Streamable HTTP
  -> MCPToolRegistrar
     -> 快照 diff -> ToolRegistry
```

已完成能力：

- 官方 MCP Python SDK 稳定 v1.x，Lumora connection contract 隔离 SDK 类型。
- stdio 与 Streamable HTTP transport，旧 stdio 配置保持兼容。
- 单 server 生命周期、故障隔离、keepalive ping、自动重连和退避。
- `tools/list_changed` 通知、工具快照 diff 和 Registry generation 刷新。
- 断线时保留工具归属并标记 unavailable，恢复后自动重新可见。
- 环境变量 header、HTTP URL 校验、禁用重定向和凭据安全诊断。
- text、image、audio、resource 与 structured content 的结构化结果；事件、审计和数据库只保存安全摘要。
- doctor 展示 state、transport、工具数、重连次数、下一次重试和最近错误。

暂不纳入核心完成范围：

- OAuth 用户交互。
- sampling 和 elicitation policy。
- 旧 SSE transport 兼容。
- MCP server 模式。
- 将 MCP 用作多 Agent 控制平面。

这些能力只有在出现明确 server 或产品需求时再单独设计，不影响当前 MCP runtime 的完成状态。具体决策见 `docs/mcp-runtime-design.md`。

Hermes 作为 MCP server 时是消息/会话桥，不是多 Agent 控制平面。真正的控制平面应提供 `agent_submit`、`agent_status`、`agent_wait`、`agent_result`、`agent_cancel`、`task_claim`、`task_update`、`artifact_read` 和 `artifact_write` 等 API。

## 4. Cron 与主动能力

Cron 只是按时运行；主动能力是判断“是否值得打扰用户”。

Lumora 当前 cron 会进入 `Gateway._handle_message()`，因而复用 Agent、会话上下文、工具、hook、权限和压缩链，适合对话摘要和上下文提醒。但定时任务不是实际的平台入站消息，这条路径还会执行命令识别、确认处理和忙碌检查等不相关阶段。

更干净的目标结构是：平台入站经过规范化、授权和命令处理，再调用 `dispatch_turn(...)`；cron 则构造 `InternalTurnRequest(session_key, prompt, origin, delivery_target, execution_mode="scheduled")`，也调用 `dispatch_turn(...)`，最后由独立的投递策略处理 `TurnResult`。

这样保留 Lumora 的上下文优势，又不把 cron 伪装成用户消息。Hermes 的短生命周期隔离 Agent 适合独立后台任务，但默认不继承原会话和用户记忆，对“总结我们的对话”或强上下文提醒较弱。

真正的主动系统应是：触发器（时间、平台事件、任务状态、记忆复盘、外部事件）-> 候选事项 -> 策略（去重、冷却、静默时间、优先级、用户偏好）-> 决策（先规则，只对少量候选调用 LLM）-> Outbox/Delivery（发送、重试、审计、取消、用户反馈）。

## 5. 多模态

入站和出站多模态是两条独立链路。

入站路径：平台 payload -> 下载、校验和缓存附件 -> `MessagePart`/`AttachmentRef` -> provider 特定模型内容或视觉/转写结果 -> Agent context。

出站路径：Agent/tool -> `OutgoingMessage` 加附件引用 -> 平台能力检查 -> 图片、音频、视频或文档发送器 -> caption、格式、重试和投递处理。

入站把外部输入视为不可信，需处理 URL/重定向安全、MIME 校验、大小限制和临时 URL。出站关注路径授权、平台能力、caption、格式限制、速率限制和重试。

Hermes 用 `MEDIA:/actual/local/file.png` 这样的文本指令表达媒体，并从可见文本中拆出本地路径后原生发送。优点是任何返回文本的工具都能参与媒体投递；缺点是附件意图和自然语言混在同一字符串。

Lumora 已有结构化 `MessagePart` 与 `AttachmentRef`，架构上更好，应保留它。可以借鉴 Hermes 的工程细节：共享媒体缓存、输入大小限制、SSRF/重定向防护、MIME 和扩展名分类、各平台音频/语音差异、单/多附件 caption 策略、原生附件投递与降级。

## 6. Memory 与 RAG 方向

不要因为都能 embedding，就把所有可检索数据混进一个向量集合。

- 当前聊天历史是有序的会话上下文，不应主要依赖向量检索。
- 会话摘要/压缩是与会话和压缩谱系绑定的恢复上下文。
- 长期用户记忆保存可编辑的事实、偏好、承诺、关系和任务状态。
- 知识 RAG 保存原始外部证据，如文档、代码、网页、PDF 和证据 chunk。

RAG 检索原始外部证据；长期记忆保存会影响 Agent 行为、且可更新的状态。

该方向已经完成第一阶段重构：

- internal Markdown、buffer、managed block、revision 和 Agent 固定快照位于核心。
- SQLite archive 保存 review checkpoint、observation、外部记忆历史、内部 buffer 和 provider 状态。
- AppRuntime-owned asyncio worker 取代 daemon thread review。
- `memory/lumora` 使用两次 Memory LLM 调用、百炼 embedding、Qdrant、FTS5/BM25 和 RRF。
- `memory/mem0` 直接适配官方依赖。
- 核心 fallback 在主 provider 不可用时保存 observation，并在恢复后迁移。

后续知识 RAG 作为独立通用插件设计，不恢复 `memory_ingest`，也不与个人记忆共用集合和更新语义。

## 7. 当前推进状态

| 方向 | 当前基础 | 主要缺口 | 状态 |
| --- | --- | --- | --- |
| MCP runtime | stdio、Streamable HTTP、重连、动态工具、结构化结果、诊断 | OAuth、sampling、elicitation 仅按需补充 | 核心完成 |
| 被动插件 | `register(ctx)`、作用域配置、Skill/MCP 文件注册、所有权、冲突检查和事务回滚 | Manager reconcile、异步资源关闭和版本代际 | 基础完成 |
| 出站多模态 | `OutboundMessage`、`PlatformCapabilities`、MCP/tool artifact 已有骨架 | TurnResult artifact、平台原生发送、降级、安全和投递审计 | 适合下一步讨论 |
| 主动能力 | Cron 可复用会话和 Agent 主链路 | `InternalTurnRequest`、统一 dispatch、Delivery/Outbox、去重和策略 | 待推进 |
| Memory / RAG | internal snapshot、buffer、Lumora/Mem0、fallback、混合检索和审计 | 知识 RAG 后续作为独立插件 | 记忆重构完成 |

方向之间的关系：

```text
结构化 tool artifact（已完成）
  -> 出站消息与 Delivery
     -> Outbox
        -> 主动提醒和后台结果投递

插件 ownership 与事务注册（已完成基础）
  -> Manager reconcile 与运行期可靠 disable/unload

Memory / RAG 拆分（独立领域）
  -> 长期记忆更新审计
  -> 外部知识证据检索
```

当前更自然的后续讨论方向是出站消息与 Delivery。MCP 已能产生结构化 artifact，平台侧也已有 `OutboundMessage` 和能力声明；补齐这条链路既能形成直接用户价值，也能为后续 Outbox 和主动能力提供统一投递边界。插件后续只在确有运行期热替换需求时再增加 Manager reconcile 和版本代际。
