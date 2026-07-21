<div align="center">

<h1>Luna Agent 项目演进记录</h1>

<p><strong>从原型到完整个人 Agent Runtime</strong></p>

<p>
  <img src="https://img.shields.io/badge/phases-22-7C3AED" alt="22 phases">
  <img src="https://img.shields.io/badge/Python%20LOC-102%2C796-0A84FF" alt="102796 Python LOC">
  <img src="https://img.shields.io/badge/tests-1282%20passed-2EA44F" alt="1282 tests passed">
</p>

<p>
  <a href="README.md">项目首页</a> ·
  <a href="docs/README.md">文档中心</a> ·
  <a href="luna-agent-roadmap.zh-CN.md">后续方向</a> ·
  <a href="BACKEND_PROGRESS.md">当前进度</a>
</p>

</div>

---

## v0.23 插件 Generation 架构收口与跨平台 CI

时间：2026-07-21

目标：在热重载、主动插件和进程隔离连续落地后，重新明确插件状态所有权、提交边界和生命周期恢复责任，并把 Linux/Windows 验证固化为持续集成门禁。

主要变化：

- 插件状态拆为不可变 `PluginDefinition`、运行中的 `PluginGeneration` 与只读 `PluginView`；`LoadedPlugin` 降为兼容 facade，不再承担新架构职责。
- `RegistrationTransaction` 统一候选 generation 的 Tool、Skill、Workflow、Platform、Hook、Command、MCP、Memory 与数据 revision 注册和回滚。
- `GenerationCoordinator` 管理状态迁移，`CapabilityRouter` 管理候选 binding 与原子快照发布；提交失败会恢复旧 generation 的路由、registry、数据指针和主动 runner。
- `WorkerSupervisor` 与 `ActiveSupervisor` 分别拥有外置 Worker 和主动 runner 生命周期；Worker 崩溃恢复、退避、熔断和运行诊断进入稳定边界。
- Memory/Platform 明确为 boot-scoped capability：运行中重载只记录 `pending_restart`，不会制造宿主已经应用新实例的假象。
- 新增 GitHub Actions：Ubuntu 执行 compileall 与完整 pytest，原生 Windows 执行 AppContainer smoke；工作流锁定 Python、uv 和 Action 版本，并保持只读仓库权限。
- 插件架构剩余兼容层、稳定性验证和发布治理集中记录到 `PLUGIN_ARCHITECTURE_DEBT.md`，不再散落在阶段交接文档中。

代表提交：`05b2d02`、`40f615e`、`010694d`。

## v0.22 外置插件 Worker 隔离与宿主资源端口

时间：2026-07-20

目标：让通用插件真正脱离宿主进程，同时保留 Tool、MCP、Conversation、LLM、文件和开发工作区等能力的统一安全管道。

主要变化：

- 外置插件按 generation 运行在独立 Worker，使用分帧 RPC；每个插件依赖集拥有独立环境，热重载只切换能力快照，不复用旧 Worker。
- Linux/WSL 通过 Bubblewrap 隔离插件包、依赖、数据目录和网络；原生 Windows 使用 AppContainer、受控句柄继承和 Job Object，安全后端不可用时拒绝加载。
- 新增声明式 `process` / `workspace` 宿主资源端口；Codex Bridge 的 App Server 与插件脚手架不再由外置插件直接创建宿主进程或写宿主路径。
- Worker 崩溃会把 generation 标为失败并拒绝新调用，随后按退避策略重建、校验能力契约、发布新快照；连续失败打开 circuit breaker，主动 runner 随 Worker 恢复。
- `plugins info/doctor` 暴露 Worker PID、sandbox、环境、重启/circuit 和 stderr 诊断；跨进程 lease 保护运行环境，`plugins environments` 与 dry-run-first GC 清理未引用环境。
- 当前安装中的 Document Converter、Markdown Structure Analyzer 和 Workspace Watch 已迁移到 SDK 0.3 generation，并正式启用外置隔离。

代表提交：`46bca09`、`b187e34`、`bdca2eb`、`4511f70`、`29b8a27`、`246026d`。

本文记录 Luna Agent 从最初原型到当前 Agent Runtime 的主要结构变化。它不是逐 commit changelog，也不再只描述 Windows 到 Linux/WSL 的迁移，而是按阶段梳理架构、能力和工程边界的演进，并标注代表提交。

当前主分支状态：

- 分支：`main`
- 本次统计基准：v0.23 插件 Generation 架构收口与跨平台 CI
- 最近全量验证：`uv run pytest -q`，结果 `1282 passed, 1 warning`

## v0.21 Bash 最小进程文件系统

时间：2026-07-19

