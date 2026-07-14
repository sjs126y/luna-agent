# 能力、边界与配置化

这份文档补充 README 的项目亮点。README 只放首页级介绍；如果想了解 Lumora 为什么适合长期运行、哪些事情有安全边界、哪些行为可以配置，就看这里。

## 项目定位

Lumora 是一个个人 AI Agent runtime，不是单一聊天界面。它把模型调用、工具执行、权限、安全、记忆、平台 Gateway、MCP、workflow、sub-agent、多模态和运行态观测放到同一个后端核心里。

核心目标：

- 多个入口复用同一个 agent runtime。
- 真实工具调用可确认、可审计、可回看。
- 平台长期在线时有重试、重连、限流和异常兜底。
- 供应商、协议、缓存、上下文、权限和多模态策略都能配置。
- 新工具、MCP、skill、平台 adapter 和未来 desktop/web 入口可以继续接入。

## 功能亮点

### 统一会话入口

CLI、inline TUI、Gateway 和未来 desktop/web 都进入 `ConversationService`，再进入 Agent Runtime。入口层只负责接入、展示和平台差异，核心推理链路保持一致。

这样可以避免每个入口各写一套工具、权限、记忆和状态逻辑。

### Provider-aware LLM Transport

Provider 不只是 `base_url` 和 `model`。Lumora 会根据 provider profile 和 API mode 选择 transport，并处理不同协议的请求格式：

- Chat Completions
- Anthropic Messages
- OpenAI Responses
- Codex Responses
- OpenAI-compatible / 中转站路径

transport 还负责归一化 usage、context、cache diagnostics、流式 delta 和错误归因。

### 工具与 MCP

内置工具、MCP 工具和插件工具进入同一套 tool registry、权限判断、执行器和审计链路。模型不能绕过这条链路直接执行危险操作。

工具运行结果会被记录到：

- 实时事件
- `tool_runs`
- `AgentTurnReport`
- audit log

### Gateway 与平台

Gateway 不是简单地把平台消息转发给模型。它管理平台 adapter、会话路由、pending 队列、busy session、异步确认、附件准备、发送重试和连接状态。

平台侧当前覆盖：

- Telegram
- 飞书
- QQ
- 微信

不同平台只处理自己的消息解析、发送、附件引用和下载能力，后续统一进入后端核心。

### 多模态链路

平台 adapter 会把图片、文件、音频、视频等输入标准化为 attachment。Gateway 在授权通过后触发附件准备，`MultiAttachmentProcessor` 再按配置决定如何处理：

- `native`：传给支持原生多模态的 provider。
- `text`：尝试文本化，例如文本文件、PDF、docx、vision fallback、OCR HTTP 服务。
- `auto`：优先使用可用能力，不可用时降级。
- `off`：不下载、不缓存、不处理，只给出提示。

处理失败不会直接打断 turn，而是转成模型可见 notice 和前端可展示 diagnostics。

### 运行中观测

Lumora 不是只看模型回复文本。它还提供：

- doctor 启动体检
- runtime health snapshot
- tool runs 查询
- turn reports
- activity runtime
- provider cache diagnostics
- context usage
- gateway/platform status

这些数据能帮助判断：模型是否真的调用工具、工具为什么被拒绝、平台为什么积压、provider 是否命中缓存、当前上下文还有多少空间。

## 安全边界

Lumora 的安全设计分两层：可配置权限和不可轻易绕过的硬边界。

### Execution Mode

`execution.mode` 控制默认自动化程度：

| mode | 定位 | 默认倾向 |
| --- | --- | --- |
| `read-only` | Read Only | roots 内只读，扩权请求拒绝 |
| `ask-first` | Ask First | roots 内只读，具体写入/网络资源按需确认 |
| `local-auto` | Local Auto | roots 内可读写，越界资源与需审批工具按需确认 |
| `full-auto` | Full Auto | roots 内可读写并允许网络，硬安全边界仍生效 |

