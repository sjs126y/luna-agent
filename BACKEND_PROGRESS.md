# Backend Progress

更新时间：2026-07-17 CST

## 2026-07-17：出站多模态与 Artifact Delivery

- 新增 Runtime-owned `ArtifactStore`：工具/MCP 产物经过大小、scope、生命周期和安全校验后落到 `data/artifacts/`，SQLite 只保存 metadata；活跃 Outbox 引用可阻止过期清理。
- 保持所有旧文本工具兼容；现有 `ToolHandlerOutput.artifacts` 在 Executor 出口物化为稳定 `artifact_id`，base64、URI 和本地路径不会进入事件、审计或模型上下文。
- 新增 `response_attach` 与 turn 隔离的 `TurnResponseDraft`。LLM 通过工具选择当前 turn 的产物，最终回答仍为普通文本；未选择的产物不会自动发送，停止轮不会投递草稿附件。
- `ConversationTurnResult`/`SubmissionOutcome` 新增结构化 `OutboundMessage`，同时保留 `final_response`/`response` 文本兼容；新增 `artifact_available` 和 `response_artifact_selected` 事件。
- 新增 `DeliveryPlanner` 和 `delivery_outbox_parts`：按平台 capability 规划 text/image/file/audio/video operation，不支持类型确定性降级；已成功 part 在重试和重启后不重复发送，partial/ambiguous 状态进入 PostDelivery 审计。
- 平台原生出站：微信按腾讯官方 iLink `getuploadurl -> AES-128-ECB CDN upload -> sendmessage` 支持图片/视频/文件；Telegram 支持图片/文件/音频/视频；飞书支持图片/文件；QQ 支持图片/音频/视频。
- 阶段提交：`997665a`、`4489e70`、`1d95f61`、`9ce254e`、`07bef47`、`b0a3feb`、`222e335`。
- Browser Operator 单独允许最多 10 MiB 的 MCP Artifact，避免正常截图先被通用 1 MiB 上限截断；进入 ArtifactStore 后仍受全局 20 MiB 默认上限约束。
- 自动验证：`1016 passed`；真实 Runtime doctor 通过，17 个启动步骤全部正常、8 个启用 MCP server 全部 ready；新增检查清单 `OUTBOUND_MULTIMODAL_TEST_CHECKLIST.md`，等待微信端工具截图实测后合并。

## 2026-07-17：独立评测问题收口

- 受保护路径检查扩展到 `grep` / `glob` 的每个枚举结果；显式访问受保护文件返回结构化 `sandbox_blocked`，宽泛搜索只返回允许结果。硬安全拒绝后 Agent 强制进入无工具收尾，不再换工具尝试绕过。
- 修复 Lumora/Qdrant UUID 表示差异导致的写入后无法召回：Qdrant 返回 ID 在适配边界统一为 SQLite 使用的 UUID hex；已有记忆无需清空或重建。默认 Lumora 所需 `qdrant-client` 改为基础依赖。
- 同一模型响应内多个无匹配安全 Hook 的审批请求会合并展示；一次确认仍为每个工具和资源分别写入精确 grant，`allow once` 在整个批次结束后撤销，执行顺序和 Hook 边界不变。
- `tool_call` 作为透明路由包装器保留 trace/audit，但不再重复计入 Turn Report 和持久化 tool runs；工具声称检测收紧为明确的进行中/已完成表达，避免把建议性文字误报为真实调用。
- MCP 工具继承各 server 的 `call_timeout_seconds`，经 `tool_call` 路由时也保持同一超时；取消中的 MCP 调用会触发 transport 重连，避免复用可能损坏的会话。
- Browser Operator 修复无效的 `--browser chromium` 参数，支持显式或自动发现 Playwright Chromium executable；本机已安装浏览器并真实验证 MCP ready、24 tools、`example.com` 导航成功。`.playwright-mcp/` 运行产物已忽略。
- Prompt cache 诊断新增 `usage_reported` / `usage_interpretation`，区分 provider 明确报告零命中与完全未返回缓存字段；稳定 system/tools/message-prefix 哈希继续保留。
- 补充延迟注册 MCP 工具的 executor 级 Hook 测试，确认 GitHub 写工具即使晚于插件加载出现，仍在 handler 前被宿主策略拦截。
- 阶段提交：`c078ac2`、`c250ed6`、`361a0a4`、`266d8b0`、`80dfd7b`、`7661748`、`1833a98`。
- 最终验证：`982 passed`；`python -m compileall -q src/personal_agent`、`git diff --check` 和真实 `personal-agent doctor --json --section runtime` 通过，8 个启用 MCP server 全部 ready。

## 2026-07-17：Conversation Runtime 收尾

- `/stop` 不再丢弃本轮已经完成的事实：ConversationService 在统一持久化边界保留完整助手文本和成对的 tool use/result，移除孤立工具块，并在 Turn Report 记录 partial persistence 摘要。
- 删除 Adapter 内的 session queue、busy state、control bypass、chat lock 和发送重试；Adapter 现在只负责入站转发、去重、附件、平台编码/分段和单次发送。
- 删除 Gateway 的兼容 Agent 执行路径与 `GatewayRunState`；Gateway 启动必须注入 Coordinator、DeliveryService 和 PlatformDirectory，运行诊断直接来自 Coordinator。
- 删除旧 `GatewayBeforeSend/GatewayAfterSend` Hook，出站扩展统一使用 `PreDelivery/PostDelivery`；AUTH、APPROVAL、SYSTEM 仍为不可抑制的受保护投递。
- 旧 `platform_pending_warning_threshold`、`platform_chat_locks_maxsize`、`platform_send_max_retries` 配置已移除，持久发送改用 `gateway.delivery_max_attempts`，默认 3 次。
- 长消息仍由 Adapter 按平台限制分段；前序分段已经送达而后续失败时标记 `partial delivery`/ambiguous，Outbox 不自动重发整个消息。
- 新增真实 SQLite、ConversationService、Coordinator、Delivery/Outbox 组合测试，覆盖正常轮和停止工具轮的持久化与发送。
- 最终全量回归 `967 passed`；`compileall`、`git diff --check` 和真实 `personal-agent doctor` 通过，配置正常、Runtime 已就绪。

## 2026-07-16：GitHub、Developer Docs 与 Browser Operator 插件

- 新增 `integrations/github-assistant`：插件化现有 GitHub Streamable HTTP MCP，提供 `repo-summary`、`review-pr`、`triage-issues`、`release-notes` 四个 Skill 和 `/github-status`；PreToolUse Hook 默认禁止 GitHub 写操作，并支持 `owner/repo` 白名单。
- 新增 `integrations/developer-docs`：插件化 Context7 MCP，提供 `library-docs`、`upgrade-library`、`compare-library-api` 三个 Skill 和 `/developer-docs-status`。
- 新增 `integrations/browser-operator`：插件化 Playwright MCP，提供 `inspect-web-page`、`test-web-page`、`operate-web-page` 三个 Skill 和 `/browser-status`；Hook 支持域名白名单，并默认禁止文件上传和页面脚本执行。
- 三个 MCP 已从顶层 `mcp.servers` 迁移到插件注册，避免同名重复；本机配置启用三个插件，example 只提供默认关闭的配置模板。
- 10 个 Skill 均通过 `skill-creator quick_validate`；插件 validate 全部通过；配置、插件与 MCP 聚焦回归 `117 passed`。
- 真实 Runtime doctor：GitHub `ready`/44 tools、Context7 `ready`/2 tools、Playwright `ready`/24 tools，三个插件均为 `LOADED`，无配置错误和重复 server。
- 最终全量回归 `956 passed`；`compileall` 与 `git diff --check` 通过。
- 新增根目录 `INTEGRATION_PLUGIN_TEST_CHECKLIST.md`，供真实 Gateway 验证状态命令、代表 Skill、GitHub 写保护、Browser 上传/脚本保护和无重复工具循环。
- Skill 自动发现明细降为 DEBUG；INFO 改为每个插件一行 `skills/mcp/hooks/commands` 注册汇总。日志与插件聚焦回归 `74 passed`。

## 2026-07-16：Conversation Runtime 与 Delivery 架构重构

- 新增统一 `SubmissionRequest -> SubmissionHandle -> SubmissionOutcome` 契约；Gateway、CLI、TUI、Cron 和主动插件使用同一提交边界。
- 新增 `ConversationCoordinator`：同 session 串行、跨 session 并发；`/stop`、`/steer` 走实时控制通道，查询和 mode 命令可即时执行，会话破坏性命令保持 barrier 顺序。
- `ActiveTurnRegistry`、turn id 和 steer 生命周期归 Coordinator；每轮捕获 `TurnPolicySnapshot`，运行中 mode 不变，切换只影响下一轮，撤销 grants 仍即时收紧。
- `SessionDirectory` 统一平台来源、逻辑 session 和投递目标；Cron 不再伪装 `MessageEvent` 或调用 Gateway 私有方法。
- 新增 `DeliveryService`、`PreDelivery/PostDelivery` Hook、受保护 AUTH/APPROVAL/SYSTEM 消息和 SQLite Outbox；后台 Worker 支持退避重试、重启恢复、ambiguous timeout 与原子 claim 防重复发送。
- 新 Runtime 下 Adapter 不再负责会话排队和控制旁路；Gateway 只完成鉴权、入站 Hook、附件准备和提交。现有平台 Adapter 的单次发送 API 保持兼容。
- 插件新增受能力约束的 `ctx.conversation` 和 `ctx.notifications`；校验 manifest capability、插件状态和允许访问的 sessions，禁用后旧端口立即失效。
- 阶段提交从 `92a0f2d` 至 `378885b`；最终完成 `952 passed`，`compileall` 与 `git diff --check` 均通过。

## 2026-07-14：Local Auto 只读根与 Codex Bridge 插件

