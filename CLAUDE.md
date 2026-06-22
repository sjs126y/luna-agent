# Personal Agent

## 这是什么

一个类似 Hermes 的多平台通用 AI Agent 系统。用户通过 Feishu等平台发消息 → 网关路由 → Agent 循环调 LLM → 执行工具 → 返回结果。

## 技术栈

- Python 3.12+
- uv 包管理
- asyncio 异步架构
- OpenAI 兼容 API 和 Anthropic API（多 Provider）
- SQLite + aiosqlite 做会话持久化
- python-telegram-bot（Telegram 适配器）
- pydantic-settings 管理配置
- 不依赖 LangChain、CrewAI 等重框架

## 参考文档

我有一份详细的架构学习笔记：`Hermes源码学习总结.md`（桌面）。里面记录了 Hermes 的完整架构设计。请在开始设计前**先读完这份笔记**，理解以下关键点：

- 消息处理链路：适配器 → 网关 → Agent → 返回
- 线程模型：主事件循环 + ThreadPoolExecutor（Agent 和工具调用在线程池）
- 两层并发控制：`_active_sessions`（适配器内）+ `_running_agents`（跨适配器）
- Agent 缓存：`_agent_cache` 复用系统提示，省 Token
- 工具系统：自注册 + 工具集解析 + BM25 渐进式披露
- 上下文压缩：token 超阈值时用备用模型做摘要
- 重试机制：6 种具体重试，不是笼统的出错重试
- 排队消息用 create_task 而非递归（C 栈溢出是真实 bug）
- 上下文溢出时不持久化（防无限循环）

## 核心功能

- **多平台接入**：Telegram 优先，架构预留 Discord 等扩展。适配器通过 registry 自注册，加平台不改核心代码
- **Agent 引擎**：while 循环调 LLM → 解析 tool_calls → 执行 → 继续
- **工具系统**：自注册（ToolEntry dataclass）+ 工具集分组 + 执行管道（中间件 → block 检查 → execute → 后处理）。工具、MemoryProvider、适配器都走同一套 registry 模式
- **插件/扩展系统**：registry 自注册模式，新增组件（平台、工具、记忆后端）无需改核心逻辑
- **记忆系统**：对话历史持久化 + 用户记忆（内置 MemoryStore + 预留 MemoryProvider 接口）
- **会话管理**：多用户多会话隔离 + 跨轮次复用 Agent 实例
- **定时任务**：MVP 阶段预留接口，Phase 2 实现（参考 Hermes 的 tick() + jobs.json + at-most-once）

## 不做的事（MVP 阶段）

- 不做上下文压缩（先用简单的 token 计数 + 截断）
- 不做多 Provider 动态切换
- 不做 BM25 渐进式披露（工具少，用不到）
- 不做多 Gateway 进程（单进程够用）

## 架构原则

- 参考 Hermes 笔记中的设计，但 MVP 阶段一切从简
- 适配器模式：每个平台一个适配器，产出统一消息格式
- 自注册：工具通过 ToolEntry dataclass 注册到 registry
- while 循环即编排，不搞复杂的 DAG / State Machine
- 异步优先：网络 I/O 全异步，Agent 和工具在线程池执行
- 保留扩展点：接口设计预留，但不提前实现

## 编码风格

- Python 类型标注
- 函数优先于类，除非确实需要状态（如 Agent 需要挂大量属性）
- 日志用 logging，不 print
- 错误处理：不要空 catch，要么处理要么让它崩
