<div align="center">

<h1>Lumora 项目演进记录</h1>

<p><strong>从原型到完整个人 Agent Runtime</strong></p>

<p>
  <img src="https://img.shields.io/badge/phases-18-7C3AED" alt="18 phases">
  <img src="https://img.shields.io/badge/Python%20LOC-86%2C280-0A84FF" alt="86280 Python LOC">
  <img src="https://img.shields.io/badge/tests-1167%20passed-2EA44F" alt="1167 tests passed">
</p>

<p>
  <a href="README.md">项目首页</a> ·
  <a href="docs/README.md">文档中心</a> ·
  <a href="lumora-roadmap.zh-CN.md">后续方向</a> ·
  <a href="BACKEND_PROGRESS.md">当前进度</a>
</p>

</div>

---

本文记录 Lumora 从最初原型到当前 Agent Runtime 的主要结构变化。它不是逐 commit changelog，也不再只描述 Windows 到 Linux/WSL 的迁移，而是按阶段梳理架构、能力和工程边界的演进，并标注代表提交。

当前主分支状态：

- 分支：`main`
- 本次统计基准：v0.17 主动插件与投递稳定性收尾
- 最近全量验证：`uv run pytest -q`，结果 `1167 passed, 1 warning`

## v0.18 外置 Plugin SDK、依赖治理与可恢复提交

时间：2026-07-19

目标：让插件真正脱离宿主源码契约开发，并让主动 runner 的稳定事件在进程重启后仍保持幂等。

主要变化：

- 从宿主实现中拆出 `lumora-plugin-sdk` workspace 包，稳定 Tool、Hook、Command、Active、Manifest 与 Context 协议。
- 建立 manifest 依赖图、兼容版本检查、拓扑加载、循环阻断和 dependent 卸载保护。
- 在 ConversationCoordinator 外围增加 SQLite Submission Ledger；普通请求保持轻量，显式 durable 请求可恢复 Conversation/Delivery 边界。
- 新增面向 AI 的 schema/capability 查询、spec 脚手架、契约测试、隔离集成测试、差异检查与打包工具。
- 现有外置插件和 examples 迁移为 SDK 的真实兼容样例。

<details>
<summary><strong>展开早期阶段：v0.1 - v0.7</strong></summary>

## v0.1 Runtime 原型落地

时间：2026-06-22 16:01 - 16:34 +0800  
范围：`ee8865a` -> `5c5116c`

目标：把项目从计划文档推进到可运行的 Python Agent runtime 骨架。

代表提交：

- `ee8865a` 2026-06-22 16:01 `docs: CLAUDE.md + implementation plan + .gitignore`
- `2a72aa6` 2026-06-22 16:11 `feat(phase-0): project scaffolding`
- `9be007c` 2026-06-22 16:12 `feat(phase-1): pydantic-settings config`
- `492937d` 2026-06-22 16:13 `feat(phase-2): core data models`
- `1495774` 2026-06-22 16:13 `feat(phase-3): aiosqlite database`
- `ad392ec` 2026-06-22 16:16 `feat(phase-4): LLM transport`
- `2e00dc0` 2026-06-22 16:19 `feat(phase-5): tool system`
- `36c8efc` 2026-06-22 16:21 `feat(phase-6): Agent engine`
- `85c4a06` 2026-06-22 16:22 `feat(phase-7): memory system`
- `401af85` 2026-06-22 16:28 `feat(phase-8): adapter system`
- `ff27c0d` 2026-06-22 16:30 `feat(phase-9-10): Gateway, SessionStore, main.py`
- `5c5116c` 2026-06-22 16:34 `docs: README.md`

主要变化：

- 建立 `src/personal_agent/` 项目结构。
- 增加配置、数据模型、SQLite 会话存储、LLM transport、工具注册/执行、Agent loop、memory、platform adapter、Gateway。
- CLI 入口和初版 README 可用。

## v0.2 平台 Gateway 与配置拆分

时间：2026-06-22 16:38 - 2026-06-23 09:58 +0800  
范围：`5381097` -> `ede18dc`

目标：让 Gateway、平台 adapter、配置和工具链从 demo 进入可运行状态。

代表提交：

