# 配置说明

Personal Agent 使用两类配置文件：

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
```

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
storage:
  data_dir: ./data
  log_level: INFO

plugins:
  dirs:
    - ./plugins
    - ./data/plugins
  enabled: []
  disabled: []

memory:
  provider: file
  external_provider: none
  review_interval: 10

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
  audit_enabled: true

auth:
  enabled: false
  admins: []
```

## 主要分区

| 分区 | 说明 |
| --- | --- |
| `agent` | 主 agent 迭代次数和每轮工具调用上限 |
| `agents` | 子 agent 并发、工具调用、token 和历史限制 |
| `storage` | 数据目录和日志级别 |
| `plugins` | 用户插件目录、显式启用/禁用插件 |
| `memory` | 内置记忆 provider、外置记忆 provider 和 review 间隔 |
| `compression` | 上下文压缩阈值和 tail budget |
| `sandbox` | 文件、bash、审计的安全边界 |
| `mcp` | MCP server 开关和 server 列表 |
| `session` | 会话过期和会话 override |
| `auth` | 平台用户认证和白名单 |

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
| `server` | 服务配置，memory external 为 `embedding`，auth 关闭 |
| `bot` | 通用 bot 配置，auth 开启，列出全部平台 env |
| `telegram` | 启用 `platforms/telegram`，`.env.example` 只列 Telegram 平台字段 |
| `feishu` | 启用 `platforms/feishu`，`.env.example` 只列飞书平台字段 |
| `wechat` | 启用 `platforms/wechat`，`.env.example` 只列微信平台字段 |

