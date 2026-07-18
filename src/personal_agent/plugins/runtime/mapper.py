"""Translate subsystem registrations into capability bindings."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from personal_agent.plugins.runtime.models import CapabilityBinding, CapabilityKind


class CapabilityMapper:
    def binding(
        self,
        *,
        kind: CapabilityKind,
        public_name: str,
        owner: str,
        generation_id: str,
        runtime_instance_id: str,
        manager_key: str | None = None,
        contract: Any = None,
        metadata: Mapping[str, Any] | None = None,
        ordinal: int = 0,
    ) -> CapabilityBinding:
        manager_key = str(manager_key or public_name)
        contract_hash = _stable_hash(contract)
        binding_id = ":".join((
            runtime_instance_id,
            kind.value,
            public_name,
            str(ordinal),
            contract_hash[:12],
        ))
        return CapabilityBinding(
            binding_id=binding_id,
            capability_id=f"{owner}:{kind.value}:{public_name}",
            public_name=public_name,
            kind=kind,
            owner=owner,
            generation_id=generation_id,
            runtime_instance_id=runtime_instance_id,
            contract_hash=contract_hash,
            manager_key=manager_key,
            metadata=dict(metadata or {}),
        )


def _stable_hash(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )
    except (TypeError, ValueError):
        payload = repr(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_default(value: Any) -> str:
    module = getattr(value, "__module__", "")
    qualname = getattr(value, "__qualname__", "")
    if module or qualname:
        return f"{module}:{qualname}"
    return repr(value)
