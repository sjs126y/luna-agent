# Codex 交接记录

更新时间：2026-07-13 01:55 CST

## 2026-07-13 收工状态

- 当前分支：`main`，本轮 Agent 工具循环与记忆恢复修复已通过 `daaadf3` 合并。
- 最近一次完整验证：`python -m compileall -q src/personal_agent`、`git diff --check`、`uv run pytest -q`；结果为 `864 passed`。
- 记忆重构和恢复链路已进入真实 Gateway 联调：Lumora embedding/Qdrant 查询成功，scope provider 保持 `lumora`，fallback observation 由后台 review worker 渐进迁移，不占用 Agent 主循环。
- 工具循环已修复消息顺序、调用上限终止和重复调用问题；filesystem/fetch MCP 已产生真实成功审计记录。
- GitHub 官方远程 MCP 使用当前 PAT 可成功连接并发现 44 个工具，但正常 Gateway 启动仍无法从 Settings 向 `headers_env` 注入该 secret。
- 下一次后端工作的第一优先级：保持统一 `ConfigLoader -> Settings -> runtime -> MCP connection` 配置边界，修复 GitHub MCP 动态 header 注入；不要让 MCP connection 主动读取 `.env`。
- 临时联调启动方式：`uv run --env-file .env personal-agent serve`。修复完成后应恢复普通 `uv run personal-agent serve` 即可工作。
- 工作区存在用户本机 `config.yaml` 改动，必须保留；`.env` 是忽略的本机 secret 文件。

## 当前状态

- 当前工作分支：`feature/legacy-cleanup`
- 后端分支 `feature/backend-provider-cache` 已合入主分支。
- 前端分支 `feature/frontend-tui-polish` 已合入主分支。
- 历史清理分支已完成入口收敛：inline TUI 是唯一正式交互终端 UI，classic/simple 已移除。
- 最近主分支合并提交：
  - `e6ccb98 Merge branch 'feature/backend-provider-cache'`
  - `73d0f24 [codex] merge frontend tui polish`
