# 配置说明

Lumora 使用两类配置文件：

- `.env`：secret、LLM provider、平台 token。
- `config.yaml`：运行行为、目录、插件、记忆、沙箱、MCP、认证和会话。

生成最小配置：

```bash
uv run personal-agent init --profile local --copy-env --fix-dirs
uv run personal-agent init --check
```

## .env

LLM 基础字段：

```dotenv
LLM_PROVIDER=deepseek
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_API_MODE=auto
LLM_MAX_TOKENS=4096
LLM_CONTEXT_WINDOW=0
LLM_REASONING_EFFORT=
```

`LLM_API_MODE` 可选 `auto` / `chat_completions` / `anthropic_messages` / `responses` / `codex_responses`。Codex/Ahoo 这类 Responses 中转站通常使用根 `LLM_BASE_URL`，并显式设置 `LLM_API_MODE=codex_responses`。

`LLM_CONTEXT_WINDOW=0` 表示按模型名自动推断上下文窗口；使用中转站自定义模型名时可以填真实窗口大小，例如 `1000000`。同一配置也可以写在 `config.yaml` 的 `llm.context_window`，优先级是 `.env` 高于 `config.yaml`。

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
```

## config.yaml

常用配置：

```yaml
execution:
  mode: standard
  policy:
    tool_permissions: {}

llm:
  context_window: 0

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
  external_provider: lumora
  review:
    external_turn_interval: 10
    internal_turn_interval: 50
    internal_buffer_limit: 20
    snapshot_refresh_turn_interval: 20
    worker_concurrency: 2
  llm:
    provider: inherit
    max_tokens: 2048
  embedding:
    api_mode: openai_compatible
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: DASHSCOPE_API_KEY
    model: text-embedding-v4
    dimensions: 0
  qdrant:
    url: http://localhost:6333
    collection: lumora_memories
    api_key_env: QDRANT_API_KEY

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
  blocked:
    - "**/.env"
    - "**/.git/**"
    - "**/.ssh/**"
  bash_work_dir: ./data
  bash_restrict_paths: true
  bash_allow_network: false
  file_max_write_bytes: 100000
  audit_enabled: true

auth:
  enabled: false
  admins: []
```

## 主要分区

| 分区 | 说明 |
| --- | --- |
| `agent` | 主 agent 迭代次数和每轮工具调用上限 |
| `execution` | 执行模式和工具权限覆盖 |
| `agents` | 子 agent 并发、工具调用、token 和历史限制 |
| `storage` | 数据目录和日志级别 |
| `plugins` | 用户插件目录、显式启用/禁用插件 |
| `memory` | 外部 provider、review/buffer、Memory LLM、百炼 embedding 和 Qdrant |
| `multimodal` | 平台附件、多模态降级和原生图片输入策略 |
| `compression` | 上下文压缩阈值和 tail budget |
| `sandbox` | 文件、bash、审计的安全边界 |
| `mcp` | MCP server 开关和 server 列表 |
| `session` | 会话过期和会话 override |
| `auth` | 平台用户认证和白名单 |

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
    - name: remote-tools
      transport: streamable_http
      url: https://example.com/mcp
      headers_env:
        Authorization: REMOTE_MCP_AUTH
```

`headers_env` 的值是环境变量名，不是凭据本身；doctor 只检查配置结构，不回显环境变量值。动态变量与普通 provider 配置一样统一由 `ConfigLoader` / `Settings` 解析：进程环境优先于项目 `.env`，MCP connection 不会自行读取环境文件。

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

## 执行模式与权限

`execution.mode` 是默认执行模式，启动时生效；运行中可以用 `/mode` 临时切换当前会话的执行模式。可选值：

