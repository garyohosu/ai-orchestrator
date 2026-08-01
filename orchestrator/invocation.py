"""Invocation-ID generation and launch-boundary validation."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import json
import re

from timeutil import now_iso


class InvocationIdError(ValueError):
    """Raised when launch metadata contains conflicting Invocation-IDs."""


class InvocationMetadataError(InvocationIdError):
    """Raised when structured task lineage is partial or malformed."""


_INVOCATION_ID_RE = re.compile(r"^(?:INV|MANUAL)-[A-Za-z0-9._-]+$")
_STRUCTURED_METADATA_KEYS = {
    "message_type",
    "task_eligible",
    "invocation_id",
    "parent_invocation_id",
    "root_invocation_id",
    "trigger_mail_uid",
    "invocation_result",
}


def _require_invocation_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _INVOCATION_ID_RE.fullmatch(value):
        raise InvocationMetadataError(
            f"{field_name} must be a valid Invocation-ID"
        )
    return value


class InvocationResult(str, Enum):
    """Outcome of one CLI invocation, independent of application state."""

    COMPLETED = "COMPLETED"
    DELEGATED = "DELEGATED"
    WAITING = "WAITING"
    FAILED = "FAILED"


@dataclass(frozen=True)
class InvocationLineage:
    parent_invocation_id: str | None
    root_invocation_id: str
    trigger_mail_uid: int


def derive_invocation_lineage(
    origin_mail: Mapping[str, object], invocation_id: str
) -> InvocationLineage:
    """Derive lineage only from structured body metadata."""

    payload: dict = {}
    body = origin_mail.get("body", "")
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as err:
            if body.lstrip().startswith(("{", "[")):
                raise InvocationMetadataError(
                    "structured task body is malformed JSON"
                ) from err
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
    structured = bool(_STRUCTURED_METADATA_KEYS.intersection(payload))
    if structured:
        parent = _require_invocation_id(
            payload.get("invocation_id"), "invocation_id"
        )
        root = _require_invocation_id(
            payload.get("root_invocation_id"), "root_invocation_id"
        )
        source_parent = payload.get("parent_invocation_id")
        if source_parent is not None:
            _require_invocation_id(source_parent, "parent_invocation_id")
        source_trigger = payload.get("trigger_mail_uid")
        if (
            isinstance(source_trigger, bool)
            or not isinstance(source_trigger, int)
            or source_trigger <= 0
        ):
            raise InvocationMetadataError(
                "trigger_mail_uid must be a positive integer"
            )
    else:
        parent = None
        root = invocation_id
    trigger = origin_mail.get("mail_id")
    if isinstance(trigger, bool) or not isinstance(trigger, int) or trigger <= 0:
        raise InvocationIdError("origin mail_id must be a positive integer")
    return InvocationLineage(parent, root, trigger)


def generate_invocation_id(attempt: int = 1) -> str:
    """Return an Invocation-ID containing a complete UUID4."""

    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise InvocationIdError("attempt must be a positive integer")
    stamp = now_iso().replace("-", "").replace(":", "").replace(".", "")
    return f"INV-{stamp}-{attempt:03d}-{str(uuid.uuid4()).upper()}"


def resolve_launch_invocation_id(
    invocation_id: str,
    environment: Mapping[str, str] | None,
    *,
    attempt: int,
) -> str:
    """Resolve and validate the ID used by tracking, prompt, and environment."""

    values: list[str] = []
    if not isinstance(invocation_id, str):
        raise InvocationIdError("invocation_id must be a string")
    if invocation_id:
        values.append(invocation_id)
    if environment is not None:
        for key in ("AI_INVOCATION_ID", "INVOCATION_ID"):
            if key in environment:
                value = environment[key]
                if not isinstance(value, str) or not value:
                    raise InvocationIdError(f"{key} must be a non-empty string")
                values.append(value)
    if values and any(value != values[0] for value in values[1:]):
        raise InvocationIdError("launch Invocation-ID values do not match")
    return values[0] if values else generate_invocation_id(attempt)