- `5381097` 2026-06-22 16:38 `fix: CC review`
- `0b07f77` 2026-06-22 17:15 `fix: Feishu adapter`
- `2437ca3` 2026-06-22 19:54 `feat: Provider + Transport + Retry system refactor`
- `b252189` 2026-06-22 20:08 `feat: cache optimization`
- `cbdaa84` 2026-06-22 20:26 `feat: ContextEngine + ContextCompressor`
- `845f590` 2026-06-22 20:50 `feat: Telegram adapter, Cron scheduler, Session expiry`
- `d8dc324` 2026-06-22 20:55 `refactor: config split`
- `03ddc01` 2026-06-22 21:03 `feat: auth system`
- `cb95636` 2026-06-22 21:31 `feat: 5 new tools`
- `03388ac` 2026-06-22 22:04 `feat: Hermes tool system`
- `c40be50` 2026-06-23 08:43 `feat: add OpenRouter provider`
- `5c8d9a8` 2026-06-23 09:24 `feat: tool execution pipeline`
- `ede18dc` 2026-06-23 09:58 `fix: CLI transport registry + platform hooks`

主要变化：

- Feishu/Telegram/Gateway 连接路径逐步稳定。
- `.env` 和 `config.yaml` 拆分：secret 与行为配置分离。
- 增加 auth、cron、session expiry、OpenRouter、provider/transport/retry 重构。
- 工具系统升级为 schema/toolset/bridge/BM25 检索结构。
- 上下文压缩和 provider cache 初版出现。

## v0.3 Memory、系统提示词、多 Agent 与沙箱

时间：2026-06-23 10:16 - 2026-06-24 09:06 +0800  
范围：`adac57b` -> `81530b`

目标：把记忆、系统提示词、工具、workflow、多 Agent 和安全边界做成可长期扩展的能力。

代表提交：

- `adac57b` 2026-06-23 10:16 `feat: external memory`
- `4b1fa70` 2026-06-23 10:56 `feat: system prompt files + grep/glob tools`
- `920ce9b` 2026-06-23 11:19 `feat: memory ingest CLI`
- `4aae146` 2026-06-23 11:28 `feat: skills summary + @ detection + todo SQLite`
- `e866999` 2026-06-23 11:51 `feat: WeChat adapter`
- `1098617` 2026-06-23 13:46 `feat: /usage command + context_usage()`
- `19bef87` 2026-06-23 16:29 `feat: new tools`
- `db437b6` 2026-06-23 17:14 `feat: workflow engine`
- `477096b` 2026-06-23 17:36 `refactor: CC-style sub-agent primitives`
- `5f8461f` 2026-06-23 17:54 `fix: sub-agent tools now go through full execution pipeline`
- `86b4918` 2026-06-23 18:19 `feat: git worktree tools`
- `02e7abe` 2026-06-23 18:45 `feat: per-user profile routing + girlfriend persona migration`
- `5ab0a91` 2026-06-23 22:10 `feat: unified sandbox`
- `81530b` 2026-06-24 09:06 `chore: sanitize real paths and usernames`

主要变化：

- 系统提示词改为 `data/system/*.md` 与 `profiles` 映射方式。
- 加入 embedding external memory、memory ingest、PDF/DOCX 支持。
- 增加 grep/glob/clarify/confirm/execute_code/delegate/process/task/worktree 等工具。
- 建立 workflow engine 和受控 sub-agent primitives。
- 引入统一 sandbox：roots、blocked patterns、bash path restrict、file write limit。
- 从 Windows/本机路径迁移痕迹中清理真实用户名和部分真实路径。

## v0.4 插件化 Runtime 与配置注册表

时间：2026-07-01 12:26 - 2026-07-02 22:03 +0800  
范围：`ae5d2cb` -> `b518f5f`

目标：把早期硬编码能力迁移到插件、registry 和共享 runtime，降低后续扩展成本。

代表提交：

