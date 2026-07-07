# Personal Agent

Personal Agent 是一个插件化、多入口、可观测的个人 AI Agent runtime。它不是一个只会在命令行里做单轮问答的小脚本，而是把对话、工具、安全、平台接入、记忆、MCP、workflow、受控多 Agent 委派和 inline TUI 放进同一套清晰的运行时。

项目当前已经具备一个能长期扩展的底座：后端负责稳定的 agent loop、provider/transport、工具执行、安全门控、会话存储和平台 Gateway；前端 inline TUI 消费结构化事件、slash command metadata、activity payload、tool runs 和 context budget。CLI、TUI、平台 Gateway 共享同一套后端能力，后续接 desktop/web shell 不需要重写 agent runtime。

如果你想要的是一个“能真实使用、能观察、能扩展、能接平台”的个人 Agent，而不是一次性 demo，这就是这个项目的方向。

## 项目亮点

- **真正的 Agent runtime，而不是聊天脚本**
  agent loop、LLM transport、工具执行、权限、安全、记忆、workflow、MCP、Gateway 和会话存储是独立模块，通过清晰接口组合。

- **插件化扩展能力**
  工具、平台、LLM provider/transport、memory provider、workflow 都通过插件系统装配。核心 runtime 保持轻量，不依赖 LangChain / CrewAI 这类重框架。

- **inline TUI 已经接入结构化后端**
  TUI 不是解析纯文本的终端皮肤，而是直接消费 `ConversationEvent`、`CommandResult.kind/payload`、slash metadata、tool events、activity payload 和 context budget。

- **工具安全在执行主链路里**
  工具调用统一经过 execution guard、precheck、permission、sandbox、audit。确认 UI 可读到风险、默认动作、可选动作、路径/命令/URL 预览。

- **工具调用真实可追踪**
  实时 `tool_start/tool_decision/tool_end`、持久化 `tool_runs`、每轮 `AgentTurnReport` 和 tool truth 可以回答“模型到底有没有真的调用工具”。

- **Provider-aware transport 与 prompt cache 诊断**
  `ProviderProfile` 描述 cache capability；transport 归一化 usage，记录 system/tools/message prefix hash，便于排查 Anthropic explicit cache 和 DeepSeek/OpenAI-compatible prefix cache。

- **运行时 Activity 统一视图**
  `/activity` 能统一查看子 agent、后台进程、Gateway agent 的 summary/list/detail，前端可以直接渲染结构化 payload。

- **Context 和 token 语义清晰**
  `context_used_tokens/context_window/context_percent` 表示当前上下文占用；`input_tokens/output_tokens` 表示最近一次模型调用消耗，避免 UI 指标混乱。

- **可观测性内建**
  启动有 `BootReport`，单轮有 `AgentTurnReport`，运行时有 doctor/health snapshot，缓存、工具、activity、turn reports 都可诊断。

## 现在已经能做什么

- **本地 CLI / inline TUI 多轮对话**
  支持流式回复、thinking 展示、会话切换、导出、记忆命令、上下文预算、slash command 菜单、工具确认、工具结果展开。

- **结构化 slash commands**
  `/commands`、`/tools`、`/permissions`、`/protocol`、`/mode`、`/tool-runs`、`/activity` 等命令既有文本输出，也能返回结构化 payload 给前端。

- **平台 Gateway**
  支持 Telegram / 飞书 / 微信插件式接入，有会话路由、pending 消息、重连、运行中 agent 状态和 Gateway activity snapshot。

- **安全工具执行**
  文件、shell、网络、后台进程、工作区等工具都有统一执行门控；危险操作会进入确认链路，而不是直接裸露给模型。

- **工具结果和 turn report 持久化**
  `tool_runs` 和 `turn_reports` 落库，能按 session/turn 查询历史工具结果和每轮审计报告。

- **子 agent 和后台任务可观察**
  delegate/sub-agent、后台进程、gateway agent 都进入统一 Activity Runtime，支持 summary/list/detail 和 slash 动态候选。

- **Provider cache 调试**
  `llm_end`、runtime health、doctor 都能暴露 cache usage 和 request diagnostics，方便看缓存命中是否来自稳定 system/tools/message prefix。

- **配置和诊断体系**
  `Settings()` 走统一 loader；`doctor`、`init --check`、`serve --dry-run` 能检查配置、启动、运行时、插件、MCP、provider cache 和 activity 状态。

