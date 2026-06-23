"""Read files within allowed data directory.

Security:
  - Path traversal prevention
  - Sensitive file blocklist (.env, .ssh, credentials, etc.)
  - Audit logging
"""

from pathlib import Path

from personal_agent.tools.entry import ToolEntry
from personal_agent.tools.registry import tool_registry

# Set at startup — overwritten by main.py
_allowed_base: Path = Path("./data").resolve()

MAX_READ_BYTES = 50_000

# ── Sensitive file patterns (filenames and path suffixes) ──
# Blocked from being read — credential stores, SSH keys, auth data.

_SENSITIVE_NAMES: set[str] = {
    ".env", ".env.local", ".env.production",
    ".anthropic_oauth.json", "auth.json", "google_oauth.json",
    "webhook_subscriptions.json", "bws_cache.json",
    ".netrc", ".pgpass", ".npmrc", ".pypirc", ".git-credentials",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    "authorized_keys", "known_hosts",
}

_SENSITIVE_SUFFIXES: tuple[str, ...] = (
    ".ssh/id_", ".ssh/config",
    ".gnupg/", ".aws/credentials", ".aws/config",
    ".kube/config", ".docker/config.json",
    ".azure/", ".config/gh/", ".config/gcloud/",
    "data/auth/", "data/wechat/creds",
)


def _check_sensitive(filepath: Path) -> str | None:
    """Check if a file path points to sensitive content. Returns error or None."""
    fname = filepath.name
    if fname in _SENSITIVE_NAMES:
        return (
            f"Error: reading '{fname}' is blocked for security reasons. "
            f"Credential and key files cannot be read by the agent."
        )

    fstr = str(filepath).replace("\\", "/")
    for suffix in _SENSITIVE_SUFFIXES:
        if suffix in fstr:
            return (
                f"Error: reading sensitive path (matches '{suffix}') is blocked. "
                f"Use a more specific tool for this operation."
            )

    return None


def set_allowed_base(path: Path) -> None:
    global _allowed_base
    _allowed_base = path.resolve()


async def _file_read(path: str) -> str:
    try:
        full = (_allowed_base / path).resolve()
        if not str(full).startswith(str(_allowed_base)):
            return f"Error: path traversal denied — '{path}' is outside allowed directory"

        # ── Sensitive file blocklist ──
        sensitive_error = _check_sensitive(full)
        if sensitive_error:
            from personal_agent.tools.audit import audit_log
            audit_log("file_read", path, sensitive_error, False)
            return sensitive_error

        if not full.exists():
            return f"Error: file not found: {path}"
        if full.is_dir():
            return f"Error: '{path}' is a directory"
        content = full.read_text(encoding="utf-8", errors="replace")
        if len(content) > MAX_READ_BYTES:
            content = content[:MAX_READ_BYTES] + f"\n\n...(truncated {len(content) - MAX_READ_BYTES} bytes)"
        from personal_agent.tools.audit import audit_log
        audit_log("file_read", path, f"{len(content)} bytes read", True)
        return content
    except Exception as e:
        from personal_agent.tools.audit import audit_log
        audit_log("file_read", path, str(e), False)
        return f"Error: {e}"


tool_registry.register(ToolEntry(
    name="read",
    description="Read a file from the agent's data directory. Path is relative to data dir. Use for reading saved notes, code, or data files.",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path to file, e.g. 'notes/ideas.txt'"},
        },
        "required": ["path"],
    },
    handler=_file_read,
    toolset="builtin",
))
