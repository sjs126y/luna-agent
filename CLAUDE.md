# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 概述

插件化的多平台 AI Agent 运行时。CLI 多轮对话 / Telegram / 飞书 / 微信 都走同一条链路：
消息 → ConversationService.run_turn → Agent while 循环调 LLM → 执行工具 → 返回。
参考 Hermes 架构，不依赖 LangChain / CrewAI 等重框架。

## 常用命令

```bash
uv sync                                    # 装依赖

uv run personal-agent init --profile local --copy-env --fix-dirs
uv run personal-agent doctor               # 校验配置/环境，--json 出机器可读
uv run personal-agent chat                 # 本地多轮对话（主要开发入口）
uv run personal-agent chat --once "..."    # 单轮
uv run personal-agent serve                # 启动平台 Gateway

uv run pytest -q                           # 全量测试（466 个）
uv run pytest tests/test_cli_shell.py -q   # 单文件
uv run pytest tests/test_tool_pipeline.py -q -k parallel   # 单测试
python -m compileall -q src/personal_agent # 语法快速检查
```

诊断类子命令（排错优先用）：`plugins list --load`、`plugins doctor <key>`、`memory doctor`、
`agents list`、`tokens session`。

**提交前必须**跑 `python -m compileall -q src/personal_agent` + `uv run pytest -q`。
部分测试会写 `src/personal_agent/skills/builtin/.usage.json`，非故意改动请在提交前还原它。

## 启动与装配（读这几个文件就懂大局）

- `runtime.py::create_app_runtime` — 唯一的启动装配点。顺序：
  `PluginManager.discover()/load_enabled()` → `invoke_hook("configure")` → `init_sandbox` →
  MCP → DB(state.db) → CompressionChain → SessionStore → MemoryManager → ConversationService。
  所有子系统都挂在 `AppRuntime` 上，`close()` 负责反向拆解。
- `conversation/service.py::ConversationService` — CLI 和 Gateway 共用的「跑一轮」逻辑。
  负责 session 解析、history 加载、`get_or_create_agent`（带 LRU + tool_registry.generation 失效）、
  调 `run_conversation`、按 status 决定落盘（正常 save_transcript / 压缩则 create_compressed_session）。
- `agent/loop.py::run_conversation` — 核心 while 循环：build api_messages → hook `on_before_llm_call`
  → 可中断 LLM 调用（每 5s 轮询 `_interrupt_requested`）→ 解析 tool_calls → 执行 → 继续。
  含 6 种重试策略（见文件头 docstring）。
- `agent/factory.py::create_agent_runtime` — 解析 provider/transport/compressor，装配 Agent，
  接线 plugin hooks，`setup_engine` 初始化 workflow 引擎。

三层消息：持久化 `history` → 本轮 `ctx.messages` → 发给 API 的 `api_messages`（含记忆/skill 注入，不落盘）。

## 插件系统（一切能力的来源）

`plugins/core/manager.py::PluginManager` 是注册中枢。内置插件在 `plugins/builtin/`
（tools / skills / workflows / platforms / llm / memory），用户插件放根目录 `plugins/` 或 `data/plugins/`。

- 每个插件靠 `plugin.yaml` manifest 声明，`register(ctx)` 时向各 registry 自注册
  （tool_registry / skill_registry / workflow_registry / platform_registry）。
- **deferred 插件**（platform / mcp）发现时不 import，`serve` 或 MCP 启动时才加载 —— 改平台/MCP 插件时注意这点。
- Hook 挂载点：`configure`、`on_agent_created`、`on_session_selected`、`on_message_received`、
  `on_before_send`、`on_before_llm_call` / `on_after_llm_call`、`on_before_tool_exec` / `on_after_tool_exec`、
  `create_builtin_memory_provider` / `create_external_memory_provider`、`wechat_qr_login`。
