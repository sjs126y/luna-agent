"""Shared application runtime bootstrap."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from personal_agent.config import Settings
from personal_agent.db.database import Database
from personal_agent.gateway.compression_chain import CompressionChain
from personal_agent.gateway.session_store import SessionStore
from personal_agent.memory.manager import MemoryManager
from personal_agent.memory.review import MemoryReviewService
from personal_agent.plugins.manager import PluginManager
from personal_agent.tools.audit import set_audit_path
from personal_agent.tools.sandbox import init_sandbox
from personal_agent.conversation import ConversationService

logger = logging.getLogger(__name__)


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
        mcp_health = _mcp_health_snapshot(
            self.mcp_manager,
            enabled=bool(self.settings.mcp_enabled),
            configured_count=len(self.settings.mcp_servers),
        )
        return {
            "data_dir": str(self.data_dir),
            "db_open": getattr(self.db, "_conn", None) is not None,
            "mcp_enabled": bool(self.settings.mcp_enabled),
            "mcp_running": self.mcp_manager is not None,
            "mcp": mcp_health,
            "gateway_created": self.gateway is not None,
            "gateway_running": bool(self.gateway is not None and self.gateway_started),
            "gateway": self.gateway.health_snapshot() if self.gateway is not None else {},
            "plugins": len(self.plugin_manager.list_plugins()),
            "cached_agents": len(self.conversation_service.agent_cache),
            "closed": self.closed,
        }


async def create_app_runtime(settings: Settings | None = None) -> AppRuntime:
    settings = settings or Settings()
    data_dir = settings.agent_data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    plugin_manager = PluginManager(settings)
    plugin_manager.discover()
    plugin_manager.load_enabled()
    await plugin_manager.invoke_hook("configure", settings=settings)

    init_sandbox(settings.sandbox_roots, settings.sandbox_blocked)
    if settings.audit_enabled:
        set_audit_path(data_dir / "audit.log")

    mcp_manager = await start_mcp_manager(settings, plugin_manager)
    db: Database | None = None
    try:
        db = Database(data_dir / "state.db")
        await db.initialize()

        compression_chain = CompressionChain(data_dir / "compression_chain.json")
        compression_chain.load()
        session_store = SessionStore(db, data_dir, chain=compression_chain)
        await session_store.initialize()
        await session_store.expire_sessions(settings.session_expire_days)

        system_dir = data_dir / "system"
        ensure_system_files(system_dir)
        memory_manager = await create_memory_manager(settings, plugin_manager, system_dir, data_dir)
        memory_review_service = MemoryReviewService()
        conversation_service = ConversationService(
            settings=settings,
            plugin_manager=plugin_manager,
            session_store=session_store,
            compression_chain=compression_chain,
            memory_manager=memory_manager,
        )
    except Exception:
        if mcp_manager is not None:
            await mcp_manager.stop()
        if db is not None:
            await db.close()
        raise

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
    )


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
