"""AI-oriented plugin scaffolding, schemas, contract checks, and packaging."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import zipfile
from typing import Any

import yaml

from luna_agent_plugin_sdk import HookEvent, PLUGIN_API_VERSION, SDK_VERSION, PluginManifest
from luna_agent_plugin_sdk.testing import run_plugin_contract

from luna_agent.plugins.dependencies import HOST_CAPABILITIES


PLUGIN_FEATURES = {
    "active", "command", "hook", "mcp", "skill", "tool",
}


def capability_catalog() -> dict[str, Any]:
    return {
        "plugin_api_version": PLUGIN_API_VERSION,
        "sdk_version": SDK_VERSION,
        "registration": sorted([
            "active", "command", "hook", "mcp", "mcp_server", "memory_provider",
            "platform", "skill", "skills", "tool", "workflow",
        ]),
        "host_resources": sorted(HOST_CAPABILITIES),
        "hook_events": [event.value for event in HookEvent],
        "scaffold_features": sorted(PLUGIN_FEATURES),
    }


def schema_document(kind: str) -> dict[str, Any]:
    name = str(kind or "").strip().lower().replace("_", "-")
    if name == "manifest":
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Luna Agent Plugin Manifest",
            "type": "object",
            "required": ["schema_version", "key", "name", "version", "plugin_api", "entrypoint"],
            "properties": {
                "schema_version": {"const": 1},
                "key": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9_.-]*(/[a-z0-9][a-z0-9_.-]*)*$"},
                "name": {"type": "string", "minLength": 1},
                "version": {"type": "string", "minLength": 1},
                "plugin_api": {"type": "string"},
                "description": {"type": "string"},
                "kind": {"enum": ["general", "integration", "memory", "platform", "user"]},
                "entrypoint": {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_.]*(?::[A-Za-z_][A-Za-z0-9_]*)?$"},
                "requires_env": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                "provides": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                "tags": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                "requires": {"type": "object"},
                "enabled_by_default": {"type": "boolean"},
            },
            "additionalProperties": False,
        }
    if name in {"create", "scaffold", "scaffold-spec"}:
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Luna Agent Plugin Scaffold Spec",
            "type": "object",
            "required": ["key", "name"],
            "properties": {
                "key": {"type": "string"},
                "name": {"type": "string"},
                "version": {"type": "string", "default": "0.1.0"},
                "description": {"type": "string"},
                "kind": {"type": "string", "default": "general"},
                "module": {"type": "string"},
                "features": {"type": "array", "items": {"enum": sorted(PLUGIN_FEATURES)}},
                "hook_events": {"type": "array", "items": {"enum": [event.value for event in HookEvent]}},
                "requires": {"type": "object"},
                "provides": {"type": "array", "items": {"type": "string"}},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        }
    if name in {"capabilities", "capability"}:
        return capability_catalog()
    if name in {"hooks", "hook"}:
        return {"events": [event.value for event in HookEvent]}
    raise KeyError(f"Unknown plugin schema: {kind}")


def load_scaffold_spec(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    value = json.loads(raw) if path.suffix.lower() == ".json" else yaml.safe_load(raw)
    if not isinstance(value, dict):
        raise ValueError("Plugin scaffold spec must be an object")
    return value


def create_plugin(target: Path, spec: dict[str, Any], *, force: bool = False) -> list[Path]:
    key = str(spec.get("key") or "").strip()
    name = str(spec.get("name") or "").strip()
    if not key or not name:
        raise ValueError("Plugin scaffold requires key and name")
    module = str(spec.get("module") or key.rsplit("/", 1)[-1]).strip().replace("-", "_")
    if not module.isidentifier():
        raise ValueError(f"Plugin scaffold module is invalid: {module}")
    features = {str(item) for item in spec.get("features", [])}
    unknown = features - PLUGIN_FEATURES
    if unknown:
        raise ValueError(f"Unknown plugin scaffold feature(s): {', '.join(sorted(unknown))}")
    hook_events = [str(item) for item in spec.get("hook_events", [])]
    if hook_events:
        features.add("hook")
    valid_hooks = {event.value for event in HookEvent}
    invalid_hooks = set(hook_events) - valid_hooks
    if invalid_hooks:
        raise ValueError(f"Unknown hook event(s): {', '.join(sorted(invalid_hooks))}")
    target = Path(target).resolve()
    if target.exists() and any(target.iterdir()) and not force:
        raise FileExistsError(f"Plugin target is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    provides = list(dict.fromkeys([*spec.get("provides", []), *sorted(features)]))
    manifest = {
        "schema_version": 1,
        "key": key,
        "name": name,
        "version": str(spec.get("version") or "0.1.0"),
        "plugin_api": ">=1,<2",
        "description": str(spec.get("description") or ""),
        "kind": str(spec.get("kind") or "general"),
        "entrypoint": f"{module}:register",
        "requires": dict(spec.get("requires") or {}),
        "provides": provides,
        "tags": list(dict.fromkeys(["external", *spec.get("tags", [])])),
        "enabled_by_default": False,
    }
    PluginManifest.from_mapping(manifest)
    created = []
    created.append(_write(target / "plugin.yaml", yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), force))
    created.append(_write(target / f"{module}.py", _module_template(features, hook_events), force))
    created.append(_write(target / "AGENTS.md", _agents_template(key, module), force))
    created.append(_write(target / "README.md", _readme_template(name, key), force))
    test_path = target / "tests" / "test_contract.py"
    created.append(_write(test_path, _test_template(module, key), force))
    if "skill" in features:
        created.append(_write(target / "skills" / "example" / "SKILL.md", _skill_template(name), force))
    if "mcp" in features:
        created.append(_write(target / "mcp.yaml", "servers: []\n", force))
    return created


def contract_test(path: Path, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    root, manifest = _manifest(path)
    snapshot = run_plugin_contract(
        root,
        manifest.entrypoint,
        plugin_key=manifest.key,
        config=config,
    )
    return {
        "ok": True,
        "mode": "contract",
        "plugin_key": manifest.key,
        "entrypoint": manifest.entrypoint,
        "registrations": snapshot.as_dict(),
    }


def diff_plugins(before: Path, after: Path) -> dict[str, Any]:
    before_root, before_manifest = _manifest(before)
    after_root, after_manifest = _manifest(after)
    before_files = _file_hashes(before_root)
    after_files = _file_hashes(after_root)
    return {
        "plugin_key": after_manifest.key,
        "compatible_key": before_manifest.key == after_manifest.key,
        "version": {"before": before_manifest.version, "after": after_manifest.version},
        "manifest": {
            "requires_changed": before_manifest.requires.as_dict() != after_manifest.requires.as_dict(),
            "provides_added": sorted(set(after_manifest.provides) - set(before_manifest.provides)),
            "provides_removed": sorted(set(before_manifest.provides) - set(after_manifest.provides)),
        },
        "files": {
            "added": sorted(set(after_files) - set(before_files)),
            "removed": sorted(set(before_files) - set(after_files)),
            "changed": sorted(name for name in set(before_files) & set(after_files) if before_files[name] != after_files[name]),
        },
    }


def package_plugin(path: Path, output: Path | None = None) -> Path:
    root, manifest = _manifest(path)
    output = output or root.parent / f"{manifest.key.replace('/', '-')}-{manifest.version}.zip"
    output = Path(output).resolve()
    if output == root or root in output.parents:
        raise ValueError("Plugin package output must be outside the source directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in _package_files(root):
            info = zipfile.ZipInfo(item.relative_to(root).as_posix())
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.external_attr = 0o644 << 16
            archive.writestr(info, item.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    return output


def _manifest(path: Path) -> tuple[Path, PluginManifest]:
    path = Path(path).resolve()
    root = path.parent if path.is_file() else path
    candidates = [root / name for name in ("plugin.yaml", "plugin.yml", "plugin.json") if (root / name).is_file()]
    if len(candidates) != 1:
        raise ValueError(f"Plugin root must contain exactly one manifest: {root}")
    manifest_path = candidates[0]
    raw = manifest_path.read_text(encoding="utf-8")
    data = json.loads(raw) if manifest_path.suffix == ".json" else yaml.safe_load(raw)
    return root, PluginManifest.from_mapping(data, path=root)


def _module_template(features: set[str], hook_events: list[str]) -> str:
    imports = ["from luna_agent_plugin_sdk import PluginRuntimeContext"]
    if "command" in features:
        imports[0] = "from luna_agent_plugin_sdk import CommandEntry, PluginRuntimeContext"
    if "tool" in features:
        imports[0] = imports[0].replace("PluginRuntimeContext", "PluginRuntimeContext, ToolEntry")
    if "active" in features:
        imports[0] = imports[0].replace("PluginRuntimeContext", "ActiveResourceRequest, PluginRuntimeContext")
    if "hook" in features:
        imports[0] = imports[0].replace("PluginRuntimeContext", "HookEvent, PluginRuntimeContext")
    lines = ["\"\"\"Luna Agent external plugin entrypoint.\"\"\"", "", "from __future__ import annotations", "", *imports, ""]
    if "active" in features:
        lines.extend([
            "", "async def run(ctx: PluginRuntimeContext) -> None:",
            "    await ctx.runtime.ready()", "    await ctx.runtime.wait_until_stopped()", "",
        ])
    if "hook" in features:
        lines.extend(["", "async def on_hook(envelope):", "    return None", ""])
    lines.extend(["", "def register(ctx: PluginRuntimeContext) -> None:"])
    body = []
    if "tool" in features:
        body.extend([
            "    async def plugin_status() -> str:",
            '        return "plugin ready"',
            "    ctx.register.tool(ToolEntry(",
            '        name="plugin_status",',
            '        description="Return plugin readiness.",',
            '        schema={"type": "object", "properties": {}, "additionalProperties": False},',
            "        handler=plugin_status,",
            '        toolset="plugin",',
            "        idempotent=True,",
            "    ))",
        ])
    if "skill" in features:
        body.append('    ctx.register.skills("skills")')
    if "mcp" in features:
        body.append('    ctx.register.mcp("mcp.yaml")')
    event_names = {event.value: event.name for event in HookEvent}
    for event in hook_events or ([HookEvent.PRE_TOOL_USE.value] if "hook" in features else []):
        enum_name = event_names[event]
        body.append(f"    ctx.register.hook(HookEvent.{enum_name}, on_hook, name=\"plugin-{event}\")")
    if "command" in features:
        body.extend([
            "    ctx.register.command(CommandEntry(",
            '        name="plugin-status",',
            '        description="Show plugin status.",',
            '        handler=lambda **_: "plugin ready",',
            "    ))",
        ])
    if "active" in features:
        body.append("    ctx.register.active(run=run, resources=ActiveResourceRequest())")
    lines.extend(body or ["    pass"])
    return "\n".join(lines) + "\n"


def _agents_template(key: str, module: str) -> str:
    return f"""# Plugin Agent Instructions

