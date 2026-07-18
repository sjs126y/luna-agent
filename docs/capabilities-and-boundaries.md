<div align="center">

<h1>功能与边界</h1>

<p><strong>Lumora 能做什么，哪些事情会先问你，哪些事情永远不会偷偷做</strong></p>

<p>
  <img src="https://img.shields.io/badge/tools-ready-2EA44F" alt="Tools ready">
  <img src="https://img.shields.io/badge/memory-ready-2EA44F" alt="Memory ready">
  <img src="https://img.shields.io/badge/multimodal-ready-2EA44F" alt="Multimodal ready">
  <img src="https://img.shields.io/badge/security-4%20modes-7C3AED" alt="4 security modes">
</p>

<p>
  <a href="../README.md">项目首页</a> ·
  <a href="README.md">文档中心</a> ·
  <a href="configuration.md">配置</a> ·
  <a href="architecture.md">实现原理</a>
</p>

</div>

---

## 一张表看懂

| 你可以让 Lumora | 它会怎样完成 |
| --- | --- |
| 处理本地项目 | 读取、搜索、修改文件，执行命令和代码，管理后台进程 |
| 查询外部信息 | 使用网页工具、MCP、GitHub 和开发文档插件 |
| 记住长期信息 | 保存偏好、经历、关系和承诺，并在以后自然召回 |
| 处理复杂任务 | 拆分步骤、调用子 Agent、运行工作流、接受中途修正 |
| 在聊天平台工作 | 连接微信、QQ、Telegram、飞书，共享同一套会话能力 |
| 收发多媒体 | 理解图片和文件，把截图、文档、音视频作为真正附件发送 |
| 解释自己做了什么 | 展示真实工具调用、权限决定、任务状态和错误原因 |

## 核心能力

<table>
  <tr>
    <td width="50%" valign="top"><strong>工具与自动化</strong><br><br>高频文件、Shell、网络和进程工具直接可用；低频任务、工作流与 MCP 按需发现。扫描、读取和输出都有明确预算，系统会记录真实调用与结果。</td>
    <td width="50%" valign="top"><strong>长期记忆</strong><br><br>稳定人格与用户资料全量进入上下文；日常事件、偏好和承诺进入可检索记忆。支持 Lumora、Mem0 和 SQLite fallback。</td>
  </tr>
  <tr>
    <td valign="top"><strong>多平台会话</strong><br><br>CLI/TUI、微信、QQ、Telegram、飞书、Cron 和插件触发共享会话、工具、记忆与安全策略。</td>
    <td valign="top"><strong>多模态</strong><br><br>入站附件可原生理解或文本化；工具生成的截图和文件可被明确选择并发送到当前平台。</td>
  </tr>
  <tr>
    <td valign="top"><strong>复杂任务</strong><br><br>支持后台进程、并行子任务、流水线工作流、任务额度、实时停止和运行中 steer。</td>
    <td valign="top"><strong>可观测性</strong><br><br>Doctor、Activity、Tool Runs、Turn Reports 和缓存诊断帮助判断系统正在做什么、为什么失败。</td>
  </tr>
</table>

## 平台与媒体

| 平台 | 文本 | 图片 | 文件 | 音频 | 视频 |
| --- | :---: | :---: | :---: | :---: | :---: |
| 微信 | Yes | Yes | Yes | - | Yes |
| QQ / NapCat | Yes | Yes | Yes | Yes | Yes |
| Telegram | Yes | Yes | Yes | Yes | Yes |
| 飞书 | Yes | Yes | Yes | - | - |

最终发送能力以平台实际限制与 Adapter capability 为准；不支持的类型会降级成文件或明确提示，不会把服务器本地路径发给用户。

## 安全模式

```mermaid
flowchart LR
    RO[Read Only] --> AF[Ask First]
    AF --> LA[Local Auto]
    LA --> FA[Full Auto]
    RO -. 自动化程度 .-> FA
```

| Mode | 适合场景 | 默认行为 |
| --- | --- | --- |
| **Read Only** | 浏览与检查 | 允许范围内只读；扩权请求直接拒绝 |
| **Ask First** | 日常协作 | 读操作优先，写入和网络按需询问 |
| **Local Auto** | 项目开发 | 工作目录内自主读写并使用网络；越界文件和显式高风险工具询问 |
| **Full Auto** | 受信环境 | 最大化自动执行，不提供交互扩权，硬安全边界仍保留 |

### 不会被模式关闭的边界

<table>
  <tr>
    <td><strong>Protected paths</strong><br>密钥、Git 内部目录、SSH 等受保护位置。</td>
    <td><strong>Path safety</strong><br>路径穿越、符号链接和越界访问检查。</td>
  </tr>
  <tr>
    <td><strong>Sandbox</strong><br>文件根目录、只读根目录、进程和网络隔离。</td>
    <td><strong>Audit</strong><br>工具决定、实际结果和失败原因保留安全摘要。</td>
  </tr>
</table>

允许操作时可以选择：

- 只允许这一次。
- 拒绝这一次并停止当前工具链。
- 在当前会话中限时允许同类工具或精确资源。

切换 Mode、重置会话或重启服务后，临时授权会被清空。

## 长期运行能力

| 问题 | Lumora 的用户体验 |
| --- | --- |
| 平台断线 | 自动重连并在 Doctor 中显示状态 |
| 同一会话连续来消息 | 保持顺序，不让两个回合互相覆盖 |
| 需要立刻停止 | `/stop` 走实时控制通道，不等待普通队列 |
| 需要中途改要求 | `/steer` 注入当前正在运行的回合 |
| 发送到一半失败 | 记录每个消息分片，只重试没有成功的部分 |
| MCP 很慢 | 后台启动，单个 Server 不阻塞核心 Gateway |
| 向量库不可用 | 保留 SQLite 权威数据并使用 fallback |
| 模型返回空内容 | 进入无工具收尾，避免发送内部占位文本 |

## 配置哪些行为

| 配置入口 | 用途 |
| --- | --- |
| `.env` | 模型、平台和外部服务密钥 |
| `config.yaml` | Mode、工具审批、沙箱、插件、MCP、Memory、附件和平台行为 |
| `data/system/*.md` | Agent 人格、用户资料、关系和长期系统提示 |
| `plugins.config.*` | 每个外置插件自己的隔离配置 |

配置示例、字段和迁移说明见 [配置说明](configuration.md)。

## 明确没有做什么

- 不让模型绕过工具 Runtime 直接操作系统。
- 不把知识 RAG 和个人记忆混成同一份数据。
- 不因为启用了 Full Auto 就开放受保护路径。
- 不把本地文件路径当成平台附件协议。
- 不把插件变成可以任意访问核心对象的 Module 注入。
- 不为每个平台复制一套 Agent Loop。

## 继续深入

| 主题 | 文档 |
| --- | --- |
| 这些能力内部如何协作 | [架构说明](architecture.md) |
| 如何配置安全、Memory 和 MCP | [配置说明](configuration.md) |
| 如何连接各聊天平台 | [平台接入](platforms.md) |
| 插件能注册哪些能力 | [插件系统](plugins.md) |
| 出问题怎样定位 | [运维与排错](operations.md) |