| mode | UI 名称 | 行为 |
| --- | --- | --- |
| `guarded` | Read Only | 只允许读和搜索，写入、bash、后台任务、网络和破坏性操作拒绝 |
| `standard` | Ask First | 日常模式，读和搜索允许，写入、bash、后台任务、网络和破坏性操作需要确认 |
| `trusted` | Edit Freely | 信任本地项目，写入和 bash 允许，后台任务、网络和破坏性操作仍需确认 |
| `sovereign` | Full Auto | sandbox 内大多数工具直接允许，破坏性操作仍需确认 |

可以用 `execution.policy.tool_permissions` 覆盖某类工具权限：

```yaml
execution:
  mode: standard
  policy:
    tool_permissions:
      bash: ask
      background: ask
      network: deny
```

权限类别：

```text
default, read, search, write, bash, background, network, destructive
```

权限决策：

```text
allow, ask, deny
```

`execution.policy` 只负责权限决策，不会关闭沙箱硬边界。密钥文件、blocked 路径、危险 bash 模式和系统路径逃逸仍会被 precheck/sandbox 拦截。

## 沙箱配置

`sandbox` 控制文件工具、bash 工具和审计行为：

| 字段 | 说明 |
| --- | --- |
| `roots` | 文件工具和 bash 允许访问的根目录 |
| `blocked` | 强制禁止访问的 glob 路径 |
| `bash_work_dir` | bash 默认工作目录 |
| `bash_restrict_paths` | 是否限制 bash 路径在 `roots` 内 |
| `bash_allow_network` | 是否允许 bash 运行 `curl` / `wget` / `pip` 等网络命令 |
| `file_max_write_bytes` | `file_write` 单次最大写入字节数 |
| `audit_enabled` | 是否记录工具审计日志到 `data/audit.log` |

`/allow` 只解锁当前 mode/config 最终策略为 `ask` 的权限类别，不能覆盖 `deny`。`standard` 下普通网络工具为 `ask`，可用 `/allow network` 解锁 `web_search` / `web_fetch`；`guarded` 下 network 仍为 `deny`。`bash_allow_network: true` 只控制 bash 内部的 `curl` / `wget` / `pip` 等网络命令，不会被 `/allow network` 自动打开。

临时授权由 `permissions` 控制：

```yaml
permissions:
  temporary_grant_ttl_hours: 24
  confirm_timeout_seconds: 120
```

`/allow <category>` 和确认框里的 `Always 24h` 会写入当前运行时的限时授权，默认 24 小时后过期；`/deny <category>` 可撤销，`/deny all` 撤销全部。服务重启后这些运行时授权不会持久化。

## 多模态配置

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

平台附件下载发生在 Gateway 授权通过之后、进入 `ConversationService` 之前。Gateway 只触发平台适配器的统一准备方法；具体平台下载逻辑由 adapter 提供，缓存入库由 `AttachmentStore` 统一处理。provider 只影响后续 `native` / `text` / `notice` 的处理方式，不参与附件下载决策。

## 旧配置迁移

这些顶层配置已废弃：

- `llm`：迁移到 `.env` 的 `LLM_*`。
- `platform` / `platforms`：平台 secret 迁移到 `.env`，平台插件 key 使用 `platforms/telegram`、`platforms/feishu`、`platforms/wechat`。

迁移检查：

```bash
uv run personal-agent init --check
uv run personal-agent doctor
```

默认不会自动改写旧 `config.yaml`。按诊断里的“迁移建议”和“推荐命令”手动处理。

## Profile

| profile | 行为 |
| --- | --- |
| `local` | 最小 CLI 配置，memory external 为 `none`，auth 关闭 |
| `server` | 服务配置，memory external 为 `lumora`，auth 关闭 |
| `bot` | 通用 bot 配置，auth 开启，列出全部平台 env |
| `telegram` | 启用 `platforms/telegram`，`.env.example` 只列 Telegram 平台字段 |
| `feishu` | 启用 `platforms/feishu`，`.env.example` 只列飞书平台字段 |
| `wechat` | 启用 `platforms/wechat`，`.env.example` 只列微信平台字段 |
