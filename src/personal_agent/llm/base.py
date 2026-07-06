"""Base transport abstraction — all Provider transports implement this."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
import hashlib
import json
from typing import Any

from personal_agent.models.messages import NormalizedResponse

# Incremental delta callback: (kind, chunk) where kind is "text" | "thinking".
# Transports await it while parsing a stream so callers can render token-by-token.
# Optional everywhere — when omitted, parsing collects the full response as before.
DeltaCallback = Callable[[str, str], Awaitable[None]]


class BaseTransport(ABC):
    """Strategy: handle protocol differences (Anthropic / OpenAI / etc.).
    The Agent loop only consumes NormalizedResponse.
    """

    @abstractmethod
    def build_request(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int,
    ) -> dict:
        """Build the API request body in the target format."""
        ...

    @abstractmethod
    async def parse_stream(
        self,
        stream: AsyncIterator[bytes],
        on_delta: DeltaCallback | None = None,
    ) -> NormalizedResponse:
        """Parse streaming SSE events into a unified NormalizedResponse.

        If on_delta is provided, it is called with ("text", chunk) and
        ("thinking", chunk) as incremental content arrives.
        """
        ...

    @abstractmethod
    def convert_tool_definitions(self, tools: list[dict]) -> list[dict]:
        """Convert internal tool schemas to target API format."""
        ...

    @abstractmethod
    def convert_messages(self, messages: list[dict]) -> list[dict]:
        """Convert internal message format to target API format."""
        ...

    async def close(self) -> None:
        """Optional cleanup."""
        pass

    def cache_strategy(self) -> str:
        provider = getattr(self, "_provider", None)
        return str(getattr(provider, "cache_strategy", "none") or "none")

    def normalize_usage(self, raw_usage: dict[str, Any] | None) -> dict[str, Any]:
        """Normalize provider usage fields, including prompt-cache counters."""
        raw_usage = raw_usage or {}
        input_tokens = _as_int(_first_present(raw_usage, ("input_tokens", "prompt_tokens")))
        output_tokens = _as_int(_first_present(raw_usage, ("output_tokens", "completion_tokens")))

        provider = getattr(self, "_provider", None)
        field_map = dict(getattr(provider, "cache_usage_fields", {}) or {})
        mapped = {
            key: _as_int(_get_path(raw_usage, path))
            for key, path in field_map.items()
            if path
        }

        nested_cached = _as_int(_get_path(raw_usage, "prompt_tokens_details.cached_tokens"))
        cache_hit_tokens = _as_int(_first_present(
            {
                **raw_usage,
                **mapped,
                "prompt_tokens_details.cached_tokens": nested_cached,
            },
            (
                "cache_hit_tokens",
                "prompt_cache_hit_tokens",
                "cache_read_input_tokens",
                "prompt_tokens_details.cached_tokens",
            ),
        ))
        cache_miss_tokens = _as_int(_first_present(
            {**raw_usage, **mapped},
            ("cache_miss_tokens", "prompt_cache_miss_tokens"),
        ))
        cache_write_tokens = _as_int(_first_present(
            {**raw_usage, **mapped},
            ("cache_write_tokens", "cache_creation_input_tokens"),
        ))
        cache_read_tokens = _as_int(_first_present(
            {**raw_usage, **mapped},
            ("cache_read_tokens", "cache_read_input_tokens"),
        ))

        if cache_read_tokens == 0 and cache_hit_tokens:
            cache_read_tokens = cache_hit_tokens
        if cache_hit_tokens == 0 and cache_read_tokens:
            cache_hit_tokens = cache_read_tokens
        if cache_miss_tokens == 0 and input_tokens and cache_hit_tokens <= input_tokens:
            cache_miss_tokens = input_tokens - cache_hit_tokens

        cache_hit_rate = (cache_hit_tokens / input_tokens) if input_tokens else 0.0
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_hit_tokens": cache_hit_tokens,
            "cache_miss_tokens": cache_miss_tokens,
            "cache_write_tokens": cache_write_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_hit_rate": cache_hit_rate,
        }

    def cache_diagnostics(self, body: dict[str, Any]) -> dict[str, Any]:
        """Return stable request fingerprints for cache debugging."""
        messages = list(body.get("messages") or [])
        tools = list(body.get("tools") or [])
        message_prefix = messages[:-1] if messages else []
        system = body.get("system", "")
        return {
            "cache_strategy": self.cache_strategy(),
            "system_hash": stable_request_hash(system),
            "tools_hash": stable_request_hash(tools),
            "message_prefix_hash": stable_request_hash(message_prefix),
            "stable_prefix_hash": stable_request_hash({
                "system": system,
                "tools": tools,
                "message_prefix": message_prefix,
            }),
            "message_count": len(messages),
            "tool_count": len(tools),
        }

    def remember_cache_diagnostics(self, body: dict[str, Any]) -> dict[str, Any]:
        diagnostics = self.cache_diagnostics(body)
        self._last_cache_diagnostics = diagnostics
        return diagnostics

    def last_cache_diagnostics(self) -> dict[str, Any]:
        return dict(getattr(self, "_last_cache_diagnostics", {}) or {})


def stable_request_hash(value: Any, *, length: int = 16) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:length]


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _get_path(mapping: dict[str, Any], path: str) -> Any:
    current: Any = mapping
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