- `ae5d2cb` 2026-07-01 12:26 `[codex] add plugin core and agent runtime`
- `24991e6` 2026-07-01 15:52 `[codex] finish builtin plugin migration`
- `aad20c9` 2026-07-01 16:48 `[codex] move llm transports into builtin plugin`
- `222a98d` 2026-07-01 17:24 `[codex] unify context budget reporting`
- `f345cff` 2026-07-01 17:46 `[codex] migrate sub agent tools onto runtime`
- `6cad584` 2026-07-01 19:52 `[codex] move memory providers into plugins`
- `44cb46b` 2026-07-01 22:12 `[codex] unify slash command handling`
- `b5577b1` 2026-07-01 22:44 `[codex] add shared app runtime bootstrap`
- `6305318` 2026-07-02 11:22 `[codex] unify conversation runtime`
- `d63be7c` 2026-07-02 12:39 `[codex] strengthen doctor and init cli`
- `306ce22` 2026-07-02 14:09 `[codex] add memory and gateway health`
- `c247743` 2026-07-05 00:04 `[codex] add config registry snapshot`
- `270619b` 2026-07-05 01:07 `[codex] route settings through config loader`

主要变化：

- 插件 core、plugin manifests、builtin tools/LLM/memory/platform/workflow 插件化。
- 共享 `AppRuntime`、`ConversationService`、`ConversationCommandRuntime` 成型。
- CLI/Gateway/chat 共用 slash command handling。
- runtime health、doctor、init、config diagnostics 体系建立。
- Config registry 和 ConfigLoader 接管配置字段、来源、校验和快照。

## v0.5 终端前端与 Inline TUI

时间：2026-07-03 10:06 - 2026-07-05 17:59 +0800  
范围：`295d5fe` -> `91db4a9`

目标：从普通 CLI 输出升级到事件驱动的终端前端和 inline TUI。

代表提交：

- `295d5fe` 2026-07-03 10:06 `[codex] add terminal cli event shell`
- `e339a19` 2026-07-03 11:15 `[codex] add rich terminal chat renderer`
- `66993f7` 2026-07-03 18:04 `feat(cli): PromptSession input`
- `30bdd81` 2026-07-03 20:16 `[claude code] feat(llm): incremental delta callback`
- `6758705` 2026-07-03 20:17 `[claude code] feat(cli): live token streaming`
- `d8cb864` 2026-07-03 21:53 `[claude code] make Ctrl+O a full-screen pager`
- `73ec17e` 2026-07-05 14:46 `[claude code] verify inline TUI approach`
- `0008ed7` 2026-07-05 14:55 `[claude code] add inline TUI skeleton`
- `cb431fe` 2026-07-05 15:06 `[claude code] add inline TUI input + keybindings`
- `606eeac` 2026-07-05 16:23 `[codex] stabilize inline tui printing`
- `0c3f013` 2026-07-05 17:12 `[claude code] add TUI theme.py`
- `91db4a9` 2026-07-05 17:59 `[codex] wire inline tool confirmations`

主要变化：

- CLI shell 变为事件驱动 renderer。
- LLM streaming、thinking delta、assistant delta 进入事件流。
- Ctrl+O 展开工具输出/思考内容。
- Inline TUI bottom region、输入区、slash mode、confirm panel、mode status、context meter 逐步成型。

## v0.6 Execution Mode、协议与工具可观测性

时间：2026-07-04 13:49 - 2026-07-06 16:45 +0800  
范围：`46271dd` -> `a230839`

目标：把权限模式、工具确认、前后端事件协议和工具审计做成稳定接口。

代表提交：

- `46271dd` 2026-07-04 13:49 `[codex] add execution mode policy v1`
- `b5d8279` 2026-07-04 21:35 `[codex] structure execution mode profiles`
- `9730622` 2026-07-04 21:54 `[codex] add execution policy overrides`
- `c247743` 2026-07-05 00:04 `[codex] add config registry snapshot`
- `60f2663` 2026-07-05 17:04 `[codex] add tool truth turn reporting`
- `303562f` 2026-07-05 17:38 `[codex] persist tool run results`
- `ca11bb9` 2026-07-06 02:51 `[codex] wire mode command to execution policy`
- `85f43d1` 2026-07-06 11:48 `[codex] define backend event protocol contract`
- `cfa5237` 2026-07-06 12:13 `[codex] add tool decision display metadata`
- `0b49940` 2026-07-06 15:16 `[codex] add structured slash command results`
- `ccba1d9` 2026-07-06 14:33 `[codex] add chat slash command registry`
- `a230839` 2026-07-06 16:45 `[codex] enhance doctor runtime diagnostics`

主要变化：

- `execution.mode`: `guarded | standard | trusted | sovereign`。
- 用户侧 `/mode`: `Read Only | Ask First | Edit Freely | Full Auto`。
- Tool decision / tool end metadata 支持前端确认 UI。
- Tool truth、tool runs、turn reports、doctor runtime diagnostics 可观测。
- Slash command registry、structured `CommandResult.kind/payload`、argument choices 和 dynamic providers 建立。

