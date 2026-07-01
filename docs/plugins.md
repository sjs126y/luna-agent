# 插件系统

Personal Agent 的插件系统是一个“装配层”：负责发现插件、加载入口、注册 hook/command，并把工具、平台、MCP、技能、workflow 等能力转发给已有的运行时 registry 或 manager。插件系统不接管这些子系统自己的生命周期。

## 最终目录结构

插件引擎代码固定放在 `src/personal_agent/plugins/core/`：

```text
src/personal_agent/plugins/
  core/
    context.py
    manager.py
    models.py
  builtin/
    platforms/
      feishu/
        __init__.py
        adapter.py
        plugin.yaml
      telegram/
        __init__.py
        adapter.py
        plugin.yaml
      wechat/
        __init__.py
        adapter.py
        plugin.yaml
    memory/
      plugin.yaml              # 只注册 memory 工具
      __init__.py
      file/
        __init__.py
        provider.py
        plugin.yaml
      embedding/
        __init__.py
        provider.py
        plugin.yaml
    tools/
      builtin/
        __init__.py
        plugin.yaml
        *.py
      bridge/
        __init__.py
        bridge.py
        plugin.yaml
    skills/
      builtin/
        __init__.py
        plugin.yaml
    workflows/
      review/
        __init__.py
        workflow.py
        plugin.yaml
    llm/
      builtin/
        __init__.py
        plugin.yaml
        *.py
```

仓库根目录的 `plugins/` 只给用户插件或本地开发插件使用。内置插件不要放到根目录 `plugins/`。

## plugin.yaml

每个插件包必须有 `plugin.yaml`、`plugin.yml` 或 `plugin.json`。内置插件由 `PluginManager` 递归扫描 `src/personal_agent/plugins/builtin/**/plugin.yaml` 发现；用户插件从配置里的插件目录发现。

必填字段：

```yaml
key: memory/file
name: File Memory Provider
version: "0.1.0"
entrypoint: personal_agent.plugins.builtin.memory.file:register
```

常用可选字段：

```yaml
description: File-backed system prompt and profile memory provider package.
kind: builtin
provides: [memory]
requires_env: []
enabled_by_default: true
source: builtin
deferred: false
record_import_delta: false
```

`key` 是启用/禁用插件时使用的稳定身份，不要用展示名代替。推荐类似 `platforms/telegram`、`memory/file`、`workflows/review` 这样的 key。

## 入口函数

`entrypoint` 可以指向模块，也可以指向模块里的函数。常见写法：

```python
def register(ctx) -> None:
    ctx.register_hook("configure", configure, priority=10)
```

`register()` 必须是同步函数。会阻塞、会联网、会启动进程的事情不要放进 `register()`，应该放到 hook 里，或者交给对应子系统的 manager 处理。

## PluginContext 能注册什么

每个插件加载时会拿到自己的 `PluginContext`。它只做注册转发和来源记录：

- `register_tool(ToolEntry)`
- `register_skill(SkillEntry)`
- `register_workflow(WorkflowDef)`
- `register_platform(PlatformEntry)`
- `register_mcp_server(MCPServerConfig | dict)`
- `register_hook(name, callback, priority=100)`
- `register_command(CommandEntry)`

`middleware` 字段目前只是预留。普通插件也不要注册任意 agent role/team；多 Agent 仍然是 core runtime。

## Hook 规则

Hook 由 `PluginManager` 直接管理：

- `priority` 越小越先执行。
- hook 抛异常时 fail-open，只记录日志，不打断主流程。
- 多个 hook 返回非 `None` 时，最终返回最后一个非 `None` 的结果。
- 禁用插件时会移除该插件注册的 hook。

当前内置 hook 示例：

- `configure`：把配置应用到插件自己的模块状态。
- `on_session_selected`：更新当前会话对应的记忆 profile。
- `create_builtin_memory_provider`：创建 file memory provider。
- `create_external_memory_provider`：创建 embedding memory provider。
- `wechat_qr_login`：触发微信扫码登录辅助流程。

## Command 规则

插件 command 使用 `CommandEntry`，`scope` 支持 `slash`、`cli`、`both`。

插件不能覆盖核心 slash command：

- `/stop`
- `/allow`
- `/new`
- `/session`
- `/usage`

禁用插件时会移除它注册的 command。

## 加载策略

内置插件可以默认启用。用户插件原则上默认 opt-in。

`deferred: true` 的插件会被发现，但 `load_enabled()` 默认不会 import 它们。平台插件、MCP server 插件、重依赖 backend 适合 deferred。

缺少 `requires_env` 的插件会进入 `ERROR`，但 manifest、错误和诊断信息仍然保留。

## 记忆提供器

具体记忆提供器现在都在插件包里：

- `memory/file`：拥有 `FileMemoryProvider` 和 profile 相关 hook。
- `memory/embedding`：拥有 `EmbeddingMemoryProvider` 和 external memory hook。
- `builtin/memory`：只注册 `memory` / `memory_ingest` 工具，运行时使用 provider 插件。

核心 `src/personal_agent/memory/` 只保留共享接口和编排对象，例如 `MemoryProvider`、`MemoryManager`。

## 常用诊断命令

```bash
uv run python -m personal_agent plugins list --load
uv run python -m personal_agent plugins info memory/file --load
uv run python -m personal_agent plugins doctor memory/embedding --json
uv run python -m personal_agent doctor --json
```

插件相关改动合入前至少跑：

```bash
python -m compileall -q src/personal_agent
uv run pytest -q
```
