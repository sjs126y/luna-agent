from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from personal_agent.artifacts import normalize_artifact_kind
from personal_agent.models.messages import OutboundMessage, PlatformCapabilities


@dataclass(frozen=True, slots=True)
class DeliveryOperation:
    index: int
    kind: str
    text: str = ""
    artifact_id: str = ""
    filename: str = ""
    mime_type: str = ""
    degraded_from: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "text": self.text,
            "artifact_id": self.artifact_id,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "degraded_from": self.degraded_from,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DeliveryOperation":
        return cls(
            index=int(value.get("index") or 0),
            kind=str(value.get("kind") or "text"),
            text=str(value.get("text") or ""),
            artifact_id=str(value.get("artifact_id") or ""),
            filename=str(value.get("filename") or ""),
            mime_type=str(value.get("mime_type") or ""),
            degraded_from=str(value.get("degraded_from") or ""),
        )


@dataclass(frozen=True, slots=True)
class DeliveryPlan:
    operations: tuple[DeliveryOperation, ...] = field(default_factory=tuple)
    degraded: bool = False


class DeliveryPlanner:
    def plan(
        self,
        message: OutboundMessage,
        capabilities: PlatformCapabilities,
    ) -> DeliveryPlan:
        operations: list[DeliveryOperation] = []
        degraded = False
        attachment_count = 0
        max_attachments = max(0, int(capabilities.max_attachments or 0))

        for part in message.parts:
            if part.type == "text":
                if part.text:
                    operations.append(DeliveryOperation(len(operations), "text", text=part.text))
                continue
            if not part.artifact_id:
                continue
            attachment_count += 1
            if max_attachments and attachment_count > max_attachments:
                operations.append(_unsupported_operation(len(operations), part, "attachment limit exceeded"))
                degraded = True
                continue

            requested_kind = normalize_artifact_kind(part.type, part.mime_type)
            kind = _delivery_kind(requested_kind, capabilities)
            if kind is None:
                operations.append(_unsupported_operation(len(operations), part, "platform does not support this media type"))
                degraded = True
                continue
            degraded_from = requested_kind if kind != requested_kind else ""
            degraded = degraded or bool(degraded_from)
            operations.append(DeliveryOperation(
                index=len(operations),
                kind=kind,
                artifact_id=part.artifact_id,
                filename=part.name,
                mime_type=part.mime_type,
                degraded_from=degraded_from,
            ))

        if not operations:
            operations.append(DeliveryOperation(0, "text", text=message.text_content()))
        return DeliveryPlan(tuple(operations), degraded=degraded)


def _delivery_kind(kind: str, capabilities: PlatformCapabilities) -> str | None:
    normalized = str(kind or "file").lower()
    supported = {
        "image": capabilities.image_send,
        "audio": capabilities.audio_send,
        "video": capabilities.video_send,
        "file": capabilities.file_send,
        "document": capabilities.file_send,
        "resource": capabilities.file_send,
    }
    if supported.get(normalized, False):
        return "file" if normalized in {"document", "resource"} else normalized
    if capabilities.file_send and normalized in supported:
        return "file"
    return None


def _unsupported_operation(index: int, part, reason: str) -> DeliveryOperation:
    name = part.name or part.artifact_id
    return DeliveryOperation(
        index=index,
        kind="text",
        text=f"[附件未发送: {name}，{reason}]",
        degraded_from=part.type,
    )