## v0.7 Provider Cache、Activity Runtime 与前后端合流

时间：2026-07-06 20:46 - 2026-07-07 01:49 +0800  
范围：`62e7efc` -> `3c969a8`

目标：优化 provider/transport cache、完善 activity/runtime 结构化接口，合并前后端工作线并修正文档。

代表提交：

- `62e7efc` 2026-07-06 20:46 `[codex] add provider cache diagnostics`
- `1c247f2` 2026-07-06 20:48 `[codex] refine provider cache request strategy`
- `8a0a68a` 2026-07-06 20:54 `[codex] add llm request plan cache diagnostics`
- `c4e50cf` 2026-07-06 22:32 `[codex] persist turn reports`
- `fa5ed5e` 2026-07-06 23:16 `[codex] add activity snapshots`
- `914980c` 2026-07-06 23:20 `[codex] expose activity command payloads`
- `1dfedc1` 2026-07-06 23:26 `[codex] add activity slash metadata`
- `527428a` 2026-07-06 23:47 `[codex] improve text tools ergonomics`
- `75624a8` 2026-07-07 00:45 `[codex] clarify usage context metrics`
- `4c55915` 2026-07-07 01:11 `[codex] add tool call protocol prompt`
- `e6ccb98` 2026-07-07 01:16 `Merge branch 'feature/backend-provider-cache'`
- `73d0f24` 2026-07-07 01:24 `[codex] merge frontend tui polish`
- `42cdf50` 2026-07-07 01:32 `[codex] refresh project progress docs`
- `3c969a8` 2026-07-07 01:49 `[codex] document execution mode config`

主要变化：

- Provider/transport 从简单协议适配升级为 provider-aware cache strategy。
- Anthropic explicit cache 与 DeepSeek/OpenAI-compatible prefix cache 做区分。
- `LLMRequestPlan`、stable/dynamic request hash、cache usage normalization 接入 runtime。
- Turn reports 持久化，tool runs 可和 turn report 关联。
- Activity Runtime 统一子 agent、后台进程、Gateway agent 的 summary/list/detail。
- `/activity`、slash metadata、dynamic candidates 进入前端可消费结构。
- Context meter 语义修正：当前上下文占用与最近一轮 API token 分离。
- 前端 inline TUI 接入 activity、context budget、turn token、confirm action row、tool result 展开。
- 后端/前端分支合并回 `main`，根 README 和交接文档刷新。

</details>

## v0.8 MCP Runtime 与 Memory v2

时间：2026-07-12

目标：把 MCP 和个人记忆从早期适配代码升级为有清晰生命周期、故障边界和替换接口的核心能力。

代表提交：

- `1c9e21d` `merge MCP runtime`
- `9f3983f` `merge memory v2`
- `90daa63` `Merge Qdrant payload index fix`

主要变化：

- MCP 改为 SDK-backed `MCPManager -> MCPServerRuntime -> MCPConnection`，支持 stdio、Streamable HTTP、单 server 隔离、重连、动态工具快照和结构化结果。
- 记忆拆成 internal Markdown、buffer/revision、SQLite archive、外部 provider 和后台 review worker。
- Lumora provider 使用 LLM 提取/决策、embedding、Qdrant 与 SQLite FTS5/BM25 混合检索；Mem0 作为可替换 provider，失败时进入 SQLite fallback。
- 修复 Qdrant payload index、scope 隔离、后台迁移和工具循环导致的真实 Gateway 故障。

## v0.9 被动插件与 Security v4

时间：2026-07-13 - 2026-07-14

目标：明确插件注册边界，并把工具权限、资源扩权、沙箱和用户 Mode 合成一条安全管道。

代表提交：

- `cfd809a` `Merge passive plugin v1`
- `d061acd` `Merge security pipeline v2`
- `956e99f` `Merge security cleanup and Fetch MCP fix`
- `7b1afd0` `Merge frontend security v4`

主要变化：