目标：让 Shell 保持足够自由，同时把可访问宿主资源从“解析命令字符串”提升为可审批、可审计、不可绕过的进程级边界。

主要变化：

- `bash` 与 `process_start` 通过 `cwd/read_paths/write_paths` 显式声明文件系统资源，工具身份与路径资源分别审批。
- `ProcessMountPlan` 作为共享中间层构造进程视图；Bash 使用最小 Bubblewrap root，MCP 保留兼容策略。
- 未声明宿主路径在子进程里不存在，单文件授权不扩大到兄弟文件；blocked 文件/目录在挂载后继续遮蔽。
- 自动后端缺少 bwrap 时 Bash 失败关闭，显式 legacy 是唯一兼容退出路径；Doctor 区分 Bash 与通用进程后端。
- 真实回归覆盖 Python 路径拼接、外部单文件审批、blocked mask、后台进程和 MCP 兼容性；完整测试为 `1197 passed, 1 warning`。

## v0.20 Agent 可操作的插件控制面

时间：2026-07-19

目标：让主 Agent 不依赖 Bash 或 CLI 文本解析，直接使用现有插件控制面完成插件查询、构建与热管理。

主要变化：

- 增加 `productivity/document-converter`，用单一延迟发现工具将常见本地文档转换为分页文本或 Markdown。
- Plugin SDK `0.2.0` 公开资源声明契约，外置文件工具继续进入宿主统一安全管道。
- `plugin_inspect`、`plugin_build`、`plugin_manage` 作为低频工具进入 Capability Snapshot，通过 `tool_search` 按需发现。
- 查询直接复用 `PluginQueryService`；安装、启停、重载、回滚和卸载直接调用当前 live `PluginManager`。
- 插件源码仍由普通文件工具编辑，插件构建工具只承担静态校验、SDK contract test 和确定性打包。
- 插件工具按 action 分级审批，拒绝内置插件、限制本地安装源，普通卸载始终保留插件数据。
- 路径继续经过 sandbox/resource 审批，打包拒绝符号链接；热更新后当前 Turn 保持旧 lease，新快照从下一轮生效。
- install-state 指向的 active digest 对同 key 本地开发源具有明确优先级；真正的同边界重复 key 仍拒绝加载。

## v0.19 Luna Agent 命名迁移

时间：2026-07-19

目标：把项目、Python 包、CLI、Plugin SDK 与内置 Memory provider 统一到 Luna Agent，同时保障已有插件、配置和记忆数据连续可用。

主要变化：

- 主实现迁移到 `src/luna_agent/`，主命令变为 `luna-agent`；旧命令作为迁移期别名继续工作。
- Plugin SDK 迁移到 `luna_agent_plugin_sdk`，manifest 宿主依赖字段改为 `requires.luna_agent`；旧 SDK import 与 `requires.lumora` 可继续解析。
- `memory/luna` 成为内置混合检索 provider；旧 provider 名、配置区块和内部记忆标记均有兼容读路径。
- 配置、脚本、测试、插件示例、运维命令、源码链接和路线图文件完成同步改名。
- 现有 Qdrant collection 不随品牌强制改名；SQLite Archive、向量数据和用户会话无需搬迁。

## v0.18 外置 Plugin SDK、依赖治理与可恢复提交

时间：2026-07-19

目标：让插件真正脱离宿主源码契约开发，并让主动 runner 的稳定事件在进程重启后仍保持幂等。

主要变化：

- 从宿主实现中拆出 `luna-agent-plugin-sdk` workspace 包，稳定 Tool、Hook、Command、Active、Manifest 与 Context 协议。
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

- 建立 `src/luna_agent/` 项目结构。
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
- Luna Agent provider 使用 LLM 提取/决策、embedding、Qdrant 与 SQLite FTS5/BM25 混合检索；Mem0 作为可替换 provider，失败时进入 SQLite fallback。
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

目标：用独立 Benchmark 暴露真实缺陷，并让 Luna Agent 记忆内部的 embedding/vector/keyword/fusion 可以替换。

代表提交：

- `eff77a9` `Merge evaluation findings fixes`
- `ade7ce2` `Merge Luna Agent memory backend refactor`

主要变化：

- 修复 grep/glob 受保护路径绕过、Luna Agent UUID 召回、嵌套工具统计、MCP timeout 和 cache 诊断误报。
- Luna Agent provider 内部增加轻量 backend contract/factory，支持 embedding、vector、keyword、fusion 和可选 reranker 的独立装配。
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

## 2026-07-19 - Codex 风格上下文 Checkpoint

