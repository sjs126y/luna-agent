<div align="center">

<h1>Lumora TODO</h1>

<p><strong>只保留真正还需要推进的事情</strong></p>

<p>
  <img src="https://img.shields.io/badge/platform%20E2E-pending-F59E0B" alt="Platform E2E pending">
  <img src="https://img.shields.io/badge/plugin%20lifecycle-planned-7C3AED" alt="Plugin lifecycle planned">
  <img src="https://img.shields.io/badge/active%20system-future-555555" alt="Active system future">
</p>

<p>
  <a href="README.md">项目首页</a> ·
  <a href="docs/README.md">文档中心</a> ·
  <a href="lumora-roadmap.zh-CN.md">路线图</a> ·
  <a href="PLATFORM_MEDIA_TEST_CHECKLIST.md">平台联调</a>
</p>

</div>

---

## 当前队列

| 优先级 | 方向 | 完成标准 |
| :---: | --- | --- |
| **P0** | 微信 / QQ 真实平台联调 | 微信图片和文件可打开；QQ 私聊、群聊、图片和文件通过 |
| **P1** | 插件安装、卸载与热加载 | `RuntimeSnapshot + lease + drain`，Manager 可安全 reconcile |
| **P1** | 固定 Benchmark 持续观测 | 长会话、Memory prefetch、MCP 冷启动和缓存有可比较数据 |
| **P2** | 前端 Artifact / Delivery UI | 真实需要出现后再设计缩略图、附件列表和分片状态 |
| **Later** | 主动决策系统 | 候选、去重、冷却、静默时间、优先级、预算和反馈闭环 |
| **Later** | 独立知识 RAG 插件 | 与个人记忆分离，保存原始证据和引用 |

## P0：真实平台联调

- 微信：修正 AES key 与媒体类型后的图片、文件客户端复测。
- QQ：准备可用 NapCat/QQ 环境，验证登录、私聊、群聊、图片和文件；音视频按实际需求补测。
- 统一步骤与通过条件见 [平台媒体联调清单](PLATFORM_MEDIA_TEST_CHECKLIST.md)。

## P1：插件生命周期

当前插件注册已经具备所有权、冲突检查和失败回滚，但运行中的安装/卸载还缺少：

```text
new snapshot -> switch generation -> lease old snapshot -> drain -> close resources
```

不要通过直接修改活动 Registry 或在核心中加入插件特例实现“热加载”。

## P1：性能持续观测

本地 Qdrant 已移除主要跨区网络尾延迟。后续使用相同数据集与模型比较：

- 首 Token 和完整响应 P50/P95。
- 长会话上下文与 provider cache。
- 并发 Memory prefetch。
- MCP cold start / ready time。
- Tool task success rate 与重复调用。

## 安全强化 Backlog

这些不是当前阻塞项：

- Bubblewrap network namespace 不可用环境下的 nftables/seccomp 或平台原生沙箱。
- DNS 校验与实际连接之间的 TOCTOU 窗口。
- MCP 市场的版本固定、来源签名、安装预览和首次启用确认。
- MCP tool annotations 只作为收紧提示，不能放宽本地策略。

> 已完成的 Security v4、MCP Runtime、Conversation Runtime、Memory 和出站多模态不再放在 TODO；历史见 [项目演进记录](PROJECT_EVOLUTION.md)。
