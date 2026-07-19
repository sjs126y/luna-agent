"""Text sanitization for API and persistence boundaries."""

from __future__ import annotations

from typing import Any


def clean_text(value: Any) -> str:
    """Return UTF-8 encodable text, replacing invalid surrogate code points."""
    text = "" if value is None else str(value)
    return text.encode("utf-8", errors="replace").decode("utf-8")


def clean_payload(value: Any) -> Any:
    """Recursively clean strings in JSON-like data structures."""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [clean_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(clean_payload(item) for item in value)
    if isinstance(value, dict):
        return {
            clean_text(key) if isinstance(key, str) else key: clean_payload(item)
            for key, item in value.items()
        }
    return value
