# Personal Agent

多平台 AI Agent 系统，参考 Hermes 架构从零构建。支持飞书、Telegram，多 LLM Provider，语义记忆，工具沙箱。

## 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 配置
cp .env.example .env
# 编辑 .env，填入 LLM API Key 和平台凭据

# 3. CLI 调试
uv run python -m personal_agent --cli "1+1等于几？"

# 4. 记忆摄取
uv run python -m personal_agent --ingest document.pdf

# 5. 启动服务
uv run python -m personal_agent
```

## 功能概览

### 核心引擎

- **Agent while 循环**：LLM 调用 → 解析 tool_calls → 执行工具 → 继续
- **3 层消息模型**：conversation_history（DB 只读）→ ctx.messages（可编辑）→ api_messages（injections，不持久化）
- **6 种重试**：empty_content、invalid_tool、invalid_json、post_tool_empty、scratchpad、thinking_prefill
- **中断式 LLM 调用**：`/stop` 命令 5s 内响应
- **Agent 缓存**：LRU 128，tool generation 自动失活
- **上下文压缩**：prune 旧 tool_result + LLM 摘要（迭代更新），压缩链跨会话

### 多 Provider

| Provider | API 模式 | 说明 |
|----------|---------|------|
| DeepSeek | anthropic_messages | 默认 |
| OpenAI | chat_completions | 需改 .env |
| Anthropic | anthropic_messages | 需改 .env |
| OpenRouter | chat_completions | 自动带 ranking headers |

自动检测 `api_mode`，切换 Provider 只改 `.env` 两个字段。HTTP 层带指数退避重试（429/5xx/连接错误）。

### 工具系统

**21 个内置工具，6 个分组：**

| 分组 | 工具 |
|------|------|
| 文件 | `read`, `write`, `edit`, `grep`, `glob` |
| 网络 | `web_search`, `web_fetch` |
| 终端 | `bash`（白名单，网络隔离） |
| 工具 | `calculator`, `datetime`, `random`, `timer`, `json` |
| 记忆 | `memory`, `memory_ingest`, `todo` |
| 技能 | `skill_search`, `skill_load` |

**执行管道 — 每个 tool_call 经过 5 段：**
```
scope gate（权限/配额）→ checkpoint（写前备份）→ pre-hook → dispatch → post-process（截断）
```

工具分 `is_parallel_safe` 并发执行。Destructive 工具需 `/allow write` 授权。

**安全：**

| 防护 | 说明 |
|------|------|
| bash 白名单 | 40+ 命令，未知命令拒绝，危险参数模式检测 |
| 网络隔离 | curl/pip 默认禁，需 config 开启 |
| 文件扩展名白名单 | 只允许 .txt/.md/.py 等，禁 .exe/.bat |
| 路径遍历防护 | resolve + startswith 检查 |
| 命令链接阻断 | `&&` `\|\|` `\|` `;` 全部拦截 |
| 审计日志 | file_read/write/edit/bash → `data/audit.log` |

### 记忆系统

```
data/system/           → 系统提示素材（手写，注入 system prompt）
├── SOUL.md            → 角色与人格
├── AGENT.md           → 行为规则
├── MEMORY.md          → 用户画像
└── USER.md            → 用户偏好

data/memory/           → 外部记忆（语义检索，注入 api_messages）
├── external_memories.json
└── external_embeddings.npy
```

- **内置记忆**：`memory` 工具同时写内部（MEMORY.md / USER.md）+ 外部（embedding），`§` 分隔条目
- **外部记忆**：`fastembed` + `bge-small-zh-v1.5`（512 维），cosine 检索，零向量数据库
- **prefetch**：每轮自动语义召回，注入 api_messages，不破坏 system prompt prefix cache
- **文件摄取**：`memory_ingest` 工具 + CLI `--ingest`，支持 .txt .md .pdf .docx

### Skills

- 注册式加载，3 个内置 skill：`python-expert`、`git-workflow`、`shell-guide`
- Tier 1 摘要注入 api_messages，`/skill-name` 命令加载完整内容
- `skill_search` / `skill_load` 工具让 LLM 自主发现

### 平台

| 平台 | 连接方式 | 特性 |
|------|---------|------|
| 飞书 | WebSocket v2 | 去重、防抖、@ 检测、健康检查自动重连 |
| Telegram | PTB polling | 线程桥接、Markdown 降级重试 |

认证：静态白名单 + 6 位配对挑战码，3 次机会，5 分钟过期。

### 配置

```yaml
# config.yaml（行为配置）
agent:
  max_iterations: 30
  max_tool_calls_per_turn: 20

toolsets:
  enabled: ["all"]          # ["web", "terminal", "file", "utility", "memory", "info"]

memory:
  provider: "file"
  external_provider: "embedding"    # embedding | none

security:
  bash_allow_network: false
  file_max_write_bytes: 100000
  audit_enabled: true

auth:
  enabled: true
```

```env
# .env（密钥，不入 git）
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.deepseek.com/anthropic
LLM_MODEL=deepseek-chat
LLM_PROVIDER=deepseek

FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
TELEGRAM_BOT_TOKEN=
```

## 测试

```bash
uv run pytest tests/ -v       # 95 tests
uv run python -m personal_agent --cli "你好"
uv run python -m personal_agent --ingest document.pdf
```

## 技术栈

Python 3.12+ / uv / asyncio / httpx / aiosqlite / lark-oapi / python-telegram-bot / fastembed / pymupdf / pydantic-settings