mode 可以作为全局默认，也可以在会话中临时切换。

### Tool Approval 与资源权限

工具审批模式：

```text
auto, cached, prompt, deny
```

资源类型：

```text
filesystem(read/write), network(connect)
```

`cached` 首次确认后按统一 TTL 缓存，`prompt` 每次确认，`deny` 禁止。资源授权始终记录具体路径或 host；类别级 `/allow` 命令已经删除。

### Sandbox 与硬预检

权限允许不代表可以越过沙箱。以下属于硬边界：

- sandbox roots
- blocked glob patterns
- secret/path precheck
- bash path restrict
- bash network restrict
- file write size limit
- destructive operation precheck

这些边界的目标是防止工具访问密钥、越权路径、系统目录、仓库内部敏感文件或执行明显危险的 bash。

### 审计

工具决策、工具结果和关键运行态可以记录下来。实际排查时，不需要只相信模型文本，可以回看真实工具调用、授权 scope、拒绝原因、输出截断和错误信息。

## 可靠性设计

### Gateway 长期运行

平台 Gateway 有自己的运行态，而不是只靠一次请求：

- 平台 adapter 连接状态
- 连接失败后的 reconnect delay
- pending 消息数量
- busy session 队列
- 异步确认旁路
- `/stop` 中断
- 发送失败重试
- 长文本切分
- 附件准备失败 diagnostics

这让平台 bot 更适合长期在线，而不是一次异常就丢消息。

### LLM 请求可靠性

LLM client 和 transport 会处理：

- HTTP 429/5xx 重试
- stream / non-stream 响应
- 非 JSON 响应错误摘要
- provider-specific path
- usage 和 context 归一化
- cache hit/miss/write/read 字段
- 图片输入不支持时的降级重试

### 工具执行可靠性

工具执行会经过统一 executor。权限拒绝、确认超时、工具异常、输出截断、真实调用统计和结果持久化都在同一条路径里处理，减少入口之间行为不一致。

## 可配置化

Lumora 的目标是尽量通过配置切换运行方式，而不是改代码。

### `.env`

`.env` 主要放 secret 和 provider/platform 环境变量，例如：

- `LLM_PROVIDER`
- `LLM_API_KEY`
- `LLM_BASE_URL`
- `LLM_MODEL`
- `LLM_API_MODE`
- `LLM_CONTEXT_WINDOW`
- `IMAGE_TEXT_API_KEY`
- 平台 token / app id / secret

### `config.yaml`

`config.yaml` 主要放本机行为配置：

- `execution.mode`
- `permissions.grant_ttl_minutes`
- `permissions.tool_approval`
- `sandbox.process_backend`
- `sandbox.roots`
- `sandbox.read_roots`
- `sandbox.blocked`
- `sandbox.bash_allow_network`
- `storage.data_dir`
- `plugins.enabled`
- `memory.provider`
- `mcp.servers`
- `multimodal.*`
- `attachments.*`
- `session.*`
- `auth.*`

### Profile

`personal-agent init --profile ...` 用于生成不同场景的初始配置，例如 local、server、bot、telegram、feishu、wechat。profile 是起点，不是锁死的模式，后续仍然可以手动调整配置。

## 边界说明

Lumora 不是为了让模型无限制接管机器。更准确地说，它是在安全边界内给模型自动化能力：

- 模型可以建议和调用工具，但工具必须通过 runtime。
- 用户可以给临时授权，但不能越过配置里的 `deny` 和硬安全边界。
- 平台可以长期运行，但需要正确配置 token、白名单和权限策略。
- 多模态可以原生或文本化处理，但无法处理的附件会降级成明确提示。
- MCP 和插件可以扩展能力，但仍然进入统一权限、审计和工具结果链路。

## 相关文档

- [配置说明](configuration.md)
- [平台接入](platforms.md)
- [插件系统](plugins.md)
- [运维与排错](operations.md)
- [后端接口契约](../BACKEND_INTERFACE.md)
