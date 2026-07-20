<div align="center">

<h1>配置说明</h1>

<p><strong>用 <code>.env</code> 管密钥，用 <code>config.yaml</code> 管行为</strong></p>

<p>
  <img src="https://img.shields.io/badge/secrets-.env-E34F26" alt="Secrets in env">
  <img src="https://img.shields.io/badge/behavior-config.yaml-2EA44F" alt="Behavior in config yaml">
  <img src="https://img.shields.io/badge/validation-doctor-0A84FF" alt="Doctor validation">
</p>

<p>
  <a href="../README.md">项目首页</a> ·
  <a href="README.md">文档中心</a> ·
  <a href="operations.md">运维</a> ·
  <a href="platforms.md">平台</a>
</p>

</div>

---

## 快速定位

| 我想配置 | 直接看 |
| --- | --- |
| 模型、API Key、平台 Token | [`.env`](#env) |
| Mode、沙箱、插件和 Memory | [`config.yaml`](#configyaml) |
| MCP Server | [MCP Server](#mcp-server) |
| 工具审批与安全模式 | [执行模式与权限](#执行模式与权限) |
| 图片、文件和 Artifact | [多模态配置](#多模态配置) |
| 旧配置升级 | [旧配置迁移](#旧配置迁移) |

Luna Agent 使用两类配置文件：

- `.env`：secret、LLM provider、平台 token。
- `config.yaml`：运行行为、目录、插件、记忆、沙箱、MCP、认证和会话。

生成最小配置：

```bash
uv run luna-agent init --profile local --copy-env --fix-dirs
uv run luna-agent init --check
```

## .env

LLM 基础字段：

```dotenv
LLM_PROVIDER=deepseek
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com/anthropic
LLM_MODEL=deepseek-chat
LLM_API_MODE=auto
LLM_MAX_TOKENS=4096
LLM_CONTEXT_WINDOW=0
LLM_REASONING_EFFORT=
```

`LLM_API_MODE` 可选 `auto` / `chat_completions` / `anthropic_messages` / `responses` / `codex_responses`。`auto` 先使用 provider 默认协议：OpenAI 为 Responses，Anthropic 和 DeepSeek 为 Messages，OpenRouter 和 xAI 为 Chat Completions；未知 provider 才根据明确的 base URL 判断，否则回退 Chat Completions。DeepSeek 官方根地址在 `auto/anthropic_messages` 下会规范化到 `/anthropic`，显式 `chat_completions` 时使用普通根地址。Codex/Ahoo 这类有特殊鉴权或语义的 Responses 中转站仍应显式设置 `LLM_API_MODE=codex_responses`，显式配置始终优先。

模型能力解析会区分“模型硬上限”和“Luna Agent 有效窗口”。`LLM_CONTEXT_WINDOW=0` 时，OpenAI provider 默认使用 `256000` 的经济窗口，其他已知模型使用 catalog 中的硬上限；无法可靠识别的新模型保守按 `256000` 处理。显式 `LLM_CONTEXT_WINDOW` 会覆盖默认值，但不会超过已知硬上限。`LLM_MAX_TOKENS` 同样不会超过已知输出上限。模型名中的 `1m`、`400k`、`256k` 等容量标记可用于未收录的中转模型；OpenRouter 静态未命中时会以 3 秒超时查询其模型元数据，并缓存 24 小时。`doctor` 和 `/usage` 会显示最终值、来源及裁剪状态。

`LLM_REASONING_EFFORT` 用于设置支持推理强度的模型。留空表示不发送该字段；常见值是 `minimal` / `low` / `medium` / `high`。Chat Completions 会发送 `reasoning_effort`，Responses / Codex Responses 会发送 `reasoning.effort`，Anthropic Messages 暂不额外映射。

xAI Grok 4.5 使用 OpenAI-compatible Chat Completions 协议：

```dotenv
LLM_PROVIDER=xai
LLM_API_KEY=your_xai_api_key
LLM_BASE_URL=https://api.x.ai/v1
LLM_MODEL=grok-4.5
LLM_API_MODE=chat_completions
```

`xai` provider 支持工具调用和图片输入；模型上下文窗口若需精确限制，可通过 `LLM_CONTEXT_WINDOW` 显式设置。模型名可按 xAI 账户实际可用模型覆盖。

平台字段按需填写：

```dotenv
TELEGRAM_BOT_TOKEN=
FEISHU_APP_ID=
FEISHU_APP_SECRET=
WEIXIN_TOKEN=
WEIXIN_ACCOUNT_ID=
WEIXIN_USER_ID=
WEIXIN_BASE_URL=https://ilinkai.weixin.qq.com
WEIXIN_CDN_BASE_URL=https://novac2c.cdn.weixin.qq.com/c2c
QQ_BOT_WS_URL=ws://127.0.0.1:16611
QQ_BOT_BASE_URL=
QQ_BOT_TOKEN=
QQ_BOT_WEBHOOK_SECRET=
```

QQ 使用 NapCat OneBot 11。`QQ_BOT_WS_URL` 是必填的 WebSocket Server 地址，用于接收 QQ 事件，也可直接发送 OneBot action。`QQ_BOT_BASE_URL` 可选；配置时出站 action 优先走 HTTP，未配置时复用 WebSocket。新配置建议先留空 HTTP，只开一个 WebSocket Server，减少 NapCat 端口和配置项。`QQ_BOT_TOKEN` 同时用于 WebSocket Bearer 鉴权和 HTTP Bearer 鉴权。

出站图片、语音、视频和文件统一通过 `base64://` message segment 传输，因此 Luna Agent 运行在 WSL、NapCat 运行在 Windows 时不需要共享本地路径。当前单附件上限为 20 MiB；更大的文件应后续接入 NapCat Stream API，避免超大 JSON 请求。

QQ 插件默认使用 `external` 模式，只连接已运行的 NapCat。需要 Luna Agent 随 `serve` 自动启动 NapCat 时，在 `config.yaml` 中开启受管模式：

```yaml
plugins:
  config:
    platforms/qq:
      runtime:
        mode: managed
        command:
          - /mnt/c/absolute/path/to/NapCatWinBootMain.exe
          - "123456789"
        working_dir: /mnt/c/absolute/path/to/napcat
        startup_timeout_seconds: 30
        stop_on_shutdown: true
        restart_grace_seconds: 30
```

`command` 是 argv 数组，第一项必须是 WSL 可访问的绝对可执行文件路径，不经过 shell。`working_dir` 可留空。插件会先探测 WebSocket；已运行时直接复用，未运行时启动 NapCat 并等待就绪。NapCat 输出写入 `data/logs/napcat.log`。只有插件自己启动的进程会在 `stop_on_shutdown: true` 时随 Luna Agent 退出；已有的外部 NapCat 不会被终止。

## config.yaml

常用配置：

```yaml
execution:
  mode: ask-first

permissions:
  grant_ttl_minutes: 60
  confirm_timeout_seconds: 120
  tool_approval:
    default_external: cached
    tools: {}
    mcp_servers: {}
  approval_reviewer:
    enabled: false
    model: ""                  # empty = current provider/model
    timeout_seconds: 12
    fallback: human             # human | deny
    allow_ttl_grant: false      # TTL grants remain user-only
    max_risk: medium            # low | medium

llm:
  context_window: 0  # auto-detect; unknown model names default to 256K

storage:
  data_dir: ./data
  log_level: INFO

plugins:
  dirs:
    - ./plugins
    - ./data/plugins
  enabled: []
  disabled: []
  config:
    examples/hello:
      greeting: hello

memory:
  external_provider: luna
  review:
    external_turn_interval: 10
    internal_turn_interval: 50
    internal_buffer_limit: 20
    snapshot_refresh_turn_interval: 20
    worker_concurrency: 2
  llm:
    provider: inherit
    max_tokens: 2048
  providers:
    luna:
      embedding:
        provider: openai_compatible
        base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
        api_key_env: DASHSCOPE_API_KEY
        model: text-embedding-v4
        dimensions: 0
      vector:
        provider: qdrant
        path: ./data/memory/qdrant
        collection: luna_memories
      keyword:
        provider: sqlite_fts5
      fusion:
        provider: weighted_rrf
      reranker:
        provider: none

multimodal:
  enabled: true
  image_mode: auto
  audio_mode: auto
  video_mode: off
  file_mode: auto
  native_fallback: notice

sandbox:
  roots:
    - ./data
  read_roots: []
  blocked:
    - "**/.env"
    - "**/.git/**"
    - "**/.ssh/**"
  bash_work_dir: ./data
  bash_restrict_paths: true
  bash_allow_network: false
  process_backend: auto
  file_max_write_bytes: 100000
  audit_enabled: true

auth:
  enabled: false
  admins: []
```

Luna Agent 内部通过独立 Backend 组装检索链路。`embedding`、`vector`、`keyword`、`fusion` 和 `reranker` 都采用 `provider + provider-specific options`；只有被选中的 Backend 会校验配置和可选依赖。当前内置实现为 `openai_compatible`、`qdrant`、`sqlite_fts5`、`weighted_rrf` 和 `none`。

Qdrant 远程连接使用 `url`，本地持久化使用 `path`，两者只能配置一个。切换 embedding 模型、vector Backend 或 keyword Backend 后运行：

```bash
uv run luna-agent memory reindex --index all
```

重建数据来自 `data/memory/memory.db`，不会删除原始记忆。Fusion 或 Reranker 配置变化不需要重建索引。

## 主要分区

| 分区 | 说明 |
| --- | --- |
| `agent` | 主 agent 迭代次数和每轮工具调用上限 |
| `execution` | 执行模式和工具权限覆盖 |
| `agents` | 子 agent 并发、工具调用、token 和历史限制 |
| `storage` | 数据目录和日志级别 |
| `plugins` | 用户插件目录、显式启用/禁用插件 |
| `memory` | 外部 provider、review/buffer、Memory LLM 和 Luna Agent 检索 Backend |
| `multimodal` | 平台附件、多模态降级和原生图片输入策略 |
| `compression` | Codex 风格交接压缩、用户原话和近期上下文预算 |
| `sandbox` | 文件、bash、审计的安全边界 |
| `mcp` | MCP server 开关和 server 列表 |
| `session` | 会话过期和会话 override |
| `auth` | 平台用户认证和白名单 |

### 上下文压缩

```yaml
compression:
  engine: compressor
  model: ""                    # 空值表示使用当前 Agent 模型
  threshold_ratio: 0.9
  retained_user_tokens: 20000  # 原样保留的真实用户消息预算
  tail_token_budget: 20000     # 最近完整消息预算
```

内置压缩器生成面向下一模型的完整 Handoff Summary，并创建新的物理 Session
checkpoint。摘要没有独立的字数或 token 上限；旧配置 `compression.max_tokens`
已废弃并忽略。`/compact` 可以手动创建 checkpoint，`/usage` 会显示当前窗口编号。
自动压缩阈值取 `有效窗口 * threshold_ratio` 与安全输入上限的较小值；安全输入上限会预留模型输出额度，以及 `max(4096, 有效窗口的 1%)` 安全余量。

## MCP Server

MCP server 支持显式 transport。现有只包含 `command` 的配置继续按 `stdio` 处理：

```yaml
mcp:
  enabled: true
  servers:
    - name: filesystem
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
      allow_network: true
    - name: remote-tools
      transport: streamable_http
      url: https://example.com/mcp
      headers_env:
        Authorization: REMOTE_MCP_AUTH
```

`headers_env` 的值是环境变量名，不是凭据本身；doctor 只检查配置结构，不回显环境变量值。动态变量与普通 provider 配置一样统一由 `ConfigLoader` / `Settings` 解析：进程环境优先于项目 `.env`，MCP connection 不会自行读取环境文件。

stdio server 在应用 Runtime 中会进入 `sandbox.process_backend` 指定的进程沙箱，默认工作目录为 `data/mcp`。需要联网的 stdio server 必须显式设置 `allow_network: true`。远程 transport 默认要求 HTTPS；连接明文 HTTP 或私网地址时，分别需要显式设置 `allow_insecure_http: true`、`allow_private_network: true`。MCP 还会限制工具数量/分页、schema、文本结果、结构化结果和 artifact 大小；对应 server 字段为 `max_tools`、`max_tool_pages`、`max_schema_bytes`、`max_result_chars`、`max_artifact_bytes`。

本地 MCP 如果只在文本结果中返回 Markdown 文件链接，默认不会被读取。server 可配置相对 `work_dir` 获得 `data/mcp` 下的独立工作目录；Runtime 会拒绝绝对路径和 `..` 逃逸，并在启动时创建目录。需要由受信 server 显式配置 `artifact_roots`（相对该 server 工作目录）与可选的 `artifact_extensions`，连接层才会把根目录内、非符号链接且大小合规的文件提升为 Artifact。路径穿越、绝对路径、未允许扩展名和超限文件均不会进入发送链路。Browser Operator 已固定使用隔离的 `data/mcp/playwright/` 工作目录并启用图片、PDF 与 WebM 产物。

正常 Runtime 启动不会等待 MCP 完成首次连接。插件和安全 Hook 注册完成后，MCP server 在主 asyncio 事件循环中作为独立后台任务并发连接；Gateway、CLI 和 TUI 可以先进入可用状态。连接成功或权威 `tools/list_changed` 会发布新的 Capability Snapshot，缓存 Agent 在下一轮按工具投影 fingerprint 刷新；普通断线只更新健康状态，不制造 revision churn。`doctor` 和 `serve --dry-run` 会显式等待首次连接尝试，以输出稳定诊断。使用 `npx`、`uvx` 且需要下载或检查包索引的 stdio server 必须允许网络，生产环境建议预安装并固定依赖版本。

GitHub 官方远程 MCP 可以使用 PAT 认证：

```yaml
mcp:
  enabled: true
  servers:
    - name: github
      transport: streamable_http
      url: https://api.githubcopilot.com/mcp/
      headers_env:
        Authorization: GITHUB_MCP_AUTH
```

`.env` 中保存完整的 Authorization header 值，不要把 token 写入 `config.yaml`：

```dotenv
GITHUB_MCP_AUTH="Bearer github_pat_xxx"
```

仓库内的 `integrations/codex-bridge` 插件会注册官方 `codex mcp-server`。启用后，Luna Agent 可调用 `mcp__codex__codex` 创建独立 Codex 线程，并使用 `mcp__codex__codex-reply` 继续该线程。插件的 `PreToolUse` Hook 会固定 `cwd`、sandbox 和内部 approval policy，丢弃调用方传入的危险扩权参数；外层 MCP 工具审批仍按 `permissions.tool_approval` 执行。

Codex 需要可写的状态目录。插件首次加载时只将 `source_codex_home/auth.json` 复制到 `runtime_codex_home`，权限设为 `0600`；后续数据库、会话和 token 更新均留在该隔离目录。默认建议把它放在已忽略的 `data/codex-bridge/`，不要提交其中内容。官方服务的实验性 `codex/event` 通知由插件内 stdio 适配器过滤，标准 MCP 请求、响应和错误保持原样。

Codex Bridge 还提供 App Server 驱动的插件开发会话。每个 `plugin_id` 对应一个外部开发工作区和一个持久 Codex Thread；工作区默认位于 `~/.local/share/luna-agent/plugin-workspaces/`，并且配置校验会拒绝任何位于宿主 `cwd` 内的开发目录。该专用目录不加入 Luna 普通工具的 `sandbox.roots`。首次创建时会写入脚手架、`PLUGIN_BRIEF.md` 和只读的 `LUNA_PLUGIN_DEVELOPMENT.md` 开发规范副本。`plugin_dev_message` 只负责提交或排队消息，Codex 的后续事件由 active runtime 异步投递给配置的 `notify_sessions`。

```yaml
integrations/codex-bridge:
  development_root: "~/.local/share/luna-agent/plugin-workspaces"
  approval_policy: "on-request"
  approvals_reviewer: "user"       # 也可显式使用 auto_review
  notify_sessions: []               # 例如 ["wechat:<chat-id>"]
```

相关工具分为开发会话和审批两组：`plugin_dev_create`、`plugin_dev_message`、`plugin_dev_list`、`plugin_dev_status`、`plugin_dev_events`、`plugin_dev_cancel`，以及 `codex_approval_list`、`codex_approval_decide`。后两者只映射 Codex App Server 的待处理请求；重启、插件卸载或 generation 变化时不会自动批准，未处理请求默认拒绝。插件打包、安装、启停仍由 `plugin_build` / `plugin_manage` 完成。

## 执行模式与权限

`execution.mode` 是默认执行模式，启动时生效；运行中可以用 `/mode` 临时切换当前会话的执行模式。可选值：

| mode | UI 名称 | 行为 |
| --- | --- | --- |
| `read-only` | Read Only | sandbox roots 内只读；扩权请求直接拒绝 |
| `ask-first` | Ask First | sandbox roots 内只读；写入、网络及需审批工具按具体资源确认 |
| `local-auto` | Local Auto | `sandbox.roots` 内可读写、`sandbox.read_roots` 内只读并允许普通网络；默认 cached 工具自动执行，越界文件和显式审批工具按需确认 |
| `full-auto` | Full Auto | roots 行为同 Local Auto，默认 cached 工具自动执行且不提供交互扩权；仍受 blocked path 等硬边界约束 |

旧 Mode 名称和 `execution.policy` 已删除。配置必须使用上表稳定 ID；未知值会在配置检查阶段报错。

`sandbox.read_roots` 扩展原生文件读取工具（read/list_directory/file_info/grep/glob 等）的自动读取范围，不扩展 file_write/file_edit、Bash 或 MCP 的可写边界。Bash 访问额外路径时仍必须在调用中声明 `read_paths` / `write_paths`，再由当前 Mode 判断自动放行、询问或拒绝。`sandbox.blocked` 始终优先；被 blocked pattern 命中的路径不能通过声明或限时授权放行。

工具审批使用 `permissions.tool_approval` 覆盖：

```yaml
permissions:
  tool_approval:
    default_external: cached
    tools:
      bash: prompt
      dangerous_plugin_tool: deny
    mcp_servers:
      github: cached
```

审批模式：

```text
auto, cached, prompt, deny
```

`auto` 不单独询问工具身份，仍检查资源；`cached` 首次询问并在统一 TTL 内缓存；`prompt` 每次询问；`deny` 禁用。具体文件路径和网络 host 由资源权限单独判断，工具审批不能覆盖硬边界。

Mode 会进一步解释默认 `cached`：在 `Ask First` 中仍首次询问；在 `Local Auto` / `Full Auto` 中自动执行。`permissions.tool_approval.tools` 或 `mcp_servers` 中显式配置的 `cached`、`prompt`、`deny` 始终优先，不会被 Mode 自动改写。Local Auto 的普通网络自动放行不覆盖 `sandbox.bash_allow_network: false`、MCP URL 安全校验或插件 Hook。

## 沙箱配置

`sandbox` 控制文件工具、bash 工具和审计行为：

| 字段 | 说明 |
| --- | --- |
| `roots` | 原生文件工具和默认 Bash 工作目录的基础范围 |
| `blocked` | 强制禁止访问的 glob 路径 |
| `bash_work_dir` | bash 默认工作目录 |
| `bash_restrict_paths` | 保留明显越界命令的前置扫描；严格进程边界始终由 mount plan 执行 |
| `bash_allow_network` | 是否允许 bash 运行 `curl` / `wget` / `pip` 等网络命令 |
| `process_backend` | `auto` / `bwrap` / `legacy`；Bash 在 `auto` 下缺少 bwrap 时失败关闭，只有显式 `legacy` 才关闭严格隔离；MCP 保留兼容降级 |
| `file_max_write_bytes` | `file_write` 单次最大写入字节数 |
| `audit_enabled` | 是否记录工具审计日志到 `data/audit.log` |

新安全模式不接受 `/allow write` 这类类别级预授权，因为它无法表达最小路径或 host。应在具体工具确认中选择允许一次或限时允许。`/deny all` 可清空当前会话的全部限时工具/资源授权。

临时授权由 `permissions` 控制：

```yaml
permissions:
  grant_ttl_minutes: 60
  confirm_timeout_seconds: 120
```

所有工具和资源授权共享 `grant_ttl_minutes`，只保存在当前会话的内存状态中；切换 Mode、重置/删除会话或服务重启都会清空。

`bash` 与 `process_start` 的文件系统参数一致：

```json
{
  "command": "python3 convert.py",
  "cwd": "/path/to/workspace",
  "read_paths": ["/home/user/source.docx"],
  "write_paths": ["/home/user/existing-output-directory"]
}
```

`cwd` 总是 writable resource；额外路径必须已经存在，读取按只读挂载，写入按可写挂载。需要创建新文件时声明其现有父目录，而不是声明一个尚不存在的文件。未声明的同目录兄弟文件不会因为某个单文件授权而一并可见。

## 多模态配置

`artifacts` 控制工具、MCP 和 provider 产生的出站文件。Artifact 使用 `data/artifacts/` 受控存储，SQLite 只保存 metadata：

| 字段 | 说明 |
| --- | --- |
| `max_file_bytes` | 单个 Artifact 的最大字节数，默认 20 MiB |
| `max_per_turn` | 每个 turn 最多物化的 Artifact 数，默认 10 |
| `retention_hours` | 无活跃 Outbox 引用时的保留时间，默认 24 小时 |

Artifact 与入站 `attachments` 是两套方向相反的存储：`attachments` 缓存用户发来的内容，`artifacts` 保存工具准备发给用户的产物。已被 pending/retry/ambiguous Outbox 引用的 Artifact 不会被过期清理。

普通文件工具只负责修改工作区，不会自动把每次写入都变成待发送附件。用户明确要求接收某个已有文件时，Agent 使用 `artifact_from_file(path)`；该工具会沿用统一的 filesystem read 权限、sandbox roots 和 blocked paths，拒绝目录、符号链接、空文件及超限文件，并把内容复制进 ArtifactStore。返回的 `artifact_id` 只在当前 session/turn 内可供 `response_attach` 选择。`filename` 只能覆盖展示名称，不能改变源路径。

Browser Operator 的 `max_artifact_bytes` 默认 10 MiB，避免 Playwright 截图先被 MCP 的通用 1 MiB 上限截断；它仍受全局 `artifacts.max_file_bytes` 二次限制。Playwright 返回的相对截图链接会在受控输出目录中物化，并向当前 turn 返回可供 `response_attach` 使用的 `artifact_id`。

`multimodal` 控制 gateway/平台附件进入 agent 前的处理方式：

| 字段 | 说明 |
| --- | --- |
| `enabled` | 总开关。关闭后附件不会下载、缓存、解析或传给模型，只会生成提示文本 |
| `image_mode` | 图片处理方式：`auto` / `native` / `text` / `off` |
| `audio_mode` | 音频处理方式：`auto` / `text` / `off` |
| `video_mode` | 视频处理方式：`auto` / `text` / `off` |
| `file_mode` | 文件处理方式：`auto` / `text` / `off` |
| `native_fallback` | provider 不支持原生多模态时的降级方式：`notice` / `text` |
| `image_text_provider` | 图片文本化辅助 provider，例如 `openai` / `anthropic` / `xai` |
| `image_text_api_mode` | 图片文本化 API 协议：`auto` / `chat_completions` / `anthropic_messages` / `responses` / `codex_responses`；`anthropic + auto` 会使用 Anthropic Messages，base URL 会按 `{base}/messages` 调用，例如 `https://api.deepseek.com/anthropic` -> `/anthropic/messages`；OpenAI-compatible 中转站可显式设为 `chat_completions`，Codex/Ahoo 这类 Responses 中转站建议设为 `codex_responses` 并使用根 base URL |

`off` 不会触发下载和缓存；`text` 会尝试文本化，当前没有可用解析能力时会降级成模型可见提示；`native` 目前用于支持图片输入的 provider。DeepSeek/OpenRouter 默认不启用原生图片，OpenAI/Anthropic 会按各自 transport 转换图片格式。

`attachments` 控制平台附件的下载和本地缓存，和 `multimodal` 的内容处理分开：

| 字段 | 说明 |
| --- | --- |
| `resolve_inbound` | 授权通过后，平台入口是否尝试把附件本地化 |
| `cache_inbound` | 是否写入 `data/attachments/`，并按 sha256 去重 |
| `download_urls` | 是否下载平台消息里的 URL 附件 |
| `download_platform_files` | 是否调用平台适配器下载 `platform_file_id` 附件 |

平台附件下载发生在 Gateway 授权通过之后、构造 `SubmissionRequest` 之前。Gateway 只触发平台适配器的统一准备方法；具体平台下载逻辑由 adapter 提供，缓存入库由 `AttachmentStore` 统一处理。随后请求经过 Coordinator 进入 ConversationService；provider 只影响后续 `native` / `text` / `notice` 的处理方式，不参与附件下载决策。

## 旧配置迁移

这些顶层配置已废弃：

- `llm`：迁移到 `.env` 的 `LLM_*`。
- `platform` / `platforms`：平台 secret 迁移到 `.env`，平台插件 key 使用 `platforms/telegram`、`platforms/feishu`、`platforms/wechat`。

迁移检查：

```bash
uv run luna-agent init --check
uv run luna-agent doctor
```

默认不会自动改写旧 `config.yaml`。按诊断里的“迁移建议”和“推荐命令”手动处理。

## Profile

| profile | 行为 |
| --- | --- |
| `local` | 最小 CLI 配置，memory external 为 `none`，auth 关闭 |
| `server` | 服务配置，memory external 为 `luna`，auth 关闭 |
| `bot` | 通用 bot 配置，auth 开启，列出全部平台 env |
| `telegram` | 启用 `platforms/telegram`，`.env.example` 只列 Telegram 平台字段 |
| `feishu` | 启用 `platforms/feishu`，`.env.example` 只列飞书平台字段 |
| `wechat` | 启用 `platforms/wechat`，`.env.example` 只列微信平台字段 |
