# CLAUDE.md

This file provides repository guidance to Claude Code. General contribution rules are in `AGENTS.md`; current architecture and history are in `docs/architecture.md` and `PROJECT_EVOLUTION.md`.

## Project Overview

Luna Agent is a lightweight, plugin-oriented personal Agent Runtime. CLI/TUI, platform Gateway, Cron and capability-bound plugins all submit requests through the same application boundary:

```text
Input -> ConversationCoordinator -> ConversationService -> Agent/LLM/Tools
      -> ConversationTurnResult -> DeliveryService/Outbox -> Platform Adapter
```

Do not add a second Agent loop, session queue, permission path or delivery retry path inside an entrypoint or plugin.

## Commands

Use `uv` and Python 3.12+:

```bash
uv sync
uv run luna-agent init --profile local --copy-env --fix-dirs
uv run luna-agent doctor
uv run luna-agent chat
uv run luna-agent serve

python -m compileall -q src/luna_agent
uv run pytest -q
```

The current Luna Agent rename baseline is `1171 passed, 1 warning`; the warning comes from the Feishu SDK. Focused tests should be run before the full suite. Tests may update `src/luna_agent/skills/builtin/.usage.json`; restore it unless intentional.

## Runtime Boundaries

- `runtime.py::create_app_runtime` is the application composition root. It owns Settings, PluginManager, HookManager, sandbox/audit, MCPManager, Database, ArtifactStore, SessionStore, Memory, ConversationCoordinator and Delivery.
- `conversation/coordinator.py` owns ordered per-session turns, command/control lanes, `/stop`, `/steer`, policy snapshots and submission outcomes.
- `conversation/service.py` owns one conversation turn: history, cached Agent, multimodal input, Agent loop, transcript/tool report persistence and memory review scheduling.
- `agent/loop.py` owns model/tool iteration and tool-limit/empty-response finalization. Entrypoints must not duplicate it.
- `delivery/` owns target resolution, Pre/PostDelivery Hook, multipart Outbox, retry/recovery and platform capability fallback.
- Platform adapters own protocol parsing, connection/reconnect, attachment references, encoding/chunking and one send operation. They do not own session queues or retries.

## Plugins and Hooks

Built-in plugins live under `src/luna_agent/plugins/builtin/`; user/local plugins live under `plugins/` or configured plugin directories.

- A plugin synchronously calls `register(ctx)` to register Tool, Skill, MCP server, Hook, Command, Workflow, Platform or Memory Provider capabilities.
- Registration is owned and transactional. Do not mutate registries outside PluginContext.
- Only platform plugins may be deferred. Skill/MCP/Hook/Tool plugins register before their managers initialize.
- MCP process/session lifecycle belongs to MCPManager, not the plugin.
- Runtime Hook events use typed `HookEvent` contracts, matcher, priority, timeout and event-specific outcomes. Retired `on_before_llm_call`, `on_after_llm_call`, `on_before_tool_exec`, `on_after_tool_exec`, `on_message_received` and `on_before_send` names must not return.
- `ctx.conversation` and `ctx.notifications` are capability-bound ports, not raw core object access.

## Tool Security

Every tool call, including MCP and nested `tool_call`, passes through the executor:

```text
typed Hook -> hard precheck -> tool approval -> exact resource approval
           -> sandbox -> dispatch -> audit -> safe model-visible result
```

Modes are `read-only`, `ask-first`, `local-auto` and `full-auto`. Tool approval is `auto`, `cached`, `prompt` or `deny`; resources are exact filesystem read/write or network connect requirements. Grants are session-memory scoped and share one configured TTL. There is no category-level `/allow` compatibility path.

Preserve blocked paths, sandbox roots/read roots, Bubblewrap behavior, network validation, path traversal checks and audit summaries. Hard safety limits cannot be bypassed by a Hook or confirmation.

## Memory

Core memory orchestration lives in `src/luna_agent/memory/`: internal Markdown snapshots, observation buffer, SQLite Archive, review worker, router and fallback.

- `memory/luna` and `memory/mem0` are external provider plugins.
- Luna Agent uses provider-internal factories for embedding/vector/keyword/fusion/optional reranker backends.
- SQLite Archive is authoritative; vector/keyword indexes are rebuildable.
- Qdrant may use a remote URL or local persistent path, never both.
- Knowledge RAG is intentionally separate from personal memory.

## Artifacts and Multimodal Delivery

Inbound attachments and outbound Artifacts are separate domains.

- Tool/MCP output is copied into ArtifactStore and represented by session/turn-scoped `artifact_id`.
- Existing local files use `artifact_from_file`; file writes do not automatically become attachments.
- The model explicitly calls `response_attach` to select current-turn Artifacts.
- DeliveryPlanner/Outbox handles text/image/file/audio/video operations and platform fallback.
- Never expose base64, secret media keys, full local URI or ArtifactStore path in events, audit or model context.

## Configuration and Documentation

- `.env` contains secrets. Runtime code receives them only through `ConfigLoader -> Settings`; subsystems must not read `.env` directly.
- `config.yaml` contains behavior configuration. Preserve user-local changes and never commit secrets.
- Backend-facing frontend contract changes require the same-session update to `BACKEND_INTERFACE.md`.
- Frontend backend requests belong in `FRONTEND_INTERFACE_REQUIREMENTS.md`.
- Update `BACKEND_PROGRESS.md` or `FRONTEND_PROGRESS.md` for the workstream, and add structural milestones to `PROJECT_EVOLUTION.md`.

Use short imperative commits, keep changes scoped, and preserve unrelated or untracked user files.
