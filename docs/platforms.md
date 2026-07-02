# 平台接入

平台能力由内置平台插件注册到 Gateway。平台插件是 deferred 插件：启动时发现，但直到 `personal-agent serve` 才加载并连接。

## 启动流程

```text
PluginManager.discover()
PluginManager.load_enabled()
Gateway.start()
  ├─ 加载 enabled platform 插件
  ├─ 创建 adapter
  ├─ adapter.connect()
  ├─ 启动平台消息循环
  └─ 消息进入 ConversationService / Agent
```

## Telegram

初始化：

```bash
uv run personal-agent init --profile telegram --copy-env --fix-dirs
```

`.env`：

```dotenv
LLM_API_KEY=
TELEGRAM_BOT_TOKEN=
```

插件 key：

```yaml
plugins:
  enabled:
    - platforms/telegram
```

## 飞书

初始化：

```bash
uv run personal-agent init --profile feishu --copy-env --fix-dirs
```

`.env`：

```dotenv
LLM_API_KEY=
FEISHU_APP_ID=
FEISHU_APP_SECRET=
```

插件 key：

```yaml
plugins:
  enabled:
    - platforms/feishu
```

## 微信

初始化：

```bash
uv run personal-agent init --profile wechat --copy-env --fix-dirs
```

`.env`：

```dotenv
LLM_API_KEY=
WEIXIN_TOKEN=
WEIXIN_ACCOUNT_ID=
WEIXIN_USER_ID=
WEIXIN_BASE_URL=https://ilinkai.weixin.qq.com
```

插件 key：

```yaml
plugins:
  enabled:
    - platforms/wechat
```

## Gateway 状态

`personal-agent doctor` 会展示平台运行状态：

| 字段 | 说明 |
| --- | --- |
| `runtime` | `connected`、`reconnecting`、`skipped`、`stopped` 等运行状态 |
| `connected` | adapter 当前是否认为自己已连接 |
| `attempts` | 连接尝试次数 |
| `pending` | adapter 内存队列里等待处理的消息数 |
| `next_retry` | 自动重连的下一次尝试时间 |
| `error` | 最近连接或发送错误 |

连接失败时 Gateway 默认后台重连，backoff 为 `1, 2, 5, 10, 30, 60` 秒，之后固定 60 秒。

## 常见问题

- `runtime=skipped`：平台 env 不完整，adapter check_fn 没通过。
- `runtime=reconnecting`：连接失败，Gateway 正在按 backoff 重试。
- `connected=否` 且没有 error：通常是平台未启用或 doctor 没启动 Gateway。
- `pending` 持续增长：平台能收消息，但 agent 或发送链路处理不过来，需要看 Gateway 和 LLM 日志。