- 插件通过 `register(ctx)` 原子注册 Tool、Skill、MCP、Hook 等资源，具备所有权、冲突检测、配置隔离和失败回滚。
- 用户 Mode 收敛为 `Read Only`、`Ask First`、`Local Auto`、`Full Auto`；底层由 filesystem/network profile 与 approval policy 组合。
- 工具身份审批与 filesystem/network 精确资源审批统一进入 executor；授权按 session/TTL 保存，Mode 切换或重启清空。
- Bash/进程接入 Bubblewrap 与受保护路径边界；嵌套 `tool_call`、MCP 和 Hook 不能绕过安全管道。
- Fetch、GitHub、Sequential Thinking 等 MCP 完成真实 Gateway 审批与调用验证。

## v0.10 Typed Hook 与统一配置边界

时间：2026-07-13 - 2026-07-14

目标：让插件扩展发生在稳定的宿主事件上，同时禁止子系统自行读取环境变量。

代表提交：

- `f95be36` `Merge settings environment boundary fix`
- `ac51469` `Merge typed hook contract v2`
- `e15732b` `Merge read-only roots and Codex bridge`

主要变化：

- 建立独立 `HookManager`、统一公共输入、matcher、priority、failure policy 和 typed outcome。
- Hook 覆盖 Gateway、Conversation、Tool Security 与 Delivery 生命周期，可按具体 tool name/MCP name 匹配。
- `.env -> ConfigLoader -> Settings -> Runtime` 成为 secret 和动态配置的唯一注入边界；Memory、MCP、插件不再主动读取 env。
- Local Auto 支持独立只读根目录；Codex Bridge 作为外置插件验证 Tool/MCP/Hook 的组合能力。

## v0.11 Conversation Runtime 与通用插件

时间：2026-07-16 - 2026-07-17

目标：解决 Adapter/Gateway 各自维护队列、控制命令旁路和出站发送分散的问题，为主动插件提供正式入口。

代表提交：

- `378885b` `Document unified conversation runtime`
- `b7ab651` `Merge conversation runtime closeout`
- `2b35a83` `Enable integration assistant plugins`
- `e94042d` `Merge background MCP startup`

主要变化：

- 新增 `SubmissionRequest`、`ConversationCoordinator`、共享 Session Directory 和 turn snapshot policy。
- CLI/TUI、Gateway、Cron 和插件统一向 Coordinator submit；命令、`/stop`、`/steer` 使用独立控制通道。
- Adapter 不再管理会话队列、busy state 和发送重试；DeliveryService + Outbox 统一目标解析、发送、重试与恢复。
- 插件获得受能力约束的 submit/notification 端口，但尚未开放主动决策策略。
- 新增 GitHub Assistant、Developer Docs、Browser Operator 通用插件；MCP 后台并发启动，不再阻塞 Gateway 核心启动。

## v0.12 独立评测修复与 Memory Backend 工厂

时间：2026-07-17

目标：用独立 Benchmark 暴露真实缺陷，并让 Lumora 记忆内部的 embedding/vector/keyword/fusion 可以替换。

代表提交：

- `eff77a9` `Merge evaluation findings fixes`
- `ade7ce2` `Merge Lumora memory backend refactor`

主要变化：

- 修复 grep/glob 受保护路径绕过、Lumora UUID 召回、嵌套工具统计、MCP timeout 和 cache 诊断误报。
- Lumora provider 内部增加轻量 backend contract/factory，支持 embedding、vector、keyword、fusion 和可选 reranker 的独立装配。
- SQLite Archive 继续保存权威记忆，索引可重建；Qdrant 支持本地持久化模式，避免远端向量库拖慢主链路。
- 工具额度耗尽和空模型响应会进入无工具收尾，不再把内部占位文本直接发送给用户。

## v0.13 结构化出站多模态与 QQ OneBot

时间：2026-07-17 - 2026-07-18

目标：让工具产物不依赖文本路径约定，经过受控存储、明确选择和可靠 Delivery 发到真实平台。

代表提交：

- `997665a` `Define managed artifact storage`
- `4489e70` `Materialize tool and MCP artifacts`
- `9ce254e` `Persist platform-aware multipart delivery`
- `d43986c` `Align WeChat media protocol`
- `af6606a` `Complete QQ OneBot transport`
- `451d026` `Manage NapCat with QQ plugin`
- `53220ee` `Promote local files to response artifacts`
- `0bcb55e` `Merge outbound multimodal delivery`

主要变化：

