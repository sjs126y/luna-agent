"""Shared session routing and delivery bindings."""

from __future__ import annotations

from dataclasses import dataclass
import time

from luna_agent.models.messages import SessionSource


@dataclass(frozen=True, slots=True)
class SessionBinding:
    session_key: str
    base_key: str
    source: SessionSource
    updated_at: float


class SessionDirectory:
    """Maps platform sources to logical sessions and back again."""

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self.overrides: dict[str, str] = dict(overrides or {})
        self._bindings: dict[str, SessionBinding] = {}

    def base_key(self, source: SessionSource) -> str:
        return f"{source.platform}:{source.chat_id}:{source.user_id}"

    def active_key(self, source: SessionSource) -> str:
        base_key = self.base_key(source)
        session_key = self.overrides.get(base_key, base_key)
        self.bind(session_key, source, base_key=base_key)
        return session_key

    def named_key(self, source: SessionSource, name: str) -> str:
        cleaned = str(name or "").strip()
        if not cleaned:
            raise ValueError("session name is required")
        return f"{source.platform}:{cleaned}:{source.user_id}"

    def bind(
        self,
        session_key: str,
        source: SessionSource,
        *,
        base_key: str = "",
    ) -> SessionBinding:
        key = str(session_key or "").strip()
        if not key:
            raise ValueError("session_key is required")
        binding = SessionBinding(
            session_key=key,
            base_key=base_key or self.base_key(source),
            source=_copy_source(source),
            updated_at=time.time(),
        )
        self._bindings[key] = binding
        return binding

    def resolve(self, session_key: str) -> SessionBinding | None:
        return self._bindings.get(str(session_key or "").strip())

    def restore(self, entries, *, predicate=None) -> int:
        """Restore reverse delivery routes from persisted session metadata."""
        restored = 0
        for entry in entries:
            session_key = str(getattr(entry, "session_key", "") or "").strip()
            platform = str(getattr(entry, "platform", "") or "").strip()
            chat_id = str(getattr(entry, "chat_id", "") or "").strip()
            user_id = str(getattr(entry, "user_id", "") or "").strip()
            if not session_key or not platform or not chat_id:
                continue
            source = SessionSource(
                platform=platform,
                user_id=user_id,
                user_name=str(getattr(entry, "user_name", "") or ""),
                chat_id=chat_id,
                chat_type=str(getattr(entry, "chat_type", "dm") or "dm"),
            )
            if predicate is not None and not predicate(source):
                continue
            self.bind(session_key, source)
            restored += 1
        return restored

    def switch(self, source: SessionSource, name: str) -> str:
        new_key = self.named_key(source, name)
        base_key = self.base_key(source)
        self.overrides[base_key] = new_key
        self.bind(new_key, source, base_key=base_key)
        return new_key

    def rename(self, source: SessionSource, old_key: str, new_key: str) -> None:
        base_key = self.base_key(source)
        if old_key == base_key:
            self.overrides[base_key] = new_key
        else:
            for key, value in list(self.overrides.items()):
                if value == old_key:
                    self.overrides[key] = new_key
        previous = self._bindings.pop(old_key, None)
        self.bind(new_key, previous.source if previous else source, base_key=base_key)

    def delete(self, source: SessionSource, target_key: str) -> str:
        base_key = self.base_key(source)
        for key, value in list(self.overrides.items()):
            if key == target_key or value == target_key:
                del self.overrides[key]
        self._bindings.pop(target_key, None)
        self.bind(base_key, source, base_key=base_key)
        return base_key

    def current_for_list(self, source: SessionSource) -> str:
        return self.active_key(source)

    def snapshot(self) -> dict:
        return {
            "overrides": dict(self.overrides),
            "bindings": [
                {
                    "session_key": item.session_key,
                    "base_key": item.base_key,
                    "platform": item.source.platform,
                    "user_id": item.source.user_id,
                    "chat_id": item.source.chat_id,
                    "updated_at": item.updated_at,
                }
                for item in self._bindings.values()
            ],
        }


def _copy_source(source: SessionSource) -> SessionSource:
    return SessionSource(
        platform=str(getattr(source, "platform", "") or ""),
        user_id=str(getattr(source, "user_id", "") or ""),
        user_name=str(getattr(source, "user_name", "") or ""),
        chat_id=str(getattr(source, "chat_id", "") or ""),
        chat_type=str(getattr(source, "chat_type", "dm") or "dm"),
        thread_id=getattr(source, "thread_id", None),
    )
