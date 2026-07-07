"""Image-to-text fallback abstractions and cache helpers."""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import shutil
import time
from typing import Any, Protocol
from types import SimpleNamespace

from personal_agent.attachments.store import ResolvedAttachment
from personal_agent.llm.provider import ProviderProfile, provider_registry
from personal_agent.llm.transport_registry import transport_registry
from personal_agent.models.messages import AttachmentRef
from personal_agent.text_safety import clean_text


VISION_PROMPT_VERSION = 1
DEFAULT_VISION_PROMPT = "请提取图片中的可见文字，并简要描述图片内容。不要编造看不到的信息。"
VisionCallFn = Callable[[ProviderProfile, Any, list[dict], int], Awaitable[str]]


@dataclass(frozen=True)
class ImageTextDescription:
    text: str
    method: str = "unknown"
    provider: str = ""
    model: str = ""
    prompt_version: int = 1
    confidence: str = "unknown"
    cached: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ImageTextDescribeUnavailable(RuntimeError):
    def __init__(self, reason: str = "image_text_describer_unavailable", detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail or reason)


class ImageTextDescriber(Protocol):
    async def describe(self, resolved: ResolvedAttachment, ref: AttachmentRef) -> ImageTextDescription:
        ...


class NullImageTextDescriber:
    async def describe(self, resolved: ResolvedAttachment, ref: AttachmentRef) -> ImageTextDescription:
        raise ImageTextDescribeUnavailable("image_text_describer_unavailable")


class VisionImageTextDescriber:
    def __init__(
        self,
        settings,
        *,
        cache: ImageTextCache | None = None,
        call_fn: VisionCallFn | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.call_fn = call_fn

    async def describe(self, resolved: ResolvedAttachment, ref: AttachmentRef) -> ImageTextDescription:
        provider_name = str(getattr(self.settings, "multimodal_image_text_provider", "") or "").strip()
        if not provider_name:
            raise ImageTextDescribeUnavailable("image_text_describer_unavailable")
        provider = _vision_provider(self.settings, provider_name)
        if not bool(getattr(provider, "supports_image_input", False)):
            raise ImageTextDescribeUnavailable("image_text_provider_not_supported", provider.name)

        prompt = clean_text(str(
            getattr(self.settings, "multimodal_image_text_prompt", "") or DEFAULT_VISION_PROMPT
        ))
        prompt_version = VISION_PROMPT_VERSION
        if self.cache is not None and resolved.sha256:
            cached = self.cache.get(
                sha256=resolved.sha256,
                method="vision",
                provider=provider.name,
                model=provider.model,
                prompt_version=prompt_version,
            )
            if cached is not None:
                return cached

        transport = _vision_transport(provider, self.settings)
        message = {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": _data_url(resolved)}},
            ],
        }
        max_tokens = min(int(getattr(provider, "max_tokens", 4096) or 4096), 2048)
        if self.call_fn is not None:
            text = await self.call_fn(provider, transport, [message], max_tokens)
        else:
            response = await transport.call(
                [message],
                system_prompt="",
                tools=[],
                max_tokens=max_tokens,
                stream=False,
            )
            text = response.text

        text = clean_text(text or "")
        if not text:
            raise ImageTextDescribeUnavailable("image_text_empty")
        description = ImageTextDescription(
            text=text,
            method="vision",
            provider=provider.name,
            model=provider.model,
            prompt_version=prompt_version,
            confidence="unknown",
            cached=False,
            metadata={"prompt_version": prompt_version},
        )
        if self.cache is not None and resolved.sha256:
            self.cache.put(description, sha256=resolved.sha256, source_mime_type=resolved.mime_type)
        return description


class ImageTextCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def get(
        self,
        *,
        sha256: str,
        method: str,
        provider: str = "",
        model: str = "",
        prompt_version: int = 1,
    ) -> ImageTextDescription | None:
        path = self._path(
            sha256=sha256,
            method=method,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
        )
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        text = str(data.get("text") or "")
        if not text:
            return None
        return ImageTextDescription(
            text=text,
            method=str(data.get("method") or method),
            provider=str(data.get("provider") or provider),
            model=str(data.get("model") or model),
            prompt_version=int(data.get("prompt_version") or prompt_version),
            confidence=str(data.get("confidence") or "unknown"),
            cached=True,
            metadata=dict(data.get("metadata") or {}),
        )

    def put(
        self,
        description: ImageTextDescription,
        *,
        sha256: str,
        source_mime_type: str = "",
    ) -> Path:
        path = self._path(
            sha256=sha256,
            method=description.method,
            provider=description.provider,
            model=description.model,
            prompt_version=description.prompt_version,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sha256": sha256,
            "kind": "image_text",
            "method": description.method,
            "provider": description.provider,
            "model": description.model,
            "prompt_version": description.prompt_version,
            "text": description.text,
            "created_at": _now(),
            "source_mime_type": source_mime_type,
            "confidence": description.confidence,
            "metadata": dict(description.metadata),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.move(str(tmp), str(path))
        return path

    def _path(
        self,
        *,
        sha256: str,
        method: str,
        provider: str,
        model: str,
        prompt_version: int,
    ) -> Path:
        key = _cache_key(
            sha256=sha256,
            method=method,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
        )
        return self.root / f"{key}.image_text.json"


def _cache_key(
    *,
    sha256: str,
    method: str,
    provider: str,
    model: str,
    prompt_version: int,
) -> str:
    payload = json.dumps(
        {
            "sha256": sha256,
            "method": method,
            "provider": provider,
            "model": model,
            "prompt_version": prompt_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def build_default_image_text_describer(settings, attachment_store=None) -> ImageTextDescriber:
    mode = str(getattr(settings, "multimodal_image_text_mode", "auto") or "auto").strip().lower()
    if mode == "off":
        return NullImageTextDescriber()
    cache = None
    if bool(getattr(settings, "multimodal_image_text_cache", True)):
        root = getattr(attachment_store, "root", None)
        if root is not None:
            cache = ImageTextCache(Path(root) / "derived")
    provider = str(getattr(settings, "multimodal_image_text_provider", "") or "").strip()
    if mode in {"auto", "vision"} and provider:
        return VisionImageTextDescriber(settings, cache=cache)
    return NullImageTextDescriber()


def _vision_provider(settings, provider_name: str) -> ProviderProfile:
    model = str(getattr(settings, "multimodal_image_text_model", "") or getattr(settings, "llm_model", "") or "")
    base_url = str(getattr(settings, "multimodal_image_text_base_url", "") or "")
    api_key = str(getattr(settings, "multimodal_image_text_api_key", "") or "")
    if not base_url:
        base_url = _default_base_url(provider_name) or str(getattr(settings, "llm_base_url", "") or "")
    if not api_key:
        api_key = str(getattr(settings, "llm_api_key", "") or "")
    if not model:
        model = _default_model(provider_name)
    provider_settings = SimpleNamespace(
        llm_base_url=base_url,
        llm_api_key=api_key,
        llm_model=model,
        llm_max_tokens=min(int(getattr(settings, "llm_max_tokens", 4096) or 4096), 2048),
    )
    return provider_registry.get(provider_name, provider_settings)


def _vision_transport(provider: ProviderProfile, settings) -> Any:
    api_mode = provider_registry.detect_api_mode(provider.base_url, provider.name)
    try:
        return transport_registry.get(api_mode, provider)
    except KeyError:
        if api_mode == "anthropic_messages":
            from personal_agent.plugins.builtin.llm.builtin.anthropic import AnthropicMessagesTransport

            return AnthropicMessagesTransport(provider)
        from personal_agent.plugins.builtin.llm.builtin.chat_completions import ChatCompletionsTransport

        return ChatCompletionsTransport(provider)


def _data_url(resolved: ResolvedAttachment) -> str:
    path = Path(resolved.local_path)
    if not path.exists() or not path.is_file():
        raise ImageTextDescribeUnavailable("file_not_found")
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    mime_type = resolved.mime_type or "application/octet-stream"
    return f"data:{mime_type};base64,{data}"


def _default_base_url(provider_name: str) -> str:
    if provider_name == "openai":
        return "https://api.openai.com/v1"
    if provider_name == "anthropic":
        return "https://api.anthropic.com/v1"
    if provider_name == "openrouter":
        return "https://openrouter.ai/api/v1"
    return ""


def _default_model(provider_name: str) -> str:
    if provider_name == "openai":
        return "gpt-4o-mini"
    if provider_name == "anthropic":
        return "claude-3-5-haiku-latest"
    return ""