- 新增 Runtime-owned ArtifactStore，工具/MCP 产物物化为 session/turn scoped `artifact_id`，不向模型、事件或平台暴露本地路径/base64。
- LLM 通过 `response_attach` 明确选择附件；DeliveryPlanner 和 multipart Outbox 负责平台能力降级、分片状态、重试和恢复。
- Playwright 相对截图链接在 server 专属受控目录中物化；嵌套工具直接透传已存储引用。
- 新增 `artifact_from_file`，把 `write/edit/bash` 已生成的普通文件安全复制进当前 turn，再交给 `response_attach`。
- 微信图片/视频/文件协议按官方 iLink 对齐；QQ 完成 NapCat OneBot WebSocket 双向链路、媒体发送和可选 companion 生命周期管理。

## v0.14 Plugin Runtime 热重载

时间：2026-07-18

目标：让插件能力可以在宿主不停机、活跃 Turn 不串代的前提下安装、更新、回滚和卸载。

代表提交：

- `fda608b` `Introduce plugin runtime primitives`
- `01a1e51` `Route plugin generations through snapshots`
- `f25e956` `Add hot-swappable plugin packages`

主要变化：

- `PluginRuntimeContext` 绑定 generation/runtime instance，注册 API 收敛为 `ctx.register.*`，运行时 Port 不再按插件 key 借用当前实例。
- CapabilitySnapshot 作为现有 manager 上方的不可变路由层；Turn lease 固定 Tool、Skill、Workflow、Command 和 Hook，旧 generation 在 lease 排空后回收。
- Agent 使用能力投影 fingerprint 保持缓存稳定；MCP 权威工具列表变化发布 revision，普通连接健康波动不影响 Prompt Cache。
- 插件包经过 staging 与安全检查进入不可变 digest 目录，支持本地目录/压缩包安装、更新、回滚、延迟卸载和数据保留。
- `ctx.storage` 与 `ctx.tasks` 为后续主动插件预留隔离数据和实例级任务生命周期，本阶段仍不实现主动决策系统。

## v0.15 主动插件 Runtime

时间：2026-07-18

主要变化：

- 插件可注册一个自由实现的长期 `run(ctx)`，由 Gateway 独占启停；主动执行开关与被动插件加载开关分离。
- `ctx.resources` 提供 generation-bound Tool、MCP、LLM、Conversation、Delivery、Artifact 与 Storage facade，资源声明和现有执行安全管道共同限制能力。
- 主动 generation 热更新加入 readiness handshake、v1 quiesce/resume、required MCP readiness 和数据 revision；v2 失败不会污染当前快照或持久化指针。
- 根任务异常支持退避重启和熔断，运行中通过 `/plugins active <key> on|off|restart` 控制。
- 主动 runtime 只解决生命周期与资源边界，不内置 Job/Cron/Candidate 或主动决策策略；插件内部行为保持自由。

## v0.16 主动插件套件与管理查询

时间：2026-07-18

主要变化：

- 删除无实际调用方的 `PluginRuntimeManager`；`PluginManager` 保持唯一写入和生命周期所有权，只读状态通过附属 `PluginQueryService` 组织。
- 新增同插件串行的 `PluginOperationTracker`、持久化 `PluginEventJournal`、历史版本、操作阶段和运行事件查询；CLI 与 `/plugins` 共享同一查询对象。
- GitHub Assistant 增加 PR、Issue、Commit 和 Actions 主动监视；首次建立基线，变化按仓库合并后进入主 Conversation。
- 新增 Reminder、Feed Watch 和 Inbox Watch：分别覆盖时间、网络订阅和文件 Artifact 三类主动来源，均使用隔离 Storage、session allowlist 和正式提交链路。
- Plugin Storage 增加原子 JSON；Conversation Port 支持稳定 request id 和插件自有 Artifact 输入，owner/session 不匹配会直接拒绝。
- 主动插件默认不获得写入、Shell 或进程控制能力；GitHub Watch 只申请只读 MCP，Feed 通过 URL 安全检查的专用工具，Inbox 只使用三个明确文件工具。

## v0.17 主动投递与热重载一致性收尾

时间：2026-07-19

目标：用真实 Gateway/主动插件运行暴露的故障收紧投递恢复、提交幂等和 capability lease 边界。

代表提交：

