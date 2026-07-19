<div align="center">

<h1>Luna Agent TODO</h1>

<p><strong>只保留真正还需要推进的事情</strong></p>

<p>
  <img src="https://img.shields.io/badge/platform%20E2E-pending-F59E0B" alt="Platform E2E pending">
  <img src="https://img.shields.io/badge/plugin%20hot%20reload-ready-2EA44F" alt="Plugin hot reload ready">
  <img src="https://img.shields.io/badge/active%20plugin-runtime%20ready-2EA44F" alt="Active plugin runtime ready">
  <img src="https://img.shields.io/badge/active%20plugins-4%20ready-2EA44F" alt="Four active plugins ready">
</p>

<p>
  <a href="README.md">项目首页</a> ·
  <a href="docs/README.md">文档中心</a> ·
  <a href="luna-agent-roadmap.zh-CN.md">路线图</a> ·
  <a href="PLATFORM_MEDIA_TEST_CHECKLIST.md">平台联调</a>
</p>

</div>

---

## 当前队列

| 优先级 | 方向 | 完成标准 |
| :---: | --- | --- |
| **P0** | 微信 / QQ 真实平台联调 | 微信图片和文件可打开；QQ 私聊、群聊、图片和文件通过 |
| **P1** | 固定 Benchmark 持续观测 | 长会话、Memory prefetch、MCP 冷启动和缓存有可比较数据 |
| **P2** | 前端 Artifact / Delivery UI | 真实需要出现后再设计缩略图、附件列表和分片状态 |
| **Later** | 主动决策策略插件 | 在已完成的主动 runtime 上按真实需求增加候选、去重、冷却、静默时间、优先级、预算和反馈闭环 |
| **Later** | 独立知识 RAG 插件 | 与个人记忆分离，保存原始证据和引用 |

## P0：真实平台联调

- 微信：修正 AES key 与媒体类型后的图片、文件客户端复测。
- QQ：准备可用 NapCat/QQ 环境，验证登录、私聊、群聊、图片和文件；音视频按实际需求补测。
- 统一步骤与通过条件见 [平台媒体联调清单](PLATFORM_MEDIA_TEST_CHECKLIST.md)。

## P1：性能持续观测

本地 Qdrant 已移除主要跨区网络尾延迟。后续使用相同数据集与模型比较：

- 首 Token 和完整响应 P50/P95。
- 长会话上下文与 provider cache。
- 自动识别与显式配置的 context window 是否和中转站真实限制一致。
- 并发 Memory prefetch。
- MCP cold start / ready time。
- Tool task success rate 与重复调用。

## 安全强化 Backlog

这些不是当前阻塞项：

- Bubblewrap network namespace 不可用环境下的 nftables/seccomp 或平台原生沙箱。
- DNS 校验与实际连接之间的 TOCTOU 窗口。
- MCP 市场的版本固定、来源签名、安装预览和首次启用确认。
- MCP tool annotations 只作为收紧提示，不能放宽本地策略。

> 已完成的 Security v4、MCP Runtime、Conversation Runtime、Plugin Runtime、Memory 和出站多模态不再放在 TODO；历史见 [项目演进记录](PROJECT_EVOLUTION.md)。
