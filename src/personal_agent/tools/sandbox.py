"""Unified path sandbox — shared by all file tools and bash.

Model:
  roots    — directories the agent is allowed to access
  blocked  — glob patterns for paths that are never accessible (even within roots)

File tools:
  resolve path → check against blocked → check under a root → proceed

Bash:
  on top of sandbox: command whitelist, network isolation, dangerous pattern detection
  path sandbox in bash checks command strings against blocked patterns and
  optionally restricts absolute paths to roots.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ── singleton (set at startup) ─────────────────────

_sandbox: Sandbox | None = None


def get_sandbox() -> Sandbox:
    global _sandbox
    if _sandbox is None:
        _sandbox = Sandbox([], [])
        logger.warning("Sandbox used before init_sandbox() — defaulting to empty (no roots, no blocked patterns)")
    return _sandbox


def init_sandbox(
    roots: list[Path],
    blocked: list[str],
    *,
    read_roots: list[Path] | None = None,
) -> Sandbox:
    global _sandbox
    _sandbox = Sandbox(roots, blocked, read_roots=read_roots)
    logger.info(
        "Sandbox: %d writable roots, %d read-only roots, %d blocked patterns",
        len(roots),
        len(read_roots or []),
        len(blocked),
    )
    return _sandbox


class Sandbox:
    def __init__(
        self,
        roots: list[Path],
        blocked: list[str],
        *,
        read_roots: list[Path] | None = None,
    ) -> None:
        self.roots = [r.resolve() for r in roots]
        self.read_roots = [r.resolve() for r in (read_roots or [])]
        self.blocked = blocked

    # ── File tool API ───────────────────────────────

    def resolve(self, path: str) -> Path:
        """Resolve a relative or absolute path within sandbox roots.

        Relative paths are tried against each root; the first existing match wins.
        Falls back to the first root (or cwd if no roots configured).
        """
        p = Path(path)
        if p.is_absolute():
            return p.resolve()
        for root in [*self.roots, *self.read_roots]:
            full = (root / path).resolve()
            if full.exists():
                return full
        if self.roots:
            return (self.roots[0] / path).resolve()
        return Path(path).resolve()

    def check_path(self, absolute_path: Path, *, access: str = "read") -> str | None:
        """Check a resolved absolute path. Returns error or None if allowed.

        Used by file_read, file_write, file_edit, grep, glob.
        """
        # Normalize to forward slashes for matching
        ps = str(absolute_path).replace("\\", "/")

        # 1. Blocked patterns — NEVER accessible
        err = self._check_blocked(ps)
        if err:
            return err

        security_evaluated, security_error = self._check_runtime_profile(absolute_path, access=access)
        if security_evaluated:
            return security_error

        # 2. Read-only roots never expand write access.
        allowed_roots = self.roots if access == "write" else [*self.roots, *self.read_roots]
        if allowed_roots:
            for root in allowed_roots:
                rs = str(root).replace("\\", "/")
                if ps == rs or ps.startswith(rs + "/"):
                    return None  # allowed
            return f"Error: path is outside sandbox roots — use a path within the allowed directories"

        return None  # no roots configured, allow anything not blocked

    def check_blocked_path(self, absolute_path: Path) -> str | None:
        """Apply only non-expandable blocked-path rules during preflight."""
        return self._check_blocked(str(absolute_path).replace("\\", "/"))

    @staticmethod
    def _check_runtime_profile(absolute_path: Path, *, access: str) -> tuple[bool, str | None]:
        try:
            from personal_agent.security.models import ResourceRequirement
            from personal_agent.tools.runtime_context import current_tool_agent

            agent = current_tool_agent()
            context = getattr(agent, "_security_context", None) if agent is not None else None
            if context is None:
                return False, None
            requirement = ResourceRequirement(
                "filesystem", str(absolute_path.resolve()), access, "filesystem access"
            )
            if context.profile.allows(requirement) or context.state.has_resource_grant(requirement):
                return True, None
            return True, f"Error: {access} access is not granted for this path"
        except Exception as exc:
            return True, f"Error: failed to evaluate filesystem permission: {exc}"

    # ── Bash API ────────────────────────────────────

    def check_bash_path(self, absolute_path_str: str) -> str | None:
        """Check a resolved absolute path string from a bash command. Returns error or None.

        Used by bash's path sandbox layer.
        """
        ps = absolute_path_str.replace("\\", "/")

        # Blocked patterns always apply
        err = self._check_blocked(ps)
        if err:
            return err

        return None  # allowed (roots/escape patterns checked by bash layer)

    def is_under_root(self, absolute_path_str: str) -> bool:
        """Check if a path string is under any sandbox root."""
        ps = absolute_path_str.replace("\\", "/")
        for root in self.roots:
            rs = str(root).replace("\\", "/")
            if ps == rs or ps.startswith(rs + "/"):
                return True
        return False

    # ── Blocked pattern matching ────────────────────

    def _check_blocked(self, normalized_path: str) -> str | None:
        """Check if a normalized path (forward slashes) matches any blocked glob."""
        for pattern in self.blocked:
            if _glob_match(normalized_path, pattern):
                return (
                    f"Error: path blocked by sandbox — '{pattern}' matches "
                    f"protected files. Use a more specific tool if you need to "
                    f"access this location."
                )
        return None


def _glob_match(path_str: str, pattern: str) -> bool:
    """Match a forward-slash path against a glob pattern.

    Supports ** for recursive matching, * for single-segment wildcard.
    """
    # fnmatch doesn't handle ** natively — we do a simple check:
    # For most patterns like "**/.git/**" or "**/.env", fnmatch works
    # on the full path with forward slashes.
    return fnmatch.fnmatch(path_str.replace("\\", "/"), pattern.replace("\\", "/"))
