"""Promote an allowed local file into the current turn's ArtifactStore."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path

from personal_agent.artifacts.models import normalize_artifact_kind
from personal_agent.tools.entry import ToolArtifact, ToolEntry, ToolHandlerOutput
from personal_agent.tools.registry import tool_registry
from personal_agent.tools.runtime_context import current_tool_agent
from personal_agent.tools.sandbox import get_sandbox


def _error(reason: str, message: str) -> ToolHandlerOutput:
    return ToolHandlerOutput(
        text=message,
        is_error=True,
        metadata={"reason_code": reason},
    )


def _requested_path(path: str) -> Path:
    sandbox = get_sandbox()
    requested = Path(str(path or "")).expanduser()
    if requested.is_absolute():
        return requested
    roots = [*sandbox.roots, *sandbox.read_roots]
    for root in roots:
        candidate = root / requested
        if candidate.exists() or candidate.is_symlink():
            return candidate
    return (roots[0] / requested) if roots else requested.absolute()


async def artifact_from_file(path: str, filename: str = "") -> ToolHandlerOutput:
    agent = current_tool_agent()
    store = getattr(agent, "_artifact_store", None) if agent is not None else None
    if store is None:
        return _error(
            "artifact_store_unavailable",
            "Artifact creation is unavailable in this runtime.",
        )

    sandbox = get_sandbox()
    requested = _requested_path(path)
    blocked = sandbox.check_blocked_path(requested.absolute())
    if blocked:
        return _error("sandbox_blocked", blocked)
    if requested.is_symlink():
        return _error("artifact_symlink_blocked", "Symbolic links cannot be promoted to artifacts.")

    full = requested.resolve()
    error = sandbox.check_path(full, access="read")
    if error:
        return _error("sandbox_blocked", error)
    if not full.exists():
        return _error("artifact_file_missing", f"File not found: {path}")
    if not full.is_file():
        return _error("artifact_file_invalid", f"Path is not a regular file: {path}")

    size = full.stat().st_size
    if size <= 0:
        return _error("artifact_empty", "Empty files cannot be promoted to artifacts.")
    if size > store.max_file_bytes:
        return _error(
            "artifact_too_large",
            f"File exceeds the artifact limit of {store.max_file_bytes} bytes.",
        )

    display_name = Path(str(filename or full.name)).name or full.name
    mime_type = mimetypes.guess_type(display_name)[0] or "application/octet-stream"
    kind = normalize_artifact_kind("resource", mime_type)
    return ToolHandlerOutput(
        text=json.dumps({
            "prepared": {
                "kind": kind,
                "filename": display_name,
                "mime_type": mime_type,
                "size_bytes": size,
            },
        }, ensure_ascii=False, sort_keys=True),
        artifacts=[ToolArtifact(
            kind=kind,
            name=display_name,
            mime_type=mime_type,
            uri=full.as_uri(),
        )],
    )


def _precheck(input_: dict) -> str | None:
    path = str(input_.get("path") or "")
    if not path:
        return "A file path is required."
    requested = _requested_path(path)
    return get_sandbox().check_blocked_path(requested.absolute())


tool_registry.register(ToolEntry(
    name="artifact_from_file",
    description=(
        "Copy an existing allowed local file into the current turn's ArtifactStore. "
        "The result returns an artifact_id that can be passed to response_attach. "
        "Use this when the user asks to receive a file created by write, edit, bash, or another tool."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to an existing local file in an allowed directory.",
            },
            "filename": {
                "type": "string",
                "description": "Optional display filename; does not change the source path.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    handler=artifact_from_file,
    toolset="utility",
    permission_category="read",
    tags=["artifact", "file", "read"],
    risk_level="low",
    usage_hint="Call response_attach with the returned artifact_id to include the file in the final response.",
    precheck=_precheck,
    idempotent=False,
    is_parallel_safe=False,
))
