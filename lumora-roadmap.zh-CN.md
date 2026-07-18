<div align="center">

<h1>Lumora 后续架构方向</h1>

<p><strong>已完成什么、真实缺口在哪里、下一步为什么值得做</strong></p>

<p>
  <img src="https://img.shields.io/badge/MCP-core%20complete-2EA44F" alt="MCP complete">
  <img src="https://img.shields.io/badge/memory-refactor%20complete-2EA44F" alt="Memory complete">
  <img src="https://img.shields.io/badge/plugin%20runtime-hot%20reload%20ready-2EA44F" alt="Plugin runtime ready">
  <img src="https://img.shields.io/badge/multimodal-foundation%20complete-2EA44F" alt="Multimodal complete">
  <img src="https://img.shields.io/badge/active%20decision-planned-7C3AED" alt="Active decision planned">
</p>

<p>
  <a href="README.md">项目首页</a> ·
  <a href="docs/README.md">文档中心</a> ·
  <a href="TODO.md">当前待办</a> ·
  <a href="PROJECT_EVOLUTION.md">项目演进</a>
</p>

</div>

---

## 当前路线图

| 方向 | 当前状态 | 下一步 |
| --- | :---: | --- |
| MCP Runtime | Core complete | OAuth / sampling / elicitation 按需求补充 |
| Memory | Refactor complete | 知识 RAG 独立成插件，reranker 按需实现 |
| 被动插件 | Runtime complete | 插件生态、依赖策略与主动生命周期按需推进 |
| 出站多模态 | Foundation complete | 微信/QQ 实机反馈与格式体验 |
| Conversation Runtime | Complete | 继续真实使用与性能观测 |
| 主动决策 | Planned | 候选、冷却、静默时间、预算与反馈 |

## 1. Lumora：已有基础与真实缺口

Lumora 已经有完整的 Agent 基础：工具注册、工具集和渐进式工具披露；执行模式、权限、确认、沙箱与审计；平台 Gateway、会话路由、附件处理和压缩链；结构化多模态模型；Cron、后台进程、子 Agent、工作流和长期运行的 MCP runtime；以及可注册工具、平台、MCP、skill、工作流、hook 和命令的插件系统。

下一阶段不该重写这些系统，而是改善生命周期边界：

1. **已完成**：被动插件具备版本化安装、不可变 package、generation、能力快照、Turn lease、旧实例排空、热更新、回滚、禁用与卸载；主动插件决策生命周期后续单独设计。
2. **已完成**：MCP transport、单 server runtime、连接恢复、动态工具快照和结构化结果。
3. **已完成基础**：完整的“入站媒体 -> 模型/工具 -> Artifact -> 出站媒体”路径；后续按真实平台反馈补格式与 caption 体验。
4. **已完成基础**：统一 turn 分发、Delivery/Outbox、Cron 正式提交和主动插件端口；主动决策策略仍待推进。
5. **已完成**：长期记忆与知识 RAG 已拆分；RAG 不再由 memory provider 承担。

<details>
<summary><strong>展开设计背景：Hermes、MCP、主动能力、多模态与 Memory/RAG</strong></summary>

## 2. Hermes 的工具与插件模型

Hermes 将工具分成三层：

- `ToolRegistry`：保存工具 schema、handler、toolset、`check_fn` 和元数据。
- `toolsets.py`：声明工具分组和组合方式。
- `model_tools.py`：计算当前哪些工具对模型可见，并生成 schema。

内建工具通过扫描 `tools/*.py` 顶层的 `registry.register(...)` 调用发现，再只导入相应模块。工具已注册不代表模型一定可见：`check_fn` 会在构建模型工具 schema 时决定可见性。

插件加载过程是：发现 manifest -> 判断来源、kind 和启用策略 -> 导入模块 -> 调用 `register(ctx)` -> 注册工具、hook、命令、中间件等。普通插件只需 `register(ctx)`；平台、memory provider、model provider 等替换型子系统应使用专用 contract。

插件工具应进入统一 ToolRegistry，因此继续受 toolset 过滤、权限、审计、分发和 hook 约束。好的宿主边界包括：延迟导入重型平台插件、明确内建工具覆盖策略、默认不向插件暴露原始模型凭据的 LLM facade，以及核心定义的 hook 点。

注册 hook 只是订阅已有核心事件。只有核心代码实际触发对应 HookEvent，hook 才会运行；新增 hook 点仍需修改核心代码。Lumora 已在普通 Registry 与各领域 manager 之上加入能力路由层，通过版本代际、不可变快照、Turn lease 和旧资源排空完成可靠的进程内热替换，而不以单个大对象取代原有 manager。

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

Lumora 当前 Cron 已直接向 `ConversationCoordinator` 提交带 `origin=cron` 的请求，继续复用 Agent、会话上下文、工具、hook、权限和压缩链，但不再伪装平台入站消息，也不调用 Gateway 或 Adapter 私有方法。