- 合并后验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest -q
```

结果：前后端合并后 `746 passed`；历史清理后 `708 passed`。

## 文档入口

- `BACKEND_INTERFACE.md`：前端消费后端事件、slash commands、tool metadata、tool runs、activity、usage/context 等接口的权威文档。
- `BACKEND_PROGRESS.md`：后端线进度，记录 agent runtime、provider/transport、tool pipeline、activity、turn report 等工作。
- `FRONTEND_PROGRESS.md`：前端线进度，记录 inline TUI、slash menu、confirm UI、activity UI、context meter 等工作。
- `FRONTEND_INTERFACE_REQUIREMENTS.md`：前端向后端提出的小接口/字段需求入口。
- `docs/frontend_decisions.md`：前端视觉和交互决策记录。
- 历史计划/需求文档已归档到 `docs/archive/`。

## 当前项目总进度

项目已经从“CLI agent 原型”推进到一个可用的个人 Agent runtime：

- 后端 runtime、工具执行、安全门控、插件加载、LLM transport、memory、MCP、workflow、Gateway 和会话存储已经形成稳定主链路。
- inline TUI 已经能消费后端结构化事件和 slash command metadata，不再只是普通 CLI 输出。
- 前后端通过 `ConversationEvent`、`CommandResult.kind/payload`、slash metadata、activity payload、tool runs、turn reports 和 context budget 建立了较清晰的接口边界。
- Provider/transport 已具备 provider-aware cache capability 和 request diagnostics，可继续围绕真实 provider cache 命中率做验证。
- 工具调用、权限确认、tool truth、tool runs 和 turn report 已经可观测、可持久化、可排查。

## 后端完成项

- **Execution Mode v3**：四档模式已稳定，对应权限、沙箱、工具类别和确认行为。
- **Tool execution / permission pipeline**：工具执行统一经过 executor、execution guard、precheck、permission、sandbox、audit。
- **Tool decision metadata**：`tool_decision` / `tool_end` 提供前端确认 UI 所需字段，包括风险、默认动作、可选动作、路径/命令/URL 预览等。
- **Event protocol**：事件带 `protocol_version`，`retry` / `error` / `stop` / `tool_decision` / `tool_end` / `llm_end` 等字段结构化。
- **Provider / transport cache**：`ProviderProfile`、`BaseTransport`、Anthropic、OpenAI-compatible/DeepSeek 已支持 cache capability、usage normalization、request hash diagnostics 和 `LLMRequestPlan`。
- **Turn reports**：每轮 `AgentTurnReport` 已持久化到 SQLite，可和 `tool_runs` 通过 `turn_id/session_key` 关联。
- **Tool truth**：能记录真实工具调用、模型请求工具调用、工具结果、模型声称调用但实际没有 tool call 等诊断。
- **Activity runtime**：统一暴露子 agent、后台进程、gateway agent 的 summary/list/detail，并接入 `/activity`、slash metadata 和动态候选。
- **Usage / context**：`llm_start` / `llm_end` 区分最近一次 API token 消耗与当前上下文占用估算；`/usage` 已修正工具计数语义。
- **Tool protocol prompt**：系统提示已加入稳定工具调用规则，降低模型只用文字声称调用工具的概率。
- **Doctor/runtime diagnostics**：runtime health 已能展示 commands、query、execution、activity、provider cache、turn reports、tool truth 等摘要。
- **历史入口清理**：`python -m personal_agent` 统一转发 Typer CLI；`wechat-login` 和 `memory ingest` 已迁到正式命令；skill usage 状态写入 `data/skills/usage.json`。

## 前端完成项

- **Inline TUI 主体**：输入区、状态行、上下文 meter、流式回复预览、工具活跃区、用户消息强调和多行输入已成型。
- **Context meter 语义修正**：顶部 meter 使用 `context_used_tokens/context_window/context_percent`；最近一轮模型消耗独立显示为 `↓input | ↑output`。
- **Slash command UI**：消费后端 slash command registry，支持一级命令、children、静态候选、动态候选、键盘选择和层级进入。
- **Confirm UI**：工具确认面板支持可选 action row、默认项、风险摘要、关键输入/路径/命令/URL 预览。
- **Tool trace / tool runs**：实时 `tool_start/tool_decision/tool_end` 和历史 `/tool-runs` 已消费结构化字段，并支持 `Ctrl+O` 展开完整输出。
- **Activity UI**：`/activity` 消费 `kind="activity"` payload，展示 gateway、sub agents、background processes 的总览、列表和详情。
- **前端状态消费**：`UIState` 保留 context budget、cache usage、activity summary 等结构化状态，便于后续做面板或详情页。
- **入口收敛**：`personal-agent chat` 默认启动 inline TUI；classic `TerminalRenderer` 和 `--simple` 旧 REPL 已移除。

## 当前约定

- 后端接口变更必须同步 `BACKEND_INTERFACE.md`。
- 前端提出后端需求时，应写入 `FRONTEND_INTERFACE_REQUIREMENTS.md`，优先是字段/小接口级别。
- inline TUI 是正式交互终端 UI；`--once` 保留为脚本/单轮入口。
- 每次工作结束要更新自己负责的进度文件。
- 提交信息继续使用 `[codex]` 前缀。
- 运行数据写入 `data/`；源码目录不再跟踪 skill usage 状态。

## 后续建议

- 真实 provider cache API 验证：用实际 provider 响应确认 cache usage 字段和命中率。
- 长对话压缩质量：优化压缩后的任务状态、路径、工具结果保留。
- 工具失败恢复：继续改进工具错误、权限拒绝、格式错误后的恢复提示；暂不做正则触发 retry。
- Activity 历史：如果前端需要历史分页或 gateway completed records，再扩展持久化层。
- TUI 体验手测：重点验证 slash menu、confirm、context meter、activity、工具结果展开在真实终端中的行为。
