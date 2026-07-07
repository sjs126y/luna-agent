# Backend Progress

更新时间：2026-07-07 18:05 CST

## 交接定位

这个文档只记录后端线进度，给后续接手后端的 Codex 使用。前端 TUI / desktop / prompt_toolkit 真实终端问题交给前端线处理；后端线只负责事件、接口、agent runtime、工具执行、权限、配置、平台适配、provider / transport 等基础能力。

当前工作分支：`feature/backend-next`

权威接口文档：

- `BACKEND_INTERFACE.md`：前端消费后端事件、slash commands、tool metadata、tool runs 等接口的主文档。
- `FRONTEND_INTERFACE_REQUIREMENTS.md`：前端提出的后端字段/接口需求入口。
- `CODEX_HANDOFF.md`：总交接文档，记录前后端分工和整体状态。

## 当前后端状态

后端主干能力已经比较完整；`feature/backend-provider-cache` 和历史清理分支已合并回主分支，当前分支用于继续后端收敛。最近已完成并验证的方向包括：

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

## 后续可评估方向

- 真实 provider cache API 验证：用实际 provider 响应确认 cache usage 字段与命中率。
- 上下文压缩质量：优化长对话压缩后的任务状态、路径、工具结果保留。
- 工具失败恢复策略：改进工具错误、权限拒绝、格式错误后的模型恢复提示；暂不做“声称调用工具但无 tool_call”的正则触发 retry。

最近一次验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest tests/test_agent_factory.py tests/test_agent_loop.py tests/test_transport_cache.py tests/test_context_budget.py -q
uv run pytest -q
```

结果：聚焦 `29 passed`，全量 `685 passed`。