- 旧 Hermes 式 300 字滚动摘要替换为面向下一模型的完整 Handoff Summary，不再设置压缩专属输出上限。
- replacement history 按 token 保留真实用户原话、近期完整消息和唯一一份最新摘要；旧摘要不会误识别为用户输入。
- 压缩 checkpoint 在当前 turn 执行前独立持久化，模型失败不会导致同一历史反复压缩。
- Agent 工具循环支持 mid-turn 压缩和一次 context-overflow 恢复，物理 Session 链继续负责历史审计和恢复。
- SQLite 新增窗口 lineage 与 token 诊断；`/compact` 支持手动 checkpoint，`/usage` 显示当前窗口。

## 2026-07-19 - Provider 与模型能力解析

- Provider 默认协议、模型硬上下文、最大输出和来源信息收敛到统一 capability catalog。
- OpenAI 默认使用 256K 经济窗口；其他已知模型按硬上限运行；显式配置和未知模型均有保守裁剪边界。
- OpenRouter catalog miss 支持短超时元数据补全和 24 小时缓存，远端不可用时继续使用本地 fallback。
- 主 Agent、Memory、插件与视觉 LLM 统一 `api_mode` 解析，特殊 Responses 中转仍可显式覆盖。
- 上下文压缩默认阈值提升至 90%，并为输出与安全余量保留空间；事件、turn report、`doctor` 和 `/usage` 可追踪最终解析结果。
- 当前 `gpt-5.6-terra` 配置使用 256K 有效窗口、1.05M 模型硬上限和显式 `codex_responses`；完整测试为 `1197 passed, 1 warning`。
- DeepSeek 默认采用 Anthropic Messages；官方 base URL 会按显式协议在普通根地址与 `/anthropic` 之间安全规范化。

## 当前代码规模

统计口径：v0.23 完成时 Git 已跟踪 Python 文件的物理行数；包含空行和注释，不等同于有效代码行。

| 范围 | 文件数 | 物理行数 |
| --- | ---: | ---: |
| `src/luna_agent/**/*.py` | 253 | 53,482 |
| `tests/**/*.py` | 117 | 34,489 |
| Plugins / Packages / Examples / Scripts / 其他 | 67 | 14,825 |
| Python 合计 | 437 | 102,796 |

项目规模更适合拆开理解：运行时与内置能力约 5.35 万行，测试约 3.45 万行，测试代码占 Python 总量约 33.6%。当前完整测试套件为 `1282 passed, 1 warning`。

### 2026-07-18 文档收敛

- `MIGRATION_CHANGELOG.md` 更名为本文，职责从“环境迁移”扩展为完整项目演进记录。
- 删除三份已完成的 `docs/archive` 计划、已验证的 Security/Integration Plugin 清单和 168 行 TUI Phase 0 spike。
- 出站清单收敛为 `PLATFORM_MEDIA_TEST_CHECKLIST.md`，只保留微信/QQ 真实平台待验证项。
- `BACKEND_INTERFACE.md` 继续作为前端契约唯一权威；`FRONTEND_INTERFACE_REQUIREMENTS.md` 只保留活动需求，不再复制已完成 schema。
- README 重构为功能导向的视觉化项目首页；新增文档中心，并为主文档统一状态标签、导航、图表、摘要卡与折叠历史。

## 当前能力快照

- 启动入口：`uv run luna-agent chat`、`uv run luna-agent chat --ui inline`、`uv run luna-agent serve`
- 配置入口：`.env` 管 secret，`config.yaml` 管行为配置。
- 执行模式：`config.yaml -> execution.mode`，交互临时切换用 `/mode`；权限、资源审批、沙箱和 Hook 统一进入 Tool Pipeline。
- 系统提示词：`data/system/*.md` 与 `profiles` session 映射。
- 运行数据：`data/`，不进入 git。
- 对话主链路：所有入口提交到 Coordinator，ConversationService 负责 Agent turn，Delivery/Outbox 负责出站。
- 扩展能力：插件可注册 Tool、Skill、MCP、Hook、Command、主动 runner 和受限资源端口，并支持 generation snapshot 热重载、回滚与卸载。
- 记忆：internal snapshot + review buffer + SQLite Archive + 可替换 Luna Agent/Mem0 外部 provider。
- 多模态：入站 attachment 与出站 Artifact 分离，四个平台按 capability 原生发送或确定性降级。
- 可观测性：doctor、runtime health、tool runs、turn reports、activity、cache diagnostics、delivery audit。

## 维护约定

截至本文更新时间：

- `.env`、`data/`、`.venv/`、`__pycache__/`、`.pytest_cache/`、`uv.lock` 均不应进入提交。
- 如果运行测试后 `src/luna_agent/skills/builtin/.usage.json` 改动，需要恢复后再提交。
- 结构性变化继续追加到本文；逐提交细节由 Git history 和各专项进度文档承担。
