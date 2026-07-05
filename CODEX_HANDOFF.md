# Codex 交接记录

更新时间：2026-07-05 10:50 CST

## 当前状态

- 当前分支：`feature/backend-upgrade`
- 当前 HEAD：`48d4777 [codex] expose recent turn reports`
- 最近完成主线：
  - Execution / Boot diagnostics
  - Agent turn execution reports
  - Turn report runtime summaries
- 当前结论：
  - 配置整理阶段已经完成并稳定。
  - BootReport 与 AgentTurnReport 这条运行时可观测性链路已经阶段性完成。
  - 后续如果继续推进，优先级更适合放在 turn report v3、审计统一、或前端/后台消费，而不是继续补当前缺口。

## 最近完成内容

### BootReport v1 — 启动成功路径结构化

- 在 `src/personal_agent/runtime.py` 增加：
  - `BootStep`
  - `BootReport`
- `create_app_runtime()` 记录启动阶段：
  - `settings`
  - `data_dir`
  - `plugins.*`
  - `sandbox`
  - `audit`
  - `mcp`
  - `database`
  - `compression_chain`
  - `session_store`
  - `system_files`
  - `memory`
  - `memory_review`
  - `conversation`
  - `runtime`
- `AppRuntime.health_snapshot()` 暴露：
  - `boot`
  - `boot_ok`
  - `boot_failed_step`
- doctor / serve dry-run 文本和 JSON 接入 boot 摘要。
- commit：`f496791 [codex] add runtime boot report`

### BootReport v2 — 启动失败路径可诊断

- `BootReport` 增加预置阶段列表和 `not_run` 状态。
- 启动失败时将 `BootReport` 附着到异常对象，不改变原异常类型。
- doctor / serve dry-run 失败路径读取异常上的 boot report。
- settings 初始化失败也返回完整 boot 阶段列表。
- `doctor --section runtime` 可显示失败阶段和未执行阶段。
- commit：`f34bc25 [codex] report runtime boot failures`

### AgentTurnReport v1 — 每轮执行结构化报告

- 新增 `src/personal_agent/agent/report.py`：
  - `AgentTurnReport`
  - `TurnLlmReport`
  - `TurnToolReport`
  - `TurnRetryReport`
  - `TurnReportRecorder`
- `run_conversation()` 内部统一生成 `turn_report`：
  - 汇总 llm calls / tokens
  - 汇总 tool decision + tool result
  - 汇总 retry
  - 记录 final status / duration / error
- 所有返回路径统一附加：
  - `result["turn_report"]`
- `ConversationTurnResult` 增加：
  - `turn_report`
- commit：`c70ee9b [codex] add agent turn reports`

### AgentTurnReport v2 — 运行时消费与诊断摘要

- `ConversationService` 增加内存 ring buffer：
  - `turn_reports`
  - `record_turn_report()`
  - `recent_turn_reports()`
  - `turn_report_summary()`
- 默认保留最近 `50` 条 turn report envelope。
- envelope 字段：
  - `session_key`
  - `source`
  - `created_at`
  - `status`
  - `report`
- `AppRuntime.health_snapshot()` 暴露：
  - `turns`
- doctor 完整报告 Runtime 区增加 Turns 摘要。
- `doctor --section runtime` 增加 Turns 明细小节。
- JSON doctor / serve dry-run 会自然带出 `runtime.turns`。
- commit：`48d4777 [codex] expose recent turn reports`

## 当前运行时可观测性形态

### 启动链路

- `src/personal_agent/runtime.py`
  - `BootReport.bootstrap()`
  - `boot_report_from_exception()`
  - `AppRuntime.health_snapshot()`
- 覆盖启动成功与失败两条路径。

### 对话执行链路

- `src/personal_agent/agent/report.py`
  - 负责聚合单轮 agent 执行报告
- `src/personal_agent/agent/loop.py`
  - 每轮 `run_conversation()` 都会返回 `turn_report`
- `src/personal_agent/conversation/service.py`
  - 持有最近 turn report 的内存历史
  - 可输出运行时摘要

### 诊断展示

- `src/personal_agent/cli.py`
  - 完整 doctor：Runtime 区显示 Boot + Turns 摘要
  - `doctor --section runtime`：显示 Boot steps + Turns 明细
  - `doctor --json`：可读到 `runtime.boot` 与 `runtime.turns`
  - `serve --dry-run --json`：可读到 `runtime.boot`

## 关键测试

- `tests/test_runtime.py`
  - boot success / boot failure / mcp cleanup / runtime health turns
- `tests/test_agent_loop.py`
  - turn report success / retry / denied tool / failed llm / streaming deltas
- `tests/test_conversation_service.py`
  - turn report 透传
  - turn report ring buffer
  - failed / stopped / context_overflow report recording
  - recent limit summary
- `tests/test_cli.py`
  - doctor runtime Boot / Turns 文本格式
- `tests/test_cli_entrypoints.py`
  - doctor JSON 包含 runtime boot / turns

## 已验证

最近一次全量验证：

```bash
python -m compileall -q src/personal_agent
uv run pytest -q
```

结果：

```text
598 passed
```

## 注意事项

- 测试会修改 `src/personal_agent/skills/builtin/.usage.json`，提交前必须恢复。
- turn report v2 目前只做内存态 ring buffer，不写数据库、不写 JSONL。
- `AppRuntime.health_snapshot()` 只暴露 turn report 摘要，不暴露完整 recent list。
- doctor 现在已经同时承担：
  - boot 诊断
  - turn 运行摘要
  后续如果继续扩张，要留意输出噪音，不建议直接把完整 turn report 塞进完整 doctor。

## 后续可选方向

当前这部分可以先收住。以后如需继续，建议顺序：

1. AgentTurnReport v3：
   - 前端/后台模式消费 recent turn reports
   - 或增加单独 runtime/turns API，而不是继续膨胀 doctor
2. 审计统一：
   - turn report / tool decision / tool result / audit log 做统一关联
3. 持久化 turn report：
   - JSONL 或 DB
   - 这一步要先定义脱敏、保留周期、schema 稳定性
4. 更新 `FRONTEND_ROADMAP.md`，把 Boot/Turn diagnostics 作为现成后端能力标进去

## 最近提交

```text
48d4777 [codex] expose recent turn reports
c70ee9b [codex] add agent turn reports
f34bc25 [codex] report runtime boot failures
f496791 [codex] add runtime boot report
9fb5e59 [codex] update config handoff notes
e104240 [codex] organize config field declarations
```
