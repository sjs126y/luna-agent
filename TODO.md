<div align="center">

<h1>Luna Agent TODO</h1>

<p><strong>只保留真正还需要推进的事情</strong></p>

<p>
  <img src="https://img.shields.io/badge/platform%20E2E-pending-F59E0B" alt="Platform E2E pending">
  <img src="https://img.shields.io/badge/plugin%20runtime-stabilizing-0A84FF" alt="Plugin runtime stabilizing">
  <img src="https://img.shields.io/badge/CI-first%20remote%20run-F59E0B" alt="CI first remote run pending">
  <img src="https://img.shields.io/badge/updated-2026--07--21-555555" alt="Updated 2026-07-21">
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
| **P1** | CI 首次远端验证 | 推送 `main` 后 Ubuntu 全量测试与 Windows AppContainer smoke 均通过，并按需设为 required checks |
| **P1** | 插件 Runtime 稳定性验证 | 有可重复的热重载/崩溃恢复 soak 与 fault-injection，诊断能定位 generation 和 supervisor 状态 |
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
- capability catalog 的模型限制与 provider 官方变更是否一致；使用 `doctor` 的来源与校验日期持续核对。
- 并发 Memory prefetch。
- MCP cold start / ready time。
- Tool task success rate 与重复调用。

## P1：插件稳定化与工程门禁

- 当前 generation、热重载、主动 runner 和进程隔离架构已收口，不再继续结构性重写。
- 推送当前 `main`，确认新增 GitHub Actions 在真实 Ubuntu/Windows runner 上首次完整通过；稳定后再配置分支保护。
- 为 Worker 崩溃恢复、连续 reload、active runner 切换和 shutdown 建立长时间 soak/fault-injection 基线。
- 兼容层移除条件、Manager 拆分触发线和 Release 治理见 [插件架构技术债](PLUGIN_ARCHITECTURE_DEBT.md)。

## 安全强化 Backlog

这些不是当前阻塞项：

- Bubblewrap network namespace 不可用环境下的 nftables/seccomp 或平台原生沙箱。
- DNS 校验与实际连接之间的 TOCTOU 窗口。
- MCP 市场的版本固定、来源签名、安装预览和首次启用确认。
- MCP tool annotations 只作为收紧提示，不能放宽本地策略。
- Read Only 模式下 `sandbox.read_roots` 的正向读取授权，见 [后端延期修复](BACKEND_DEFERRED_FIXES.md)。

> 已完成的 Security v4、MCP Runtime、Conversation Runtime、Plugin Runtime、Memory 和出站多模态不再放在 TODO；历史见 [项目演进记录](PROJECT_EVOLUTION.md)。