- 新增 `sandbox.read_roots`：Local Auto / Full Auto 可通过原生 read/grep/glob 读取额外目录，但 file_write/file_edit、Bash 和 MCP 不获得对应写入边界；blocked path 始终优先。
- 本机配置已将 `/home/sujinsheng` 设为只读根，当前项目仍由更具体的 `sandbox.roots` 保持可写；实测 home read=true、home write=false、workspace write=true、`/etc` read=false。
- 新增首个本地通用插件 `integrations/codex-bridge`：插件同时注册官方 `codex mcp-server` 与精确匹配的 `PreToolUse` Hook，核心 MCP/工具管道没有 Codex 特例。
- Hook 固定 Codex 新线程的项目 cwd、`workspace-write` sandbox 和 `never` 内层审批，移除调用方传入的扩权 config；Lumora 外层 MCP 工具审批仍保留。`codex-reply` 可继续插件隔离目录中的线程。
- Codex Bridge 使用 `data/codex-bridge/` 保存独立状态，首次加载只复制现有 `auth.json` 并设为 `0600`；插件内 stdio 适配器仅过滤 Python MCP SDK 不认识的实验性 `codex/event`，标准响应和错误保持原样。
- 真实握手通过：`codex-mcp-server 0.144.3`，发现 `codex` / `codex-reply`，stderr 为空。当前 Codex 执行沙箱访问 `api.openai.com` 时流断连，最终联网调用留给真实 Gateway 环境复测。
- 聚焦与扩展回归：`158 passed`；`compileall` 与 `git diff --check` 通过。全量套件在正在运行的 Gateway 占用真实 `data/state.db` 时停于 doctor/Runtime SQLite 初始化，未将其误报为全量通过。

## 2026-07-14：Typed Hook Contract v2

- 新增独立 `personal_agent.hooks.HookManager`，由 AppRuntime 持有；PluginManager 只负责注册转发、owner 归属和插件卸载清理。
- Gateway、Conversation、压缩、Stop、工具 proposal、权限请求和工具结果已接入事件专属 outcome；入站消息 Hook 固定在鉴权后，出站 Hook 包围平台真实发送。
- Hook matcher、优先级、超时、失败策略和健康诊断已统一；PreToolUse fail-closed，PermissionRequest 失败 abstain，其余非安全事件默认 fail-open。
- Hook 附加上下文保持 turn 内临时状态，不写入 transcript；工具参数改写后重新执行统一 guard/permission 评估，真实工具结果和审计不会被 PostToolUse 覆盖。
- 删除 Agent/Gateway 旧运行时 Hooks、LLM request/response 改写链和嵌套工具 hooks context；平台私有解析回调迁移到 `AdapterHooks`。`configure`、`on_agent_created`、`on_session_selected`、`wechat_qr_login` 等专用生命周期回调保留。
- 阶段提交：`3a843e7`、`d5d1d2f`、`2865884`、`1af786f`、`0d69c4b`、`97ed678`。
- 使用仅位于 `/tmp` 的事件循环心跳辅助完成全量回归：`900 passed, 4 failed`。失败项均与本次 Hook 改造无关：2 项因受限网络无法解析 `example.com`，2 项为前端线仍期待旧 `/allow` 和旧 mode 刷新方式的 TUI 断言。

## 2026-07-14：补充 Time 与 Context7 MCP

- `config.yaml` 新增官方 `mcp-server-time`，固定本地时区为 `Asia/Shanghai`，在无网络的 Bubblewrap stdio 环境运行。
- 新增 Upstash `@upstash/context7-mcp` v3.2.3，用于解析库标识和查询最新技术文档；不配置 API key 时使用公开基础能力。
- Time 注册 `mcp__time__get_current_time`、`mcp__time__convert_time`；Context7 注册 `mcp__context7__resolve-library-id`、`mcp__context7__query-docs`。
- `doctor --verbose` 验证两台 server 均为 `runtime=ready`、各 2 个工具、0 次重连；配置与 MCP 聚焦回归 `46 passed`。

## 2026-07-13：安全兼容层收尾与 Fetch MCP 修复

- 删除旧 `ExecutionPolicy`、permission category grant、旧 Mode 别名与 `/allow`；工具执行只使用 session `SecurityContext`、精确工具授权和精确资源授权。
- `/deny all` 保留为当前 session 授权清理入口；Mode 切换、会话重置/删除和进程重启仍会清空授权。
- Doctor execution 诊断改为直接展示 mode、permission profile、approval policy、filesystem/network、tool approval 与统一 TTL。
- 前端/TUI 本轮不修改；所需 `/allow` 移除和 Security v4 payload 适配已记录到 `FRONTEND_INTERFACE_REQUIREMENTS.md`。
- 修复 Bubblewrap 只读绑定宿主 `/dev` 导致 `uvx` 无法检查 Python 的问题，改为创建最小隔离 `/dev`；Fetch MCP 实测达到 `runtime=ready`、1 个工具、0 次重连。
- MCP 连接失败后会保留 stderr 尾部，`doctor --verbose` 可显示真实子进程退出原因，不再只报告 `Connection closed`。
- 后端回归（排除由前端线负责的 `test_tui_*`）结果为 `782 passed`；安全/会话持久化/工具管道聚焦回归为 `100 passed`。

## 2026-07-13：被动插件 v1 与安全待办

- 被动插件 v1 已合并到 `main`，合并提交为 `cfd809a Merge passive plugin v1`；全量回归 `878 passed`。
- MCP 安全审查结果已写入根目录 `TODO.md`。后续执行顺序明确为先检查 Execution Mode 与权限模型，再在统一权限语义上收口 MCP 安全。
- 本次仅记录待办，没有修改 Mode、MCP 或其他运行时行为；用户本机 `config.yaml` 保持原状。

## 2026-07-13：Settings 配置边界收口

- `ConfigLoader` 统一合并项目 `.env` 与进程环境，进程环境优先；`Settings.get_env()` 成为动态命名配置值的唯一运行时解析入口。
- MCP Streamable HTTP 的 `headers_env` 由 Settings 在 runtime 装配阶段解析并显式注入 connection，connection 不再读取 `os.environ` 或 `.env`。
- Memory embedding/Qdrant 的 `api_key_env` 改为通过 Settings 解析；专用 `MEMORY_*_API_KEY`、动态 key 名和继承主 LLM key 的优先级保持不变。
- PluginManager 的 `requires_env`、LLM API mode 和 doctor 配置报告移除重复环境读取，统一消费 Settings/ConfigLoader 结果。
- 配置快照对所有 `.env` 值统一脱敏，未注册的 MCP/plugin 动态 token 也只显示 `<set>` / `<unset>`。
- 修正 doctor 对 Streamable HTTP MCP 的误报；普通 `uv run personal-agent doctor --verbose` 已验证 GitHub `runtime=ready`、44 个工具，Lumora memory 正常，不再需要 `--env-file` 启动参数。
- 测试隔离补充：runtime 单测显式关闭真实 external memory，避免继承本机 Lumora/Qdrant 配置。

验证：配置/MCP/memory/runtime/doctor 聚焦回归 `139 passed`；全量回归 `868 passed`；`python -m compileall -q src/personal_agent` 与 `git diff --check` 通过。

## 2026-07-13：真实联调收尾

当前状态：本轮后端修复已合并回 `main`，合并提交为 `daaadf3 Merge agent tool loop and memory recovery fixes`。合并前完成 `python -m compileall -q src/personal_agent`、`git diff --check` 和全量测试，结果为 `864 passed`。

已完成并经微信 Gateway 真实验证：

- 修复 tool result 消息顺序、工具调用上限后的终止行为和重复工具循环，避免模型在收到配额拒绝后继续无休止调用。
- 外部记忆 provider 状态按 scope 隔离；前台 search 失败会重试一次，连续故障才进入 fallback，后台迁移失败只延期，不切换当前会话 provider。
- fallback observation 可由 `MemoryReviewService` 后台批量迁回 Lumora，不占用 Agent 主循环；迁移失败保留 attempts/error，后续继续恢复。
- Lumora 会探测并修复 Qdrant collection/vector/payload index；本轮真实运行中 embedding 与 Qdrant query 均成功，当前微信 scope 保持 `lumora`，已有 pending observation 成功迁移。
- MCP 的 `tool_search -> tool_call -> executor -> audit` 链路已真实验证，filesystem 与 fetch 调用成功；首次 `npx` 安装输出污染 stdio JSON-RPC 的问题未阻断本轮调用，但后续仍可统一收敛 npm 静默启动参数。
- GitHub 官方远程 MCP 的 PAT 已真实联网验证成功，服务返回 `github-mcp-server`，可发现 44 个工具；本地 `.env` 中 `GITHUB_MCP_AUTH` 已修正为带引号的 dotenv 格式。

下次优先处理：

- GitHub MCP 的 `headers_env` 当前由 connection 直接读取 `os.environ`，与项目统一的 `ConfigLoader -> Settings` 边界不一致。普通 `uv run personal-agent serve` 因此无法使用 `.env` 中的动态 MCP header；临时可用 `uv run --env-file .env personal-agent serve` 启动。
- 正式修复应由 Settings 解析动态 header secret，再经 runtime 显式注入 MCP connection；MCP 模块不得自行读取 `.env`，诊断和 health 不得回显 secret。
- 修复后补 Settings/runtime/connection 聚焦测试，并用微信 Gateway 再验证 `tool_search` 能发现 `mcp__github__*`，随后调用只读仓库工具。

工作区说明：`config.yaml` 仍有用户本机配置改动，不应覆盖或随文档提交；`.env` 为忽略的本机 secret 文件。

## 2026-07-12：MCP Runtime

- MCP client 切换到官方 Python SDK 稳定 v1.x，协议与 transport 封装在 Lumora connection contract 后，保留旧 stdio 配置兼容。
- 新增单 server runtime、自动重连、健康检查、故障隔离、动态 `tools/list_changed` 快照同步和 transport-aware doctor 诊断。
- 新增 Streamable HTTP、环境变量 header、安全 URL 校验和结构化 MCP 工具结果；图片、音频、resource 的原始数据只保留在内存结果中，事件、审计和 SQLite 仅保存安全摘要。

