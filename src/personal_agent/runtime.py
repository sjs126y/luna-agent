"""Shared application runtime bootstrap."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from personal_agent.config import Settings
from personal_agent.db.database import Database
from personal_agent.gateway.compression_chain import CompressionChain
from personal_agent.gateway.session_store import SessionStore
from personal_agent.memory.manager import MemoryManager
from personal_agent.memory.review import MemoryReviewService
from personal_agent.hooks import HookManager
from personal_agent.plugins.core.manager import PluginManager
from personal_agent.tools.audit import set_audit_path
from personal_agent.tools.sandbox import init_sandbox
from personal_agent.conversation import ConversationService

logger = logging.getLogger(__name__)


BootStepStatus = Literal["not_run", "pending", "ok", "skipped", "error"]

BOOT_STEP_NAMES: tuple[str, ...] = (
    "settings",
    "data_dir",
    "plugins.discover",
    "plugins.load_enabled",
    "plugins.configure",
    "sandbox",
    "audit",
    "mcp",
    "database",
    "compression_chain",
    "session_store",
    "system_files",
    "memory",
    "memory_review",
    "conversation",
    "runtime",
)


@dataclass
class BootStep:
    name: str
    status: BootStepStatus = "not_run"
    detail: str = ""
    error: str = ""
    duration: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "error": self.error,
            "duration": self.duration,
        }


class _BootStepContext:
    def __init__(self, step: BootStep) -> None:
        self._step = step
        self._started = 0.0

    def __enter__(self) -> BootStep:
        self._started = monotonic()
        self._step.status = "pending"
        self._step.error = ""
        self._step.duration = 0.0
        return self._step

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._step.duration = max(monotonic() - self._started, 0.0)
        if exc is not None:
            self._step.status = "error"
            exc_name = exc_type.__name__ if exc_type is not None else type(exc).__name__
            self._step.error = f"{exc_name}: {exc}"
        elif self._step.status == "pending":
            self._step.status = "ok"
        return False


@dataclass
class BootReport:
    steps: list[BootStep] = field(default_factory=list)

    @classmethod
    def bootstrap(cls) -> BootReport:
        return cls([BootStep(name=name) for name in BOOT_STEP_NAMES])

    def step(self, name: str, detail: str = "") -> _BootStepContext:
        step = self._step(name)
        step.detail = detail
        step.error = ""
        return _BootStepContext(step)

    def skip(self, name: str, detail: str = "") -> BootStep:
        step = self._step(name)
        step.status = "skipped"
        step.detail = detail
        step.error = ""
        step.duration = 0.0
        return step

    def error(self, name: str, error: str, detail: str = "") -> BootStep:
        step = self._step(name)
        step.status = "error"
        step.detail = detail
        step.error = error
        step.duration = 0.0
        return step

    def attach_to_exception(self, exc: BaseException) -> BaseException:
        try:
            setattr(exc, "boot_report", self)
        except Exception:
            logger.debug("Unable to attach boot report to exception", exc_info=True)
        return exc

    @property
    def ok(self) -> bool:
        return not any(step.status in {"not_run", "pending", "error"} for step in self.steps)

    @property
    def failed_step(self) -> str:
        for step in self.steps:
            if step.status == "error":
                return step.name
        return ""

    def summary(self) -> dict[str, int]:
        counts = {"not_run": 0, "pending": 0, "ok": 0, "skipped": 0, "error": 0}
        for step in self.steps:
            counts[step.status] += 1
        counts["total"] = len(self.steps)
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "failed_step": self.failed_step,
            "summary": self.summary(),
            "steps": [step.as_dict() for step in self.steps],
        }

    def _step(self, name: str) -> BootStep:
        for step in self.steps:
            if step.name == name:
                return step
        step = BootStep(name=name)
        self.steps.append(step)
        return step


def boot_report_from_exception(exc: BaseException) -> BootReport | None:
    report = getattr(exc, "boot_report", None)
    return report if isinstance(report, BootReport) else None


@dataclass
class AppRuntime:
    settings: Settings
    hook_manager: HookManager
    plugin_manager: PluginManager
    db: Database
    compression_chain: CompressionChain
    session_store: SessionStore
    memory_manager: MemoryManager
    conversation_service: ConversationService
    memory_review_service: MemoryReviewService
    mcp_manager: Any | None
    data_dir: Path
    system_dir: Path
    boot_report: BootReport = field(default_factory=BootReport)
    gateway: Any | None = None
    gateway_started: bool = False
    closed: bool = False

    def create_gateway(self, system_prompt_template: str = ""):
        if self.gateway is not None:
            return self.gateway

        from personal_agent.gateway.gateway import Gateway

        self.gateway = Gateway(
            self.settings,
            self.db,
            self.memory_manager,
            system_prompt_template=system_prompt_template,
            plugin_manager=self.plugin_manager,
            hook_manager=self.hook_manager,
            conversation_service=self.conversation_service,
            memory_review_service=self.memory_review_service,
        )
        self.gateway_started = False
        return self.gateway

    async def start_gateway(self, system_prompt_template: str = ""):
        gateway = self.create_gateway(system_prompt_template=system_prompt_template)
        if not self.gateway_started:
            await gateway.start()
            self.gateway_started = True
        return gateway

    async def stop_gateway(self) -> None:
        gateway = self.gateway
        self.gateway = None
        was_started = self.gateway_started
        self.gateway_started = False
        if gateway is not None and was_started:
            await gateway.stop()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.stop_gateway()
        await self.memory_review_service.close()
        await self.memory_manager.close()
        mcp_manager = self.mcp_manager
        self.mcp_manager = None
        if mcp_manager is not None:
            await mcp_manager.stop()
        await self.db.close()

    def health_snapshot(self) -> dict[str, Any]:
        from personal_agent.activity import activity_snapshot

        mcp_health = _mcp_health_snapshot(
            self.mcp_manager,
            enabled=bool(self.settings.mcp_enabled),
            configured_count=len(self.settings.mcp_servers),
        )
        boot = self.boot_report.as_dict()
        command_health = _command_health_snapshot(self)
        query_health = _query_health_snapshot(self)
        turns = self.conversation_service.turn_report_summary()
        turns["persisted"] = self.conversation_service.turn_report_persistence_summary()
        gateway = self.gateway.health_snapshot() if self.gateway is not None else {}
        return {
            "data_dir": str(self.data_dir),
            "db_open": getattr(self.db, "_conn", None) is not None,
            "mcp_enabled": bool(self.settings.mcp_enabled),
            "mcp_running": self.mcp_manager is not None,
            "mcp": mcp_health,
            "boot": boot,
            "boot_ok": bool(boot["ok"]),
            "boot_failed_step": str(boot["failed_step"]),
            "gateway_created": self.gateway is not None,
            "gateway_running": bool(self.gateway is not None and self.gateway_started),
            "gateway": gateway,
            "activity": activity_snapshot(gateway_snapshot=gateway),
            "turns": turns,
            "llm_cache": _llm_cache_health_snapshot(self.settings, turns),
            "tool_truth": self.conversation_service.tool_truth_summary(),
            "tool_runs": self.conversation_service.tool_run_memory_summary(),
            "commands": command_health,
            "query": query_health,
            "execution": _execution_health_snapshot(self.settings),
            "plugins": len(self.plugin_manager.list_plugins()),
            "hooks": self.hook_manager.health_snapshot(),
            "cached_agents": len(self.conversation_service.agent_cache),
            "closed": self.closed,
        }


async def create_app_runtime(settings: Settings | None = None) -> AppRuntime:
    boot_report = BootReport.bootstrap()
    mcp_manager = None
    db: Database | None = None
    try:
        if settings is None:
            with boot_report.step("settings", "default"):
                settings = Settings()
        else:
            with boot_report.step("settings", "provided"):
                settings = settings

        with boot_report.step("data_dir", str(settings.agent_data_dir)):
            data_dir = settings.agent_data_dir
            data_dir.mkdir(parents=True, exist_ok=True)

        hook_manager = HookManager()
        with boot_report.step("plugins.discover"):
            plugin_manager = PluginManager(settings, hook_manager=hook_manager)
            plugin_manager.discover()
        with boot_report.step("plugins.load_enabled"):
            plugin_manager.load_enabled()
        with boot_report.step("plugins.configure"):
            await plugin_manager.invoke_hook("configure", settings=settings)

        with boot_report.step("sandbox"):
            init_sandbox(
                settings.sandbox_roots,
                settings.sandbox_blocked,
                read_roots=getattr(settings, "sandbox_read_roots", []),
            )
        if settings.audit_enabled:
            with boot_report.step("audit", str(data_dir / "audit.log")):
                set_audit_path(data_dir / "audit.log")
        else:
            boot_report.skip("audit", "disabled")

        if not settings.mcp_enabled:
            boot_report.skip("mcp", "disabled")
        else:
            mcp_server_count = len(settings.mcp_servers) + len(plugin_manager.get_mcp_servers())
            if mcp_server_count == 0:
                boot_report.skip("mcp", "servers=0")
            else:
                with boot_report.step("mcp", f"servers={mcp_server_count}"):
                    mcp_manager = await start_mcp_manager(settings, plugin_manager)

        with boot_report.step("database", str(data_dir / "state.db")):
            db = Database(data_dir / "state.db")
            await db.initialize()

        with boot_report.step("compression_chain", str(data_dir / "compression_chain.json")):
            compression_chain = CompressionChain(data_dir / "compression_chain.json")
            compression_chain.load()
        with boot_report.step("session_store"):
            session_store = SessionStore(db, data_dir, chain=compression_chain)
            await session_store.initialize()
            await session_store.expire_sessions(settings.session_expire_days)

        with boot_report.step("system_files", str(data_dir / "system")):
            system_dir = data_dir / "system"
            ensure_system_files(system_dir)
        with boot_report.step("memory"):
            memory_manager = await create_memory_manager(settings, plugin_manager, system_dir, data_dir)
        with boot_report.step("memory_review"):
            memory_review_service = MemoryReviewService(
                memory_manager,
                interval=settings.memory_external_turn_interval,
                concurrency=settings.memory_worker_concurrency,
            )
            await memory_review_service.start()
        with boot_report.step("conversation"):
            conversation_service = ConversationService(
                settings=settings,
                plugin_manager=plugin_manager,
                hook_manager=hook_manager,
                session_store=session_store,
                compression_chain=compression_chain,
                memory_manager=memory_manager,
                memory_review_service=memory_review_service,
            )

        with boot_report.step("runtime"):
            return AppRuntime(
                settings=settings,
                hook_manager=hook_manager,
                plugin_manager=plugin_manager,
                db=db,
                compression_chain=compression_chain,
                session_store=session_store,
                memory_manager=memory_manager,
                conversation_service=conversation_service,
                memory_review_service=memory_review_service,
                mcp_manager=mcp_manager,
                data_dir=data_dir,
                system_dir=system_dir,
                boot_report=boot_report,
            )
    except Exception as exc:
        boot_report.attach_to_exception(exc)
        if mcp_manager is not None:
            await mcp_manager.stop()
        if db is not None:
            await db.close()
        raise


def _mcp_health_snapshot(
    manager: Any | None,
    *,
    enabled: bool,
    configured_count: int,
) -> dict[str, Any]:
    if manager is not None and hasattr(manager, "health_snapshot"):
        health = dict(manager.health_snapshot())
        health.setdefault("enabled", enabled)
        return health
    return {
        "enabled": enabled,
        "running": manager is not None,
        "configured_count": configured_count,
        "connected_count": 0,
        "total_tools": 0,
        "registered_tools": [],
        "servers": [],
    }


def _command_health_snapshot(runtime: AppRuntime) -> dict[str, Any]:
    from personal_agent.commands.registry import (
        SLASH_COMMAND_REGISTRY_VERSION,
        command_specs_as_dict,
    )

    registry = command_specs_as_dict(runtime)
    commands = list(registry.get("commands") or [])
    plugin_commands = list(registry.get("plugin_commands") or [])
    argument_specs = _count_argument_specs(commands)
    dynamic_providers = sorted(_dynamic_providers(commands))
    command_names = {str(item.get("name") or "") for item in commands if isinstance(item, dict)}
    return {
        "registry_version": SLASH_COMMAND_REGISTRY_VERSION,
        "core_commands": len(commands),
        "plugin_commands": len(plugin_commands),
        "argument_specs": argument_specs,
        "dynamic_providers": dynamic_providers,
        "has_tool_runs": "tool-runs" in command_names,
        "has_activity": "activity" in command_names,
        "has_mode_arguments": _command_has_arguments(commands, "mode", child="set"),
    }


def _query_health_snapshot(runtime: AppRuntime) -> dict[str, Any]:
    query_service = getattr(runtime.conversation_service, "queries", None)
    return {
        "conversation_query_service": query_service is not None,
        "tool_runs_query": all(
            hasattr(query_service, name)
            for name in ("recent_tool_runs", "tool_run_detail", "tool_run_summary")
        ) if query_service is not None else False,
    }


def _execution_health_snapshot(settings: Settings) -> dict[str, Any]:
    from personal_agent.security.session import security_settings_snapshot

    return security_settings_snapshot(settings)


def _llm_cache_health_snapshot(settings: Settings, turns: dict[str, Any]) -> dict[str, Any]:
    from personal_agent.llm.provider import provider_registry

    try:
        provider = provider_registry.get(settings.llm_provider, settings)
        capability = provider.cache_capability()
    except Exception as exc:
        return {
            "provider": getattr(settings, "llm_provider", ""),
            "model": getattr(settings, "llm_model", ""),
            "strategy": "none",
            "supports_usage": False,
            "usage_fields": {},
            "cacheable_blocks": [],
            "last_usage": {},
            "last_diagnostics": {},
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "provider": provider.name,
        "model": provider.model,
        "strategy": capability["strategy"],
        "supports_usage": capability["supports_usage"],
        "usage_fields": capability["usage_fields"],
        "cacheable_blocks": capability["cacheable_blocks"],
        "last_usage": {
            "cache_hit_tokens": int(turns.get("last_cache_hit_tokens") or 0),
            "cache_miss_tokens": int(turns.get("last_cache_miss_tokens") or 0),
            "cache_write_tokens": int(turns.get("last_cache_write_tokens") or 0),
            "cache_read_tokens": int(turns.get("last_cache_read_tokens") or 0),
            "cache_hit_rate": float(turns.get("last_cache_hit_rate") or 0.0),
        },
        "last_diagnostics": dict(turns.get("last_cache_diagnostics") or {}),
        "error": "",
    }


def _count_argument_specs(commands: list[dict[str, Any]]) -> int:
    count = 0
    for item in commands:
        if not isinstance(item, dict):
            continue
        count += len(item.get("arguments") or [])
        count += _count_argument_specs(list(item.get("children") or []))
    return count


def _dynamic_providers(commands: list[dict[str, Any]]) -> set[str]:
    providers: set[str] = set()
    for item in commands:
        if not isinstance(item, dict):
            continue
        for argument in item.get("arguments") or []:
            if isinstance(argument, dict) and argument.get("kind") == "dynamic":
                provider = str(argument.get("provider") or "")
                if provider:
                    providers.add(provider)
        providers.update(_dynamic_providers(list(item.get("children") or [])))
    return providers


def _command_has_arguments(
    commands: list[dict[str, Any]],
    name: str,
    *,
    child: str = "",
) -> bool:
    for item in commands:
        if not isinstance(item, dict) or item.get("name") != name:
            continue
        if child:
            for child_item in item.get("children") or []:
                if isinstance(child_item, dict) and child_item.get("name") == child:
                    return bool(child_item.get("arguments"))
            return False
        return bool(item.get("arguments"))
    return False


async def start_mcp_manager(settings: Settings, plugin_manager: PluginManager):
    mcp_servers = list(settings.mcp_servers)
    for cfg in plugin_manager.get_mcp_servers():
        if isinstance(cfg, dict):
            mcp_servers.append(cfg)
        else:
            mcp_servers.append(cfg)
    if not settings.mcp_enabled or not mcp_servers:
        return None

    from personal_agent.mcp.manager import MCPManager

    env_names = {
        str(env_name)
        for server in mcp_servers
        for env_name in _mcp_headers_env(server).values()
        if str(env_name)
    }
    env_values = {name: settings.get_env(name) for name in env_names}
    mcp_work_dir = settings.agent_data_dir / "mcp"
    mcp_work_dir.mkdir(parents=True, exist_ok=True)
    manager = MCPManager(
        mcp_servers,
        env_values=env_values,
        process_backend=settings.process_sandbox_backend,
        sandbox_roots=list(settings.sandbox_roots),
        work_dir=mcp_work_dir,
    )
    await manager.start()
    return manager


def _mcp_headers_env(server: Any) -> dict[str, str]:
    value = server.get("headers_env", {}) if isinstance(server, dict) else getattr(server, "headers_env", {})
    if not isinstance(value, dict):
        return {}
    return {str(header): str(env_name) for header, env_name in value.items()}


async def create_memory_manager(
    settings: Settings,
    plugin_manager: PluginManager,
    system_dir: Path,
    data_dir: Path,
) -> MemoryManager:
    from personal_agent.memory.archive import MemoryArchive
    from personal_agent.memory.config import resolve_memory_context
    from personal_agent.memory.external import ExternalMemoryRouter, FallbackMemoryProvider
    from personal_agent.memory.internal import InternalMemoryService, InternalMemoryStore
    from personal_agent.memory.internal.consolidator import InternalMemoryConsolidator
    from personal_agent.memory.llm import MemoryLLMFacade
    from personal_agent.memory.provider_registry import memory_provider_registry

    context = resolve_memory_context(settings)
    archive = MemoryArchive(data_dir / "memory" / "memory.db")
    await archive.initialize()
    internal = InternalMemoryStore(system_dir, profile_map=settings.profile_map)
    memory_llm = MemoryLLMFacade(context.llm)
    fallback = FallbackMemoryProvider(archive, memory_llm)
    router = ExternalMemoryRouter(
        context=context,
        archive=archive,
        fallback=fallback,
        registry=memory_provider_registry,
    )
    await router.initialize()
    internal_service = InternalMemoryService(
        archive=archive,
        store=internal,
        consolidator=InternalMemoryConsolidator(memory_llm),
        buffer_limit=context.review.internal_buffer_limit,
    )
    logger.info(
        "External memory: requested=%s effective=%s",
        router.requested_provider,
        router.effective_provider,
    )
    manager = MemoryManager(
        internal=internal,
        router=router,
        archive=archive,
        internal_service=internal_service,
        internal_turn_interval=context.review.internal_turn_interval,
    )
    from personal_agent.memory.tools import set_memory_manager

    set_memory_manager(manager)
    return manager


def ensure_system_files(system_dir: Path) -> None:
    """Create default system prompt files if they do not exist."""
    system_dir.mkdir(parents=True, exist_ok=True)
    defaults = {
        "SOUL.md": "# 角色与人格\n\n- 你是一个智能个人助理，名字叫小助\n- 你擅长编程、问题分析和技术支持\n- 回复风格：简洁、直接、有条理\n",
        "AGENT.md": "# 行为规则\n\n- 涉及实时数据时必须调用工具，不要凭记忆回答\n- 使用中文回复\n- 工具返回的结果要如实转述，不要编造\n- 优先使用工具而不是猜测\n",
        "USER.md": "# 用户偏好\n\n- 用户偏好从这里开始记录\n",
        "MEMORY.md": "# 用户画像\n\n- 从这里开始记录用户的重要信息\n",
        "RELATIONSHIP.md": "# 关系状态\n\n<!-- 关系记忆需要更高置信度或人工确认 -->\n",
    }
    for name, content in defaults.items():
        path = system_dir / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")
