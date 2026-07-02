"""Gateway session key routing."""

from __future__ import annotations


class GatewaySessionRouter:
    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self.overrides: dict[str, str] = dict(overrides or {})

    def base_key(self, source) -> str:
        return f"{source.platform}:{source.chat_id}:{source.user_id}"

    def active_key(self, source) -> str:
        base_key = self.base_key(source)
        return self.overrides.get(base_key, base_key)

    def named_key(self, source, name: str) -> str:
        return f"{source.platform}:{name}:{source.user_id}"

    def switch(self, source, name: str) -> str:
        new_key = self.named_key(source, name)
        self.overrides[self.base_key(source)] = new_key
        return new_key

    def rename(self, source, old_key: str, new_key: str) -> None:
        base_key = self.base_key(source)
        if old_key == base_key:
            self.overrides[base_key] = new_key
            return
        for key, value in list(self.overrides.items()):
            if value == old_key:
                self.overrides[key] = new_key

    def delete(self, source, target_key: str) -> str:
        base_key = self.base_key(source)
        for key, value in list(self.overrides.items()):
            if key == target_key or value == target_key:
                del self.overrides[key]
        return base_key

    def current_for_list(self, source) -> str:
        return self.active_key(source)