- `2928a44` `Fix Gateway active plugin lifecycle`
- `f2df284` `Fix active plugin artifact scoping`
- `8748be6` `Fix proactive delivery replay`
- `8ca04e8` `Pin bridged tools to capability leases`

主要变化：

- 修复 Gateway 删除旧管理包装层后未启动主动 runner，以及主动 Tool 长期复用 Artifact turn scope 的问题。
- Gateway 从 SessionStore 恢复 Delivery Binding；主动提交不再依赖用户重启后先发一条平台消息。
- Conversation 成功但 Delivery 暂不可用时进入 Outbox 延迟投递，不再把发送失败扩大成整轮 Agent 重跑。
- Coordinator 增加有界 `request_id` 幂等，同一事件复用原结果；Inbox 同一文件签名默认最多提交 3 次。
- `tool_search`、`tool_describe`、嵌套 `tool_call` 固定到当前 Turn lease，旧 generation 不会发现或执行新快照工具。
- 模型上下文识别更新，未知模型 fallback 从 64K 提升至 256K，显式配置仍优先。

## 当前代码规模

统计口径：v0.16 收尾时 Git 已跟踪文件的物理行数；包含空行和注释，不等同于有效代码行。

| 范围 | 文件数 | 物理行数 |
| --- | ---: | ---: |
| `src/personal_agent/**/*.py` | 251 | 53,154 |
| `tests/**/*.py` | 98 | 30,474 |
| `plugins/**/*.py` | 8 | 1,974 |
| `scripts/**/*.py` | 2 | 287 |
| `examples/**/*.py` | 5 | 391 |
| 其他 Python 包装文件 | 1 | 0 |
| Python 合计 | 365 | 86,280 |
| Markdown 文档 | 42 | 7,394 |
| Git 已跟踪文件总数 | 458 | - |

项目规模更适合拆开理解：运行时与内置能力约 5.32 万行，测试约 3.05 万行，测试代码占 Python 总量约 35.3%。当前完整测试套件为 `1167 passed, 1 warning`。

### 2026-07-18 文档收敛

- `MIGRATION_CHANGELOG.md` 更名为本文，职责从“环境迁移”扩展为完整项目演进记录。
- 删除三份已完成的 `docs/archive` 计划、已验证的 Security/Integration Plugin 清单和 168 行 TUI Phase 0 spike。
- 出站清单收敛为 `PLATFORM_MEDIA_TEST_CHECKLIST.md`，只保留微信/QQ 真实平台待验证项。
- `BACKEND_INTERFACE.md` 继续作为前端契约唯一权威；`FRONTEND_INTERFACE_REQUIREMENTS.md` 只保留活动需求，不再复制已完成 schema。
- README 重构为功能导向的视觉化项目首页；新增文档中心，并为主文档统一状态标签、导航、图表、摘要卡与折叠历史。

## 当前能力快照

- 启动入口：`uv run personal-agent chat`、`uv run personal-agent chat --ui inline`、`uv run personal-agent serve`
- 配置入口：`.env` 管 secret，`config.yaml` 管行为配置。
- 执行模式：`config.yaml -> execution.mode`，交互临时切换用 `/mode`；权限、资源审批、沙箱和 Hook 统一进入 Tool Pipeline。
- 系统提示词：`data/system/*.md` 与 `profiles` session 映射。
- 运行数据：`data/`，不进入 git。
- 对话主链路：所有入口提交到 Coordinator，ConversationService 负责 Agent turn，Delivery/Outbox 负责出站。
- 扩展能力：插件可注册 Tool、Skill、MCP、Hook、Command、主动 runner 和受限资源端口，并支持 generation snapshot 热重载、回滚与卸载。
- 记忆：internal snapshot + review buffer + SQLite Archive + 可替换 Lumora/Mem0 外部 provider。
- 多模态：入站 attachment 与出站 Artifact 分离，四个平台按 capability 原生发送或确定性降级。
- 可观测性：doctor、runtime health、tool runs、turn reports、activity、cache diagnostics、delivery audit。

## 维护约定

截至本文更新时间：

- `.env`、`data/`、`.venv/`、`__pycache__/`、`.pytest_cache/`、`uv.lock` 均不应进入提交。
- 如果运行测试后 `src/personal_agent/skills/builtin/.usage.json` 改动，需要恢复后再提交。
- 结构性变化继续追加到本文；逐提交细节由 Git history 和各专项进度文档承担。
