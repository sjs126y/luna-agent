"""Entry point — bootstrap and run the agent system."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from personal_agent.config import Settings
from personal_agent.runtime import create_app_runtime, ensure_system_files

logger = logging.getLogger("personal_agent")


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

    from personal_agent.trace import TraceFilter
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
    logger.info("Personal Agent starting...")

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

    async def _norm_message(event):
        """Normalize inbound text: strip whitespace, collapse blank lines."""
        event.text = event.text.strip()
        return event

    async def _truncate_response(text, source):
        """Truncate very long responses (>4000 chars) with a note."""
        if len(text) > 4000:
            return text[:4000] + f"\n\n…(截断 {len(text) - 4000} 字符)"
        return text

    async def _log_usage(response, usage):
        """Log per-call token usage for observability."""
        logger.info("LLM usage: in=%d out=%d total=%d",
                     usage.get("input_tokens", 0),
                     usage.get("output_tokens", 0),
                     usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
        return response

    gateway.hooks.on_message_received.append(_norm_message)
    gateway.hooks.on_before_send.append(_truncate_response)

    # ── 8. Start ───────────────────────────────────────
    await runtime.start_gateway(system_prompt_template=system_prompt)

    # ── 9. Wait for shutdown ──────────────────────────
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(runtime)))
        except NotImplementedError:
            pass

    logger.info("Personal Agent running. Press Ctrl+C to stop.")
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
    """CLI entry: python -m personal_agent"""
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        _run_cli(sys.argv[2] if len(sys.argv) > 2 else "Hello")
    elif len(sys.argv) > 1 and sys.argv[1] == "--wechat-login":
        _run_wechat_login()
    elif len(sys.argv) > 1 and sys.argv[1] == "--ingest":
        _run_ingest(sys.argv[2] if len(sys.argv) > 2 else "")
    elif len(sys.argv) > 1:
        from personal_agent.cli import run
        run()
    else:
        try:
            asyncio.run(boot())
        except KeyboardInterrupt:
            pass


def _run_wechat_login() -> None:
    """CLI: QR login for WeChat."""
    import asyncio

    settings = Settings()

    async def _run():
        from personal_agent.plugins.manager import PluginManager

        plugin_manager = PluginManager(settings)
        plugin_manager.discover()
        plugin_manager.load_plugin("platforms/wechat")
        result = await plugin_manager.invoke_hook("wechat_qr_login", settings=settings)
        if result is None:
            print("WeChat login plugin is unavailable.")

    asyncio.run(_run())


def _run_ingest(file_path: str) -> None:
    """CLI: ingest a file into external memory."""
    import asyncio
    from pathlib import Path

    async def _run():
        path = Path(file_path)
        if not path.exists():
            print(f"Error: file not found: {file_path}")
            return

        settings = Settings()
        runtime = await create_app_runtime(settings)
        try:
            ext = await runtime.plugin_manager.invoke_hook(
                "create_external_memory_provider",
                settings=runtime.settings,
                data_dir=runtime.data_dir / "memory",
                force=True,
            )
            if ext is None:
                print("Error: external embedding memory provider is unavailable.")
                return
            count = await ext.ingest_file(str(path.resolve()))
            print(f"Ingested {path.name}: {count} chunks stored.")
        except ValueError as e:
            print(f"Error: {e}")
        finally:
            await runtime.close()

    asyncio.run(_run())


def _run_cli(message: str) -> None:
    """Compatibility one-shot CLI entry."""
    from personal_agent.cli_chat import run_cli_once_sync

    run_cli_once_sync(message)


if __name__ == "__main__":
    main()
