# 运维与排错

这份文档只覆盖日常使用和排错入口。

## 新环境启动

本地 CLI：

```bash
uv sync
uv run personal-agent init --profile local --copy-env --fix-dirs
# 编辑 .env，填写 LLM_API_KEY
uv run personal-agent doctor
uv run personal-agent chat
```

Telegram bot：

```bash
uv run personal-agent init --profile telegram --copy-env --fix-dirs
# 编辑 .env，填写 LLM_API_KEY 和 TELEGRAM_BOT_TOKEN
uv run personal-agent doctor
uv run personal-agent serve
```

## 常用命令

```bash
uv run personal-agent chat
uv run personal-agent chat "单轮消息"
uv run personal-agent serve

uv run personal-agent doctor
uv run personal-agent doctor --json
uv run personal-agent init --check

uv run personal-agent plugins list --load
uv run personal-agent plugins doctor <plugin-key>
uv run personal-agent plugins validate examples/plugins/hello

uv run personal-agent memory doctor
uv run personal-agent memory list
uv run personal-agent agents list
uv run personal-agent tokens session
```

## 配置迁移

旧配置迁移先诊断，不自动覆盖：

```bash
uv run personal-agent init --check
uv run personal-agent doctor
```

重点看输出里的：

- `已废弃配置`
- `迁移建议`
- `警告`
- `推荐命令`

常见迁移：

- 顶层 `llm` 改到 `.env` 的 `LLM_*`。
- 顶层 `platform` / `platforms` 删除，平台 token 改到 `.env`。
- 平台插件用 `plugins.enabled` 显式启用，例如 `platforms/telegram`。

## Doctor 结果怎么读

`Config`：

- `config.yaml: 否`：运行 `personal-agent init`。
- `.env: 否`：运行 `personal-agent init --copy-env` 或 `cp .env.example .env`。
- `LLM key: 否`：填写 `.env` 的 `LLM_API_KEY`。
- `unknown keys`：确认是否是旧配置或拼写错误。

`Gateway`：

- `running agents` 大于 0：有正在处理的会话。
- `pending messages` 持续增长：消息进入了 adapter，但处理链路积压。
- `stop requested` 大于 0：有 run 收到了停止请求。

`平台配置`：

- `runtime=skipped`：平台 env 不完整或插件未启用。
- `runtime=reconnecting`：连接失败，Gateway 正在自动重试。
- `attempts` 持续增加：平台一直连接不上。
- `pending` 持续增加：平台收消息正常，但处理速度不足。

`插件`：

- `ERROR`：manifest、env 或 entrypoint 有问题。
- `DEFERRED`：延迟加载，平台/MCP 触发时才 import，通常不是错误。

## 验证命令

提交前建议运行：

```bash
python -m compileall -q src/personal_agent
uv run pytest -q
```

