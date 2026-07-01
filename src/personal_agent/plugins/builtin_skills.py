"""Built-in skill plugin entrypoint."""

import importlib


def register(ctx) -> None:
    module = importlib.import_module("personal_agent.skills.builtin")
    importlib.reload(module)

    from personal_agent.skills.registry import discover_skills

    data_dir = getattr(ctx.settings, "agent_data_dir", None)
    if data_dir is not None:
        discover_skills(data_dir / "skills")
