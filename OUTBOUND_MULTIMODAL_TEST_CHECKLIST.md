# 小鹿出站多模态测试清单

更新时间：2026-07-17

## 测试前

1. 重启 Gateway，确保加载当前分支代码。
2. 执行 `/mode ask-first`，清空或确认当前授权状态。
3. 不要让小鹿直接读取任意本地敏感文件；优先使用 Playwright 生成新的截图 Artifact。

## 1. 微信图片端到端

首轮实测记录：Playwright 成功生成 `data/mcp/outbound-multimodal-example.png`，但 MCP 只返回 `./outbound-multimodal-example.png`，未产生 `artifact_id`，因此链路在 `response_attach` 前停止。当前分支已修复该物化边界；需重启 Gateway 后按本节重新验证，未复测前不标记通过。

发送：

```text
小鹿，请用 Playwright 打开 https://example.com，截一张图并把截图发给我。只执行一次截图，不要重复调用。
```

预期：

- Playwright 返回一个带 `artifact_id` 的图片产物。
- 小鹿调用一次 `response_attach`，然后输出普通文字。
- 微信先收到说明文字，再收到真实图片，而不是本地路径或 `MEDIA:` 文本。
- 日志依次出现 `getuploadurl`、CDN upload 和 `sendmessage`，不包含 AES key、base64 或完整 Artifact 路径。

## 2. 不自动发送

让小鹿生成截图，但明确要求“只告诉我生成结果，不要把图片发出来”。

预期：工具可以生成 Artifact，但不调用 `response_attach`，微信只收到文本。

## 3. 同一 Artifact 不重复

要求小鹿把同一个 `artifact_id` 选择两次。

预期：`response_attach` 幂等，最终只发送一次图片。

## 4. 不支持类型降级

在不支持目标媒体类型的平台发起测试。

预期：Delivery 降级为普通文件或明确的“附件未发送”文字，不泄露 `/home/...`、`/tmp/...` 或 `data/artifacts/...`。

## 5. 重试与部分投递

在测试环境模拟媒体上传明确失败后恢复，再处理到期 Outbox。

预期：已经成功的文本 part 不再发送，只重试失败媒体；超时且不确定是否送达时标记 ambiguous，不自动重复发送。

## 6. 会话隔离

在另一个 session 尝试使用前一个 session 的 `artifact_id`。

预期：`response_attach` 返回 scope mismatch，不发送附件。

## 7. 停止行为

工具生成 Artifact 后、模型完成最终回复前发送 `/stop`。

预期：停止轮不投递尚未形成的附件回复，后续普通消息仍正常。

## 日志判定

通过条件：

- 有 `artifact_available` 和 `response_artifact_selected`。
- `tool_end.artifacts` 只有安全摘要。
- `PostDelivery` 能看到每个 part 的 success/error/ambiguous/attempts。
- 没有 base64、AES key、完整 URI 或本地路径泄漏。
- 没有重复工具循环和重复平台发送。

## Playwright Artifact 断点排查

- 截图成功但只有 `./xxx.png`：确认 Gateway 已重启，Playwright 进程 cwd 为 `data/mcp/playwright`，启动参数包含 `--output-dir .`。
- 正常工具结果应同时保留截图说明，并追加 `Available response artifacts` 及 `artifact_id`。
- `data/mcp/playwright/` 是 MCP 临时输出目录；真正发送使用的是 `data/artifacts/<artifact_id>/` 中的受控副本。
- 不要把任意 MCP 的文本路径直接提升为附件；未配置 `artifact_roots` 的 server 必须保持文本行为。
- PNG/JPEG 的 Outbox operation 应为 `kind: image`，不能是 `kind: file, degraded_from: resource`；后者会让微信按文件协议发送图片。
- 最终文字不得包含 `assistant_final` 或其前面的英文内部分析；Codex Responses 若只有内部通道内容，应触发无工具安全收尾。
