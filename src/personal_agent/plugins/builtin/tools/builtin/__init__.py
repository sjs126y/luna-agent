"""Built-in tool plugin entrypoint."""

import importlib


_TOOL_MODULES: list[tuple[str, tuple[str, ...]]] = [
    ("personal_agent.plugins.builtin.tools.builtin.bash", ("bash",)),
    ("personal_agent.plugins.builtin.tools.builtin.calculator", ("calculator",)),
    ("personal_agent.plugins.builtin.tools.builtin.clarify", ("clarify",)),
    ("personal_agent.plugins.builtin.tools.builtin.confirm", ("confirm",)),
    ("personal_agent.plugins.builtin.tools.builtin.datetime_tool", ("datetime",)),
    ("personal_agent.plugins.builtin.tools.builtin.delegate", (
        "delegate_task",
        "run_research",
        "run_review",
        "run_workflow",
        "sub_agent",
        "sub_parallel",
        "sub_pipeline",
    )),
    ("personal_agent.plugins.builtin.tools.builtin.execute_code", ("execute_code",)),
    ("personal_agent.plugins.builtin.tools.builtin.file_edit", ("edit",)),
    ("personal_agent.plugins.builtin.tools.builtin.file_read", ("read",)),
    ("personal_agent.plugins.builtin.tools.builtin.file_write", ("write",)),
    ("personal_agent.plugins.builtin.tools.builtin.glob_tool", ("glob",)),
    ("personal_agent.plugins.builtin.tools.builtin.grep_tool", ("grep",)),
    ("personal_agent.plugins.builtin.tools.builtin.json_tool", ("json",)),
    ("personal_agent.plugins.builtin.tools.builtin.process_tool", (
        "process_start",
        "process_list",
        "process_read",
        "process_kill",
        "process_wait",
    )),
    ("personal_agent.plugins.builtin.tools.builtin.random_tool", ("random",)),
    ("personal_agent.plugins.builtin.tools.builtin.skill_tools", ("skill_search", "skill_load")),
    ("personal_agent.plugins.builtin.tools.builtin.task", ("task",)),
    ("personal_agent.plugins.builtin.tools.builtin.timer", ("timer",)),
    ("personal_agent.plugins.builtin.tools.builtin.todo", ("todo",)),
    ("personal_agent.plugins.builtin.tools.builtin.weather", ("weather",)),
    ("personal_agent.plugins.builtin.tools.builtin.web_fetch", ("web_fetch",)),
    ("personal_agent.plugins.builtin.tools.builtin.web_search", ("web_search",)),
    ("personal_agent.plugins.builtin.tools.builtin.workflow_tool", ("workflow_run", "workflow_list")),
    ("personal_agent.plugins.builtin.tools.builtin.worktree_tool", (
        "worktree_create",
        "worktree_merge",
        "worktree_cleanup",
        "worktree_list",
    )),
]


def _load(module_name: str):
    return importlib.import_module(module_name)


def _ensure_module_entries(module_name: str, names: tuple[str, ...]) -> None:
    from personal_agent.tools.registry import tool_registry

    missing = [name for name in names if tool_registry.get(name) is None]
    if not missing:
        return
    module = importlib.import_module(module_name)
    importlib.reload(module)


def _configure(settings=None, **kwargs) -> None:
    if settings is None:
        return

    from personal_agent.plugins.builtin.tools.builtin.bash import (
        set_allow_network,
        set_restrict_paths,
        set_work_dir,
    )
    from personal_agent.plugins.builtin.tools.builtin.file_write import set_max_write_bytes

    policy = getattr(settings, "execution_policy", None)
    set_allow_network(getattr(policy, "network", None) == "allow")
    set_restrict_paths(settings.bash_restrict_paths)
    set_work_dir(settings.bash_work_dir)
    set_max_write_bytes(settings.file_max_write_bytes)


def _setup_delegate(call_fn=None, tools=None, max_tokens: int | None = None, **kwargs) -> None:
    if call_fn is None or tools is None:
        return

    from personal_agent.plugins.builtin.tools.builtin.delegate import setup_delegate

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
    from personal_agent.tools.registry import tool_registry

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
            ctx.register_tool(entry)

    ctx.register_hook("configure", _configure, priority=20)
    ctx.register_hook("on_agent_created", _setup_delegate, priority=20)