## 2026-07-12：xAI Provider 与路线图文档

- 新增 `xai` LLM provider，使用 OpenAI-compatible Chat Completions，默认 xAI base URL，支持图片输入；配置注册、doctor 校验、视觉辅助 provider 和回归测试已同步。
- 根目录新增 `lumora-roadmap.zh-CN.md`，将后续架构路线图整理为中文；移除已完成的 provider/transport 实施指令和前端历史清理计划，并修正 `CLAUDE.md` 的过时链接与 provider/transport 描述。

## 阶段性收尾状态

状态：后端主干进入稳定可展示阶段，短期如果没有新产品方向，可以先暂停大功能开发，后续主要做真实使用反馈下的修 bug、文档补齐和小幅体验打磨。

当前主干：`main`，最近后端提交：

- `待提交 [codex] add llm reasoning effort env`
- `7d5967f [codex] prepare public config templates`
- `dd9bc4d [codex] fix codex responses tool context`
- `6987e3a [codex] expand runtime architecture docs`

当前整体判断：

- Agent runtime、provider/transport、工具执行、权限/sandbox、gateway、platform adapter、多模态附件链路、activity、turn report、runtime steer、doctor/config/docs 都已经具备完整底座。
- README、架构文档、能力边界文档、example 配置和 data 目录骨架已整理到可公开推送状态。
- `data/` 现在只提交目录骨架和 `data/system.example/` 模板；真实数据库、日志、附件、auth、微信凭据、个人 system prompt 都继续被 `.gitignore` 保护。
- `openai_responses` 与 `codex_responses` 的工具上下文转换已分开处理：官方 Responses 走结构化工具项，Codex/Ahoo 类中转站走文本化工具链路，避免上游 5xx。
- `LLM_REASONING_EFFORT` 已纳入统一 `.env` 配置链路，Chat Completions 写入 `reasoning_effort`，Responses / Codex Responses 写入 `reasoning.effort`，留空则不发送。
- 当前不建议再做大规模架构改造；如果后续继续推进，优先基于真实使用日志和前端/桌面端需求做小步验证。

暂停期间最值得关注的后续方向：

- 真实长对话下的上下文压缩质量：路径、任务状态、工具结果是否被保留得足够好。
- codex_responses 中转站真实使用下，文本化工具结果是否足够降低重复工具调用；如果仍重复，再考虑同轮只读工具去重或更强的 tool-result 提示。
- Feishu / Telegram 真实附件下载器、OCR/ASR/vision 服务接入，可以等实际平台需求再做。
- Desktop/Web 客户端如果启动，优先复用 `ConversationInput + attachments + ConversationService.run_turn_input()`，不要绕开后端主链路。

## 交接定位

这个文档只记录后端线进度，给后续接手后端的 Codex 使用。前端 TUI / desktop / prompt_toolkit 真实终端问题交给前端线处理；后端线只负责事件、接口、agent runtime、工具执行、权限、配置、平台适配、provider / transport 等基础能力。

当前工作分支：`main`

权威接口文档：

- `BACKEND_INTERFACE.md`：前端消费后端事件、slash commands、tool metadata、tool runs 等接口的主文档。
- `FRONTEND_INTERFACE_REQUIREMENTS.md`：前端提出的后端字段/接口需求入口。
- `CODEX_HANDOFF.md`：总交接文档，记录前后端分工和整体状态。

## 当前后端状态

后端主干能力已经比较完整；`feature/backend-provider-cache` 和历史清理分支已合并回主分支，当前分支用于继续后端收敛。最近已完成并验证的方向包括：

- README showcase refresh：根目录 README 已整理为更适合公开推送的项目首页，突出项目定位、架构图、核心亮点、当前能力、快速开始和文档索引。
- README showcase polish：README 再次调整为更偏项目展示页，扩充“一眼看懂”、为什么做、12 个核心亮点和能力地图；命令部分收敛为基础启动路径，其余用法导向文档索引。
- README boundaries refresh：README 首页补充安全边界、可靠性、execution mode 和可配置化入口；新增 `docs/capabilities-and-boundaries.md`，把详细功能亮点、安全下限、Gateway/LLM/tool 可靠性和配置项说明拆到独立文档。
- Runtime Flow architecture：README 新增开发者内部流转分层 Mermaid，覆盖入口、启动、会话、输入标准化、Agent 核心、LLM、工具、扩展、持久化与观测；`docs/architecture.md` 扩展为详细分层架构文档，补充每层职责、边界、关键流转和维护清单。
- Project display rename：对外展示名从 `Personal Agent` 调整为 `Lumora`；内部 Python 包名 `personal_agent` 和 CLI 命令 `personal-agent` 暂时保留，避免破坏运行入口。
- Streaming restore：确认 main 上 TUI/事件协议仍支持 `assistant_delta` / `thinking_delta`，实际断点在 ChatCompletions transport 没有把 `on_delta` 传给 parser，且 ChatCompletions / Responses 默认非流式；已修复为 renderer 请求 delta 时强制 `stream=True` 并转发 `on_delta`，补 call 层回归测试。
- Doctor output v1：`personal-agent doctor` 默认改为普通用户摘要，只展示状态、模型、运行时、配置、记忆、MCP、工具、网关、平台、插件、最多 5 条注意事项和下一步；新增 `doctor --verbose` 保留原完整开发诊断，`--json` / `--section` 行为不变。
- Execution Mode v3：四档模式已经稳定，对应权限、沙箱、工具类别和确认行为。
- Permission mode cleanup：`standard / Ask First` 下普通网络工具调整为 `ask`，`/allow network` 可解锁 `web_search` / `web_fetch`；`/allow` 只对 `ask` 生效，遇到 `deny` 会明确提示不能覆盖，bash 网络仍由 `sandbox.bash_allow_network` 单独控制。
- Execution / Sandbox 配置开放：`execution.policy.tool_permissions`、`sandbox.*` 已在 example、配置文档、init 模板和 doctor 重点字段中显式展示；未新增 per-tool 权限、timeout 或关闭硬安全边界的配置。
- Tool execution / permission pipeline：工具执行门控已经统一到 executor 路径，权限只负责自己的决策层，不再和其他阻断逻辑混在一起。
- Tool decision metadata：`tool_decision` / `tool_end` 已带前端确认 UI 所需字段，包括展示名、风险摘要、默认动作、可选动作、路径/命令/URL 预览等。
- Event protocol：事件有 `protocol_version`，`retry` / `error` / `stop` / `tool_decision` / `tool_end` 等事件结构化。
- Tool truth / turn report：`AgentTurnReport` 能记录工具真实调用、retry、错误、口头声称工具调用但实际未调用等信息。
- Tool runs：工具执行结果已持久化，并提供 `/tool-runs` 与 `ConversationQueryService` 查询。
- Turn reports：每轮 `AgentTurnReport` 已进入持久化审计链路，可和 tool runs 通过 `turn_id/session_key` 关联。
- Activity runtime：已提供统一结构化接口，覆盖子 agent、后台进程和 gateway agent，并支持 `/activity`、结构化 `CommandResult.kind="activity"`、runtime/query API、slash metadata 和动态候选。
- Runtime steer：新增 `/steer <text>` 运行中修正机制，gateway 平台可旁路同会话队列入队，agent loop 下一步消费并重答；health、activity、turn report 和事件协议已同步。

## 2026-07-07：权限限时授权 v1

状态：已完成 v1 实现并通过聚焦验证。

已完成：

- `/allow <category>` 改为写入限时临时授权，默认 24 小时；下一条普通消息不会再被 turn reset 清掉。
- CLI/TUI confirm 的 `always` 改为写入同一套限时授权；`allow once` 仍只本轮有效。
- 新增 `/deny <category>` / `/deny all` 撤销限时授权。
- `/permissions` 增加 `temporary_grants`、`turn_grants`、`temporary_grant_ttl_seconds`。
- 新增配置 `permissions.temporary_grant_ttl_hours` 和 `permissions.confirm_timeout_seconds`，并同步配置示例和文档。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_commands.py::test_shared_command_core_session_usage_export_and_allow tests/test_commands.py::test_allow_network_follows_execution_mode_policy tests/test_tool_pipeline.py::test_tool_confirm_always_persists_grant_for_later_tool_calls tests/test_agent_loop.py::test_permission_required_network_tool_stops_without_looping tests/test_agent_loop.py::test_temporary_network_grant_survives_turn_reset -q
```

结果：聚焦 `5 passed`。

## 2026-07-07：Gateway 异步工具确认 v2

状态：已完成 v2 实现并通过聚焦验证。

已完成：

- Gateway 平台遇到工具权限 `ask` 时会发送确认消息：`1 允许一次 / 2 拒绝 / 3 24小时允许`。
- pending confirm 回复会绕过 busy check，不进入普通 agent turn。
- `/stop` 会取消 pending confirm，并中断等待中的工具确认。
- `Gateway.health_snapshot()` 暴露 `pending_confirmations` / `pending_confirmation_count`。
- `/permissions` 可返回当前 session 的 `pending_confirmation`。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_gateway_commands.py::test_gateway_async_confirmation_allows_once tests/test_gateway_commands.py::test_gateway_async_confirmation_always_and_stop tests/test_gateway_commands.py::test_gateway_regular_message_uses_active_session_key tests/test_gateway_commands.py::test_gateway_passes_attachments_as_conversation_input -q
```

结果：聚焦 `4 passed`。

## 2026-07-07：权限确认可观测性 v3

状态：已完成 v3 实现并通过聚焦验证。

已完成：

