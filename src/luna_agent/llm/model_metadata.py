"""Optional provider metadata enrichment for models missing from the static catalog."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import time
from typing import Any

import httpx

from luna_agent.llm.provider import ProviderProfile

logger = logging.getLogger(__name__)

OPENROUTER_CACHE_TTL_SECONDS = 24 * 60 * 60
OPENROUTER_METADATA_TIMEOUT_SECONDS = 3.0
MetadataFetch = Callable[[ProviderProfile], Awaitable[dict[str, Any] | None]]


async def enrich_openrouter_profile(
    profile: ProviderProfile,
    data_dir: str | Path,
    *,
    fetch: MetadataFetch | None = None,
    now: float | None = None,
) -> bool:
    """Fill unknown OpenRouter model limits without making metadata availability mandatory."""
    if profile.name != "openrouter" or profile.capability_source != "conservative-fallback":
        return False

    timestamp = time.time() if now is None else float(now)
    cache_path = Path(data_dir) / "cache" / "model_capabilities.json"
    cache = _load_cache(cache_path)
    cache_key = f"openrouter:{profile.model}"
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and timestamp - float(cached.get("fetched_at", 0) or 0) < OPENROUTER_CACHE_TTL_SECONDS:
        return _apply_metadata(profile, cached)

    try:
        metadata = await (fetch or _fetch_openrouter_metadata)(profile)
    except Exception as exc:
        logger.debug("OpenRouter model metadata unavailable for %s: %s", profile.model, exc)
        return False
    if not metadata:
        return False

    entry = {
        "context_window": int(metadata.get("context_window", 0) or 0),
        "max_output_tokens": int(metadata.get("max_output_tokens", 0) or 0),
        "fetched_at": timestamp,
        "verified_at": datetime.fromtimestamp(timestamp, UTC).date().isoformat(),
    }
    if entry["context_window"] <= 0:
        return False
    cache[cache_key] = entry
    _write_cache(cache_path, cache)
    return _apply_metadata(profile, entry)


async def _fetch_openrouter_metadata(profile: ProviderProfile) -> dict[str, Any] | None:
    base_url = profile.base_url.rstrip("/")
    headers = dict(profile.extra_headers)
    if profile.api_key:
        headers["Authorization"] = f"Bearer {profile.api_key}"
    timeout = httpx.Timeout(OPENROUTER_METADATA_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(f"{base_url}/models", headers=headers)
        response.raise_for_status()
        payload = response.json()
    models = payload.get("data", []) if isinstance(payload, dict) else []
    for item in models:
        if not isinstance(item, dict) or str(item.get("id") or "") != profile.model:
            continue
        top_provider = item.get("top_provider") if isinstance(item.get("top_provider"), dict) else {}
        return {
            "context_window": item.get("context_length") or top_provider.get("context_length"),
            "max_output_tokens": (
                top_provider.get("max_completion_tokens")
                or item.get("max_completion_tokens")
            ),
        }
    return None


def _apply_metadata(profile: ProviderProfile, metadata: dict[str, Any]) -> bool:
    context_window = int(metadata.get("context_window", 0) or 0)
    if context_window <= 0:
        return False
    output_limit = int(metadata.get("max_output_tokens", 0) or 0)
    configured_context = profile.context_window
    configured_output = profile.max_tokens

    profile.model_context_limit = context_window
    profile.context_window = min(configured_context, context_window) if profile.context_source == "configured" else context_window
    profile.context_clamped = profile.context_source == "configured" and configured_context > context_window
    if profile.context_source != "configured":
        profile.context_source = "openrouter-model-api"
    profile.model_max_output_tokens = output_limit
    if output_limit > 0:
        profile.max_tokens = min(configured_output, output_limit)
        profile.output_clamped = configured_output > output_limit
        profile.output_source = "configured-clamped" if profile.output_clamped else "configured"
    profile.capability_source = "openrouter-model-api"
    profile.capability_verified_at = str(metadata.get("verified_at") or "")
    return True


def _load_cache(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_cache(path: Path, payload: dict[str, dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        logger.debug("Unable to cache OpenRouter model metadata: %s", exc)
