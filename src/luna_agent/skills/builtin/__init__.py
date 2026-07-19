"""Built-in skills — import triggers registration into SkillRegistry."""

from luna_agent.skills.entry import SkillEntry
from luna_agent.skills.registry import skill_registry

skill_registry.register(SkillEntry(
    name="python-expert",
    description="Python coding best practices: type hints, async/await, dataclasses, error handling, common patterns",
    path="python_expert.md",
    triggers=["/python", "/py"],
))

skill_registry.register(SkillEntry(
    name="git-workflow",
    description="Git commands and workflows: branching, conventional commits, undoing, stash, cherry-pick",
    path="git_workflow.md",
    triggers=["/git"],
))

skill_registry.register(SkillEntry(
    name="shell-guide",
    description="Shell commands: file ops, process management, text processing, networking, Windows equivalents",
    path="shell_guide.md",
    triggers=["/shell", "/bash"],
))
