"""A conservative, generation-owned proactive conversation companion."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from luna_agent_plugin_sdk import (
    ActiveConversationIntent,
    ActiveResourceRequest,
    CommandEntry,
)


class ActiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    sessions: list[str] = Field(default_factory=list)
    restart_backoff_seconds: list[float] = Field(default_factory=lambda: [1, 2, 5, 10, 30])


class CheckInConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    min_silence_seconds: float = Field(default=8 * 3600, ge=60)


class FollowUpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_recent_messages: int = Field(default=3, ge=1, le=5)


class CompanionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: ActiveConfig = Field(default_factory=ActiveConfig)
    quiet_hours: list[tuple[str, str]] = Field(default_factory=list)
    max_daily_messages: int = Field(default=3, ge=0, le=20)
    min_gap_seconds: float = Field(default=2 * 3600, ge=0)
    review_threshold: float = Field(default=0.75, ge=0, le=1)
    expire_after_seconds: float = Field(default=24 * 3600, ge=60)
    check_in: CheckInConfig = Field(default_factory=CheckInConfig)
    follow_up: FollowUpConfig = Field(default_factory=FollowUpConfig)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:20]


def _response_text(response: Any) -> str:
    value = getattr(response, "content", None)
    if value is None:
        value = getattr(response, "text", response)
    return str(value or "").strip()


def _json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _in_quiet_hours(now: datetime, ranges: list[tuple[str, str]]) -> bool:
    current = now.strftime("%H:%M")
    for start, end in ranges:
        start_text, end_text = str(start), str(end)
        if start_text <= end_text:
            if start_text <= current < end_text:
                return True
        elif current >= start_text or current < end_text:
            return True
    return False


def _activation(
    *,
    continuity: float,
    interest: float,
    novelty: float,
    urgency: float,
    availability: float,
    semantic_value: float,
    fatigue: float,
    interruption_cost: float,
    duplicate_penalty: float,
    previous: float = 0.0,
    elapsed_seconds: float = 0.0,
) -> float:
    decay = math.exp(-max(0.0, elapsed_seconds) / (24 * 3600))
    raw = (
        0.15 * continuity
        + 0.15 * interest
        + 0.10 * novelty
        + 0.20 * urgency
        + 0.15 * availability
        + 0.20 * semantic_value
        - 0.10 * fatigue
        - 0.15 * interruption_cost
        - 0.10 * duplicate_penalty
    )
    return max(0.0, min(1.0, previous * decay + raw))


class CompanionStore:
    def __init__(self, storage) -> None:
        self.storage = storage
        self._lock = asyncio.Lock()
        self.state = storage.read_json(
            "companion.json",
            default={
                "schema_version": 1,
                "candidates": {},
                "sessions": {},
                "sent": [],
            },
            schema_version=1,
        )

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return json.loads(json.dumps(self.state, ensure_ascii=False))

    async def update(self, callback) -> None:
        async with self._lock:
            callback(self.state)
            self.storage.write_json_atomic("companion.json", self.state)


class CompanionRunner:
    def __init__(self, ctx, config: CompanionConfig, store: CompanionStore) -> None:
        self.ctx = ctx
        self.config = config
        self.store = store

    async def run(self) -> None:
        await self.ctx.runtime.ready()
        next_check = 0.0
        while not self.ctx.runtime.stop_requested:
            await self.ctx.runtime.wait_until_resumed()
            delay = max(0.0, next_check - _now().timestamp()) if next_check else 0.0
            reason = await self.ctx.runtime.wait_for_wakeup(timeout=delay)
            if str(reason) in {"stop", "quiesce"}:
                continue
            self.ctx.runtime.heartbeat()
            await self.cycle()
            next_check = self._next_check_at()

    async def cycle(self) -> None:
        now = _now()
        if _in_quiet_hours(now, self.config.quiet_hours):
            return
        for session_key in self.config.active.sessions:
            if session_key == "*":
                continue
            try:
                status = await self.ctx.resources.conversation.status(session_key)
            except Exception:
                # A single unavailable session must not stop the companion root.
                continue
            if status.busy:
                continue
            if not self._eligible_for_message(status, now):
                continue
            candidates = self._candidates(session_key, status, now)
            for candidate in candidates:
                assessed = await self._assess(candidate)
                if assessed is None:
                    self._defer_candidate(candidate, now, error="invalid_llm_assessment")
                    await self._save_candidate(candidate)
                    continue
                candidate.update(assessed)
                candidate["review_count"] = int(candidate.get("review_count") or 0) + 1
                candidate["reviewed_at"] = now.isoformat()
                candidate["activation"] = _activation(**self._activation_args(candidate))
                candidate["expires_at"] = (
                    now + timedelta(seconds=self.config.expire_after_seconds)
                ).isoformat()
                candidate["next_review_at"] = self._next_review_at(candidate["activation"], now).isoformat()
                await self._save_candidate(candidate)
            selected = await self._select_candidate(session_key)
            if selected is not None:
                await self._submit(selected)

    def _candidates(self, session_key: str, status, now: datetime) -> list[dict[str, Any]]:
        state = self.store.state
        session = state.setdefault("sessions", {}).setdefault(session_key, {})
        candidates: list[dict[str, Any]] = []

        def add_candidate(candidate: dict[str, Any]) -> None:
            existing = state.setdefault("candidates", {}).get(candidate["intent_id"])
            if isinstance(existing, dict):
                expires_at = _parse_time(existing.get("expires_at", ""))
                next_review_at = _parse_time(existing.get("next_review_at", ""))
                if expires_at is not None and now >= expires_at:
                    existing = None
                elif next_review_at is not None and now < next_review_at:
                    return
            if existing:
                candidate.update({
                    key: existing[key]
                    for key in (
                        "activation",
                        "review_count",
                        "reviewed_at",
                        "expires_at",
                        "next_review_at",
                        "reason",
                    )
                    if key in existing
                })
                candidate["previous"] = float(existing.get("activation") or 0.0)
                reviewed_at = _parse_time(existing.get("reviewed_at", ""))
                candidate["elapsed_seconds"] = (
                    max(0.0, (now - reviewed_at).total_seconds()) if reviewed_at else 0.0
                )
            candidates.append(candidate)

        last_user = _parse_time(status.last_user_at)
        last_message = (status.recent_user_messages or [""])[-1]
        if (
            self.config.check_in.enabled
            and last_user is not None
            and (now - last_user).total_seconds() >= self.config.check_in.min_silence_seconds
            and session.get("last_check_in_text") != last_message
        ):
            add_candidate({
                "intent_id": f"check-in:{session_key}:{_hash_text(last_message)}",
                "kind": "check_in",
                "session_key": session_key,
                "instruction": "自然地问候用户，确认近况；不要连续追问，也不要声称知道用户当前状态。",
                "evidence": {"last_user_message": last_message, "silence_seconds": (now - last_user).total_seconds()},
                "urgency": 0.15,
                "continuity": 0.30,
                "previous": 0.0,
            })
        if self.config.follow_up.enabled and last_message:
            digest = _hash_text(last_message)
            if session.get("last_follow_up_digest") != digest:
                add_candidate({
                    "intent_id": f"follow-up:{session_key}:{digest}",
                    "kind": "follow_up",
                    "session_key": session_key,
                    "instruction": "判断是否自然跟进用户最近提到的话题；如果没有足够关联就不要主动追问。",
                    "evidence": {"recent_user_messages": list(status.recent_user_messages)},
                    "urgency": 0.20,
                    "continuity": 0.75,
                    "previous": 0.0,
                })
        return candidates

    async def _assess(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        prompt = (
            "你是主动交流决策器。只返回 JSON，不要输出 Markdown。"
            "评估这个候选是否值得打扰用户，所有分数为 0 到 1。"
            "返回 continuity、interest、novelty、confidence、reason。\n"
            + json.dumps(candidate, ensure_ascii=False)
        )
        try:
            response = await self.ctx.resources.llm.complete(prompt, max_tokens=256)
            value = _json_object(_response_text(response))
        except Exception:
            return None
        if value is None:
            return None
        result: dict[str, Any] = {}
        for key in ("continuity", "interest", "novelty", "confidence", "semantic_value"):
            try:
                result[key] = max(0.0, min(1.0, float(value.get(key, 0.0))))
            except (TypeError, ValueError):
                result[key] = 0.0
        result["reason"] = str(value.get("reason") or "")[:500]
        return result

    def _activation_args(self, candidate: dict[str, Any]) -> dict[str, Any]:
        reviewed_at = _parse_time(candidate.get("reviewed_at", ""))
        elapsed_seconds = float(candidate.get("elapsed_seconds") or 0.0)
        if reviewed_at is not None:
            elapsed_seconds = max(0.0, (_now() - reviewed_at).total_seconds())
        # The first assessment is not fatigue or duplication; those costs only
        # grow when the same candidate survives multiple review cycles.
        repeat_count = max(0, int(candidate.get("review_count") or 0) - 1)
        return {
            "continuity": float(candidate.get("continuity") or 0),
            "interest": float(candidate.get("interest") or 0),
            "novelty": float(candidate.get("novelty") or 0),
            "urgency": float(candidate.get("urgency") or 0),
            "availability": float(candidate.get("confidence", 1.0) or 0.0),
            "semantic_value": float(candidate.get("semantic_value") or 0),
            "fatigue": min(1.0, repeat_count / 5),
            "interruption_cost": float(candidate.get("interruption_cost") or 0.0),
            "duplicate_penalty": min(1.0, repeat_count / 10),
            "previous": float(candidate.get("previous") or 0),
            "elapsed_seconds": elapsed_seconds,
        }

    def _next_review_at(self, activation: float, now: datetime) -> datetime:
        threshold = self.config.review_threshold
        if activation >= threshold:
            return now
        if activation >= threshold * 0.8:
            delay = 15 * 60
        elif activation >= threshold * 0.5:
            delay = 60 * 60
        else:
            delay = min(4 * 60 * 60, self.config.expire_after_seconds / 2)
        return now + timedelta(seconds=max(60.0, delay))

    def _defer_candidate(self, candidate: dict[str, Any], now: datetime, *, error: str) -> None:
        candidate["reviewed_at"] = now.isoformat()
        candidate["review_count"] = int(candidate.get("review_count") or 0) + 1
        candidate["last_error"] = error
        candidate["next_review_at"] = (now + timedelta(minutes=15)).isoformat()
        candidate["expires_at"] = (
            now + timedelta(seconds=self.config.expire_after_seconds)
        ).isoformat()

    async def _save_candidate(self, candidate: dict[str, Any]) -> None:
        await self.store.update(
            lambda state: state.setdefault("candidates", {}).update({candidate["intent_id"]: dict(candidate)})
        )

    async def _select_candidate(self, session_key: str) -> dict[str, Any] | None:
        state = await self.store.snapshot()
        now = _now()
        candidates = [
            item for item in state.get("candidates", {}).values()
            if item.get("session_key") == session_key
            and float(item.get("activation") or 0) >= self.config.review_threshold
            and (
                _parse_time(item.get("next_review_at", "")) is None
                or now >= _parse_time(item.get("next_review_at", ""))
            )
            and (
                _parse_time(item.get("expires_at", "")) is None
                or now < _parse_time(item.get("expires_at", ""))
            )
            and item.get("intent_id") not in {
                str(entry.get("intent_id") or "") if isinstance(entry, dict) else str(entry)
                for entry in state.get("sent", [])
            }
        ]
        candidates.sort(key=lambda item: float(item.get("activation") or 0), reverse=True)
        return candidates[0] if candidates else None

    async def _submit(self, candidate: dict[str, Any]) -> None:
        intent = ActiveConversationIntent(
            intent_id=candidate["intent_id"],
            session_key=candidate["session_key"],
            kind=candidate["kind"],
            instruction=candidate["instruction"],
            evidence=candidate.get("evidence") or {},
            request_id=f"luna-companion:{candidate['intent_id']}",
            metadata={"plugin": "luna-companion", "reason": candidate.get("reason", "")},
        )
        handle = await self.ctx.resources.conversation.submit_intent(intent)
        outcome = await handle.outcome() if hasattr(handle, "outcome") else None
        if outcome is not None and not bool(getattr(outcome, "succeeded", False)):
            candidate["last_error"] = "conversation_submission_failed"
            candidate["next_review_at"] = (
                _now() + timedelta(minutes=15)
            ).isoformat()
            await self._save_candidate(candidate)
            return
        now = _now().isoformat()

        def mark(state):
            state.setdefault("sent", []).append({
                "intent_id": candidate["intent_id"],
                "sent_at": now,
            })
            state.setdefault("sessions", {}).setdefault(candidate["session_key"], {}).update({
                "last_sent_at": now,
                "last_check_in_text": (candidate.get("evidence") or {}).get("last_user_message", ""),
                "last_follow_up_digest": _hash_text((candidate.get("evidence") or {}).get("recent_user_messages", [""])[-1]),
            })

        await self.store.update(mark)

    def _eligible_for_message(self, status, now: datetime) -> bool:
        state = self.store.state
        session = state.setdefault("sessions", {}).setdefault(status.session_key, {})
        last_sent = _parse_time(session.get("last_sent_at", ""))
        if last_sent is not None and (now - last_sent).total_seconds() < self.config.min_gap_seconds:
            return False
        today = now.date().isoformat()
        count = sum(
            1
            for item in state.get("sent", [])
            if isinstance(item, dict) and str(item.get("sent_at") or "").startswith(today)
        )
        return count < self.config.max_daily_messages

    def _next_check_at(self) -> float:
        return (_now() + timedelta(minutes=15)).timestamp()


def register(ctx) -> None:
    config = ctx.parse_config(CompanionConfig)
    store = CompanionStore(ctx.storage)

    async def status_command(args="", **kwargs):
        state = await store.snapshot()
        return json.dumps({
            "candidates": len(state.get("candidates", {})),
            "sent": len(state.get("sent", [])),
            "sessions": list((state.get("sessions") or {}).keys()),
        }, ensure_ascii=False)

    ctx.register.command(CommandEntry(
        "luna-companion-status",
        "Show Luna Companion state.",
        status_command,
        scope="both",
    ))

    async def run(active_ctx) -> None:
        await CompanionRunner(active_ctx, config, store).run()

    ctx.register.active(
        run=run,
        resources=ActiveResourceRequest(llm=True, conversation=True),
        restart_policy="on_failure",
        startup_timeout=15,
        shutdown_timeout=15,
    )
