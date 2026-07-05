# Personal Agent

Personal Agent 是一个插件化的多平台 AI Agent 运行时，不是一个只能在命令行里做单轮问答的小工具。它把对话、工具、安全、平台接入、记忆、MCP、workflow 和受控多 Agent 委派放进同一个清晰的运行时里，让本地 CLI、平台 Gateway、后续 TUI 和 desktop 能共用一套后端能力。

当前主入口是 `personal-agent` CLI，但项目本身的重点不是“做了一个 CLI”，而是已经把一个真正可扩展、可观测、可长期维护的 Agent runtime 搭起来了。插件系统负责装配工具、平台、LLM transport、memory provider 和 workflow；Gateway 负责平台连接、会话路由、消息队列和 agent 调度；运行时则负责把模型调用、工具执行、安全约束和会话状态稳定地串起来。

如果你想找的是一个“已经能用、而且后面还能继续长”的个人 Agent 后端，这个项目就是朝这个方向做的。

## 项目亮点

- **插件化运行时，不是单体聊天脚本**
  工具、平台、LLM transport、memory provider、workflow 都通过插件系统装配，核心 runtime 保持轻量，方便裁剪和扩展。

- **一个后端，多个入口**
  既能跑本地 CLI 多轮对话，也能挂 Telegram / 飞书 / 微信这类平台入口；CLI、Gateway、未来 TUI / desktop 共用同一套对话与工具执行链路。

- **工具安全不是补丁，而是执行主链路的一部分**
  工具执行统一经过 execution guard、precheck、权限、沙箱、审计，不是把危险工具直接暴露给模型。

- **运行时可观测性已经内建**
  启动阶段有 `BootReport`，单轮对话有 `AgentTurnReport`，`doctor` 和 `serve --dry-run` 可以直接看到启动/运行摘要，而不是只给一个异常字符串。

- **前端友好的事件模型**
  对话过程通过结构化事件流驱动，CLI 只是第一个消费者；后续上 TUI 或 desktop，不需要重写 agent/runtime 本身。

- **不依赖 LangChain / CrewAI 一类重框架**
  保持 Python 原生、asyncio、插件和显式 runtime 结构，链路更直，调试和定制成本更低。

## 现在已经能做什么

- **本地多轮 CLI 对话已经可用**
  支持会话切换、上下文预算查看、导出、记忆命令、子 Agent 运行记录、工具调用 trace、流式输出和 thinking 展示。

- **平台 Gateway 已经能跑**
  不是只停留在 demo webhook 层，而是有平台接入、会话路由、pending 消息管理、重连和运行中 agent 状态。

- **工具执行不是裸奔**
  模型想调用工具时，会经过统一的 execution guard、precheck、权限、沙箱和审计链路。

- **配置和诊断已经成体系**
  `Settings()` 走统一 loader；`doctor`、`init --check`、`serve --dry-run` 能直接用来查配置、启动和运行时状态。

- **运行时可观测性已经不是空壳**
  启动有 `BootReport`，每轮对话有 `AgentTurnReport`，最近 turn summary 已经进入 runtime health 和 doctor。

这意味着它不是“还在想怎么做”的半成品，而是一个已经能用、并且后面适合继续补前端和桌面壳的后端底座。

## 快速开始

```bash
uv sync

uv run personal-agent init --profile local --copy-env --fix-dirs
# 编辑 .env，至少填写 LLM_API_KEY

uv run personal-agent doctor
uv run personal-agent chat
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

uv run personal-agent plugins list --load
uv run personal-agent plugins doctor platforms/telegram
uv run personal-agent memory doctor
uv run personal-agent agents list
uv run personal-agent tokens session
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
- `config.yaml`：放行为配置，例如 storage、plugins、memory、sandbox、mcp、auth、session。
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

## 技术栈

Python 3.12+ / uv / Typer / asyncio / httpx / aiohttp / aiosqlite / tiktoken / fastembed / PyMuPDF / python-docx。

不依赖 LangChain、CrewAI 等重框架。
