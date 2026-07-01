"""File-backed memory provider plugin."""


def _configure(settings=None, **kwargs) -> None:
    if settings is None:
        return

    from personal_agent.plugins.builtin.memory.file.provider import set_profile_map, set_system_dir

    set_profile_map(settings.profile_map)
    set_system_dir(settings.agent_data_dir / "system")


def _set_current_session(session_key: str | None = None, **kwargs) -> None:
    if not session_key:
        return

    from personal_agent.plugins.builtin.memory.file.provider import set_current_session

    set_current_session(session_key)


def _create_builtin_provider(system_dir=None, **kwargs):
    if system_dir is None:
        return None

    from personal_agent.plugins.builtin.memory.file.provider import FileMemoryProvider

    return FileMemoryProvider(system_dir)


def register(ctx) -> None:
    ctx.register_hook("configure", _configure, priority=10)
    ctx.register_hook("on_session_selected", _set_current_session, priority=10)
    ctx.register_hook("create_builtin_memory_provider", _create_builtin_provider, priority=10)