- `tool_decision` / `tool_end` 追加 `grant_scope`、`grant_expires_at`、`temporary_grant_ttl_seconds`。
- Turn Report 工具条目和 tool truth 同步记录授权 scope / 过期时间 / TTL。
- Tool Runs SQLite 表新增兼容迁移列：`grant_scope`、`grant_expires_at`、`temporary_grant_ttl_seconds`。
- `ConversationQueryService` 和 `/tool-runs` 查询会返回新增授权字段。
- `BACKEND_INTERFACE.md` 已同步前端可消费字段。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_agent_loop.py::test_temporary_network_grant_survives_turn_reset tests/test_database.py::test_tool_runs_roundtrip_and_summary tests/test_conversation_service.py::test_run_turn_persists_tool_runs_from_events tests/test_event_protocol.py -q
uv run pytest -q
```

结果：聚焦 `11 passed`，全量 `785 passed`。
- Usage / context：`llm_start` / `llm_end` 已区分“最近一次 API token 消耗”和“当前上下文占用估算”；`/usage` 已修正工具计数文案，避免把活跃 turn 内部计数显示成会话统计。
- Tool protocol prompt：系统提示已加入稳定工具调用规则，要求需要工具时必须发出 tool call，避免只用文字声称已调用工具；未加入正则 retry 或额外控制流。
- Slash commands v2：chat / inline TUI / gateway 共用 slash command registry，`/commands`、`/tools`、`/permissions`、`/protocol`、`/mode` 等支持结构化 `CommandResult`。
- Doctor diagnostics：runtime health 已能展示 commands、query、execution、doctor 配置/运行时状态。
- Config registry：配置整理已进入可用状态，新增配置通过 registry/field 描述，不再散落硬编码。
- Platform adapter base：平台消息基类、media attachment v1 和授权后附件准备链路已打底；QQ/微信真实下载器 v1 已补，Feishu/Telegram 可后续补。
- Platform adapter attachments v1：Telegram / Feishu / QQ / WeChat 已统一附件引用语义，标准 kind 为 `image/audio/video/file`，保留 `name/mime_type/size/url/platform_file_id/metadata`。
- Multimodal input v1-v4：gateway 附件已进入结构化输入链路，支持本地附件缓存、配置化降级、OpenAI/Anthropic 原生图片输入、DeepSeek/OpenRouter 保守文本降级。
- Multimodal text extraction v1：`text` / `auto -> text` 模式下已支持文本类、PDF、docx 附件抽取，结果进入本轮模型上下文；OCR / ASR / 视频仍留后续。
- Image text fallback v2.1：新增图片文本化抽象、默认 null describer 和 `data/attachments/derived/` 缓存 helper；图片 text fallback 已有稳定 notice 与可注入扩展点。
- Image text fallback v2.2：新增 vision fallback describer，可通过 `multimodal.image_text_provider` + `.env` 的 `IMAGE_TEXT_API_KEY` 调用辅助视觉模型，并按图片 sha256/provider/model/prompt version 缓存结果。
- Image text fallback v2.3：新增本地 OCR HTTP describer，支持 `GET /health` + `POST /ocr` 协议，OCR 结果复用同一套图片文本化缓存；主项目不引入 OCR 重依赖。
- Platform attachment resolve v1：新增 `attachments.*` 配置、adapter 基类 `prepare_inbound_attachments()` / `download_attachment()` 扩展点、`DownloadedAttachment` 入库结构；Gateway 在授权通过且命令未被消费后触发 adapter 准备附件，provider 不参与下载决策。
- Platform downloader v1：QQ adapter 支持 OneBot 风格 `get_image/get_record/get_file/get_group_file_url` 下载候选；WeChat adapter 支持 iLink CDN 加密媒体下载和 AES 解密。
- Multimodal attachment diagnostics：`turn_start.multimodal_diagnostics` 已补充失败 reason 聚合和每个附件的安全摘要，前端可直接展示单个附件为何失败。
- WeChat encrypted media hardening：微信顶层或嵌套 `encrypt_query_param/encrypted_query_param` 均会进入平台下载链路；缺少 `aes_key` 时稳定返回 `decrypt_key_unavailable`，不再只给泛化失败提示。
- Image text protocol override：新增 `multimodal.image_text_api_mode` / `IMAGE_TEXT_API_MODE`，支持 `auto` / `chat_completions` / `anthropic_messages` / `responses` / `codex_responses`；Anthropic 模型经 OpenAI-compatible 中转站调用时可显式设为 `chat_completions`，Codex/Ahoo 这类 Responses 中转站建议设为 `codex_responses`。
- Main LLM Responses mode：主 Agent 的 `LLM_API_MODE` 正式开放 `responses` / `codex_responses`，`doctor`、配置 schema、agent runtime 测试已覆盖 Codex/Ahoo 中转站模式。
- Desktop multimodal contract：`BACKEND_INTERFACE.md` 已新增桌面端预留接口说明，明确未来 desktop/web 发送 `text + attachments`，后端转换为 `ConversationInput` 后调用 `run_turn_input()`。

最近一次记录的全量测试结果：`757 passed`。
最近一次聚焦验证：`tests/test_multimodal_processor.py tests/test_platform_adapters.py`，`52 passed`。

## 已完成方向：Multimodal Input v1-v4

状态：已完成实现并通过全量回归。

已完成：

- 新增 `ConversationInput` / `ResolvedConversationInput` / `ProcessedAttachment`，gateway 不再只把 `event.text` 传给 agent。
- `ConversationService` 新增 `run_turn_input()` / `run_turn_input_events()`，旧 `run_turn()` 保持兼容。
- 新增 `AttachmentStore`，缓存目录为 `data/attachments/`，按 sha256 去重，并复用 sandbox 与 URL safety。
- 新增 `MultiAttachmentProcessor`，支持 `auto` / `native` / `text` / `off`，附件失败会转成模型可见 notice，不中断 turn。
- 新增 `multimodal.*` 配置字段，并同步 `config.yaml.example`、配置文档和 doctor known keys。
- `ProviderProfile` 增加图片输入能力字段；OpenAI/Anthropic 默认支持图片，DeepSeek/OpenRouter 默认保守关闭。
- ChatCompletions transport 支持 `image_url` mixed content；Anthropic transport 会把 data URL 转成 Anthropic image source。
- provider 拒绝图片输入时，agent loop 会移除 image blocks 并纯文本重试一次。
- token/context 估算对图片使用固定 token 估值，不按 base64 字符串长度计算。
- cache diagnostics hash 会对 data URL 做指纹化，不记录完整 base64。
- `MultiAttachmentProcessor` 默认文本化能力已支持文本类、PDF、docx 附件，并通过 `multimodal.text_extract_max_chars` / `multimodal.text_extract_pdf_max_pages` 控制上下文注入上限。
- `MultiAttachmentProcessor` 已预留 `ImageTextDescriber` 扩展点，图片 text fallback 可走 vision/OCR 描述器；vision provider 与本地 OCR HTTP 服务均已可配置。
- 修复附件 resolve 失败时 `effective_mode` 未赋值导致的异常，失败现在会稳定转成 notice/diagnostics。
- `turn_start` 新增 `attachments_count`、`attachment_kinds`、`multimodal_diagnostics`，`AgentTurnReport` 同步记录。
- `BACKEND_INTERFACE.md` 已同步多模态事件字段。
- `BACKEND_INTERFACE.md` 已新增桌面端预留接口：请求结构、`AttachmentRef` 字段、前端职责边界、事件消费方式和 CLI 不承载附件上传的约定。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_attachment_store.py tests/test_multimodal_processor.py tests/test_transport_multimodal.py tests/test_event_protocol.py tests/test_transport_cache.py tests/test_conversation_service.py -q
uv run pytest -q
```

结果：多模态/配置/文档目标回归 `60 passed`；全量 `754 passed`。

## 已完成方向：Platform Adapter Attachments v1

状态：已完成实现并通过全量回归。

已完成：

- 新增 `personal_agent.platforms.attachments` 公共 helper，统一 kind 归一化和 `url/local_path/platform_file_id` 归类。
- Telegram adapter 支持解析 photo/document/voice/audio/video；附件-only 消息不会被丢弃。
- Feishu adapter 支持解析 image/file/audio/video/media/post 中的附件引用；附件消息不进入 debounce 合并。
- QQ adapter 将 `record/voice` 统一为 `audio`；OneBot `data.file` 会按 URL / 本机路径 / 平台 id 归类，不再无脑写入 `local_path`。
- WeChat adapter 将 `voice/audio` 统一为 `audio`，保留 `file_id/media_id/url/cdn_url/name/mime/size` 和原始 media metadata。
- `MessagePart.to_attachment_ref()` 会把 `metadata.size` 映射到 `AttachmentRef.size`。
- `BACKEND_INTERFACE.md` 已说明平台 adapter 当前只保证附件引用结构，不保证下载、OCR、ASR 或文本化。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_platform_adapters.py tests/test_platforms_core.py tests/test_gateway_commands.py -q
uv run pytest -q
```

结果：目标测试 `56 passed`；全量 `723 passed`。

## 已完成方向：Platform Attachment Resolve v1

状态：已完成链路修正和扩展点，并通过全量回归。

已完成：

- 新增 `attachments.resolve_inbound`、`attachments.cache_inbound`、`attachments.download_urls`、`attachments.download_platform_files` 配置，并同步 example / 配置文档 / config registry。
- `AttachmentStore` 新增 `DownloadedAttachment` 和 `store_downloaded()`，平台下载结果统一进入附件缓存、hash 去重和索引链路。
- `BasePlatformAdapter` 新增 `prepare_inbound_attachments()`、`download_attachment()` 和 `AttachmentDownloadError`；默认真实平台 file id 下载返回稳定失败 reason，具体平台后续覆盖。
- Gateway 在授权通过、slash command 未被内部消费、busy check 通过后触发来源 adapter 准备附件；Gateway 不包含平台下载细节。
- `MultiAttachmentProcessor` 收敛为消费已准备的附件；provider 不再提前阻断本地化，只影响后续 `native` / `text` / `notice`。
- `BACKEND_INTERFACE.md` 已说明 `attachments.*`、`AttachmentRef.metadata.attachment_resolve`、成功/失败 reason 和职责边界。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_attachment_store.py tests/test_multimodal_processor.py tests/test_platforms_core.py tests/test_platform_adapters.py tests/test_gateway_commands.py tests/test_config_registry.py tests/test_config_loader.py tests/test_config_diagnostics.py tests/test_event_protocol.py tests/test_transport_multimodal.py -q
uv run pytest -q
```

