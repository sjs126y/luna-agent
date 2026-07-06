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
            "plugins": len(self.plugin_manager.list_plugins()),
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

        with boot_report.step("plugins.discover"):
            plugin_manager = PluginManager(settings)
            plugin_manager.discover()
        with boot_report.step("plugins.load_enabled"):
            plugin_manager.load_enabled()
        with boot_report.step("plugins.configure"):
            await plugin_manager.invoke_hook("configure", settings=settings)

        with boot_report.step("sandbox"):
            init_sandbox(settings.sandbox_roots, settings.sandbox_blocked)
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
            memory_review_service = MemoryReviewService()
        with boot_report.step("conversation"):
            conversation_service = ConversationService(
                settings=settings,
                plugin_manager=plugin_manager,
                session_store=session_store,
                compression_chain=compression_chain,
                memory_manager=memory_manager,
            )

        with boot_report.step("runtime"):
            return AppRuntime(
                settings=settings,
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


async def start_mcp_manager(settings: Settings, plugin_manager: PluginManager):
    mcp_servers = list(settings.mcp_servers)
    for cfg in plugin_manager.get_mcp_servers():
        if isinstance(cfg, dict):
            mcp_servers.append(cfg)
        else:
            mcp_servers.append({
                "name": getattr(cfg, "name", ""),
                "command": getattr(cfg, "command", ""),
                "args": getattr(cfg, "args", []),
                "env": getattr(cfg, "env", {}),
                "enabled": getattr(cfg, "enabled", True),
            })
    if not settings.mcp_enabled or not mcp_servers:
        return None

    from personal_agent.mcp.manager import MCPManager

    manager = MCPManager(mcp_servers)
    await manager.start()
    return manager


async def create_memory_manager(
    settings: Settings,
    plugin_manager: PluginManager,
    system_dir: Path,
    data_dir: Path,
) -> MemoryManager:
    builtin_memory = await plugin_manager.invoke_hook(
        "create_builtin_memory_provider",
        system_dir=system_dir,
    )
    if builtin_memory is None:
        raise RuntimeError("No built-in memory provider registered")

    external_memory = None
    if settings.memory_external_provider == "embedding":
        try:
            external_memory = await plugin_manager.invoke_hook(
                "create_external_memory_provider",
                settings=settings,
                data_dir=data_dir / "memory",
            )
            if external_memory is not None:
                logger.info("External memory: embedding (BAAI/bge-small-zh-v1.5)")
        except Exception as exc:
            logger.warning("External memory provider unavailable, falling back to file memory: %s", exc)
            external_memory = None

    return MemoryManager(builtin=builtin_memory, external=external_memory)


def ensure_system_files(system_dir: Path) -> None:
    """Create default system prompt files if they do not exist."""
    system_dir.mkdir(parents=True, exist_ok=True)
    defaults = {
        "SOUL.md": "# 角色与人格\n\n- 你是一个智能个人助理，名字叫小助\n- 你擅长编程、问题分析和技术支持\n- 回复风格：简洁、直接、有条理\n",
        "AGENT.md": "# 行为规则\n\n- 涉及实时数据时必须调用工具，不要凭记忆回答\n- 使用中文回复\n- 工具返回的结果要如实转述，不要编造\n- 优先使用工具而不是猜测\n",
        "USER.md": "# 用户偏好\n\n- 用户偏好从这里开始记录\n",
        "MEMORY.md": "# 用户画像\n\n- 从这里开始记录用户的重要信息\n",
    }
    for name, content in defaults.items():
        path = system_dir / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")
