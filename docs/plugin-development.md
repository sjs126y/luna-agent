# Luna Agent Plugin Development

This document is the canonical development contract for plugins created by the
Codex Bridge. It is copied as a read-only snapshot into each plugin workspace
when a development session is created.

The user supplies functional intent, not framework boilerplate. Codex must read
this file and `PLUGIN_BRIEF.md` before the first edit, infer routine engineering
details from this contract and the SDK, and only surface decisions that change
public behavior, permissions, data formats, or architecture.

## Package contract

A plugin package contains one root `plugin.yaml` and an entrypoint module. The
entrypoint must expose `register(ctx)` and use the public SDK rather than
private `luna_agent.*` modules.

```text
plugin.yaml
src/<plugin_module>.py
tests/
README.md
```

The manifest declares the plugin key, version, entrypoint, SDK range, provided
capabilities, dependencies, and configuration shape. A plugin must not modify
the host application's source tree or install itself while it is being built.
Python dependencies belong in `requires.python`; the host builds an immutable
environment for that exact dependency set and installs wheel distributions
only. Do not run pip, uv, npm, or another package manager from plugin code.

## Registration

Use the grouped registration API:

```python
def register(ctx):
    ctx.register.tool(entry)
    ctx.register.skill(entry)
    ctx.register.hook(event, callback)
    ctx.register.mcp_server(config)
```

Active plugins additionally register one runtime with
`ctx.register.active(...)`. The runtime owns its background resources and must
release them from `on_stop` or normal cancellation.

## Boundaries

- Use `ctx.storage` for plugin-owned persistent data.
- Declare active resources explicitly.
- Treat host ports and tool results as capability boundaries.
- Do not read secrets from arbitrary environment variables.
- Do not start detached processes or write outside the declared development
  workspace.
- Do not change sandbox, approval, or MCP configuration from model input.

External plugins run in a generation-scoped Worker. The Worker has no direct
host filesystem or network access by default. A tool that accepts a local file
must declare a `ToolResourceBinding`; the host permission pipeline validates the
original path and stages an immutable copy for that invocation. Active plugins
request Tool, MCP, conversation, LLM, process, or workspace capabilities through
`ActiveResourceRequest`. Process and workspace names are declarations, not
arbitrary commands or paths: the host must have a matching allowlisted config.

## Development and verification

The host creates an isolated development directory and a Codex Thread for one
plugin. Codex may edit and test that directory, but packaging and installation
remain host operations. Before installation the host validates the manifest,
imports, registered capabilities, tests, paths, and package contents.

The development session is asynchronous. A new message is submitted to the
same Thread; events are delivered to Luna with a type and text. Normal progress
does not require a user decision. Capability, permission, public API, or data
format changes must be surfaced for user confirmation.

## Lifecycle

Thread history is persisted by the isolated Codex home. The bridge persists the
plugin-to-thread mapping, brief path, current turn, and compact event history.
On restart the bridge may resume an idle Thread, but it must not silently resume
an in-progress Turn. Pending approvals are denied on shutdown, reload, crash,
or generation replacement.