结果：目标测试 `111 passed`；全量 `731 passed`。

剩余：

- QQ / WeChat 真实 `download_attachment()` 实现。
- Feishu / Telegram 真实下载器可在 QQ / WeChat 后补。
- OCR / ASR / 文件文本提取仍属于后续 multimodal describer / 工具层，不在平台下载链路内。

## 已完成方向：Platform Downloader v1

状态：已完成 QQ / WeChat 下载器首版实现。

已完成：

- QQ adapter 覆盖 `download_attachment()`，按附件类型尝试 OneBot 风格 `get_image`、`get_record`、`get_file`，群文件额外尝试 `get_group_file_url`。
- QQ 下载器支持 OneBot 返回 URL、file URI、本机绝对路径或 base64 inline 内容，统一转换为 `DownloadedAttachment` 后交给 `AttachmentStore`。
- WeChat adapter 对带 `aes_key` / `encrypt_query_param` 的 iLink CDN 媒体优先走平台下载器，避免通用 URL 缓存加密内容。
- WeChat 下载器支持 AES-ECB + PKCS7 解密，解密后的 bytes 再进入统一附件缓存。
- adapter 自己下载 URL 时复用 URL safety 检查和附件大小上限。
- `BACKEND_INTERFACE.md` 已同步 QQ / WeChat 下载能力和剩余平台边界。

已验证：

```bash
python -m compileall -q src/personal_agent tests/test_platform_adapters.py
uv run pytest tests/test_platform_adapters.py tests/test_platforms_core.py tests/test_gateway_commands.py tests/test_attachment_store.py tests/test_multimodal_processor.py tests/test_docs.py -q
uv run pytest -q
```

结果：目标测试 `76 passed`；全量 `734 passed`。

剩余：

- Feishu / Telegram 真实 `download_attachment()`。
- QQ 不同 OneBot 实现的文件下载 API 仍需结合真实服务验证。
- 微信缺少 `cdn_url` / `encrypt_query_param` / `aes_key` 的媒体仍会稳定失败。

## 当前分工约定

- 历史清理分支允许同时整理前后端遗留，但代码行为变更仍需按版本拆分提交。
- 后端接口变更必须同步 `BACKEND_INTERFACE.md`。
- 前端 Codex 如果需要字段或接口，应通过 `FRONTEND_INTERFACE_REQUIREMENTS.md` 明确写出小需求。
- `CLAUDE.md` 不处理。
- Skill usage 运行数据写入 `data/skills/usage.json`，不再写入源码目录。

## 已完成方向：Provider / Transport Cache

状态：v1/v2/v3 已完成并提交。

已完成：

- `ProviderProfile` 已增加 cache capability：`cache_strategy`, `supports_cache_usage`, `cache_usage_fields`, `cacheable_blocks`。
- `BaseTransport` 已增加 cache diagnostics、usage normalization、request hash 与 `LLMRequestPlan` 支持。
- Anthropic explicit cache 策略已优化：保留 stable system cache marker，不再默认标记最后一条动态 message。
- DeepSeek / OpenAI-compatible transport 保持 prefix-cache 友好布局，不添加非标准 cache 字段，并解析 provider cache usage。
- agent loop 已能向支持的 transport 传入 request plan。
- `llm_end`、runtime health、doctor、`BACKEND_INTERFACE.md` 已暴露 cache usage / diagnostics。

验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_transport_cache.py tests/test_agent_loop.py tests/test_cli.py tests/test_runtime.py -q
uv run pytest -q
```

结果：`68 passed`，全量 `667 passed`。

## 已完成方向：历史入口与运行数据清理

状态：已完成并提交。

已完成：

- 新增 `config.yaml.example` 作为可发布模板，当前 `config.yaml` 保留本机路径配置。
- `personal-agent chat` 默认启动 inline TUI；`--simple` 旧 REPL 和 classic `TerminalRenderer` 已移除。
- `python -m personal_agent` 统一转发 Typer CLI，不再维护 `--cli` / `--ingest` / `--wechat-login` 旧参数分发。
- 微信登录迁移为 `personal-agent wechat-login`；文件记忆导入迁移为 `personal-agent memory ingest <path>`。
- Skill usage 运行数据迁到 `data/skills/usage.json`，源码目录不再跟踪 `.usage.json`。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest -q
```

结果：全量 `708 passed`。

## 已完成方向：Turn Report 持久化

目标：把内存态 `AgentTurnReport` 升级为可长期查询的后端审计链路，并和已落库的 `tool_runs` 通过 `turn_id/session_key` 关联。

### 2026-07-06 v1/v2/v3 实施进度

状态：已完成实现并通过全量回归。

已完成：

- SQLite 新增 `turn_reports` 表，常用查询字段列化，完整报告保存在 `report_json`。
- `Database` / `SessionStore` 增加 turn report 保存、最近查询、详情查询、摘要查询。
- `ConversationService.run_turn_events(...)` 在 turn 结束后持久化 turn report；落库失败只记录日志，不影响用户对话。
- 保留现有内存 ring buffer 行为，新增持久化查询入口：
  - `recent_persisted_turn_reports(...)`
  - `get_persisted_turn_report(...)`
  - `tool_runs_for_turn_report(report_id)`
  - `persisted_turn_report_summary()`
- `tool_runs` 查询支持 `turn_id` 过滤，便于和 turn report 关联。
- runtime health / doctor 增加 `runtime.turns.persisted` 摘要。
- `BACKEND_INTERFACE.md` 已同步 Turn Reports 和 doctor 字段。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_database.py tests/test_conversation_service.py tests/test_runtime.py tests/test_cli.py -q
uv run pytest -q
```

结果：针对性 `76 passed`，全量 `668 passed`。

下一步：

- 如前端需要历史详情 UI，可基于 `ConversationService.recent_persisted_turn_reports(...)` 和 `tool_runs_for_turn_report(...)` 接入。

## 当前推进方向：Activity 稳定结构化接口

状态：v1/v2/v3 已完成并提交。

目标：给 inline TUI / future desktop-web 提供稳定 Activity 接口，用于查看当前系统正在运行的子 agent、后台进程和 gateway agent。

已完成：

- v1：新增 `personal_agent.activity` 聚合层，统一 summary/list/detail 结构；后台进程工具增加 `process_snapshot(...)`、`process_detail(...)`、`process_choices(...)`；runtime health 增加 `activity`。
- v2：新增 `/activity [agents|processes|gateway] [id]`；`CommandResult` 增加可选 `kind` / `payload`；`ConversationCommandRuntime` 提供 `activity_snapshot(...)`、`activity_detail(...)`、`activity_choices(...)`。
- v3：补齐前端便捷字段 `task_preview`、`command_preview`、`has_stdout`、`has_stderr`、`stdout_bytes`、`stderr_bytes`、`output_preview`；新增 `slash_command_metadata(...)` 和 `slash_argument_choices(...)`；activity 动态 provider 为 `activity_agents`、`activity_processes`、`activity_gateway`。
- `BACKEND_INTERFACE.md` 已同步 Activity payload、detail payload、slash metadata 和动态候选契约。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_activity.py tests/test_commands.py tests/test_conversation_command_runtime.py tests/test_tui_app.py -q
```

结果：`49 passed`。

下一步：

- 跑全量回归，确认没有影响旧命令和 CLI/TUI。
- 前端接入后如果需要 activity 历史分页或 gateway 已完成记录，再扩展持久化层；当前 gateway detail 只覆盖运行中的 gateway agent。

## 已完成方向：Usage / Context 语义修正

状态：已完成实现并通过聚焦验证。

目标：修正前端 context meter 和 `/usage` 工具计数的语义错位。

已完成：

