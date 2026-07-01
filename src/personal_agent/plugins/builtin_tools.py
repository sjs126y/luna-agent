"""Built-in tool plugin entrypoint."""

import importlib


def _load(module_name: str) -> None:
    module = importlib.import_module(module_name)
    importlib.reload(module)


def register(ctx) -> None:
    modules = [
        "personal_agent.memory.file_store",
        "personal_agent.tools.builtin.bash",
        "personal_agent.tools.builtin.calculator",
        "personal_agent.tools.builtin.clarify",
        "personal_agent.tools.builtin.confirm",
        "personal_agent.tools.builtin.datetime_tool",
        "personal_agent.tools.builtin.delegate",
        "personal_agent.tools.builtin.execute_code",
        "personal_agent.tools.builtin.file_edit",
        "personal_agent.tools.builtin.file_read",
        "personal_agent.tools.builtin.file_write",
        "personal_agent.tools.builtin.glob_tool",
        "personal_agent.tools.builtin.grep_tool",
        "personal_agent.tools.builtin.json_tool",
        "personal_agent.tools.builtin.process_tool",
        "personal_agent.tools.builtin.random_tool",
        "personal_agent.tools.builtin.skill_tools",
        "personal_agent.tools.builtin.task",
        "personal_agent.tools.builtin.timer",
        "personal_agent.tools.builtin.todo",
        "personal_agent.tools.builtin.weather",
        "personal_agent.tools.builtin.web_fetch",
        "personal_agent.tools.builtin.web_search",
        "personal_agent.tools.builtin.workflow_tool",
        "personal_agent.tools.builtin.worktree_tool",
    ]
    for module_name in modules:
        _load(module_name)
