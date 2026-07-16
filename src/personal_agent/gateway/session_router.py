"""Compatibility import for the shared session directory."""

from personal_agent.conversation.session_directory import SessionDirectory


class GatewaySessionRouter(SessionDirectory):
    """Deprecated name retained while Gateway call sites migrate."""
