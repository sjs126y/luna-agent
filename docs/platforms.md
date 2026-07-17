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

## QQ（NapCat）

Lumora 作为 OneBot WebSocket 客户端主动连接 NapCat，不需要对外开 webhook 端口。NapCat 的 WebSocket Server 同时推送入站事件并接受 Lumora 的 OneBot action。

1. 在 NapCat WebUI 进入“网络配置”，新建 **WebSocket 服务端**（正向 WS）。
2. 监听主机填 `0.0.0.0`，端口例如 `3001`，消息上报格式选 `array`。
3. 设置一个非空 Token，启用并保存该配置。
4. 如果希望 action 单独走 HTTP，再新建 **HTTP 服务端**，例如端口 `3000`；这一步可选。

`.env`：

```dotenv
QQ_BOT_WS_URL=ws://127.0.0.1:3001
QQ_BOT_BASE_URL=
QQ_BOT_TOKEN=replace-with-the-same-napcat-token
```

Lumora 在 WSL、NapCat 在 Windows 时，先尝试 `127.0.0.1`（WSL mirrored networking）。无法连接时，在 WSL 执行 `ip route show default`，将默认网关 IP 用作 Windows 主机地址，例如 `ws://172.20.64.1:3001`。Windows 防火墙需允许对应端口的本地网络访问，不要把未鉴权的 NapCat 端口暴露到公网。

插件 key：

```yaml
plugins:
  enabled:
    - platforms/qq
```

检查并启动：

```bash
uv run personal-agent serve --check-platform qq
uv run personal-agent serve
```

启动日志应出现 `QQ adapter connected via OneBot WebSocket`。`doctor` 的 QQ `adapter_health` 会显示 `ws_connected`、`action_transport`、`ws_reconnect_attempts`、`last_ws_event_at` 和 `self_id`。

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