- `llm_start` / `llm_end` 新增 `context_used_tokens`、`context_remaining_tokens`、`context_percent`、`context_budget`。
- `AgentTurnReport.llm` 同步记录最新 context 估算，历史详情和实时事件语义一致。
- `input_tokens` / `output_tokens` 保持为 provider 最近一次 API 调用消耗，不再建议前端用它们作为当前上下文占用。
- CLI shell / TUI 状态栏优先使用 `context_used_tokens`，没有新字段时回退旧逻辑。
- `/usage` 将“本轮工具调用”改为“最近一轮工具执行”和“单轮工具上限”，避免常见 `0 / 20` 误导。
- `BACKEND_INTERFACE.md` 已同步 context 字段和 `/usage` 语义。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_event_protocol.py tests/test_agent_loop.py tests/test_commands.py tests/test_conversation_service.py tests/test_tui_renderer.py tests/test_tui_layout.py -q
uv run pytest -q
```

结果：聚焦 `108 passed`，全量 `684 passed`。

## 已完成方向：LLM 上下文窗口显式配置

状态：已完成实现并通过全量验证。

背景：中转站自定义模型名（例如 `gpt-5.5`）无法被 `_detect_context_window(...)` 准确识别时，后端会回退到默认 `64000`，导致前端 context meter、`/usage` 和 turn report 显示的上下文窗口偏小。

已完成：

- 新增 `LLM_CONTEXT_WINDOW` / `llm.context_window` 配置，默认 `0` 表示继续按模型名自动推断；正整数会覆盖推断结果。
- `ProviderProfile.context_window` 创建时优先读取显式配置，DeepSeek/OpenAI/Anthropic/OpenRouter 统一生效。
- `build_context_budget(...)` 和 `personal-agent tokens session` 会使用显式上下文窗口。
- `doctor` / config diagnostics 增加 `LLM_CONTEXT_WINDOW` 校验和 env 报告字段。
- `config.yaml.example`、`.env.example`、`docs/configuration.md`、`BACKEND_INTERFACE.md` 已同步该配置含义和前端可见影响。
- `llm` 顶层不再整体视为废弃；仅旧的 `llm.provider` / `llm.model` / `llm.api_key` 等字段继续给迁移提示，`llm.context_window` 合法。

已验证：

```bash
uv run pytest tests/test_config_loader.py tests/test_config_registry.py tests/test_config_diagnostics.py tests/test_transport_cache.py -q
python -m compileall -q src/personal_agent
uv run pytest -q
```

结果：聚焦 `43 passed`，全量 `781 passed`。

## 已完成方向：权限拒绝工具循环止血

状态：已完成实现并通过聚焦验证。

背景：微信实测中，模型在网络工具未授权时连续调用 `web_search` 30 次，全部被 `permission_required` 拒绝，单轮持续约 268 秒并消耗大量 tokens。根因是 agent loop 将授权拒绝当作普通 tool result 继续喂给模型，模型反复尝试同一工具。

已完成：

- `ToolExecutionResult` 增加结构化 guard metadata：`guard_stage`, `reason_code`, `permission_category`, `permission_decision`, `required_allow`, `execution_mode`, `grant_matched`。
- `run_conversation(...)` 在一批工具结果全部为 `permission_required` 授权拒绝时，直接结束当前 turn，并返回 `/allow <category>` 后重试的提示。
- 网络工具未授权时会返回“网络工具需要授权，本轮已停止。请发送 /allow network 后重试。”，不再进入重复 tool-call 循环。
- `BACKEND_INTERFACE.md` 已同步该行为：`tool_end` / Tool Runs / Turn Reports 仍记录 denied 工具结果，随后会有一条 `assistant_message` 结束本轮。
- `tests/conftest.py` 增加 audit log 隔离，测试期间写入临时 `audit.log`，避免污染真实 `data/audit.log`。

已验证：

```bash
uv run pytest tests/test_agent_loop.py tests/test_tool_pipeline.py tests/test_config_loader.py tests/test_config_registry.py tests/test_config_diagnostics.py tests/test_transport_cache.py -q
python -m compileall -q src/personal_agent
uv run pytest -q
```

结果：聚焦 `131 passed`，全量 `782 passed`。

## 已完成方向：平台确认回复队列旁路

状态：已完成实现并通过聚焦验证。

背景：微信实测中，工具触发授权确认后回复 `1` 仍然没有放行。日志显示 `web_search` 的授权确认等待了约 `120s` 后超时拒绝，随后用户的 `1` 被当作普通聊天消息进入数据库。根因是平台适配器会串行处理同一会话消息：原消息在等待确认回复，而确认回复被排到原消息后面，形成等待死锁。

已完成：

- `BasePlatformAdapter` 新增窄口径 message bypass predicate，只在网关判断当前 session 存在 pending tool confirmation 时启用。
- bypass 消息不占用普通 active session，不抢同一 chat lock，不影响普通同会话消息继续串行。
- `Gateway` 在平台 adapter 启动时注入 pending-confirm 判断，确认回复 `1/2/3`、无效回复、`/stop` 都能及时进入网关处理。
- 新增回归测试覆盖真实平台适配器路径：原消息等待确认时，同 session 的 `1` 不再进入 pending queue，而是直接放行当前工具调用。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_gateway_commands.py::test_platform_confirmation_reply_bypasses_same_session_queue tests/test_gateway_commands.py::test_gateway_async_confirmation_allows_once tests/test_gateway_commands.py::test_base_adapter_same_session_messages_are_serialized -q
uv run pytest tests/test_gateway_commands.py -q
```

结果：聚焦 `3 passed`，gateway 命令测试 `38 passed`。

## 已完成方向：平台长回复发送保护

状态：已完成实现并通过聚焦验证。

背景：平台消息长度有限，模型一次回复过长时，平台可能发送失败；同时 gateway/CLI 对空 `final_response` 的兜底是 `...`，用户侧很难判断是模型空回复、发送失败，还是内容被截断。

已完成：

- `BasePlatformAdapter._send_with_retry(...)` 按平台 `capabilities.max_text_length` 自动拆分长文本后逐段发送，普通平台不配置长度时保持原行为。
- 新增通用 `split_text_for_platform(...)`，会在必要时硬切长行，保证每段不超过平台限制。
- WeChat adapter 复用通用分片逻辑，不再因为“保留代码块”让超长代码块单段超过 2000 字符。
- Conversation service 新增统一空回复兜底文案；gateway 和 CLI 不再把空回复显示成 `...`。
- 新增回归测试覆盖平台基类分片、微信超长代码块分片、gateway 空 final 响应。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_gateway_commands.py::test_base_adapter_splits_outbound_text_by_platform_limit tests/test_gateway_commands.py::test_gateway_empty_final_response_uses_clear_message tests/test_gateway_commands.py::test_base_adapter_format_send_error_strips_formatting_and_retries tests/test_platform_adapters.py::test_wechat_send_splits_long_text tests/test_platform_adapters.py::test_wechat_send_splits_long_code_fence -q
uv run pytest tests/test_gateway_commands.py tests/test_platform_adapters.py -q
```

结果：聚焦 `5 passed`，gateway/platform 测试 `69 passed`。

## 2026-07-08：Runtime Steer v1-v3

状态：已完成实现并通过聚焦验证。

背景：用户在 gateway/CLI 长任务运行中，希望能补充“回答短一点”“先停一下这个方向”等修正，而不是只能等待本轮完成或用 `/stop` 中断。平台 adapter 原本会把同会话后续消息排队，普通文本也无法安全区分是新 turn 还是运行中修正。

已完成：

- 新增 `SteerManager` / `SteerSignal`，按 `session_key + turn_id` 管理运行中修正，支持 pending、consumed、expired 状态。
- `ConversationService` 每轮分配稳定 `turn_id`，登记 active turn，并向 `run_conversation(...)` 传入 steer manager。
- agent loop 在循环边界消费 steer，追加 `[高优先级运行中用户指令]` user message；如果修正在 LLM 最终答案返回时到达，会保留旧 assistant 文本后注入修正并继续下一次 LLM 调用。
- 新增 slash command `/steer <text>`，非运行中会返回明确提示，运行中会返回 `st_xxx` 回执。
- Gateway busy 期间允许 `/steer` 像确认回复一样旁路 adapter 队列；普通 busy 文本仍然不会进入当前 turn。
- `Gateway.health_snapshot()` 新增 `pending_steer_count`、`active_steer_sessions`、`steer`，并在 `running_agent_runs[]` 暴露 `active_turn_id` / `pending_steers`。
- `ConversationEventType` 新增 `steer_consumed`；`AgentTurnReport.report.steer` 记录本轮 received / consumed / expired / pending 和安全文本预览。
- `/activity gateway` item 同步 `active_turn_id` / `pending_steers`，前端可在运行列表展示。
- `BACKEND_INTERFACE.md` 已新增 Runtime Steer 合约，前端只需按文档接 slash command / health / event，不需要后端改 TUI 交互。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_conversation_steer.py tests/test_agent_loop.py tests/test_conversation_service.py tests/test_commands.py tests/test_gateway_commands.py tests/test_event_protocol.py -q
uv run pytest -q
```

结果：聚焦 `110 passed`，全量 `798 passed`。

## 暂停期后续 Backlog

- 真实 provider cache API 验证：用实际 provider 响应确认 cache usage 字段与命中率，尤其是中转站是否返回标准 usage。
- 上下文压缩质量：优化长对话压缩后的任务状态、路径、工具结果保留。
- 工具失败恢复策略：改进工具错误、权限拒绝、格式错误后的模型恢复提示；暂不做“声称调用工具但无 tool_call”的正则触发 retry。
- 平台附件下载器补全：Feishu / Telegram 可等真实使用需要再做，QQ / WeChat 已有首版。
- OCR / ASR / vision：当前后端已预留扩展点，本地服务或中转站视觉模型可后续按需接入。

最近一次验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_transport_responses.py -q
```

结果：Responses transport 聚焦测试 `9 passed`；最近一次记录的全量回归为 `798 passed`。
## 2026-07-13：工具循环与 Memory Router 修复

状态：已完成实现并通过全量验证。

背景：微信实测中，模型在工具成功后仍反复调用相同工具，并在工具配额耗尽后继续请求；迭代上限还可能产生空回复。Memory Router 在运行中安装依赖或恢复配置后也可能长期停留在 fallback，且旧记录的来源提供器容易被误判为当前有效提供器。

已完成：

- `LLMRequestPlan` 新增 `turn_tail`，保持本轮消息严格按照“当前用户 → 工具调用 → 工具结果/steer”发送，不再为了 prompt cache 把原始用户消息移动到工具结果之后。
- agent loop 对同一轮内完全相同且已经成功的工具调用执行熔断，返回上一次结果摘要，不重复产生副作用。
- 任一工具结果出现 `quota_exceeded` 时立即硬停止；迭代预算耗尽时返回确定性的非空消息。
- External Memory Router 在前台 scoped 操作前按冷却时间尝试恢复主提供器，并在路由状态变化时及时持久化 `provider_state`。
- Memory `search/list` 记录新增 `source_provider` 与 `effective_provider`，区分历史写入来源和当前路由。
- Lumora 返回实际落库后的 `memory_id/content/previous_content`，并记录 search、resolve、apply、embedding、Qdrant 各阶段耗时。
- 归档读取以独立 `index_status` 列为权威值，避免 `metadata_json` 中的旧 `pending` 覆盖数据库中的 `ready`。
- `BACKEND_INTERFACE.md` 已同步新增诊断字段和记忆提供器字段语义。

阶段提交：

- `ac33187 Preserve tool result message order`
- `4499bf0 Stop runaway tool call loops`
- `4e8d0ec Recover external memory providers`

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_agent_loop.py tests/test_tool_pipeline.py -q
uv run pytest tests/test_*memory*.py tests/test_internal_memory*.py -q
uv run pytest -q
```

