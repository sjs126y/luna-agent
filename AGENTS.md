# Repository Guidelines

## Project Structure & Module Organization

Source code lives in `src/personal_agent/`. Major subsystems include `agent/` for the agent loop, `gateway/` for platform routing, `tools/` for execution and safety, `plugins/` for built-in and user extension loading, `memory/`, `mcp/`, `workflow/`, and CLI entrypoints such as `cli.py`, `cli_chat.py`, and `cli_shell.py`.

Tests live in `tests/` and follow the subsystem names, for example `test_cli_shell.py`, `test_tool_pipeline.py`, and `test_gateway_commands.py`. Documentation is in `docs/`; examples are in `examples/`; utility scripts are in `scripts/`.

## Build, Test, and Development Commands

Use `uv` for the project environment.

```bash
uv sync
uv run personal-agent doctor
uv run personal-agent chat
uv run personal-agent serve
python -m compileall -q src/personal_agent
uv run pytest -q
```

`doctor` validates configuration, `chat` starts the local CLI, `serve` starts platform gateway mode, `compileall` catches syntax errors, and `pytest` runs the test suite.

## Coding Style & Naming Conventions

Target Python 3.12+. Use 4-space indentation, type annotations for public interfaces, and small async-friendly functions. Prefer existing local patterns over new abstractions. Modules, functions, and variables use `snake_case`; classes use `PascalCase`; constants use `UPPER_SNAKE_CASE`.

Keep comments concise and only where they clarify non-obvious behavior. Avoid introducing heavy frameworks; this project intentionally uses a lightweight runtime rather than LangChain-style orchestration.

## Testing Guidelines

Tests use `pytest` and `pytest-asyncio`. Name files `test_<area>.py` and tests `test_<behavior>()`. Add focused tests near the subsystem changed, and run:

```bash
uv run pytest tests/test_cli_shell.py -q
uv run pytest -q
```

Some tests update `src/personal_agent/skills/builtin/.usage.json`; restore that file before committing unless the usage data is intentionally changed.

## Frontend/Backend Codex Workflow

This project may have separate frontend and backend Codex agents working in parallel. Each Codex owns its own branch and progress file, and must update that progress file at the end of each work session.

The backend Codex owns backend runtime, agent loop, tools, permissions, provider/transport, gateway/platform adapters, tests, and backend-facing documentation. The frontend Codex owns TUI/classic CLI/desktop-web layout, interaction, visual polish, and frontend progress.

Backend-to-frontend contracts are documented in `BACKEND_INTERFACE.md`. When backend work adds or changes any frontend-consumable event, command, payload, diagnostic field, or API, the backend Codex must update `BACKEND_INTERFACE.md` in the same work session.

Frontend-to-backend requests are tracked in `FRONTEND_INTERFACE_REQUIREMENTS.md`. The frontend Codex should record backend needs there as small field/interface requests; the backend Codex should use that file as the intake list for frontend-facing backend changes.

## Commit & Pull Request Guidelines

Use short imperative commit messages. Keep commits scoped and include tests with behavior changes.

Pull requests should describe the user-visible change, list tests run, mention config or migration impacts, and include screenshots for terminal UI changes when relevant.

## Security & Configuration Tips

Secrets belong in `.env`, not in git. Runtime configuration is in `config.yaml`; generated data belongs under `data/`. Destructive tools require the current session's exact tool/resource approval; category-level `/allow write` no longer exists. Preserve audit, sandbox, blocked-path, and path-safety checks when editing tool code.
