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
        "process_list",
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

    set_allow_network(settings.bash_allow_network)
    set_restrict_paths(settings.bash_restrict_paths)
    set_work_dir(settings.bash_work_dir)
    set_max_write_bytes(settings.file_max_write_bytes)


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