结果：工具循环聚焦 `95 passed`，Memory 聚焦 `29 passed`，全量 `858 passed`。

### 后续实测修复：scope fallback、可续跑迁移与主动探测

微信实测确认一次空消息的 `ResponseHandlingException` 会让全局 Router 粘在 fallback，恢复还会在前台同步迁移59条 observation。进一步完成：

- provider route state 改为按 `user_id + session_key + profile` 隔离，一个 scope 降级不影响其他 scope。
- 幂等主提供器搜索和健康 probe 都会重试一次，并保留异常 cause/context。
- 主提供器恢复改为“readiness -> 真实 probe -> 立即切换”，不再同步迁移 backlog。
- observations schema v4 记录逐条迁移尝试次数、最近错误和更新时间；review worker 每次后台迁移一条。
- memories schema v4 记录逐条索引尝试次数、最近错误和更新时间；Lumora worker 每次后台修复一条 pending index。
- `memory doctor` 执行真实百炼 embedding + Qdrant probe，同时显示当前 scope 和全局 backlog。
- 不同 scope 并发恢复通过 recovery lock 串行切换 provider，旧 client 延迟到 Router 关闭时释放。

阶段提交：

- `91c58a8 Isolate memory provider state by scope`
- `f26ae98 Resume memory migrations in background`
- `97e1dfe Probe and repair Lumora memory health`

真实配置验证：probe 状态为 `ok`，Lumora 可用；诊断准确报告全局 migration pending `59`、index pending `1`，未在前台自动迁移这些数据。

## 2026-07-13：Execution Security Pipeline v2

状态：已完成实现并通过全量验证。

背景：旧 `ModeProfile -> ExecutionPolicy -> permission category -> /allow` 同时表达自动化程度、沙箱与临时授权，Mode 名称和 policy 发生分裂；工具 hooks、嵌套 `tool_call`、MCP 和 Bash/后台进程也缺少统一的资源审批语义。

已完成：

- Mode 统一为 `Read Only / Ask First / Local Auto / Full Auto`，稳定 ID 为 `read-only / ask-first / local-auto / full-auto`；每个 session 持有独立内存安全状态，Mode 切换清空 grants。
- 新增 filesystem/network permission profile、`on-request/never` approval policy、`auto/cached/prompt/deny` tool approval 与统一 `permissions.grant_ttl_minutes`。
- 工具调用在 proposal hooks 后冻结参数，再执行 hard precheck、工具/资源审批与 dispatch；确认后不重新调用模型，`tool_call` 嵌套调用继续进入统一 executor。
- 文件、网络、Bash、后台进程和 HTTP MCP 暴露具体资源；`/permissions` 和前端事件新增 tool/resource grant 与 `requested_resources` 诊断。
- Bash 与后台进程支持 Bubblewrap 文件系统隔离；`auto` 可诊断降级，显式 `bwrap` 不可用时失败关闭。
- MCP 默认 cached 审批、不可并行、不可自动重试；stdio 复用进程沙箱，HTTP 默认 HTTPS 并检查所有 DNS 地址；工具列表、schema、文本、structured content 和 artifact 都有硬上限。
- 配置模板、README、配置/架构/能力文档、`BACKEND_INTERFACE.md` 和根目录 `TODO.md` 已同步新语义；旧 execution policy/category 仅保留兼容。

阶段提交：

- `bcbcc5d Document security refactor prerequisites`
- `ff0041b Define permission profiles and session security state`
- `9d20e3d Refactor tool security and approval pipeline`
- `fe563f5 Add resource-scoped tool approvals`
- `8e04989 Isolate shell processes with bubblewrap`
- `8e90ff7 Harden MCP transports and payloads`

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest -q
```

结果：`904 passed`。

真实环境待复测：GitHub HTTP MCP、需要联网安装/运行的 stdio MCP，以及宿主是否支持 Bubblewrap network namespace。`doctor` 会报告 network namespace 降级；DNS 检查到实际连接之间的 TOCTOU 风险已记录在 `TODO.md`。

## 2026-07-14：重复工具调用收尾修复

状态：已完成实现并通过回归验证。

微信实测发现，相同工具调用第二次被熔断后，Agent Loop 会直接把内部诊断和截断的原始工具结果作为最终回复。现已调整为：同一轮内相同参数的工具调用最多成功执行 3 次；第 4 次请求不再执行，并只进行一次禁用工具的 LLM 收尾，让模型根据已有工具结果正常回答。收尾指令仅存在于该次 API 请求，不写入持久化会话；收尾为空或仍请求工具时返回不含原始结果的安全兜底文案。

接口同步：新增 `retry.category="duplicate_tool_call"` 的恢复事件语义，已记录在 `BACKEND_INTERFACE.md`。

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_agent_loop.py tests/test_mcp.py::test_tool_search_discovers_mcp_tools -q
uv run pytest -q -k 'not test_refresh_mode_updates_status_after_command and not test_partial_slash_command_shows_matching_candidates'
```

结果：聚焦测试 `23 passed`；排除两项仍断言旧 `/allow` / TUI Mode 行为的既有测试后，全量回归 `879 passed, 2 deselected`。

## 2026-07-16：工具配额无工具收尾

状态：已完成实现并通过回归验证。

- 单轮工具调用总配额触发后不再直接返回固定停止文案，而是保留已有工具结果并额外调用一次 LLM；该调用不暴露任何工具，只生成面向用户的最终总结。
- 模型收尾为空或调用失败时仍返回明确的固定兜底文案，避免空回复。
- 新增 `retry.category="tool_quota"`，用于表达“配额已触发，正在整理已有结果”的可恢复状态。
- 主 Agent 默认及当前测试配置由 20 次工具调用提高到 40 次；迭代预算由 30 提高到 50，确保连续 40 次单工具调用后仍有一次总结空间。委派子 Agent 配额保持不变。
- 小鹿集成插件实测发现 GitHub MCP 的 `issue_write` 命名未被原有动作前缀规则识别；写保护现同时覆盖危险动作前缀和 `*_write` 后缀，包括 `issue_write` 与 `pull_request_review_write`。

已验证：

```bash
python -m compileall -q src/personal_agent plugins/github_assistant
uv run pytest tests/test_agent_loop.py tests/test_integration_plugins.py tests/test_config_loader.py tests/test_config_diagnostics.py tests/test_cli_entrypoints.py -q
uv run pytest -q
```

结果：聚焦测试 `74 passed`；全量回归 `958 passed`。

### 文件工具快照收敛

- 文件写入前快照改用微秒时间戳，避免同名文件在同一秒连续修改时覆盖备份。
- 每个原文件只保留最近 5 份快照，其他文件的快照互不影响。
- `checkpoints/` 加入 Git 忽略规则，运行时安全快照不再污染 worktree。
- 快照聚焦测试 `63 passed`，全量回归 `959 passed`。

## 2026-07-17：MCP 后台启动

状态：已完成实现并通过回归与真实环境验证。

- MCP Runtime 启动与首次连接等待已拆分；AppRuntime 调度各 server 后继续初始化核心服务，`doctor` 和 `serve --dry-run` 仍显式等待稳定诊断。
- MCP 连接任务支持启动期间取消和有界关闭，单个慢连接不再拖住 Gateway 启动或退出。
- MCP 健康快照新增 enabled、starting、degraded、failed 和首次尝试耗时；工具不可用原因显示具体 runtime 状态，不再只返回 `check_fn returned False`。
- Tool Registry generation 保证后台注册只在 Agent 下一轮刷新，不改变当前 turn 的工具快照。
- Time MCP 增加 stdio 网络权限，避免 `uvx` 在 Bubblewrap 中因无法访问包索引反复等待 DNS 失败。

阶段提交：

- `a8103f1 Start MCP runtimes in background`
- `db4bfac Expose background MCP readiness`
- `2b9916f Document background MCP readiness`

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_mcp_runtime.py tests/test_mcp.py tests/test_runtime.py tests/test_tool_registry.py tests/test_agent_factory.py tests/test_cli.py tests/test_cli_entrypoints.py tests/test_config_diagnostics.py -q
uv run pytest -q
```

结果：聚焦回归 `127 passed`，全量回归 `963 passed`。

真实配置测速：核心 Runtime 在 `1.868s` 返回，此时 8 个已启用 MCP 均在后台 starting；所有首次尝试在 `20.477s` 完成，其中 GitHub 单独耗时 `20.341s`，但不再阻塞核心。最终 8/8 server ready、85 个 MCP 工具，Time MCP 在 `1.748s` 正常上线；Runtime 关闭耗时 `2.319s`。

真实 GitHub 写保护复测：在 GitHub runtime 为 ready 时通过完整 executor pipeline 调用 `mcp__github__issue_write`，结果为 `status=denied`、`category=hook`，错误为 `GitHub Assistant write operations are disabled by plugin configuration`，请求未到达远端。

## 2026-07-17：Lumora 内部检索 Backend

状态：已完成实现并通过全量回归与真实远程环境验证。

- Lumora 外部记忆提供器内部拆分为 embedding、vector、keyword、fusion 与可选 reranker Backend，通过轻量 Registry 和工厂组装；公共接口不包含 Qdrant、PostgreSQL 或本地文件数据库的专属字段。
- 当前内置实现为 OpenAI Compatible Embedding、Qdrant、SQLite FTS5、Weighted RRF 和 No-op Reranker。Qdrant 同一 Backend 同时支持远程 `url` 与本地持久化 `path`，真实本地测试覆盖关闭重开和 scope 过滤。
- 配置迁入 `memory.providers.lumora`，删除 embedding `api_mode` 和公共 Qdrant 配置；只校验当前选中 Backend，密钥仍统一通过 `Settings` 解析。
- 混合检索并发运行 semantic 与 keyword 通道，保留原始相似度/BM25 分数；单通道故障会降级，双通道故障才交由 External Memory Router fallback，Reranker 故障返回 Fusion 结果。
- SQLite Archive 继续作为权威数据源，新增 vector/keyword 独立索引状态、Backend fingerprint 与 generation；切换索引实现后从 Archive 重建，不迁移旧向量库数据。
- 新增 `personal-agent memory reindex --index all|vector|keyword`，并扩展 memory doctor 的组件状态、fingerprint、generation 与分索引 backlog。

阶段提交：

- `c430b58 Define Lumora backend contracts`
- `7841415 Configure Lumora retrieval backends`
- `97cd71d Track Lumora backend indexes`

已验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest -q
uv run personal-agent memory doctor --json
```

