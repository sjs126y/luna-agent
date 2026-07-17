# Codex 交接记录

更新时间：2026-07-18 CST

本文是当前主干的协作入口。阶段历史与代码规模见 `PROJECT_EVOLUTION.md`；后端详细实现记录见 `BACKEND_PROGRESS.md`；前端状态见 `FRONTEND_PROGRESS.md`。

## 当前主干

- 分支：`main`
- 当前基准：`f3da3d7 Refresh project docs and remove obsolete plans`；最近功能合并为 `0bcb55e Merge outbound multimodal delivery`。
- 本地 `main` 尚未推送到 `origin/main`。
- 最近完整验证：`python -m compileall -q src/personal_agent` 通过；`uv run pytest -q` 为 `1050 passed, 1 warning`。
- 唯一 warning 来自飞书 SDK 内部弃用 API，不是当前 Runtime 回归。
- 用户本地未跟踪的联调文件不属于项目提交，后续 Agent 不应擅自删除或纳入 commit。

基准提交的代码规模：

| 范围 | 文件数 | 物理行数 |
| --- | ---: | ---: |
| Runtime：`src/personal_agent/**/*.py` | 228 | 47,458 |
| Tests：`tests/**/*.py` | 88 | 27,549 |
| 项目插件：`plugins/**/*.py` | 5 | 488 |
| Scripts / Examples | 3 | 316 |
| 其他 Python 包装文件 | 1 | 0 |
| Python 合计 | 325 | 75,811 |

统计只包含 Git tracked Python 文件，包含注释与空行。

## 当前架构

Lumora 已从单一 CLI Agent 原型演进为多入口、可扩展的个人 Agent Runtime：

```text
CLI/TUI | Gateway Adapters | Cron | Plugin Submit
                     |
          ConversationCoordinator
          | command/control lane
          | ordered turn queue
                     |
           ConversationService
          Agent -> LLM -> Tool Pipeline
                     |
          ConversationTurnResult
                     |
        DeliveryService + Outbox
                     |
          PlatformDirectory -> Adapter
```

主要边界：

- Coordinator 管 session 顺序、命令、`/stop`、`/steer` 和 turn snapshot；Adapter 不再维护会话队列。
- ConversationService 管一轮 Agent 执行和持久化；平台、Cron、插件不绕开该主链路。
- Delivery/Outbox 管目标解析、Hook、发送、分片状态、重试和重启恢复；Adapter 只做平台协议转换与单次发送。
- Tool Pipeline 统一执行 Hook、hard precheck、工具审批、资源审批、sandbox、dispatch、audit 和安全收尾。
- 插件通过 `register(ctx)` 注册 Tool、Skill、MCP、Hook、Command、Workflow 和受限 submit 端口；核心 contract 不因单个插件变化。

## 最近完成

### Conversation Runtime

- CLI/TUI、Gateway、Cron 和主动插件入口统一提交 `SubmissionRequest`。
- 删除 Adapter/Gateway 遗留的 queue、busy、控制旁路和发送重试。
- `/stop` 会保留本轮已经完成且成对的 tool use/result；中止不会丢弃已完成事实。
- MCP runtime 后台并发启动，单 server 慢启动不再阻塞 Gateway ready。

### Security v4 与 Hook v2

- Mode 为 `Read Only`、`Ask First`、`Local Auto`、`Full Auto`。
- 工具身份和 filesystem/network 资源使用精确 session grant，共享可配置 TTL；Mode 切换或重启清空。
- Bubblewrap、blocked paths、嵌套 `tool_call`、MCP 与 Hook 均走统一安全边界。
- HookManager 提供 typed context/outcome、matcher、priority 和 failure policy；Gateway、Conversation、Tool 与 Delivery 都有稳定 Hook 点。

### Memory 与评测修复

- internal Markdown、review buffer/revision、SQLite Archive、Lumora/Mem0 provider 和 fallback 已完成重构。
- Lumora 内部使用 backend factory 装配 embedding/vector/keyword/fusion/可选 reranker；Qdrant 支持本地或远程配置。
- 独立评测发现并修复 protected path 搜索绕过、UUID 召回、嵌套工具统计、MCP timeout 和 cache usage 诊断问题。
- SQLite Archive 是权威数据源，向量和关键词索引可重建。

### 出站多模态与 QQ

- Tool/MCP 产物进入 ArtifactStore，模型只看到 session/turn scoped `artifact_id`。
- `response_attach` 明确选择本轮产物；未选择的文件不会自动发送。
- `artifact_from_file` 可把 `write/edit/bash` 生成的普通文件安全复制进 ArtifactStore。
- DeliveryPlanner + multipart Outbox 支持能力降级、部分成功、重试、恢复和审计。
- 微信、Telegram、飞书、QQ 已实现各自声明的原生媒体发送；QQ Adapter 完成 NapCat OneBot WebSocket 双向通道和可选 companion 管理。

## 前后端状态

后端负责 Runtime、Agent Loop、Conversation、Tool/Security、Memory、MCP、Plugin、Gateway/Adapter、Delivery、配置和后端文档。

前端负责 TUI/desktop-web 的布局、交互和视觉。Security v4 TUI 已合并主干；当前 TUI 已消费 slash registry、tool events、confirm、permissions、activity 和 usage/context。Artifact/Delivery 新契约已写入 `BACKEND_INTERFACE.md`，但附件缩略图、Artifact 列表和 multipart Delivery 专用 UI 尚未实现，也不是后端完成项。

协作约定：

- 后端新增或修改前端可消费事件、command、payload 或诊断字段时，同步更新 `BACKEND_INTERFACE.md`。
- 前端需要后端字段或接口时，写入 `FRONTEND_INTERFACE_REQUIREMENTS.md`。
- 两条工作线只更新自己负责的源码和进度文件；合并时保留用户本地配置与未跟踪数据。

## 当前待办

1. 完成微信修正后媒体实机复测，以及 QQ/NapCat 登录、私聊/群聊和多媒体端到端联调。
2. 继续用固定 Benchmark 观察本地 Qdrant、长会话、并发 Memory prefetch、MCP 冷启动和 provider cache。
3. 插件安装、卸载和热加载需要 `RuntimeSnapshot + lease + drain`、Manager reconcile 和异步资源关闭。
4. 主动决策系统需要候选生成、去重、冷却、静默时间、优先级、预算和用户反馈策略。
5. 知识 RAG 保持为后续独立通用插件，不重新塞进个人记忆 provider。

## 文档入口

- `PROJECT_EVOLUTION.md`：项目阶段变化、代表提交和代码规模。
- `BACKEND_PROGRESS.md`：后端详细完成记录和验证结果。
- `FRONTEND_PROGRESS.md`：前端 TUI 状态、偏好和接口消费情况。
- `BACKEND_INTERFACE.md`：前端消费后端事件、命令和 payload 的权威契约。
- `FRONTEND_INTERFACE_REQUIREMENTS.md`：前端向后端提出的小接口需求。
- `lumora-roadmap.zh-CN.md`：后续架构方向和完成状态。
- `TODO.md`：当前剩余工作。
