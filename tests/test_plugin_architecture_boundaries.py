"""Guards for ownership boundaries introduced by the generation runtime."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "src" / "luna_agent" / "plugins"


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_generation_state_is_written_only_by_coordinator_or_model_facade():
    owners = set()
    for path in PLUGIN_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "runtime_state"
                and isinstance(node.ctx, ast.Store)
            ):
                owners.add(path.relative_to(ROOT).as_posix())
    assert owners == {
        "src/luna_agent/plugins/core/coordinator.py",
        "src/luna_agent/plugins/core/models.py",
    }


def test_manager_does_not_own_worker_or_active_supervisor_state():
    manager = _source("src/luna_agent/plugins/core/manager.py")
    external = _source("src/luna_agent/plugins/runtime/external_service.py")

    for source in (manager, external):
        for field in (
            "_recovery_tasks",
            "_environment_leases",
            "_watch_tasks",
            "_specs",
            "ActivePluginRunner(",
        ):
            assert field not in source


def test_registration_context_only_stages_compatibility_registrations():
    context = _source("src/luna_agent/plugins/core/context.py")
    for expression in (
        "tool_registry.register(",
        "skill_registry.register(",
        "workflow_registry.register(",
        "platform_registry.register(",
        "memory_provider_registry.register(",
    ):
        assert expression not in context
    assert "registration_transaction.stage_named(" in context


def test_capability_route_state_is_owned_by_router():
    router = _source("src/luna_agent/plugins/runtime/router.py")
    manager = _source("src/luna_agent/plugins/core/manager.py")

    assert "self.active_bindings = active" in router
    assert "self.dynamic_bindings = dynamic" in router
    assert "self.active_bindings =" not in manager
    assert "self.dynamic_bindings =" not in manager