结果：全量回归 `989 passed`。真实 Memory doctor 使用现有百炼 Embedding 与远程 Qdrant 探测成功，Lumora 保持主提供器，embedding/vector/keyword/fusion 均为 ready、reranker 为 disabled；102 条现有记忆的 vector 与 keyword 索引均为 ready，全局索引 pending 为 0。

后续已将当前 `config.yaml` 切换为 Qdrant Local `./data/memory/qdrant`。全量重建增加按 Embedding Backend 限制的批处理；百炼 `text-embedding-v4` 使用每批 10 条，避免超出接口批量上限。现有 Archive 的 102 条记忆已全部迁移到本地 Qdrant，真实 doctor 显示本地 fingerprint、102 条 vector ready、0 pending，Lumora 未 fallback。远程 Qdrant collection 未删除。

### 微信统一消息管线修复

Conversation Runtime 重构后，微信 Adapter 的原始更新解析器仍命名为 `_process_message`，覆盖了 `BasePlatformAdapter._process_message(MessageEvent)`。微信解析完成调用 `handle_message()` 后会通过多态递归回旧解析器，导致 `MessageEvent.get` 异常。现已将微信侧入口改为 `_process_update(dict)`，结构化事件统一交回 Base 消息管线，并新增单次进入回归测试。

验证：平台/网关聚焦回归 `83 passed`，全量回归 `991 passed`。

### Memory 列表与空回复恢复

真实微信会话发现 `memory(action=list)` 返回约 14 KB 完整记录，被工具管线在 8,000 字符处截断为无效 JSON；模型在工具后连续空回复，Agent Loop 最终把内部 `(empty response from model)` 占位符发送给用户，并将三条重试指令持久化为用户消息。

- Memory list 默认最多返回 10 条精简记录，支持 `limit`，省略 scope/metadata 并限制单条 content，响应包含 `returned/limit/has_more` 且保持有效 JSON。
- 工具后空回复改为一次无工具强制收尾；仍为空时返回稳定中文提示，不再进入两轮普通空回复重试。
- 普通空回复、无效响应和无效工具参数的重试指令改为请求级临时上下文，不写入 transcript。
- 已有会话中的旧版空回复重试序列会在构建模型请求时被过滤，但数据库原始记录保持不变。

## 2026-07-17：Playwright 出站 Artifact 物化修复

状态：代码与聚焦回归已完成，等待重启 Gateway 后进行微信全链路复测。

- 小鹿真实测试确认 Playwright 截图调用成功，但官方 MCP 仅返回 Markdown 相对路径，原连接层只处理原生 image/audio/resource block，导致当前 turn 没有 `artifact_id`。
- MCP server 配置新增显式 `artifact_roots` 与 `artifact_extensions`。只有受信输出根目录内的相对链接会提升为 file resource；绝对路径、路径穿越、符号链接、未允许扩展名与超限内容均受控。
- Browser Operator 固定使用 `data/mcp/playwright/`，支持 PNG/JPEG/PDF/WebM 输出；ArtifactStore 继续负责复制、哈希、session/turn scope 与最终 `artifact_id`。
- stdio MCP 在 legacy backend 下也显式使用 `data/mcp` 工作目录，保证 Bubblewrap 与非 Bubblewrap 环境行为一致。
- 新增 Playwright 实际 Markdown 格式、安全边界，以及 MCP resource 到模型可见 `artifact_id` 的端到端聚焦测试。

验证：聚焦回归 `86 passed`；全量回归 `1019 passed`，仅有原有飞书 SDK 弃用警告。

真实微信首轮复测发现官方 Playwright MCP 在显式传入 `filename` 时仍相对进程 cwd 写文件，没有使用 `--output-dir playwright`。MCP Runtime 因此新增受校验的 server 级相对 `work_dir`；Browser Operator 的 cwd、`--output-dir .` 与 `artifact_roots: [.]` 现统一指向 `data/mcp/playwright/`，其他 MCP 保持原工作目录。使用官方 `@playwright/mcp@0.0.78` 的真实隔离冒烟已确认截图文件存在、连接层产生 1 个 `image/png` resource；聚焦回归 `56 passed`，全量回归 `1022 passed`。等待 Gateway 再次重启后进行微信复测。

第二轮微信复测确认内层 Playwright MCP 已成功物化 `StoredArtifactRef`，Tool Run 中存在完整 `artifact_id`、MIME、大小和 MCP 来源；失败来自外层 `tool_call` 把已存储引用当作原始 `ToolArtifact` 再次物化，因其不再携带 data/file URI 而返回 `artifact_content_missing`。执行器现仅物化原始 handler artifact，嵌套 `ToolExecutionResult` 中的受控引用直接透传。新增嵌套 Artifact 回归，聚焦测试 `71 passed`，全量回归 `1023 passed`。

第三轮微信复测成功选择并投递 Artifact，但暴露两个后续问题：Codex Responses 中转把内部分析与 `assistant_final` 通道标记展平为最终文本；PNG Artifact 保留 `resource` kind，DeliveryPlanner 将其降级为 file，微信服务端虽接受消息但客户端显示失败占位。Codex Responses 现缓冲并只输出 `assistant_final` 后的用户文本，空 final 交给现有工具后安全收尾；Artifact 与 Delivery 统一按 MIME 识别图片/音频/视频，旧 resource 记录同样兼容。聚焦回归 `26 passed`，全量回归 `1026 passed`。

第四轮微信复测已走原生 `image_item`，但客户端提示“图片过期或已被清理”。对照腾讯 `openclaw-weixin` 源码确认媒体 `aes_key` 应为 `base64(hex string of 16 bytes)`，原实现误用了 raw 16-byte key 的 base64；同时原文件/视频消息类型写反，官方定义为 `FILE=4`、`VIDEO=5`。现已逐字段对齐图片、文件、视频 payload，并以固定 key 精确断言编码内容。聚焦回归 `42 passed`，全量回归 `1028 passed`。

### QQ 出站多媒体补齐

- QQ/OneBot Adapter 新增 `file_send`，与现有图片、音频、视频统一进入 Artifact/Delivery 链路。
- 出站媒体从宿主 `file://` URI 改为 `base64://` segment，解决 Lumora 在 WSL、NapCat 在 Windows 时路径不可见的问题，并避免把本地路径交给协议端。
- 平台声明 20 MiB 单附件上限与 10 个附件上限；图片、语音、视频和文件均覆盖私聊/群聊共用的 OneBot 消息发送路径。

验证：QQ/平台聚焦回归 `45 passed`，全量回归 `1031 passed`。当前未连接真实 NapCat，协议 payload 已验证，QQ 客户端端到端仍待配置后实测。

### QQ OneBot 双向链路

- QQ Adapter 新增 NapCat OneBot WebSocket Server 客户端，消息、群聊和附件事件现可直接进入 Gateway/Conversation 管线。
- `QQ_BOT_WS_URL` 作为 QQ 平台必填配置；`QQ_BOT_BASE_URL` 改为可选 HTTP action 通道。未配置 HTTP 时，发送、附件下载等 action 通过 WebSocket `echo` 请求/响应配对。
- WebSocket 使用与 NapCat 一致的 Bearer Token，支持 ping/pong、心跳事件、无效帧隔离、并发 action 和有界重连退避。断线时未完成 action 立即失败，交由上层 Delivery 重试，不会无期卡住。
- QQ 健康快照增加 WS 连接、action transport、重连次数、待处理 action、最后事件时间和机器人 QQ 号。

阶段提交：

- `af6606a Complete QQ OneBot transport`
- `1e42ca0 Document QQ NapCat setup`

验证：QQ/平台聚焦回归 `81 passed`；全量回归 `1035 passed`，仅保留原有飞书 SDK 弃用警告。当前本地 `.env` 尚未配置 QQ，真实 NapCat/QQ 客户端端到端需在开启 NapCat WebSocket Server 后复测。

### QQ 受管 NapCat 运行时

- `platforms/qq` 插件新增隔离的 `runtime.mode: external|managed` 配置，受管进程生命周期不进入 Gateway 或 Conversation 核心。
- `serve` 启动 QQ 时先探测现有 WebSocket，只在无法连接时执行用户配置的 NapCat argv；Gateway 重试产生的 adapter 共享同一 supervisor，不会重复启动。
- 执行文件必须为绝对存在路径，不经过 shell；标准输出写入 `data/logs/napcat.log`。进程支持启动等待、重启节流、健康快照和可配置的退出清理。

阶段提交：

- `451d026 Manage NapCat with QQ plugin`
- `2f7472d Document managed NapCat runtime`

验证：QQ/插件/配置聚焦回归 `127 passed`；全量回归 `1043 passed`，仅保留原有飞书 SDK 弃用警告。当前 Windows 用户常见目录未发现 NapCat 可执行文件，因此未向本机 `config.yaml` 写入无效受管路径。