- Plugin key: `{key}`
- Entrypoint: `{module}:register`
- Import public contracts from `luna_agent_plugin_sdk`; do not import host internals from `luna_agent`.
- Declare every host/plugin/MCP dependency in `plugin.yaml` before using it.
- Register capabilities only inside `register(ctx)` through `ctx.register.*`.
- Keep `register(ctx)` deterministic and free of network/process side effects.
- Active work belongs in the registered `run(ctx)` coroutine and must honor runtime stop/quiesce signals.
- Use stable `request_id` values for durable `conversation.submit` calls.
- Store mutable state through the scoped storage resource, never inside the installed package.
- Run `luna-agent plugins test . --contract --integration` before packaging.
"""


def _readme_template(name: str, key: str) -> str:
    return f"# {name}\n\nLuna Agent external plugin `{key}`.\n\nSee `AGENTS.md` for the machine-oriented development contract.\n"


def _test_template(module: str, key: str) -> str:
    return f'''from pathlib import Path\n\nfrom luna_agent_plugin_sdk.testing import run_plugin_contract\n\n\ndef test_plugin_contract():\n    snapshot = run_plugin_contract(\n        Path(__file__).parents[1],\n        "{module}:register",\n        plugin_key="{key}",\n    )\n    assert snapshot is not None\n'''


def _skill_template(name: str) -> str:
    return f"---\nname: example\ndescription: Example capability for {name}.\n---\n\n# Example\n\nExecute the plugin-specific workflow.\n"


def _write(path: Path, value: str, force: bool) -> Path:
    if path.exists() and not force:
        raise FileExistsError(f"Plugin scaffold file exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        item.relative_to(root).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in _package_files(root)
    }


def _package_files(root: Path) -> list[Path]:
    ignored = {"__pycache__", ".git", ".pytest_cache", ".venv"}
    return sorted(
        item for item in root.rglob("*")
        if item.is_file()
        and not any(part in ignored for part in item.relative_to(root).parts)
        and item.suffix not in {".pyc", ".pyo"}
    )
