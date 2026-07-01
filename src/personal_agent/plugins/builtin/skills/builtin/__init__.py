"""Built-in skill plugin entrypoint."""


def register(ctx) -> None:
    from personal_agent.skills.entry import SkillEntry
    from personal_agent.skills.registry import discover_skills

    ctx.register_skill(SkillEntry(
        name="python-expert",
        description="Python coding best practices: type hints, async/await, dataclasses, error handling, common patterns",
        path="python_expert.md",
        triggers=["/python", "/py"],
    ))
    ctx.register_skill(SkillEntry(
        name="git-workflow",
        description="Git commands and workflows: branching, conventional commits, undoing, stash, cherry-pick",
        path="git_workflow.md",
        triggers=["/git"],
    ))
    ctx.register_skill(SkillEntry(
        name="shell-guide",
        description="Shell commands: file ops, process management, text processing, networking, Windows equivalents",
        path="shell_guide.md",
        triggers=["/shell", "/bash"],
    ))

    data_dir = getattr(ctx.settings, "agent_data_dir", None)
    if data_dir is not None:
        discover_skills(data_dir / "skills", registrar=ctx)
