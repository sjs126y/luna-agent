<div align="center">

<h1>平台接入</h1>

<p><strong>让同一个 Luna Agent 出现在终端、微信、QQ、Telegram 和飞书</strong></p>

<p>
  <img src="https://img.shields.io/badge/WeChat-ready-07C160?logo=wechat&logoColor=white" alt="WeChat ready">
  <img src="https://img.shields.io/badge/QQ-NapCat-12B7F5" alt="QQ NapCat">
  <img src="https://img.shields.io/badge/Telegram-ready-26A5E4?logo=telegram&logoColor=white" alt="Telegram ready">
  <img src="https://img.shields.io/badge/Feishu-ready-3370FF" alt="Feishu ready">
</p>

<p>
  <a href="../README.md">项目首页</a> ·
  <a href="README.md">文档中心</a> ·
  <a href="configuration.md">配置</a> ·
  <a href="operations.md">排错</a>
</p>

</div>

---

## 平台矩阵

| 平台 | 连接方式 | 入站 | 原生出站 |
| --- | --- | --- | --- |
| **微信** | iLink 长轮询 | 文本、附件 | 图片、文件、视频 |
| **QQ** | NapCat OneBot WebSocket | 私聊、群聊、附件 | 图片、文件、音频、视频 |
| **Telegram** | Bot API | 文本、附件 | 图片、文件、音频、视频 |
| **飞书** | WebSocket | 文本、附件 | 图片、文件 |

平台能力由内置平台插件注册到 Gateway。平台插件是 deferred 插件：启动时发现，但直到 `luna-agent serve` 才加载并连接。

## 启动流程

```text
PluginManager.discover()
PluginManager.load_enabled()
Gateway.start()
  ├─ 加载 enabled platform 插件
  ├─ 创建 adapter
  ├─ adapter.connect()
  ├─ 启动平台消息循环
  └─ 消息提交 ConversationCoordinator
```

> 平台只负责连接和协议差异。会话、工具、记忆、权限与发送恢复仍由共享 Runtime 处理。

## Telegram

初始化：

```bash
uv run luna-agent init --profile telegram --copy-env --fix-dirs
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
uv run luna-agent init --profile feishu --copy-env --fix-dirs
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
uv run luna-agent init --profile wechat --copy-env --fix-dirs
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

Luna Agent 作为 OneBot WebSocket 客户端主动连接 NapCat，不需要对外开 webhook 端口。NapCat 的 WebSocket Server 同时推送入站事件并接受 Luna Agent 的 OneBot action。QQ 插件可以复用外部 NapCat，也可在 Luna Agent 启动时自动管理 NapCat 伴随进程。

1. 在 NapCat WebUI 进入“网络配置”，新建 **WebSocket 服务端**（正向 WS）。
2. 监听主机填 `0.0.0.0`，端口建议 `16611`，消息上报格式选 `array`。
3. 设置一个非空 Token，启用并保存该配置。
4. 如果希望 action 单独走 HTTP，再新建 **HTTP 服务端**，例如端口 `3000`；这一步可选。

`.env`：

```dotenv
QQ_BOT_WS_URL=ws://127.0.0.1:16611
QQ_BOT_BASE_URL=
QQ_BOT_TOKEN=replace-with-the-same-napcat-token
```

Luna Agent 在 WSL、NapCat 在 Windows 时，先尝试 `127.0.0.1`（WSL mirrored networking）。无法连接时，在 WSL 执行 `ip route show default`，将默认网关 IP 用作 Windows 主机地址，例如 `ws://172.20.64.1:16611`。Windows 防火墙需允许对应端口的本地网络访问，不要把未鉴权的 NapCat 端口暴露到公网。

插件 key：

```yaml
plugins:
  enabled:
    - platforms/qq
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
```

`mode: managed` 下，`luna-agent serve` 会先探测 `QQ_BOT_WS_URL`。如果 NapCat 已运行则不启动新进程；如果未运行则执行 `command`。默认等待 30 秒，超时后 NapCat 仍继续运行，Gateway 在后台按平台退避策略重连，不会重复拉起进程。

首次使用仍需在 NapCat WebUI 扫码登录 QQ。扫码后 NapCat 保留快速登录状态，后续只需启动 Luna Agent。NapCat 启动日志位于 `data/logs/napcat.log`，其中可查看 WebUI 地址和登录错误。

检查并启动：

```bash
uv run luna-agent serve --check-platform qq
uv run luna-agent serve
```

启动日志应出现 `QQ adapter connected via OneBot WebSocket`。`doctor` 的 QQ `adapter_health` 会显示 `ws_connected`、`action_transport`、`ws_reconnect_attempts`、`last_ws_event_at`、`self_id` 和 `companion`。

## Gateway 状态

`luna-agent doctor` 会展示平台运行状态：

| 字段 | 说明 |
| --- | --- |
| `runtime` | `connected`、`reconnecting`、`skipped`、`stopped` 等运行状态 |
| `connected` | adapter 当前是否认为自己已连接 |
| `attempts` | 连接尝试次数 |
| `pending` | Coordinator 中等待处理的提交数量 |
| `next_retry` | 自动重连的下一次尝试时间 |
| `error` | 最近连接或发送错误 |

连接失败时 Gateway 默认后台重连，backoff 为 `1, 2, 5, 10, 30, 60` 秒，之后固定 60 秒。

## 常见问题

- `runtime=skipped`：平台 env 不完整，adapter check_fn 没通过。
- `runtime=reconnecting`：连接失败，Gateway 正在按 backoff 重试。
- `connected=否` 且没有 error：通常是平台未启用或 doctor 没启动 Gateway。
- `pending` 持续增长：平台能收消息，但 Coordinator/Agent 处理不过来；发送积压应另看 Delivery Outbox。
