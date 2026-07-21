"""Built-in tool plugin entrypoint."""

import importlib


_TOOL_MODULES: list[tuple[str, tuple[str, ...]]] = [
    ("luna_agent.plugins.builtin.tools.builtin.artifact_from_file", ("artifact_from_file",)),
    ("luna_agent.plugins.builtin.tools.builtin.bash", ("bash",)),
    ("luna_agent.plugins.builtin.tools.builtin.calculator", ("calculator",)),
    ("luna_agent.plugins.builtin.tools.builtin.clarify", ("clarify",)),
    ("luna_agent.plugins.builtin.tools.builtin.confirm", ("confirm",)),
    ("luna_agent.plugins.builtin.tools.builtin.datetime_tool", ("datetime",)),
    ("luna_agent.plugins.builtin.tools.builtin.delegate", (
        "delegate_task",
        "run_research",
        "run_review",
        "run_workflow",
        "sub_agent",
        "sub_parallel",
        "sub_pipeline",
    )),
    ("luna_agent.plugins.builtin.tools.builtin.execute_code", ("execute_code",)),
    ("luna_agent.plugins.builtin.tools.builtin.file_edit", ("edit",)),
    ("luna_agent.plugins.builtin.tools.builtin.file_navigation", ("list_directory", "file_info")),
    ("luna_agent.plugins.builtin.tools.builtin.file_read", ("read",)),
    ("luna_agent.plugins.builtin.tools.builtin.file_write", ("write",)),
    ("luna_agent.plugins.builtin.tools.builtin.glob_tool", ("glob",)),
    ("luna_agent.plugins.builtin.tools.builtin.grep_tool", ("grep",)),
    ("luna_agent.plugins.builtin.tools.builtin.json_tool", ("json",)),
    ("luna_agent.plugins.builtin.tools.builtin.plugin_tools", (
        "plugin_inspect", "plugin_build", "plugin_manage",
    )),
    ("luna_agent.plugins.builtin.tools.builtin.observability_tools", (
        "runtime_inspect", "conversation_inspect", "platform_inspect", "config_inspect",
        "memory_inspect", "audit_inspect", "logs_query",
    )),
    ("luna_agent.plugins.builtin.tools.builtin.process_tool", (
        "process_start",
        "process_list",
        "process_read",
        "process_clear",
        "process_kill",
        "process_wait",
    )),
    ("luna_agent.plugins.builtin.tools.builtin.random_tool", ("random",)),
    ("luna_agent.plugins.builtin.tools.builtin.response_attach", ("response_attach",)),
    ("luna_agent.plugins.builtin.tools.builtin.skill_tools", ("skill_search", "skill_load")),
    ("luna_agent.plugins.builtin.tools.builtin.task", ("task",)),
    ("luna_agent.plugins.builtin.tools.builtin.timer", ("timer",)),
    ("luna_agent.plugins.builtin.tools.builtin.todo", ("todo",)),
    ("luna_agent.plugins.builtin.tools.builtin.weather", ("weather",)),
    ("luna_agent.plugins.builtin.tools.builtin.web_search", ("web_search",)),
    ("luna_agent.plugins.builtin.tools.builtin.workflow_tool", ("workflow_run", "workflow_list")),
    ("luna_agent.plugins.builtin.tools.builtin.worktree_tool", (
        "worktree_create",
        "worktree_merge",
        "worktree_cleanup",
        "worktree_list",
    )),
]


def _load(module_name: str):
    return importlib.import_module(module_name)


def _ensure_module_entries(module_name: str, names: tuple[str, ...]) -> None:
    from luna_agent.tools.registry import tool_registry

    missing = [name for name in names if tool_registry.get(name) is None]
    foreign = [
        name
        for name in names
        if tool_registry.get(name) is not None
        and getattr(tool_registry.get(name), "_plugin_key", "") != "builtin/tools"
    ]
    if not missing and not foreign:
        return
    module = importlib.import_module(module_name)
    importlib.reload(module)


def _configure(settings=None, **kwargs) -> None:
    if settings is None:
        return

    from luna_agent.plugins.builtin.tools.builtin.bash import (
        set_allow_network,
        set_process_backend,
        set_restrict_paths,
        set_work_dir,
    )
    from luna_agent.plugins.builtin.tools.builtin.file_write import set_max_write_bytes
    from luna_agent.plugins.builtin.tools.builtin.file_edit import (
        set_max_write_bytes as set_max_edit_bytes,
    )

    set_allow_network(bool(settings.bash_allow_network))
    set_process_backend(settings.process_sandbox_backend)
    set_restrict_paths(settings.bash_restrict_paths)
    set_work_dir(settings.bash_work_dir)
    set_max_write_bytes(settings.file_max_write_bytes)
    set_max_edit_bytes(settings.file_max_write_bytes)


def _setup_delegate(call_fn=None, tools=None, max_tokens: int | None = None, **kwargs) -> None:
    if call_fn is None or tools is None:
        return

    from luna_agent.plugins.builtin.tools.builtin.delegate import setup_delegate

    settings = kwargs.get("settings")
    run_store_path = None
    runtime_max_tokens = max_tokens or 4096
    max_concurrent_runs = 4
    max_tool_calls = 10
    history_limit = 100
    if settings is not None:
        run_store_path = settings.agent_data_dir / "agent_runs.jsonl"
        runtime_max_tokens = getattr(settings, "agent_runtime_max_tokens", runtime_max_tokens)
        max_concurrent_runs = getattr(settings, "agent_runtime_max_concurrent_runs", max_concurrent_runs)
        max_tool_calls = getattr(settings, "agent_runtime_max_tool_calls", max_tool_calls)
        history_limit = getattr(settings, "agent_runtime_history_limit", history_limit)

    setup_delegate(
        call_fn=call_fn,
        tools=tools,
        max_tokens=runtime_max_tokens,
        run_store_path=run_store_path,
        max_concurrent_runs=max_concurrent_runs,
        max_tool_calls=max_tool_calls,
        history_limit=history_limit,
    )


def register(ctx) -> None:
    from luna_agent.tools.registry import tool_registry

    for module_name, names in _TOOL_MODULES:
        module = _load(module_name)
        explicit_register = getattr(module, "register", None)
        if explicit_register is not None:
            explicit_register(ctx)
            continue

        _ensure_module_entries(module_name, names)
        for name in names:
            entry = tool_registry.get(name)
            if entry is None:
                raise RuntimeError(f"Built-in tool did not register: {name}")
            ctx.register.tool(entry)

    ctx.register.hook("configure", _configure, priority=20)
    ctx.register.hook("on_agent_created", _setup_delegate, priority=20)
