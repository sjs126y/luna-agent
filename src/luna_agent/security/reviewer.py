"""Optional model-backed review for permission requests.

The reviewer is deliberately narrower than the security evaluator: it can only
answer a request that has already passed hard safety checks and is waiting for
approval. It never receives tools and cannot grant a persistent TTL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from luna_agent.security.config import normalize_approval_reviewer_config

logger = logging.getLogger(__name__)

ReviewDecision = Literal["allow_once", "deny", "ask_human"]
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class ApprovalRequest:
    tool_name: str
    source: str
    risk_level: str
    mode: str
    reason: str
    input_summary: str
    resources: tuple[dict[str, Any], ...] = ()
    user_context: str = ""

    def as_prompt_payload(self) -> dict[str, Any]:
        return {
            "tool": self.tool_name,
            "source": self.source,
            "risk": self.risk_level,
            "mode": self.mode,
            "reason": self.reason,
            "input_summary": self.input_summary,
            "resources": [dict(item) for item in self.resources],
            "user_context": self.user_context[-2000:],
        }


@dataclass(frozen=True)
class ApprovalReview:
    decision: ReviewDecision
    reason: str = ""
    model: str = ""
    latency_ms: int = 0
    error: str = ""


class ApprovalReviewer:
    """Review eligible permission requests with the current Agent model."""

    def __init__(self, agent: Any, config: dict[str, Any] | None = None) -> None:
        self.agent = agent
        self.config = normalize_approval_reviewer_config(
            config if config is not None else getattr(agent, "_approval_reviewer_config", {})
        )

    @property
    def enabled(self) -> bool:
        return bool(self.config["enabled"])

    def eligible(self, risk_level: str) -> bool:
        if not self.enabled:
            return False
        risk = str(risk_level or "medium").strip().lower()
        maximum = str(self.config.get("max_risk") or "medium")
        return _RISK_ORDER.get(risk, 2) <= _RISK_ORDER.get(maximum, 1)

    async def review(self, request: ApprovalRequest) -> ApprovalReview:
        if not self.eligible(request.risk_level):
            return ApprovalReview(
                decision="ask_human",
                reason="risk level requires human approval",
                model=self._model_name(),
            )

        transport = getattr(self.agent, "_transport", None)
        provider = getattr(self.agent, "_provider", None)
        if transport is None or provider is None:
            return ApprovalReview(
                decision="ask_human",
                reason="current model transport is unavailable",
                model=self._model_name(),
                error="missing_agent_transport",
            )

        prompt = (
            "Review one permission request for an AI agent. The request has already "
            "passed hard safety checks. Decide only whether this single operation "
            "may run once. Never grant persistent access, ignore blocked paths, or "
            "change the sandbox. Return JSON only: "
            '{"decision":"allow_once|deny|ask_human","reason":"short reason"}.\n\n'
            + json.dumps(request.as_prompt_payload(), ensure_ascii=False, sort_keys=True)
        )
        system = (
            "You are a conservative permission reviewer. Approve only an operation "
            "that is clearly implied by the user context and limited to the listed "
            "resources. When uncertain, return ask_human."
        )
        started = time.monotonic()
        own_transport = None
        try:
            review_transport = transport
            configured_model = str(self.config.get("model") or "").strip()
            current_model = str(getattr(provider, "model", "") or "")
            if configured_model and configured_model != current_model:
                from dataclasses import replace
                from luna_agent.llm.transport_registry import transport_registry

                review_provider = replace(provider, model=configured_model)
                own_transport = transport_registry.get(
                    str(getattr(provider, "api_mode", "") or ""),
                    review_provider,
                )
                review_transport = own_transport
            response = await asyncio.wait_for(
                review_transport.call(
                    messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
                    system_prompt=system,
                    tools=[],
                    max_tokens=256,
                ),
                timeout=float(self.config.get("timeout_seconds") or 12),
            )
            payload = _parse_review_json(str(getattr(response, "text", "") or ""))
            decision = str(payload.get("decision") or "ask_human").strip().lower()
            if decision not in {"allow_once", "deny", "ask_human"}:
                decision = "ask_human"
            return ApprovalReview(
                decision=decision,  # type: ignore[arg-type]
                reason=str(payload.get("reason") or "").strip()[:500],
                model=self._model_name(configured_model or current_model),
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:
            logger.warning("Approval reviewer failed; falling back to human approval: %s", exc)
            return ApprovalReview(
                decision="ask_human",
                reason="approval reviewer unavailable",
                model=self._model_name(),
                latency_ms=int((time.monotonic() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if own_transport is not None:
                try:
                    await own_transport.close()
                except Exception:
                    logger.debug("Failed to close approval reviewer transport", exc_info=True)

    def _model_name(self, fallback: str = "") -> str:
        return str(
            fallback
            or self.config.get("model")
            or getattr(getattr(self.agent, "_provider", None), "model", "")
            or ""
        )


def _parse_review_json(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3:
            value = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start, end = value.find("{"), value.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("approval reviewer did not return a JSON object")
        parsed = json.loads(value[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("approval reviewer response must be an object")
    return parsed
