<div align="center">

<h1>Frontend Interface Requirements</h1>

<p><strong>前端仍需要后端提供什么，以及哪些能力已经可以直接消费</strong></p>

<p>
  <img src="https://img.shields.io/badge/blocking%20requirements-0-2EA44F" alt="No blocking requirements">
  <img src="https://img.shields.io/badge/artifact%20UI-optional-0A84FF" alt="Artifact UI optional">
  <img src="https://img.shields.io/badge/updated-2026--07--19-555555" alt="Updated 2026-07-19">
</p>

<p>
  <a href="README.md">项目首页</a> ·
  <a href="docs/README.md">文档中心</a> ·
  <a href="BACKEND_INTERFACE.md">后端契约</a> ·
  <a href="FRONTEND_PROGRESS.md">前端进度</a>
</p>

</div>

---

本文是前端向后端提出小型字段/接口需求的活动清单。已经实现的字段级契约不在这里重复，统一以 `BACKEND_INTERFACE.md` 为准。

## 当前状态

Luna Agent 命名迁移没有产生新的前端接口需求：事件协议仍为 v1，字段、命令结果和确认语义均未变化。仓库内前端代码使用 `luna_agent.*`，启动命令使用 `luna-agent`；旧 `personal-agent` 仅作为迁移期兼容入口。

inline TUI 已消费：

- `ConversationEvent`、协议 schema 与 delta opt-in。
- `tool_start`、`tool_decision`、`tool_end` 的结构化展示和确认字段。
- allow once / deny / timed allow 语义，以及 `temporary_grant_ttl_seconds`。
- Security v4 四档 Mode、`/deny all`、`/permissions`、精确 tool/resource grants。
- Slash command registry、children、choice/dynamic arguments 与 `CommandResult v2`。
- `/tool-runs`、`/activity`、usage/context、cache diagnostics 和 turn report 摘要。
- `/stop`、`/steer` 及 pending confirmation 的实时控制结果。

当前没有阻塞 inline TUI 的后端必做接口。

## 已提供但尚未做专门 UI

后端出站多模态新增以下稳定契约：

- `artifact_available`
- `response_artifact_selected`
- `tool_end.artifacts[]`
- `ConversationTurnResult.outbound_message`
- `SubmissionOutcome.message`
- Delivery part 的 `kind/success/error/ambiguous/attempts`

安全摘要字段包括 `artifact_id`、`kind`、`filename`、`mime_type`、`size_bytes`、`source` 和 `delivery_eligible`，不包含 base64、完整 URI 或本地路径。

当前 TUI 可以继续依赖通用 tool/event renderer，不要求立即增加附件缩略图或 multipart Delivery 面板。未来实现时只消费 `artifact_id` 和安全 metadata，不直接读取 ArtifactStore。

## 可选后端增强

这些不是当前阻塞项，只在前端准备实现对应 UI 时推进：

### 更专门的文件预览

- `write` / `edit`：可选 `diff_summary`、`diff_preview`
- network tool：可选 request body/header 的脱敏摘要
- artifact：可选受控 thumbnail/preview endpoint，不能暴露本地路径

所有字段必须允许为空；前端按存在性渐进展示。

### Activity 历史

当前 `/activity` 只需要活跃对象和近期详情。如果未来增加历史抽屉，再考虑 completed activity 分页、时间范围和稳定 cursor；现在不要求后端持久化新的 Activity 副本。

## Future Desktop App

Desktop 不作为当前 CLI/TUI 阻塞项。建议技术边界：

```text
Tauri 2
  -> React + TypeScript + Vite
  -> localhost HTTP + WebSocket
  -> Python desktop facade
  -> AppRuntime / ConversationCoordinator / Delivery
```

后端 facade 应：

- 只监听 `127.0.0.1`，启动时生成临时 token。
- HTTP 提供 session、history、command、activity、tool runs、turn reports、confirm、附件上传和配置/doctor 查询。
- WebSocket 复用当前 `ConversationEvent` 语义，不创建第二套事件协议。
- 上传文件先进入后端 AttachmentStore，composer 只引用 `attachment_id`。
- 出站结果只暴露 Artifact metadata，不暴露本地路径。
- 所有文件、工具和网络操作继续经过后端 permission/sandbox/audit。

建议前端栈仍为 React/TypeScript、Tailwind、Radix/shadcn、lucide-react、Zustand 与 TanStack Query；这只是前端技术方向，不要求后端现在实现 `desktop-serve`。

## 前端自行待办

- 继续观察 confirm 面板的视觉密度、风险层级和键盘提示。
- 保持 tool trace 简洁，避免 raw JSON、过度拟人化和冗长中文标签。
- 长输出继续使用 `Ctrl+O` 基础展开策略；多工具选择展开暂缓。
- Artifact 缩略图、附件列表和 Delivery part 状态等真实需要出现后再设计。
- 状态行中文化不推进。

## 维护规则

- 新需求应描述消费场景、最小字段、空值语义和安全边界。
- 后端提供且前端消费后，从本文移除并写入 `BACKEND_INTERFACE.md` / `FRONTEND_PROGRESS.md`。
- 被明确暂缓的需求必须标注，不能被当作当前阻塞项。
- 不在本文复制完整已实现 schema；权威定义只保留一份。
