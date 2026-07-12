# 插件系统

Lumora 的插件系统是一个“装配层”：负责发现插件、加载入口、注册 hook/command，并把工具、平台、MCP、技能、workflow 等能力转发给已有的运行时 registry 或 manager。插件系统不接管这些子系统自己的生命周期。

插件注册的 MCP server 配置会在应用启动时统一交给 `MCPManager`。连接、重连、动态工具快照和关闭都由 MCP runtime 管理；插件不应自行启动 MCP 子进程或网络 session。

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

仓库里也保留了一个最小示例插件：

```text
examples/plugins/hello/
  __init__.py
  plugin.yaml
```

它演示了用户插件最常见的包式结构：插件目录本身是 Python package，`plugin.yaml` 的 `entrypoint` 写成 `hello:register`。

## plugin.yaml

每个插件包必须有 `plugin.yaml`、`plugin.yml` 或 `plugin.json`。内置插件由 `PluginManager` 递归扫描 `src/personal_agent/plugins/builtin/**/plugin.yaml` 发现；用户插件从配置里的插件目录发现。

必填字段：

```yaml
key: memory/lumora
name: Lumora Memory Provider
version: "1.0.0"
entrypoint: personal_agent.plugins.builtin.memory.lumora:register
```

常用可选字段：

```yaml
description: Hybrid semantic and BM25 external memory provider.
kind: memory
provides: [memory_provider:lumora]
requires_env: []
enabled_by_default: true
source: builtin
deferred: false
record_import_delta: false
```

`key` 是启用/禁用插件时使用的稳定身份，不要用展示名代替。推荐类似 `platforms/telegram`、`memory/lumora`、`workflows/review` 这样的 key。

manifest 会做严格校验：

- `key` 必须是小写分段格式，例如 `builtin/tools`、`platforms/telegram`、`examples/hello`。
- `entrypoint` 必须是 `module` 或 `module:function`，模块名和函数名都要是合法 Python 标识符。
- `kind` 目前支持 `builtin`、`platform`、`tool`、`tools`、`skill`、`skills`、`workflow`、`memory`、`llm`、`mcp`、`user`。
- `source` 目前支持 `builtin`、`user`。
- `requires_env`、`provides` 必须是字符串或字符串列表。
- `enabled_by_default`、`deferred`、`record_import_delta` 必须是布尔值。

manifest 有错时插件不会消失，会以 `invalid/<目录名>` 留在插件列表里，方便 `plugins doctor` 或 `plugins validate` 给出具体错误。

## 入口函数

`entrypoint` 可以指向模块，也可以指向模块里的函数。常见写法：

```python
def register(ctx) -> None:
    ctx.register_hook("configure", configure, priority=10)
```

`register()` 必须是同步函数。会阻塞、会联网、会启动进程的事情不要放进 `register()`，应该放到 hook 里，或者交给对应子系统的 manager 处理。

用户插件可以用两种组织方式：

```text
plugins/demo/
  plugin.yaml              # entrypoint: demo_plugin:register
  demo_plugin.py
```

或：

```text
plugins/hello/
  plugin.yaml              # entrypoint: hello:register
  __init__.py
```

## PluginContext 能注册什么

每个插件加载时会拿到自己的 `PluginContext`。它只做注册转发和来源记录：

- `register_tool(ToolEntry)`
- `register_skill(SkillEntry)`
- `register_workflow(WorkflowDef)`
- `register_platform(PlatformEntry)`
- `register_mcp_server(MCPServerConfig | dict)`
- `register_memory_provider(name, factory, validator)`
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
- `wechat_qr_login`：触发微信扫码登录辅助流程。

Memory provider 使用专用 registry 注册，不通过通用 hook 创建，避免多个插件互相覆盖。

## Command 规则

插件 command 使用 `CommandEntry`，`scope` 支持 `slash`、`cli`、`both`。

命令路由规则：

- 用户输入 `/xxx` 时先匹配核心命令，再匹配插件命令。
- Gateway 只会执行 `scope="slash"` 或 `scope="both"` 的插件命令。
- CLI chat 会优先执行 `scope="cli"`，也兼容 `scope="slash"` 和 `scope="both"`，避免旧插件默认 scope 失效。
- `/help` 会展示当前入口可见的插件命令。

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

记忆领域与编排位于核心 `src/personal_agent/memory/`：internal Markdown、Agent revision snapshot、observation buffer、SQLite archive、异步 review worker、router 和 fallback。

可替换的外部提供器位于插件包：

- `memory/lumora`：百炼 embedding、Qdrant 语义检索、SQLite FTS5/BM25 和 RRF。
- `memory/mem0`：官方 `mem0ai` 依赖的薄适配层。
- `builtin/memory`：只注册 `memory` / `memory_buffer` 工具。

核心 fallback 不属于插件，主 provider 缺依赖、配置错误或运行失败时自动接管 SQLite + BM25 存储。

## 常用诊断命令

```bash
uv run python -m personal_agent plugins list --load
uv run python -m personal_agent plugins info memory/lumora --load
uv run python -m personal_agent plugins doctor memory/mem0 --json
uv run python -m personal_agent plugins validate examples/plugins/hello
uv run python -m personal_agent doctor --json
```

`plugins validate <path>` 可以直接校验一个插件目录或 manifest 文件，不要求先把插件目录写进 `config.yaml`：

```bash
uv run python -m personal_agent plugins validate examples/plugins/hello
uv run python -m personal_agent plugins validate examples/plugins/hello --json
uv run python -m personal_agent plugins validate examples/plugins/hello --no-load
```

默认会执行 `register()`，所以能发现入口导入失败、缺环境变量、注册时报错、command/hook 注册冲突等问题。`--no-load` 只检查 manifest、环境变量和入口导入，不执行注册函数。

示例插件端到端检查：

```bash
uv run python -m personal_agent plugins validate examples/plugins/hello
```

输出里应该能看到：

- `校验结果: 通过`
- `commands: hello`
- `hooks: example_hello:100`

插件相关改动合入前至少跑：

```bash
python -m compileall -q src/personal_agent
uv run pytest -q
```
