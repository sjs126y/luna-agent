<div align="center">

<h1>Frontend Decisions</h1>

<p><strong>Inline TUI 的视觉和交互取舍</strong></p>

<p>
  <img src="https://img.shields.io/badge/style-quiet%20%26%20informative-0A84FF" alt="Quiet and informative">
  <img src="https://img.shields.io/badge/tool%20trace-compact-2EA44F" alt="Compact tool trace">
</p>

<p>
  <a href="../README.md">项目首页</a> ·
  <a href="README.md">文档中心</a> ·
  <a href="../FRONTEND_PROGRESS.md">前端进度</a> ·
  <a href="../BACKEND_INTERFACE.md">接口契约</a>
</p>

</div>

---

本文记录当前用户对 inline TUI / future frontend polish 的取舍偏好。以下判断是 2026-07-06 的当前意见，后续可以随产品体验变化而调整。

## 当前偏好

- 本轮多工具结果列表 / Ctrl+O 选择展开：用户感兴趣，但先不着急推进。之前做过并失败过，说明实现难度和交互细节风险都不低。
- 工具 trace 文案：不要过度拟人化，也不要堆太多中文标签。工具行应简洁、信息化，例如展示搜索词、路径、命令、URL、进程标签；拟人化语气主要留给助手回复正文。
- 确认框视觉层级：可以继续优化。重点是风险、默认动作、允许/拒绝动作和关键预览信息更清楚。
- 长输出折叠：可以做，当前已实现基础策略。完整输出展开时作为新的展开块打印在当前 scrollback 位置；不要尝试回到原截断处插入，因为普通终端 scrollback 不适合做历史位置重写。
- 输入区细节：当前已推进基础视觉优化。输入框需要简洁但有对比度；输入 `/` 后应预留命令区域并让输入框上移，后续命令内容继续跟随后端能力稳定。
- 状态行中文化：没必要，不作为当前目标。
- 出站 Artifact/Delivery：后端契约已经稳定，但当前 TUI 不急于增加附件缩略图或复杂 multipart 状态面板；先保持工具事件和最终文本兼容，等真实使用需求再设计。

## 当前可推进项

- 继续调整确认框视觉密度和层级。
- 继续观察长工具输出的折叠与展开体验，必要时再调整阈值和样式。
- 保持工具 trace 简洁化，避免 raw JSON 和过度拟人化描述。
- 继续观察输入框背景、slash 命令区域高度和补全位置是否符合真实终端使用习惯。
