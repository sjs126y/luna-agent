<div align="center">

<h1>Luna Agent 平台媒体联调清单</h1>

<p><strong>自动化测试已通过，剩下的是微信与 QQ 的真实客户端体验</strong></p>

<p>
  <img src="https://img.shields.io/badge/automated-1050%20passed-2EA44F" alt="Automated tests passed">
  <img src="https://img.shields.io/badge/WeChat-E2E%20pending-F59E0B" alt="WeChat E2E pending">
  <img src="https://img.shields.io/badge/QQ-E2E%20pending-F59E0B" alt="QQ E2E pending">
</p>

<p>
  <a href="README.md">项目首页</a> ·
  <a href="docs/platforms.md">平台接入</a> ·
  <a href="docs/operations.md">排错</a> ·
  <a href="TODO.md">当前待办</a>
</p>

</div>

---

本文只保留仍需真实平台验证的出站媒体场景。ArtifactStore、DeliveryPlanner、multipart Outbox 和各 Adapter 的自动化测试已经进入全量回归，不在这里重复测试内部实现。

## 测试规则

1. 使用当前 `main` 并重启 Gateway，不启动第二个使用同一数据库的实例。
2. 不读取或发送 `.env`、token、Cookie、私钥和其他 blocked 文件。
3. 每项只调用完成验证所需的最少工具次数；成功或明确失败后停止。
4. 记录平台、session、Artifact kind、工具调用次数、最终客户端表现和关键错误。
5. 日志不得出现 AES key、base64 正文、完整本地 URI 或 ArtifactStore 内部路径。

## 1. 通用 Artifact 选择

让小鹿用 Playwright 打开 `https://example.com`，截图并发送。

预期：

- MCP 返回当前 turn 的 `artifact_id`。
- Agent 只调用一次 `response_attach`。
- `artifact_available` 和 `response_artifact_selected` 均出现。
- 最终文字不包含内部分析、`assistant_final`、`MEDIA:` 或本地路径。

再生成一张截图，但明确要求“只告诉我结果，不发送图片”。预期生成 Artifact，但不调用 `response_attach`，平台只收到文本。

## 2. 普通本地文件

让小鹿用 `write` 创建一个新的无敏感内容 `.txt` 文件并发给用户。

预期链路：

```text
write -> artifact_from_file -> artifact_id -> response_attach -> Delivery
```

文件写入本身不得自动发送；`artifact_from_file` 应声明精确 filesystem read 资源，并把源文件复制进 ArtifactStore。

## 3. 微信

分别验证一张 PNG 和一个小型 TXT/PDF 文件。

预期：

- 图片使用 `image_item`，客户端可正常打开，不显示“过期或已清理”。
- 文件使用 `file_item`，文件名与 MIME 正常。
- 日志依次完成 `getuploadurl -> encrypted CDN upload -> sendmessage`。
- 视频如有样本再验证；当前不要求微信语音出站。

微信仍失败时重点检查 CDN upload 响应、媒体 AES key 编码、item type 和服务端返回的 media metadata，不要反复调用工具生成新 Artifact。

## 4. QQ / NapCat

前置条件：NapCat 已登录 QQ，WebSocket Server 使用配置中的 URL/Token；managed 模式下 companion health 为 running 或已复用外部进程。

依次验证：

1. 私聊文本收发。
2. 群聊文本收发与目标解析。
3. PNG 图片。
4. TXT/PDF 文件。
5. 小型音频和视频（有合适样本时）。

预期：OneBot action 只执行一次，媒体使用 `base64://` segment，不要求 Windows NapCat 访问 WSL 路径。断线后 Adapter 进入 reconnecting，上层 Delivery 保留待投递记录。

## 5. 隔离与恢复

- 在另一 session 使用旧 `artifact_id`：`response_attach` 应返回 scope mismatch。
- 工具生成 Artifact 后、最终回复前 `/stop`：未完成的附件回复不应投递。
- 模拟媒体 part 明确失败后恢复：只重试失败 part，已成功文本不重复发送。
- 超时且无法判断是否送达：part 标记 ambiguous，不盲目重复发送。

## 完成标准

- 微信图片和文件客户端实测通过。
- QQ 私聊/群聊、图片和文件实测通过；音视频可按实际需求补测。
- 没有重复工具循环、重复平台发送、内部分析泄漏或本地路径泄漏。
- 结果写回 `BACKEND_PROGRESS.md` 与 `TODO.md`，完成后删除本清单。
