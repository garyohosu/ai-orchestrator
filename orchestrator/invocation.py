"""Invocation-ID generation and launch-boundary validation."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from timeutil import now_iso


class InvocationIdError(ValueError):
    """Raised when launch metadata contains conflicting Invocation-IDs."""


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
