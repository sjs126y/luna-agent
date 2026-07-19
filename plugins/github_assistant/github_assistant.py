"""GitHub MCP registration with repository and write-operation policy."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lumora_plugin_sdk import (
    ActiveResourceRequest,
    CommandEntry,
    HookEvent,
    PreToolUseOutcome,
)


class ActiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    sessions: list[str] = Field(default_factory=list)
    restart_backoff_seconds: list[float] = Field(default_factory=lambda: [1, 2, 5, 10, 30])


class WatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pull_requests: bool = True
    issues: bool = True
    commits: bool = True
    workflows: bool = True
    poll_interval_seconds: float = Field(default=300.0, ge=30.0)
    per_page: int = Field(default=20, ge=1, le=50)
    notify_on_start: bool = False


class GitHubAssistantConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = "https://api.githubcopilot.com/mcp/"
    auth_header_env: str = "GITHUB_MCP_AUTH"
    repositories: list[str] = Field(default_factory=list)
    write_enabled: bool = False
    connect_timeout_seconds: float = Field(default=60.0, gt=0)
    call_timeout_seconds: float = Field(default=120.0, gt=0)
    active: ActiveConfig = Field(default_factory=ActiveConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)


_WRITE_TOOL = re.compile(
    r"(?:^|__)(?:add|create|delete|fork|merge|push|remove|request|rerun|submit|update)_",
    re.IGNORECASE,
)


def register(ctx) -> None:
    config = ctx.parse_config(GitHubAssistantConfig)
    repositories = {_normalize_repo(value) for value in config.repositories if _normalize_repo(value)}

    ctx.register.mcp_server({
        "name": "github",
        "transport": "streamable_http",
        "url": config.url,
        "headers_env": {"Authorization": config.auth_header_env},
        "connect_timeout_seconds": config.connect_timeout_seconds,
        "call_timeout_seconds": config.call_timeout_seconds,
        "allow_network": True,
        "max_tools": 100,
    })
    ctx.register.skills("skills")

    def enforce_github_policy(envelope):
        tool_name = str(envelope.payload.get("tool_name") or "")
        tool_input = dict(envelope.payload.get("tool_input") or {})
        if not config.write_enabled and _is_write_tool(tool_name):
            return PreToolUseOutcome.block(
                "GitHub Assistant write operations are disabled by plugin configuration"
            )
        requested = _repo_from_input(tool_input)
        if repositories and requested and requested not in repositories:
            return PreToolUseOutcome.block(
                f"GitHub repository is outside the plugin allowlist: {requested}"
            )
        return None

    ctx.register.hook(
        HookEvent.PRE_TOOL_USE,
        enforce_github_policy,
        name="enforce-github-policy",
        matcher=r"^mcp__github__.+$",
        priority=10,
    )
    ctx.register.command(CommandEntry(
        name="github-status",
        description="Show GitHub Assistant configuration and MCP runtime status.",
        handler=lambda args="", **kwargs: _status(config, repositories, kwargs),
        scope="both",
    ))
    ctx.register.command(CommandEntry(
        name="github-watch-status",
        description="Show active GitHub repository watch status.",
        handler=lambda args="", **kwargs: _watch_status(ctx, config),
        scope="both",
    ))

    async def run(active_ctx) -> None:
        await GitHubWatcher(active_ctx, config).run()

    ctx.register.active(
        run=run,
        resources=ActiveResourceRequest(
            mcp={"github": (
                "list_pull_requests",
                "list_issues",
                "list_commits",
                "actions_list",
                "list_workflow_runs",
            )},
            required_mcp_servers=("github",),
            conversation=True,
        ),
        restart_policy="on_failure",
        startup_timeout=max(20.0, config.connect_timeout_seconds),
        shutdown_timeout=20.0,
    )


def _is_write_tool(tool_name: str) -> bool:
    short = tool_name.rsplit("__", 1)[-1]
    lowered = short.lower()
    return lowered.endswith("_write") or bool(_WRITE_TOOL.search(short)) or lowered in {
        "add_comment_to_pending_review",
        "create_pull_request_review",
        "dismiss_notification",
        "mark_all_notifications_read",
    }


def _repo_from_input(value: dict) -> str:
    owner = str(value.get("owner") or "").strip()
    repo = str(value.get("repo") or value.get("repository") or "").strip()
    if owner and repo:
        return _normalize_repo(f"{owner}/{repo}")
    if "/" in repo:
        return _normalize_repo(repo)
    return ""


def _normalize_repo(value: str) -> str:
    return str(value or "").strip().strip("/").lower()


def _status(config, repositories: set[str], kwargs: dict) -> str:
    state = _server_state(kwargs, "github")
    allowlist = ", ".join(sorted(repositories)) if repositories else "all repositories"
    return (
        "GitHub Assistant\n"
        f"- MCP: {state}\n"
        f"- repositories: {allowlist}\n"
        f"- write operations: {'enabled' if config.write_enabled else 'disabled'}"
    )


class GitHubWatcher:
    def __init__(self, ctx, config: GitHubAssistantConfig) -> None:
        self.ctx = ctx
        self.config = config
        self.storage = ctx.resources.storage
        self.state = self.storage.read_json(
            "watch-state.json",
            default=_empty_watch_state(),
            schema_version=1,
        )

    async def run(self) -> None:
        self._write_status("starting")
        await self.ctx.runtime.ready()
        if not self.config.repositories:
            self._write_status("degraded", error="active GitHub watch requires explicit repositories")
        else:
            self._write_status("active")
        while not self.ctx.runtime.stop_requested:
            await self.ctx.runtime.wait_until_resumed()
            self.ctx.runtime.heartbeat()
            if self.config.repositories:
                await self.poll_once()
            try:
                await asyncio.wait_for(
                    self.ctx.runtime.wait_until_stopped(),
                    timeout=self.config.watch.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass
        self._write_status("stopped")

    async def poll_once(self) -> list[dict[str, Any]]:
        observed = dict(self.state.get("observed_snapshot") or {})
        delivered = dict(self.state.get("delivered_snapshot") or {})
        pending = list(self.state.get("pending_events") or [])
        new_events: list[dict[str, Any]] = []
        errors: dict[str, str] = {}

        for repository in self.config.repositories:
            normalized = _normalize_repo(repository)
            if not normalized or "/" not in normalized:
                errors[repository] = "repository must use owner/repo format"
                continue
            owner, repo = normalized.split("/", 1)
            previous = dict(observed.get(normalized) or {})
            current, repo_errors = await self._repository_snapshot(owner, repo, previous)
            errors.update({f"{normalized}:{key}": value for key, value in repo_errors.items()})
            if not previous:
                observed[normalized] = current
                if self.config.watch.notify_on_start:
                    new_events.extend(_snapshot_events(normalized, current, initial=True))
                else:
                    delivered[normalized] = current
                continue
            new_events.extend(_snapshot_diff(normalized, previous, current))
            observed[normalized] = current

        known_ids = {str(item.get("event_id") or "") for item in pending}
        pending.extend(item for item in new_events if item["event_id"] not in known_ids)
        self.state.update({
            "schema_version": 1,
            "observed_snapshot": observed,
            "delivered_snapshot": delivered,
            "pending_events": pending,
            "last_checked_at": datetime.now(UTC).isoformat(),
            "last_errors": errors,
        })
        self._save_state()

        delivered_now = False
        if pending and self.config.active.sessions:
            delivered_now = await self._deliver(pending)
        if delivered_now:
            self.state["delivered_snapshot"] = observed
            self.state["pending_events"] = []
            self.state["last_delivered_at"] = datetime.now(UTC).isoformat()
            self._save_state()
        self._write_status(
            "active" if not errors else "degraded",
            pending_events=len(self.state.get("pending_events") or []),
            new_events=len(new_events),
            errors=errors,
        )
        return new_events

    async def _repository_snapshot(
        self,
        owner: str,
        repo: str,
        previous: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        current: dict[str, Any] = {}
        errors: dict[str, str] = {}
        categories = []
        if self.config.watch.pull_requests:
            categories.append(("pull_requests", self._pull_requests(owner, repo)))
        if self.config.watch.issues:
            categories.append(("issues", self._issues(owner, repo)))
        if self.config.watch.commits:
            categories.append(("commits", self._commits(owner, repo)))
        if self.config.watch.workflows:
            categories.append(("workflows", self._workflows(owner, repo)))
        for name, awaitable in categories:
            try:
                current[name] = await awaitable
            except Exception as exc:
                current[name] = dict(previous.get(name) or {})
                errors[name] = f"{type(exc).__name__}: {exc}"
        return current, errors

    async def _pull_requests(self, owner: str, repo: str) -> dict[str, Any]:
        return await self._call_items("list_pull_requests", {
            "owner": owner,
            "repo": repo,
            "state": "all",
            "sort": "updated",
            "direction": "desc",
            "perPage": self.config.watch.per_page,
        }, key="number")

    async def _issues(self, owner: str, repo: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for state in ("OPEN", "CLOSED"):
            result.update(await self._call_items("list_issues", {
                "owner": owner,
                "repo": repo,
                "state": state,
                "perPage": self.config.watch.per_page,
            }, key="number"))
        return result

    async def _commits(self, owner: str, repo: str) -> dict[str, Any]:
        return await self._call_items("list_commits", {
            "owner": owner,
            "repo": repo,
            "perPage": self.config.watch.per_page,
        }, key="sha")

    async def _workflows(self, owner: str, repo: str) -> dict[str, Any]:
        try:
            return await self._call_items("actions_list", {
                "method": "list_workflow_runs",
                "owner": owner,
                "repo": repo,
                "per_page": self.config.watch.per_page,
            }, key="id", list_keys=("workflow_runs", "runs"))
        except (KeyError, PermissionError):
            return await self._call_items("list_workflow_runs", {
                "owner": owner,
                "repo": repo,
                "perPage": self.config.watch.per_page,
            }, key="id", list_keys=("workflow_runs", "runs"))

    async def _call_items(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        key: str,
        list_keys: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        result = await self.ctx.resources.mcp.call("github", tool, arguments)
        if str(getattr(result, "status", "")) != "success":
            raise RuntimeError(str(getattr(result, "error", "") or getattr(result, "content", "")))
        payload = _json_payload(getattr(result, "content", ""))
        items = payload if isinstance(payload, list) else []
        if isinstance(payload, dict):
            for list_key in list_keys:
                if isinstance(payload.get(list_key), list):
                    items = payload[list_key]
                    break
            else:
                if isinstance(payload.get("items"), list):
                    items = payload["items"]
        normalized: dict[str, Any] = {}
        for item in items:
            if not isinstance(item, dict) or item.get(key) is None:
                continue
            normalized[str(item[key])] = _watch_item(item)
        return normalized

    async def _deliver(self, events: list[dict[str, Any]]) -> bool:
        prompt = _watch_prompt(events)
        digest = hashlib.sha256(
            json.dumps([item["event_id"] for item in events], sort_keys=True).encode()
        ).hexdigest()[:20]
        for session_key in self.config.active.sessions:
            handle = await self.ctx.resources.conversation.submit(
                session_key=session_key,
                text=prompt,
                request_id=f"github-watch:{digest}:{hashlib.sha256(session_key.encode()).hexdigest()[:8]}",
                metadata={"plugin": "github-assistant", "github_event_ids": [item["event_id"] for item in events]},
            )
            outcome_method = getattr(handle, "outcome", None)
            if callable(outcome_method):
                outcome = await outcome_method()
                if not bool(getattr(outcome, "succeeded", False)):
                    return False
        return True

    def _save_state(self) -> None:
        self.storage.write_json_atomic("watch-state.json", self.state)

    def _write_status(self, state: str, **details: Any) -> None:
        self.storage.write_json_atomic(
            "watch-status.json",
            {"schema_version": 1, "state": state, "updated_at": datetime.now(UTC).isoformat(), **details},
        )


def _empty_watch_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "observed_snapshot": {},
        "delivered_snapshot": {},
        "pending_events": [],
        "last_checked_at": "",
        "last_delivered_at": "",
        "last_errors": {},
    }


def _json_payload(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("GitHub MCP returned non-JSON content") from exc


def _watch_item(item: dict[str, Any]) -> dict[str, Any]:
    commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
    commit_message = commit.get("message") if isinstance(commit, dict) else ""
    return {
        "title": str(item.get("title") or item.get("name") or commit_message or "")[:500],
        "state": str(item.get("state") or item.get("status") or ""),
        "conclusion": str(item.get("conclusion") or ""),
        "draft": bool(item.get("draft", False)),
        "merged": bool(item.get("merged", False) or item.get("merged_at")),
        "updated_at": str(item.get("updated_at") or item.get("created_at") or ""),
        "url": str(item.get("html_url") or item.get("url") or ""),
        "sha": str(item.get("sha") or item.get("head_sha") or "")[:40],
    }


def _snapshot_diff(repository: str, previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for category, items in current.items():
        old_items = previous.get(category) if isinstance(previous.get(category), dict) else {}
        if not isinstance(items, dict):
            continue
        for item_key, item in items.items():
            old = old_items.get(item_key)
            if old is None:
                events.append(_watch_event(repository, category, item_key, "created", {}, item))
            elif item != old:
                events.append(_watch_event(repository, category, item_key, "updated", old, item))
    return events


def _snapshot_events(repository: str, snapshot: dict[str, Any], *, initial: bool) -> list[dict[str, Any]]:
    events = []
    for category, items in snapshot.items():
        if isinstance(items, dict):
            events.extend(
                _watch_event(repository, category, key, "initial" if initial else "created", {}, item)
                for key, item in items.items()
            )
    return events


def _watch_event(
    repository: str,
    category: str,
    item_key: str,
    change: str,
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    identity = f"{repository}:{category}:{item_key}:{change}:{json.dumps(current, sort_keys=True)}"
    return {
        "event_id": hashlib.sha256(identity.encode()).hexdigest()[:24],
        "repository": repository,
        "category": category,
        "item_key": item_key,
        "change": change,
        "previous": previous,
        "current": current,
    }


def _watch_prompt(events: list[dict[str, Any]]) -> str:
    payload = json.dumps(events, ensure_ascii=False, indent=2)
    return (
        "GitHub Watch 检测到以下仓库变化。请基于这些结构化事实向用户做简洁汇总；"
        "不要声称执行了任何写操作。如果存在失败的 Workflow，优先指出仓库、提交和状态。\n\n"
        + payload
    )


def _watch_status(ctx, config: GitHubAssistantConfig) -> str:
    state = ctx.storage.read_json("watch-status.json", default={}) or {}
    repositories = ", ".join(config.repositories) if config.repositories else "none"
    errors = state.get("errors") or {}
    return (
        "GitHub Watch\n"
        f"- active: {'enabled' if config.active.enabled else 'disabled'}\n"
        f"- runtime: {state.get('state') or ctx.runtime.safe_summary().get('state')}\n"
        f"- repositories: {repositories}\n"
        f"- interval: {config.watch.poll_interval_seconds:g}s\n"
        f"- pending events: {state.get('pending_events', 0)}\n"
        f"- errors: {len(errors)}"
    )


def _server_state(kwargs: dict, name: str) -> str:
    runtime = kwargs.get("runtime")
    app_runtime = getattr(runtime, "app_runtime", None)
    manager = getattr(app_runtime, "mcp_manager", None)
    gateway = kwargs.get("gateway")
    manager = manager or getattr(gateway, "_mcp_manager", None)
    if manager is None or not hasattr(manager, "health_snapshot"):
        return "configured (runtime status unavailable)"
    for server in manager.health_snapshot().get("servers", []):
        if str(server.get("name") or "") == name:
            return str(server.get("state") or "unknown")
    return "not registered"
