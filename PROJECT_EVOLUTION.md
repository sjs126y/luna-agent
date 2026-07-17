# Lumora 项目演进记录

更新时间：2026-07-18 CST

本文记录 Lumora 从最初原型到当前 Agent Runtime 的主要结构变化。它不是逐 commit changelog，也不再只描述 Windows 到 Linux/WSL 的迁移，而是按阶段梳理架构、能力和工程边界的演进，并标注代表提交。

当前主分支状态：

- 分支：`main`
- 本次统计基准：`0bcb55e Merge outbound multimodal delivery`
- 基准提交数：`549`
- 最近全量验证：`uv run pytest -q`，结果 `1050 passed, 1 warning`

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

## 当前代码规模

统计口径：基准提交 `0bcb55e`，只统计 Git 已跟踪文件的物理行数；包含空行和注释，不等同于有效代码行。

| 范围 | 文件数 | 物理行数 |
| --- | ---: | ---: |
| `src/personal_agent/**/*.py` | 228 | 47,458 |
| `tests/**/*.py` | 88 | 27,549 |
| `plugins/**/*.py` | 5 | 488 |
| `scripts/**/*.py` | 3 | 455 |
| `examples/**/*.py` | 1 | 29 |
| 其他 Python 包装文件 | 1 | 0 |
| Python 合计 | 326 | 75,979 |
| Markdown 文档 | 44 | 7,782 |
| Git 已跟踪文件总数 | 413 | - |

项目规模更适合拆开理解：运行时与内置能力约 4.75 万行，测试约 2.75 万行，测试代码占 Python 总量约 36.3%。当前完整测试套件为 `1050 passed`。

## 当前能力快照

- 启动入口：`uv run personal-agent chat`、`uv run personal-agent chat --ui inline`、`uv run personal-agent serve`
- 配置入口：`.env` 管 secret，`config.yaml` 管行为配置。
- 执行模式：`config.yaml -> execution.mode`，交互临时切换用 `/mode`；权限、资源审批、沙箱和 Hook 统一进入 Tool Pipeline。
- 系统提示词：`data/system/*.md` 与 `profiles` session 映射。
- 运行数据：`data/`，不进入 git。
- 对话主链路：所有入口提交到 Coordinator，ConversationService 负责 Agent turn，Delivery/Outbox 负责出站。
- 扩展能力：被动插件可注册 Tool、Skill、MCP、Hook、Command 和受限 submit 端口。
- 记忆：internal snapshot + review buffer + SQLite Archive + 可替换 Lumora/Mem0 外部 provider。
- 多模态：入站 attachment 与出站 Artifact 分离，四个平台按 capability 原生发送或确定性降级。
- 可观测性：doctor、runtime health、tool runs、turn reports、activity、cache diagnostics、delivery audit。

## 维护约定

截至本文更新时间：

- `.env`、`data/`、`.venv/`、`__pycache__/`、`.pytest_cache/`、`uv.lock` 均不应进入提交。
- 如果运行测试后 `src/personal_agent/skills/builtin/.usage.json` 改动，需要恢复后再提交。
- 结构性变化继续追加到本文；逐提交细节由 Git history 和各专项进度文档承担。
