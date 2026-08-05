"""Persisted rate-limit cooldown tracking, keyed by agent name.

Consulted by availability.py before selecting a fallback/initial candidate,
so a job doesn't immediately re-pick an agent that very recently failed
with CliEvidence.category == "rate_limit". Purely advisory pre-screening:
per this project's own availability-check policy (see availability.py), a
live launch attempt is always the authoritative result -- this store only
avoids *wasting* an attempt on an agent already known to be down, it never
blocks one that a health check merely suspects is down.

Note on `retry_at`: this is the best-effort, possibly-timezone-ambiguous
timestamp error_taxonomy.parse_retry_at() extracts from a CLI's free-text
error message (e.g. "try again at Aug 8th, 2026 5:17 PM" -- no timezone
stated). It intentionally does NOT use timeutil's strict
"YYYY-MM-DDTHH:MM:SS.mmmZ" mail-timestamp format, because it is not a
timestamp this project generated -- it is a guess parsed out of another
system's prose. Treat comparisons against it as approximate.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from timeutil import now_iso


def _atomic_write_json(path: Path, data: object) -> None:
    # Mirrors runtime.py's private helper of the same name/behavior; kept
    # as a local copy rather than importing runtime._atomic_write_json so
    # this module doesn't depend on another module's underscore-prefixed
    # (non-public) helper.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


@dataclass(frozen=True)
class RateLimitRecord:
    rule_id: str
    retry_at: str | None
    recorded_at: str
    evidence: str | None = None


class RateLimitStore:
    """runtime/rate_limit_state.json: agent_name -> most recent rate-limit record."""

    def __init__(self, runtime_dir: Path) -> None:
        self._path = Path(runtime_dir) / "rate_limit_state.json"

    def _load(self) -> dict[str, dict]:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def record(
        self, agent_name: str, *, rule_id: str, retry_at: str | None, evidence: str | None
    ) -> None:
        data = self._load()
        data[agent_name] = {
            "rule_id": rule_id,
            "retry_at": retry_at,
            "recorded_at": now_iso(),
            "evidence": evidence,
        }
        _atomic_write_json(self._path, data)

    def clear(self, agent_name: str) -> None:
        """Drop any cooldown record for agent_name (e.g. after it succeeds)."""
        data = self._load()
        if agent_name in data:
            del data[agent_name]
            _atomic_write_json(self._path, data)

    def get(self, agent_name: str) -> RateLimitRecord | None:
        raw = self._load().get(agent_name)
        if raw is None or not isinstance(raw, dict):
            return None
        return RateLimitRecord(
            rule_id=str(raw.get("rule_id", "")),
            retry_at=raw.get("retry_at"),
            recorded_at=str(raw.get("recorded_at", "")),
            evidence=raw.get("evidence"),
        )

    def is_in_cooldown(self, agent_name: str, *, now: datetime | None = None) -> bool:
        """True if agent_name has a recent rate-limit record whose cooldown
        has not (as far as we can tell) elapsed yet.

        A record with no parseable retry_at is treated as an indefinite
        cooldown -- conservatively safer than assuming immediate recovery --
        until clear() is called (dispatch.py clears it the next time that
        agent completes something successfully).
        """
        record = self.get(agent_name)
        if record is None:
            return False
        if not record.retry_at:
            return True
        try:
            retry_dt = datetime.fromisoformat(record.retry_at)
        except ValueError:
            return True
        current = now if now is not None else datetime.now(timezone.utc)
        # error_taxonomy.parse_retry_at() never attaches a timezone (the
        # source CLI messages don't state one), so retry_dt is normally
        # naive. Compare on equal footing: if retry_dt is naive, drop
        # current's tzinfo too rather than raise on a naive/aware mix.
        if retry_dt.tzinfo is None and current.tzinfo is not None:
            current = current.replace(tzinfo=None)
        elif retry_dt.tzinfo is not None and current.tzinfo is None:
            current = current.replace(tzinfo=retry_dt.tzinfo)
        return current < retry_dt
