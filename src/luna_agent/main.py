"""Entry point — bootstrap and run the agent system."""

from __future__ import annotations

import asyncio
import logging
import signal

from luna_agent.config import Settings
from luna_agent.runtime import create_app_runtime, ensure_system_files

logger = logging.getLogger("luna_agent")


def setup_logging(level: str = "INFO") -> None:
    class ColorFormatter(logging.Formatter):
        """Colorize log level + highlight key events."""

        COLORS = {
            "DEBUG": "\033[90m",     # grey
            "INFO": "\033[37m",      # white
            "WARNING": "\033[93m",   # yellow
            "ERROR": "\033[91m",     # red
            "CRITICAL": "\033[91;1m",  # bold red
        }
        RESET = "\033[0m"
        GREEN = "\033[92m"
        CYAN = "\033[96m"

        def format(self, record):
            msg = super().format(record)
            color = self.COLORS.get(record.levelname, "")
            if color:
                msg = msg.replace(f"[{record.levelname}]", f"{color}[{record.levelname}]{self.RESET}", 1)

            # Highlight key events
            if record.levelname in ("WARNING", "ERROR"):
                return msg
            text = record.getMessage()
            if "connected" in text and ("Platform" in text or "connected" in text):
                msg = msg.replace(text, f"{self.GREEN}{text}{self.RESET}")
            elif "inbound" in text and "user=" in text:
                msg = msg.replace(text, f"{self.CYAN}{text}{self.RESET}")
            elif "Auth:" in text:
                msg = msg.replace(text, f"{self.CYAN}{text}{self.RESET}")
            elif "HTTP Request:" in text and "200" in text:
                msg = msg.replace(text, f"{self.GREEN}{text}{self.RESET}")
            elif "HTTP Request:" in text and ("4" in text or "5" in text):
                msg = msg.replace(text, f"{self.COLORS['ERROR']}{text}{self.RESET}")
            return msg

    from luna_agent.trace import TraceFilter
    handler = logging.StreamHandler()
    handler.setFormatter(ColorFormatter(
        "%(asctime)s [%(levelname)s] [%(trace_id)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    handler.addFilter(TraceFilter())
    logging.root.handlers = []
    logging.root.addHandler(handler)
    logging.root.setLevel(getattr(logging, level.upper(), logging.INFO))


_ensure_system_files = ensure_system_files


async def boot() -> None:
    # ── 1. Config ─────────────────────────────────────
    settings = Settings()
    setup_logging(settings.log_level)
    logger.info("Luna Agent starting...")

    runtime = await create_app_runtime(settings)

    # ── 7. Gateway ─────────────────────────────────────
    system_prompt = (
        "你是一个智能个人助理。你有以下能力：\n"
        "- 使用工具获取实时信息（日期、搜索、网页抓取等）\n"
        "- 执行计算、管理待办事项、读写文件\n"
        "- 管理用户记忆\n\n"
        "重要规则：\n"
        "1. 涉及实时数据（日期、天气、搜索）时，必须调用工具，不要凭记忆回答\n"
        "2. 用户要求计算时，使用 calculator 工具\n"
        "3. 用中文回复，保持简洁有条理\n"
        "4. 工具返回的结果要如实转述，不要编造"
    )
    gateway = runtime.create_gateway(system_prompt_template=system_prompt)

    # ── 7.5. Default hooks — non-restrictive utility hooks ──

    from luna_agent.hooks import (
        GatewayMessageOutcome,
        HookEvent,
        HookSource,
        PreDeliveryOutcome,
    )

    async def _norm_message(event):
        return GatewayMessageOutcome.replace_message(
            text=str(event.payload.get("text") or "").strip()
        )

    async def _truncate_response(event):
        text = str(event.payload.get("text") or "")
        if len(text) > 4000:
            text = text[:4000] + f"\n\n…(截断 {len(text) - 4000} 字符)"
            return PreDeliveryOutcome.replace_text(text)
        return PreDeliveryOutcome()

    runtime.hook_manager.register(
        owner="core.gateway.defaults",
        source=HookSource.CORE,
        event=HookEvent.GATEWAY_MESSAGE_RECEIVED,
        callback=_norm_message,
        name="normalize_text",
        priority=10,
    )
    runtime.hook_manager.register(
        owner="core.gateway.defaults",
        source=HookSource.CORE,
        event=HookEvent.PRE_DELIVERY,
        callback=_truncate_response,
        name="truncate_text",
        priority=10,
    )

    # ── 8. Start ───────────────────────────────────────
    await runtime.start_gateway(system_prompt_template=system_prompt)

    # ── 9. Wait for shutdown ──────────────────────────
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(runtime)))
        except NotImplementedError:
            pass

    logger.info("Luna Agent running. Press Ctrl+C to stop.")
    # Windows: poll with sleep so KeyboardInterrupt can interrupt
    try:
        while not hasattr(gateway, '_shutdown_event') or not gateway._shutdown_event.is_set():
            await asyncio.sleep(1)
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Interrupted, shutting down...")
    finally:
        await runtime.close()


async def _shutdown(runtime) -> None:
    logger.info("Shutting down...")
    await runtime.stop_gateway()


def main() -> None:
    """Compatibility module entry: delegate to the Typer CLI."""
    from luna_agent.cli import run

    run()


if __name__ == "__main__":
    main()