当前结构是：平台、CLI/TUI、Cron 和主动插件构造 `SubmissionRequest`；Coordinator 负责会话队列、命令/控制通道和 turn 快照；`DeliveryService + Outbox` 负责目标解析、Hook、发送、重试和恢复。

这样保留 Lumora 的上下文优势，又不把 cron 伪装成用户消息。Hermes 的短生命周期隔离 Agent 适合独立后台任务，但默认不继承原会话和用户记忆，对“总结我们的对话”或强上下文提醒较弱。

真正的主动系统应是：触发器（时间、平台事件、任务状态、记忆复盘、外部事件）-> 候选事项 -> 策略（去重、冷却、静默时间、优先级、用户偏好）-> 决策（先规则，只对少量候选调用 LLM）-> Outbox/Delivery（发送、重试、审计、取消、用户反馈）。

## 5. 多模态

入站和出站多模态是两条独立链路。

入站路径：平台 payload -> 下载、校验和缓存附件 -> `MessagePart`/`AttachmentRef` -> provider 特定模型内容或视觉/转写结果 -> Agent context。

出站路径：Agent/tool -> `OutgoingMessage` 加附件引用 -> 平台能力检查 -> 图片、音频、视频或文档发送器 -> caption、格式、重试和投递处理。

入站把外部输入视为不可信，需处理 URL/重定向安全、MIME 校验、大小限制和临时 URL。出站关注路径授权、平台能力、caption、格式限制、速率限制和重试。

Hermes 用 `MEDIA:/actual/local/file.png` 这样的文本指令表达媒体，并从可见文本中拆出本地路径后原生发送。优点是任何返回文本的工具都能参与媒体投递；缺点是附件意图和自然语言混在同一字符串。

Lumora 已保留结构化消息并完成出站基础：工具/MCP 的 `ToolArtifact` 经 ArtifactStore 生成稳定引用，普通工作区文件通过 `artifact_from_file` 显式提升，LLM 使用 `response_attach` 选择本轮产物，`OutboundMessage -> DeliveryPlanner -> multipart Outbox -> Adapter` 负责平台能力降级、分片状态、重启恢复和原生发送。当前微信支持图片/视频/文件，Telegram 支持图片/文件/音频/视频，飞书支持图片/文件，QQ 支持图片/文件/音频/视频；caption、格式转换和真实平台限制继续按需打磨。

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
- Lumora provider 内部通过 factory 装配 embedding、vector、keyword、fusion 与可选 reranker backend；Qdrant 支持远程和本地持久化配置，SQLite Archive 始终是可重建索引的权威数据源。
- `memory/mem0` 直接适配官方依赖。
- 核心 fallback 在主 provider 不可用时保存 observation，并在恢复后迁移。

后续知识 RAG 作为独立通用插件设计，不恢复 `memory_ingest`，也不与个人记忆共用集合和更新语义。

</details>

## 7. 当前推进状态

| 方向 | 当前基础 | 主要缺口 | 状态 |
| --- | --- | --- | --- |
| MCP runtime | stdio、Streamable HTTP、重连、动态工具、结构化结果、诊断 | OAuth、sampling、elicitation 仅按需补充 | 核心完成 |
| 被动插件 | `PluginRuntimeContext`、`ctx.register.*`、版本安装、快照路由、Turn lease、MCP reconcile、资源排空、回滚和卸载 | 插件索引、第三方依赖策略和主动生命周期按需求推进 | Runtime 完成 |
| 出站多模态 | ArtifactStore、`artifact_from_file`、`response_attach`、结构化 Outcome、能力规划、分片 Outbox 和四平台原生发送 | caption、格式转换和真实平台限制按使用反馈补充 | 基础完成 |
| 主动能力 | Cron、插件 submit、统一 Coordinator、Delivery/Outbox 已完成 | 候选生成、去重、冷却、静默时间和决策策略 | 基础完成 |
| Memory / RAG | internal snapshot、buffer、Lumora/Mem0、backend factory、local/remote Qdrant、fallback、混合检索和审计 | 知识 RAG 后续作为独立插件，reranker 按需实现 | 记忆重构完成 |

方向之间的关系：

```text
结构化 tool artifact（已完成）
  -> 出站消息与 Delivery
     -> Outbox
        -> 主动提醒和后台结果投递

插件 Runtime 与热重载（已完成）
  -> 插件发现、版本安装、快照切换、回滚与卸载
     -> 后续主动插件生命周期和插件生态

Memory / RAG 拆分（独立领域）
  -> 长期记忆更新审计
  -> 外部知识证据检索
```

出站多模态与插件热重载基础链路都已经完成。下一步更自然的产品方向是主动决策，或继续依据真实长对话、第三方插件和平台联调反馈做稳定化。主动能力可直接复用现有 `PluginRuntimeContext`、runtime-owned tasks、CapabilitySnapshot、Coordinator、Artifact、Delivery 和 Outbox。
