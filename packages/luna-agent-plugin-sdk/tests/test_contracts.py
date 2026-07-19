from __future__ import annotations

from types import SimpleNamespace

from luna_agent_plugin_sdk import ActiveResourceRequest, HookEvent, PluginRuntimeContext


def test_active_resource_request_normalizes_mcp_contract() -> None:
    request = ActiveResourceRequest(
        tools=("file_info",),
        mcp={"github": ("list_pull_requests",)},
        required_mcp_servers=("github",),
        conversation=True,
    )

    assert request.safe_summary()["conversation"] is True
    assert request.safe_summary()["mcp"] == {"github": ["list_pull_requests"]}


def test_runtime_context_protocol_is_structural() -> None:
    context = SimpleNamespace(
        plugin_key="test/plugin",
        generation_id="generation",
        runtime_instance_id="runtime",
        register=SimpleNamespace(),
        config={},
        root=None,
        runtime=None,
        resources=None,
        storage=None,
        tasks=None,
        parse_config=lambda model: model,
        get_env=lambda name, default="": default,
        resolve_path=lambda path: path,
    )

    assert isinstance(context, PluginRuntimeContext)
    assert HookEvent.PRE_TOOL_USE.value == "PreToolUse"


def test_legacy_sdk_namespace_reexports_canonical_types() -> None:
    from lumora_plugin_sdk import PluginRuntimeContext as LegacyPluginRuntimeContext

    assert LegacyPluginRuntimeContext is PluginRuntimeContext
