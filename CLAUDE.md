# Personal Agent

## 概述

多平台 AI Agent 系统，参考 Hermes 架构。飞书/Telegram/微信 → Gateway → Agent 循环调 LLM → 执行工具 → 返回。

## 技术栈

Python 3.12+ / uv / asyncio / httpx / aiohttp / aiosqlite /
lark-oapi (飞书) / python-telegram-bot / iLink API (微信) /
fastembed (语义记忆) / pymupdf + python-docx / pydantic-settings

不依赖 LangChain、CrewAI 等重框架。

## 架构

```
平台适配器（Feishu / Telegram / WeChat）
    │ BasePlatformAdapter 自注册 → PlatformRegistry
    ▼
Gateway（中央调度器）
    ├─ Auth（白名单 + 6位配对码）/ 命令检测 / 忙检查 / Agent 调度
    ├─ SessionStore（JSON 索引 + SQLite）/ 压缩链
    ├─ _agent_cache LRU 128 + generation invalidation
    └─ 6 个 Hook 挂载点
    │
    ▼
Agent 引擎（while 循环）
    ├─ system_prompt（模板 + 工具列表 + data/system/*.md）
    ├─ LLM 调用 → 解析 tool_calls → 执行 → 继续
    ├─ 3 层消息：history → ctx.messages → api_messages
    ├─ 6 种重试 / 中断式 LLM / 上下文压缩
    └─ MemoryManager（内置 system 素材 + 外部 embedding 检索）
```

## 数据目录

```
data/
├── system/              # 系统提示素材（手写，注入 system prompt）
│   ├── SOUL.md          # 角色与人格
│   ├── AGENT.md         # 行为规则
│   ├── MEMORY.md        # 用户画像（§ 分隔条目）
│   └── USER.md          # 用户偏好
├── memory/              # 外部语义记忆（embedding）
│   ├── external_memories.json
│   ├── external_embeddings.npy
│   └── .fastembed_cache/  # ONNX 模型
├── wechat/              # 微信凭据
├── auth/                # 认证白名单
├── checkpoints/         # 文件写入备份
├── cron/                # 定时任务
├── todos.db             # 待办（SQLite，跨 session）
├── audit.log            # 审计日志
└── state.db             # 会话（SQLite）
```

## 工具系统

21 个工具，6 个分组（toolsets），config.yaml 控制启用。

**执行管道**：scope gate（权限/配额）→ checkpoint（写前备份）→ pre-hook → dispatch → post-process（截断 8000）

**并发**：is_parallel_safe → asyncio.to_thread 并发，其余串行逐个 await。

**安全**：bash 白名单（40+ 命令）+ 网络隔离 + 文件扩展名白名单 + 路径遍历防护 + 审计日志。Destructive 工具需 `/allow write`。

**桥接**：tool_search 返回完整 schema → LLM 直接调真实工具，不走 tool_call 绕过。

3 个桥接工具仅在 deferrable 工具存在时才注入 prompt（当前无 deferrable，桥接处于 dormant）。

## 记忆

- **内置**（FileMemoryProvider）：读 data/system/*.md → system prompt。memory 工具写 MEMORY.md / USER.md（type=user）+ 外部同步。
- **外部**（EmbeddingMemoryProvider）：fastembed + bge-small-zh-v1.5（512维），cosine 检索，prefetch 注入 api_messages。文件摄取支持 .txt .md .pdf .docx（CLI + 工具）。

## 多 Provider

4 个 Provider（deepseek/openai/anthropic/openrouter），2 种 Transport（AnthropicMessages / ChatCompletions），自动检测 api_mode。HTTP 层指数退避重试（429/5xx/连接错误）。

## 编码约定

- Python 类型标注
- 日志用 logging，颜色 formatter（绿=连接，青=消息/认证，黄=警告，红=错误）
- 自注册模式：工具/平台/skill import 即注册
- 线程安全：per-chat asyncio.Lock + _active_sessions 排队
