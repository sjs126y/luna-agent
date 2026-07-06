"""Git worktree isolation — safe parallel file modifications.

Each worktree = independent working directory + branch.
Sub-agents work in isolated worktrees, main agent merges when done.

Flow:
  main agent: worktree_create("fix-auth") → returns path
  main agent: sub_agent("fix auth.py", cwd=path, allowed_tools=["write","edit","bash"])
  sub-agent:  modifies auth.py in the worktree
  main agent: worktree_merge("fix-auth") → merges back
  main agent: worktree_cleanup("fix-auth") → removes worktree + branch
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry

logger = logging.getLogger(__name__)

_WORKTREE_DIR: Path = Path("./.worktrees")


def set_worktree_dir(path: Path) -> None:
    global _WORKTREE_DIR
    _WORKTREE_DIR = path


async def _git(*args: str, cwd: str | None = None) -> tuple[int, str, str]:
    """Run a git command. Returns (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or str(Path.cwd()),
    )
    stdout, stderr = await proc.communicate()
    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    return proc.returncode or 0, out, err


async def _worktree_create(name: str, base_branch: str = "") -> str:
    """Create a new git worktree on a new branch.

    Returns the worktree path for the sub-agent to use as cwd.
    """
    _WORKTREE_DIR.mkdir(parents=True, exist_ok=True)
    worktree_path = _WORKTREE_DIR / name
    branch_name = f"worktree/{name}"

    # Check if already exists
    if worktree_path.exists():
        return f"Error: worktree '{name}' already exists at {worktree_path}"

    # Determine base ref
    if not base_branch:
        # Get default branch
        rc, out, err = await _git("rev-parse", "--abbrev-ref", "HEAD")
        base_branch = out if rc == 0 else "main"

    # Create worktree
    rc, out, err = await _git(
        "worktree", "add", "-b", branch_name,
        str(worktree_path), base_branch
    )
    if rc != 0:
        return f"Error creating worktree: {err}"

    logger.info("Worktree created: %s → %s", name, worktree_path)
    return f"Worktree '{name}' created at {worktree_path}\nBranch: {branch_name}\nBase: {base_branch}"


async def _worktree_merge(name: str) -> str:
    """Merge a worktree's branch back into the current branch.

    Returns merge result or conflict details.
    """
    branch_name = f"worktree/{name}"
    worktree_path = _WORKTREE_DIR / name

    if not worktree_path.exists():
        return f"Error: worktree '{name}' not found"

    # Check for uncommitted changes in worktree
    rc, out, err = await _git("status", "--porcelain", cwd=str(worktree_path))
    if out.strip():
        return (
            f"Warning: worktree '{name}' has uncommitted changes.\n"
            f"Changes:\n{out[:500]}\n\n"
            f"Commit changes first with: bash('git add -A && git commit -m ...') in the worktree, "
            f"or run the sub-agent again to complete its work."
        )

    # Merge the worktree branch
    rc, out, err = await _git("merge", branch_name, "--no-edit")
    if rc != 0:
        return f"Merge conflict in worktree '{name}':\n{out}\n{err}"

    # Delete the merged branch
    await _git("branch", "-d", branch_name)

    logger.info("Worktree merged: %s", name)
    return f"Worktree '{name}' merged successfully.\n{out}"


async def _worktree_cleanup(name: str, force: bool = False) -> str:
    """Remove a worktree and its branch (discard changes).

    force=True: remove even if unmerged changes exist.
    """
    worktree_path = _WORKTREE_DIR / name
    branch_name = f"worktree/{name}"

    if not worktree_path.exists():
        return f"Error: worktree '{name}' not found"

    if not force:
        rc, out, err = await _git("status", "--porcelain", cwd=str(worktree_path))
        if rc != 0:
            return f"Error checking worktree status: {err}"
        if out.strip():
            return (
                f"Error: worktree '{name}' has uncommitted changes. "
                "Commit, merge, or call cleanup with force=true to discard them.\n"
                f"Changes:\n{out[:500]}"
            )
        rc, out, err = await _git("branch", "--merged")
        if rc == 0 and branch_name not in out.split():
            return (
                f"Error: branch '{branch_name}' is not merged. "
                "Merge it first or call cleanup with force=true to discard it."
            )

    # Remove worktree
    args = ["worktree", "remove", str(worktree_path)]
    if force:
        args.append("--force")
    rc, out, err = await _git(*args)
    if rc != 0:
        return f"Error removing worktree: {err}"

    # Delete the branch
    await _git("branch", "-D" if force else "-d", branch_name)

    # Clean up directory if git left anything
    try:
        shutil.rmtree(worktree_path, ignore_errors=True)
    except Exception:
        pass

    logger.info("Worktree cleaned up: %s", name)
    return f"Worktree '{name}' removed."


async def _worktree_list() -> str:
    """List all active worktrees."""
    rc, out, err = await _git("worktree", "list")
    if rc != 0:
        return f"Error: {err}"
    return out or "No worktrees."


# ── tool handlers ──────────────────────────────────────


async def _wt_create(name: str, base_branch: str = "") -> str:
    return await _worktree_create(name, base_branch)


async def _wt_merge(name: str) -> str:
    return await _worktree_merge(name)


async def _wt_cleanup(name: str, force: bool = False) -> str:
    return await _worktree_cleanup(name, force)


async def _wt_list() -> str:
    return await _worktree_list()


# ── registration ───────────────────────────────────────


tool_registry.register(ToolEntry(
    name="worktree_create",
    description="Create an isolated git worktree for a sub-agent to work in. Returns the path to pass as cwd to sub_agent.",
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Worktree name (used in branch name worktree/<name>)"},
            "base_branch": {"type": "string", "description": "Base branch (default: current HEAD)"},
        },
        "required": ["name"],
    },
    handler=_wt_create,
    toolset="builtin",
    permission_category="write",
    tags=["git", "worktree", "write"],
    risk_level="medium",
    usage_hint="Use to create an isolated git worktree before parallel backend/frontend work.",
    is_parallel_safe=False,
))

tool_registry.register(ToolEntry(
    name="worktree_merge",
    description="Merge a worktree's changes back into the current branch. Requires the sub-agent's work to be committed.",
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Worktree name to merge"},
        },
        "required": ["name"],
    },
    handler=_wt_merge,
    toolset="builtin",
    permission_category="write",
    tags=["git", "worktree", "merge"],
    risk_level="high",
    usage_hint="Use after reviewing and committing worktree changes; may create merge conflicts.",
    is_destructive=True,
    is_parallel_safe=False,
))

tool_registry.register(ToolEntry(
    name="worktree_cleanup",
    description="Remove a worktree and discard its changes (or clean up after merge).",
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Worktree name to remove"},
            "force": {"type": "boolean", "description": "Force removal even if unmerged"},
        },
        "required": ["name"],
    },
    handler=_wt_cleanup,
    toolset="builtin",
    permission_category="write",
    tags=["git", "worktree", "cleanup"],
    risk_level="high",
    usage_hint="Use after merge to remove worktree records; force=true discards unmerged work.",
    is_destructive=True,
    is_parallel_safe=False,
))

tool_registry.register(ToolEntry(
    name="worktree_list",
    description="List all active git worktrees.",
    schema={"type": "object", "properties": {}, "required": []},
    handler=_wt_list,
    toolset="builtin",
    permission_category="read",
    tags=["git", "worktree", "read"],
    risk_level="low",
    usage_hint="Use to inspect active git worktrees before merge or cleanup.",
))