- 核心 slash 命令（`stop`/`allow`/`new`/`session`/`usage`）保留，插件命令不能覆盖。

## 工具系统

工具定义在 `plugins/builtin/tools/builtin/`，通过 `tools/toolsets.py` 分组，config.yaml `toolsets.enabled` 控制启用。

- 分组：`web` `terminal` `file` `utility` `memory` `info` `code` `interact` `mcp`，`["all"]` = 全部。
- `_CORE_TOOLS`（toolsets.py）里的工具永远拿完整 schema；非核心工具可经 bridge 工具延迟暴露
  （当前无 deferrable 工具，bridge 处于 dormant）。
- 执行管道（`tools/executor.py`）：scope gate（权限/配额）→ checkpoint（写前备份到 data/checkpoints）
  → pre-hook → dispatch → post-process（截断 8000）。
- 并发：`is_parallel_safe` 的工具 `asyncio.to_thread` 并发，其余串行 await。
- 安全（`tools/sandbox.py` + config.yaml `sandbox`）：roots 白名单 + blocked glob（.env/.git/config.yaml 等）
  + bash 命令白名单 + 网络隔离（`bash_allow_network`）+ 文件大小/扩展名限制 + 审计日志。
  destructive 工具需运行时 `/allow write` 授权（存于 `agent._destructive_allowed`）。
- 子 Agent 委派：`delegate_task` / `sub_agent` / `sub_parallel` / `sub_pipeline`，受 config.yaml `agents.*` 限额。

## 记忆

`memory/manager.py::MemoryManager` = builtin + external 两路，都由插件 hook 创建。

- **内置**（file provider）：读 `data/system/*.md`（SOUL/AGENT/USER/MEMORY）注入 system prompt；
  `memory` 工具写 MEMORY.md/USER.md 并同步外部。`runtime.ensure_system_files` 保证这些文件存在。
- **外部**（embedding provider）：fastembed + bge-small-zh-v1.5（512 维）cosine 检索，prefetch 注入 api_messages；
  支持 .txt/.md/.pdf/.docx 摄取。启用与否看 config.yaml `memory.external_provider`。
- 每 N 轮（`memory.review_interval`）触发 MemoryReviewService 自动复盘。

## 多 Provider / Transport

`llm/` 下有 5 个 provider（deepseek/openai/anthropic/openrouter/xai）和 Anthropic Messages、
Chat Completions、Responses、Codex Responses transport；`create_agent_runtime` 按 `detect_api_mode`
自动选择。HTTP 层对 429/5xx/连接错误使用指数退避重试。压缩引擎见 `compression/`（config.yaml
`compression`）；架构边界见 `docs/architecture.md`。

## 配置与数据

- `.env` — secret 和 provider/platform env（`LLM_API_KEY`、`TELEGRAM_BOT_TOKEN`、`FEISHU_APP_ID/SECRET`…）。被 sandbox blocked。
- `config.yaml` — 行为配置（agent/agents/storage/compression/toolsets/memory/sandbox/auth/mcp…）。被 sandbox blocked。
- `data/` — 运行数据：`state.db`（会话）、`todos.db`、`system/`（提示素材）、`memory/`（embedding）、
  `checkpoints/`、`audit.log`、`plugins/state.json`（插件启停状态）、`cron/`。

## 工作流约定

- **改代码前先开分支**：`git checkout -b feature/<描述>`，不在 main 上直接改。
- **小步 commit**：改动验证 OK 后就 `git commit`，别攒一堆；提交信息使用简短祈使句。
- 改工具/安全代码时保留 audit / sandbox / 路径遍历检查。
- 自注册模式：工具/平台/skill/workflow import 即注册，别手动维护列表。
- 线程安全：per-chat asyncio.Lock + _active_sessions 排队。
- 类型标注 + snake_case/PascalCase/UPPER_SNAKE_CASE，注释只写非显然逻辑，优先复用本地模式而非引入新抽象。
