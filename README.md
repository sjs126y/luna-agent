# Personal Agent

Personal Agent 是一个插件化的多平台 AI Agent 运行时。它支持 CLI 多轮对话、Telegram/飞书/微信平台接入、工具安全管线、记忆、MCP、workflow 和受控多 Agent 委派。

当前主入口是 `personal-agent` CLI。插件系统负责装配工具、平台、LLM transport、memory provider 和 workflow；Gateway 负责平台连接、会话路由、消息队列和 agent 调度。

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