## 快速开始

```bash
uv sync

uv run personal-agent init --profile local --copy-env --fix-dirs
# 编辑 .env，至少填写 LLM_API_KEY

uv run personal-agent doctor
uv run personal-agent chat
```

使用 inline TUI：

```bash
uv run personal-agent chat --ui inline
```

单轮调用：

```bash
uv run personal-agent chat "你好"
uv run personal-agent chat --once "总结一下当前项目"
```

启动平台 Gateway：

```bash
uv run personal-agent init --profile telegram --copy-env --fix-dirs
# 编辑 .env，填写 LLM_API_KEY 和 TELEGRAM_BOT_TOKEN
uv run personal-agent serve
```

## 常用命令

```bash
uv run personal-agent chat
uv run personal-agent serve
uv run personal-agent doctor
uv run personal-agent doctor --json
uv run personal-agent protocol schema --json

uv run personal-agent plugins list --load
uv run personal-agent plugins doctor platforms/telegram
uv run personal-agent memory doctor
uv run personal-agent agents list
uv run personal-agent tokens session
```

交互式 chat / inline TUI 中常用 slash commands：

```text
/commands
/tools list
/tool-runs summary
/activity
/usage
/mode
/permissions
/protocol schema
```

## 初始化 Profile

`personal-agent init` 支持这些 profile：

| profile | 用途 |
| --- | --- |
| `local` | 本地 CLI 对话，最小配置 |
| `server` | 长期运行服务，启用 external memory |
| `bot` | 通用 bot 配置，平台 env 都列出来 |
| `telegram` | Telegram bot，启用 `platforms/telegram` |
| `feishu` | 飞书 bot，启用 `platforms/feishu` |
| `wechat` | 微信 bot，启用 `platforms/wechat` |

常用参数：

```bash
uv run personal-agent init --check
uv run personal-agent init --fix-dirs
uv run personal-agent init --copy-env
uv run personal-agent init --profile telegram --force
```

`--check` 只诊断，不写文件。旧配置迁移默认只给建议，不自动改写用户已有的 `config.yaml`。

## 配置文件

- `.env`：放 secret 和 provider/platform 环境变量，例如 `LLM_API_KEY`、`TELEGRAM_BOT_TOKEN`。
- `config.yaml`：本机行为配置，例如 storage、plugins、memory、sandbox、mcp、auth、session、`agent.ui`、`execution.mode`。
- `config.yaml.example`：可发布模板，不包含个人本机路径；新环境可从它复制出自己的 `config.yaml`。
- `plugins/`：用户插件或本地开发插件目录。
- `data/`：运行数据、会话、记忆、审计日志等。

详细说明见 [配置文档](docs/configuration.md)。

## 平台接入

平台插件是 deferred 插件：启动时先发现，`serve` 时由 Gateway 加载、创建 adapter、连接平台。

支持的平台：

| 平台 | 插件 key | 必要 env |
| --- | --- | --- |
| Telegram | `platforms/telegram` | `TELEGRAM_BOT_TOKEN` |
| 飞书 | `platforms/feishu` | `FEISHU_APP_ID`、`FEISHU_APP_SECRET` |
| 微信 | `platforms/wechat` | `WEIXIN_TOKEN`、`WEIXIN_ACCOUNT_ID` |

Gateway 会记录平台 runtime 状态、自动重连、pending 消息、发送失败和运行中 agent。详细说明见 [平台文档](docs/platforms.md)。

## 插件与扩展

内置插件在 `src/personal_agent/plugins/builtin/`，用户插件放在根目录 `plugins/` 或 `data/plugins/`。插件负责注册能力，不接管具体运行时生命周期。

插件开发说明见 [插件系统文档](docs/plugins.md)。

## 运维与排错

优先使用：

```bash
uv run personal-agent doctor
uv run personal-agent init --check
uv run personal-agent plugins list --load
```

常见问题和命令说明见 [运维文档](docs/operations.md)。

## 验证

改动合入前至少运行：

```bash
python -m compileall -q src/personal_agent
uv run pytest -q
```

当前主分支最近一次前后端合并后全量结果：`746 passed`。

## 技术栈

Python 3.12+ / uv / Typer / asyncio / httpx / aiohttp / aiosqlite / tiktoken / fastembed / PyMuPDF / python-docx。

不依赖 LangChain、CrewAI 等重框架。
