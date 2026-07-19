"""Durable idempotency ledger for explicitly replayable submissions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import time

from luna_agent.conversation.submission import (
    ResponseMode,
    SubmissionKind,
    SubmissionOutcome,
    SubmissionRequest,
    SubmissionStatus,
)
from luna_agent.models.messages import MessagePart, OutboundMessage


class SubmissionClaimKind(StrEnum):
    NEW = "new"
    RESUME = "resume"
    CACHED = "cached"
    ACTIVE = "active"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class SubmissionLedgerRecord:
    scope: str
    owner_id: str
    request_id: str
    session_key: str
    payload_hash: str
    response_mode: ResponseMode
    status: str
    kind: SubmissionKind
    turn_id: str = ""
    delivery_id: str = ""
    response: str = ""
    message: OutboundMessage | None = None
    error: str = ""
    attempts: int = 1

    def outcome(self) -> SubmissionOutcome:
        status = {
            "conversation_completed": SubmissionStatus.COMPLETED,
            "completed": SubmissionStatus.COMPLETED,
            "delivery_pending": SubmissionStatus.COMPLETED,
            "failed": SubmissionStatus.FAILED,
            "cancelled": SubmissionStatus.CANCELLED,
            "rejected": SubmissionStatus.REJECTED,
        }.get(self.status, SubmissionStatus.ACCEPTED)
        return SubmissionOutcome(
            request_id=self.request_id,
            session_key=self.session_key,
            status=status,
            kind=self.kind,
            response=self.response,
            error=self.error,
            payload={
                "idempotent_replay": True,
                "ledger_status": self.status,
                "turn_id": self.turn_id,
                "delivery_id": self.delivery_id,
            },
            message=self.message,
        )


@dataclass(frozen=True, slots=True)
class SubmissionClaim:
    kind: SubmissionClaimKind
    record: SubmissionLedgerRecord


class DurableSubmissionLedger:
    def __init__(self, db, *, retention_seconds: float = 30 * 24 * 60 * 60) -> None:
        self.db = db
        self.retention_seconds = max(3600.0, float(retention_seconds))

    async def claim(self, request: SubmissionRequest) -> SubmissionClaim:
        now = time.time()
        scope = request.origin.value
        owner_id = request.owner_id
        payload_hash = self.payload_hash(request)
        row, owned = await self.db.claim_submission((
            scope,
            owner_id,
            request.request_id,
            request.session_key,
            payload_hash,
            request.response_mode.value,
            "accepted",
            SubmissionKind.CONVERSATION.value,
            "",
            "",
            "",
            "",
            "",
            1,
            now,
            now,
            0.0,
        ))
        record = self._record(row)
        if record.payload_hash != payload_hash:
            return SubmissionClaim(SubmissionClaimKind.CONFLICT, record)
        if owned:
            return SubmissionClaim(SubmissionClaimKind.NEW, record)
        if record.status == "conversation_completed":
            return SubmissionClaim(SubmissionClaimKind.RESUME, record)
        if record.status in {"completed", "delivery_pending", "failed", "cancelled", "rejected"}:
            return SubmissionClaim(SubmissionClaimKind.CACHED, record)
        return SubmissionClaim(SubmissionClaimKind.ACTIVE, record)

    async def mark_running(self, request: SubmissionRequest, *, turn_id: str = "") -> None:
        await self._update(request, status="running", turn_id=turn_id)

    async def store_conversation(
        self,
        request: SubmissionRequest,
        outcome: SubmissionOutcome,
    ) -> None:
        await self._update(
            request,
            status="conversation_completed",
            kind=outcome.kind.value,
            response=outcome.response,
            message_json=self._message_json(outcome.message),
            error=outcome.error,
        )

    async def store_final(
        self,
        request: SubmissionRequest,
        outcome: SubmissionOutcome,
        *,
        delivery_id: str = "",
        delivery_pending: bool = False,
    ) -> None:
        status = (
            "delivery_pending"
            if delivery_pending
            else outcome.status.value
        )
        await self._update(
            request,
            status=status,
            kind=outcome.kind.value,
            delivery_id=delivery_id,
            response=outcome.response,
            message_json=self._message_json(outcome.message),
            error=outcome.error,
            completed_at=time.time(),
        )

    async def prune(self) -> int:
        return await self.db.prune_submissions(
            before=time.time() - self.retention_seconds,
        )

    async def _update(self, request: SubmissionRequest, **changes) -> None:
        await self.db.update_submission(
            request.origin.value,
            request.owner_id,
            request.request_id,
            updated_at=time.time(),
            **changes,
        )

    @staticmethod
    def payload_hash(request: SubmissionRequest) -> str:
        attachments = [
            {
                "id": str(getattr(item, "id", "") or ""),
                "kind": str(getattr(item, "kind", "") or ""),
                "name": str(getattr(item, "name", "") or ""),
            }
            for item in request.input.attachments
        ]
        payload = {
            "session_key": request.session_key,
            "origin": request.origin.value,
            "owner_id": request.owner_id,
            "response_mode": request.response_mode.value,
            "text": request.input.text,
            "attachments": attachments,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def delivery_id(request: SubmissionRequest) -> str:
        value = f"{request.origin.value}\0{request.owner_id}\0{request.request_id}"
        return f"del_sub_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:32]}"

    @staticmethod
    def _message_json(message: OutboundMessage | None) -> str:
        if message is None:
            return ""
        return json.dumps(message.as_dict(), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _record(row: dict) -> SubmissionLedgerRecord:
        message = None
        if row.get("message_json"):
            raw = json.loads(row["message_json"])
            message = OutboundMessage(
                parts=[MessagePart(**part) for part in raw.get("parts", [])],
            )
        return SubmissionLedgerRecord(
            scope=str(row["scope"]),
            owner_id=str(row["owner_id"]),
            request_id=str(row["request_id"]),
            session_key=str(row["session_key"]),
            payload_hash=str(row["payload_hash"]),
            response_mode=ResponseMode(str(row["response_mode"])),
            status=str(row["status"]),
            kind=SubmissionKind(str(row.get("kind") or SubmissionKind.CONVERSATION.value)),
            turn_id=str(row.get("turn_id") or ""),
            delivery_id=str(row.get("delivery_id") or ""),
            response=str(row.get("response") or ""),
            message=message,
            error=str(row.get("error") or ""),
            attempts=int(row.get("attempts") or 1),
        )
