"""JSONL logging under orchestrator/logs/ (SPEC.md 23章). Never logs secrets."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from timeutil import now_iso

_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "api-key",
    "token",
    "password",
    "secret",
    "cookie",
    "authorization",
)


@dataclass(frozen=True)
class LogEntry:
    job_id: str | None
    mail_id: int | None
    agent_name: str | None
    command_summary: str | None
    started_at: str | None
    finished_at: str | None
    exit_code: int | None
    result: str
    changed_files: list[str] = field(default_factory=list)
    error: str | None = None
    next_recipient: str | None = None
    stdout_artifact: dict[str, object] | None = None
    stderr_artifact: dict[str, object] | None = None
    classification: dict[str, object] | None = None
    handoff_count: int = 0
    visited_agents: list[str] = field(default_factory=list)
    handoff_reason: str | None = None
    invocation_id: str | None = None
    parent_invocation_id: str | None = None
    root_invocation_id: str | None = None
    trigger_mail_uid: int | None = None
    result_mail_uid: int | None = None
    invocation_result: str | None = None
    duplicate_mail_uids: tuple[int, ...] = ()
    timestamp: str = field(default_factory=now_iso)


def _scrub(value: str | None) -> str | None:
    """Defensively drop a field's value if it looks like it carries a secret.

    Callers are expected to keep secrets out of LogEntry in the first
    place (SPEC.md 23章); this is a last-resort net, not the primary
    control.
    """
    if value is None:
        return None
    lowered = value.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        return "[REDACTED]"
    return value


class JobLogger:
    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = Path(logs_dir)
        self._log_path = self._logs_dir / "orchestrator.jsonl"

    def _write(self, entry: LogEntry, kind: str) -> None:
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": entry.timestamp,
            "kind": kind,
            "job_id": entry.job_id,
            "mail_id": entry.mail_id,
            "agent_name": entry.agent_name,
            "command_summary": _scrub(entry.command_summary),
            "started_at": entry.started_at,
            "finished_at": entry.finished_at,
            "exit_code": entry.exit_code,
            "result": entry.result,
            "changed_files": entry.changed_files,
            "error": _scrub(entry.error),
            "next_recipient": entry.next_recipient,
            "stdout": entry.stdout_artifact,
            "stderr": entry.stderr_artifact,
            "classification": entry.classification,
            "handoff_count": entry.handoff_count,
            "visited_agents": entry.visited_agents,
            "handoff_reason": _scrub(entry.handoff_reason),
            "invocation_id": entry.invocation_id,
            "parent_invocation_id": entry.parent_invocation_id,
            "root_invocation_id": entry.root_invocation_id,
            "trigger_mail_uid": entry.trigger_mail_uid,
            "result_mail_uid": entry.result_mail_uid,
            "invocation_result": entry.invocation_result,
            "duplicate_mail_uids": list(entry.duplicate_mail_uids),
        }
        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def log_launch(self, entry: LogEntry) -> None:
        self._write(entry, kind="launch")

    def log_outcome(self, entry: LogEntry) -> None:
        self._write(entry, kind="outcome")

    def log_warning(self, entry: LogEntry) -> None:
        self._write(entry, kind="warning")
