"""Optional OpenRouter model metadata enrichment."""

import json

import pytest

from luna_agent.llm.model_metadata import enrich_openrouter_profile
from luna_agent.llm.provider import ProviderProfile


def _profile() -> ProviderProfile:
    return ProviderProfile(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="secret",
        model="vendor/new-model",
        max_tokens=20_000,
        context_window=256_000,
        model_context_limit=256_000,
        context_source="conservative-fallback",
        capability_source="conservative-fallback",
    )


@pytest.mark.asyncio
async def test_openrouter_metadata_enriches_unknown_model_and_caches_result(tmp_path):
    calls = 0

    async def fetch(_profile):
        nonlocal calls
        calls += 1
        return {"context_window": 131_072, "max_output_tokens": 8192}

    profile = _profile()
    assert await enrich_openrouter_profile(profile, tmp_path, fetch=fetch, now=1000) is True

    assert profile.context_window == 131_072
    assert profile.model_context_limit == 131_072
    assert profile.max_tokens == 8192
    assert profile.output_clamped is True
    assert profile.capability_source == "openrouter-model-api"
    assert calls == 1
    cached = json.loads((tmp_path / "cache" / "model_capabilities.json").read_text())
    assert cached["openrouter:vendor/new-model"]["context_window"] == 131_072

    second = _profile()
    assert await enrich_openrouter_profile(second, tmp_path, fetch=fetch, now=1001) is True
    assert second.context_window == 131_072
    assert calls == 1


@pytest.mark.asyncio
async def test_openrouter_metadata_failure_keeps_conservative_profile(tmp_path):
    async def fetch(_profile):
        raise TimeoutError("slow")

    profile = _profile()
    assert await enrich_openrouter_profile(profile, tmp_path, fetch=fetch) is False
    assert profile.context_window == 256_000
    assert profile.capability_source == "conservative-fallback"
