"""Runtime state: double-launch guard, STALE recovery, stop control (SPEC.md 24章, 25章)."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from mail_adapter import ExpectedReply, MailPort, MailReplyQuery
from timeutil import shift_ms
from winproc import is_same_running_process


class RuntimeStateError(RuntimeError):
    """Raised when a persistent launch/terminal guard is unsafe to use."""


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


@dataclass(frozen=True)
class RunningAgentState:
    pid: int
    process_start_time_iso: str
    agent_name: str
    agent_uid: str
    job_id: str
    origin_mail_id: int
    launch_command: list[str]
    recorded_at_iso: str
    retry_count: int = 0
    invocation_id: str = ""
    origin_mail_max_id: int = 0
    decision_id: str = ""
    parent_invocation_id: str | None = None
    root_invocation_id: str = ""
    trigger_mail_uid: int | None = None

    def __post_init__(self) -> None:
        positive_ints = {
            "pid": self.pid,
            "origin_mail_id": self.origin_mail_id,
        }
        for name, value in positive_ints.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RuntimeStateError(f"{name} must be a positive integer")
        for name, value in {
            "retry_count": self.retry_count,
            "origin_mail_max_id": self.origin_mail_max_id,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RuntimeStateError(f"{name} must be a non-negative integer")
        for name in (
            "process_start_time_iso", "agent_name", "agent_uid", "job_id",
            "recorded_at_iso", "invocation_id", "decision_id",
            "root_invocation_id",
        ):
            if not isinstance(getattr(self, name), str):
                raise RuntimeStateError(f"{name} must be a string")
        if not isinstance(self.launch_command, list) or not all(
            isinstance(item, str) for item in self.launch_command
        ):
            raise RuntimeStateError("launch_command must be a list of strings")
        if self.parent_invocation_id is not None and not isinstance(
            self.parent_invocation_id, str
        ):
            raise RuntimeStateError("parent_invocation_id must be a string or null")
        if self.trigger_mail_uid is not None and (
            isinstance(self.trigger_mail_uid, bool)
            or not isinstance(self.trigger_mail_uid, int)
            or self.trigger_mail_uid <= 0
        ):
            raise RuntimeStateError("trigger_mail_uid must be a positive integer or null")

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "process_start_time_iso": self.process_start_time_iso,
            "agent_name": self.agent_name,
            "agent_uid": self.agent_uid,
            "job_id": self.job_id,
            "origin_mail_id": self.origin_mail_id,
            "launch_command": self.launch_command,
            "recorded_at_iso": self.recorded_at_iso,
            "retry_count": self.retry_count,
            "invocation_id": self.invocation_id,
            "origin_mail_max_id": self.origin_mail_max_id,
            "decision_id": self.decision_id,
            "parent_invocation_id": self.parent_invocation_id,
            "root_invocation_id": self.root_invocation_id,
            "trigger_mail_uid": self.trigger_mail_uid,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RunningAgentState":
        if not isinstance(data, dict):
            raise RuntimeStateError("running state must be a JSON object")
        return cls(
            pid=data["pid"],
            process_start_time_iso=data["process_start_time_iso"],
            agent_name=data["agent_name"],
            agent_uid=data["agent_uid"],
            job_id=data["job_id"],
            origin_mail_id=data["origin_mail_id"],
            launch_command=data["launch_command"],
            recorded_at_iso=data["recorded_at_iso"],
            retry_count=data.get("retry_count", 0),
            invocation_id=data.get("invocation_id", ""),
            origin_mail_max_id=data.get("origin_mail_max_id", 0),
            decision_id=data.get("decision_id", ""),
            parent_invocation_id=data.get("parent_invocation_id"),
            root_invocation_id=data.get("root_invocation_id", ""),
            trigger_mail_uid=data.get("trigger_mail_uid"),
        )


class RuntimeStateStore:
    """Persists running-agent info and the stop.request flag under runtime/ (SPEC.md 24章)."""

    _STOP_REQUEST_NAME = "stop.request"

    def __init__(self, runtime_dir: Path) -> None:
        self._runtime_dir = Path(runtime_dir)

    def _path_for(self, job_id: str) -> Path:
        path = (self._runtime_dir / f"running-{job_id}.json").resolve()
        # Defense in depth beyond dispatch.extract_job_id's charset check:
        # never write outside runtime_dir regardless of how job_id arrived.
        path.relative_to(self._runtime_dir.resolve())
        return path

    def save(self, state: RunningAgentState) -> None:
        _atomic_write_json(self._path_for(state.job_id), state.to_dict())

    def load_all(self) -> list[RunningAgentState]:
        if not self._runtime_dir.is_dir():
            return []
        states: list[RunningAgentState] = []
        for path in sorted(self._runtime_dir.glob("running-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                states.append(RunningAgentState.from_dict(data))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as err:
                raise RuntimeStateError(
                    f"invalid running-state guard: {path.name}"
                ) from err
        return states

    def remove(self, job_id: str) -> None:
        path = self._path_for(job_id)
        if path.exists():
            path.unlink()

    def read_stop_request(self) -> bool:
        return (self._runtime_dir / self._STOP_REQUEST_NAME).exists()

    def clear_stop_request(self) -> None:
        path = self._runtime_dir / self._STOP_REQUEST_NAME
        if path.exists():
            path.unlink()


class TerminalStateStore:
    """Remembers origin mail IDs whose outcome is already terminal.

    Without this, DELIVERY_FAILED origin mails stay unread forever (no CLI
    ever ran to consume them), so every monitoring cycle would see
    unread > 0 and retry indefinitely, re-sending the same notification
    each time (violates SPEC.md 30章「失敗確定後の扱い」/「同じ失敗通知が
    無限に生成されない」). This index is the guard the dispatcher consults
    before launching, and it survives restarts under runtime/.
    """

    def __init__(self, runtime_dir: Path) -> None:
        self._path = Path(runtime_dir) / "terminal_mail_ids.json"
        self._delegation_path = Path(runtime_dir) / "delegated_invocations.json"

    @staticmethod
    def _validate_index(data: object, *, label: str) -> dict[str, str]:
        if not isinstance(data, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in data.items()
        ):
            raise RuntimeStateError(f"invalid {label} index")
        return data

    def _load(self) -> dict[str, str]:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise RuntimeStateError("invalid terminal mail index") from err
        validated = self._validate_index(data, label="terminal mail")
        if any(not key.isdigit() or int(key) <= 0 for key in validated):
            raise RuntimeStateError("invalid terminal mail index key")
        return validated

    def _load_delegations(self) -> dict[str, str]:
        if not self._delegation_path.is_file():
            return {}
        try:
            data = json.loads(self._delegation_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise RuntimeStateError("invalid delegated invocation index") from err
        validated = self._validate_index(data, label="delegated invocation")
        if any(not value.isdigit() or int(value) <= 0 for value in validated.values()):
            raise RuntimeStateError("invalid delegated invocation mail ID")
        return validated

    def claim_delegation(self, delegation_key: str, mail_id: int) -> bool:
        if not isinstance(delegation_key, str) or not delegation_key:
            raise RuntimeStateError("delegation key must be non-empty")
        if isinstance(mail_id, bool) or not isinstance(mail_id, int) or mail_id <= 0:
            raise RuntimeStateError("delegation mail ID must be positive")
        data = self._load_delegations()
        existing = data.get(delegation_key)
        if existing is not None:
            return int(existing) == mail_id
        data[delegation_key] = str(mail_id)
        _atomic_write_json(self._delegation_path, data)
        return True

    def mark(self, origin_mail_id: int, status: str) -> None:
        data = self._load()
        data[str(origin_mail_id)] = status
        _atomic_write_json(self._path, data)

    def is_terminal(self, origin_mail_id: int) -> bool:
        return str(origin_mail_id) in self._load()

    def get_status(self, origin_mail_id: int) -> str | None:
        return self._load().get(str(origin_mail_id))


class RunDurationGuard:
    """Enforces max_run_duration_sec for watch mode (SPEC.md 14章); 0 means unlimited."""

    def __init__(self, max_run_duration_sec: int, now_fn: Any = time.monotonic) -> None:
        self.max_run_duration_sec = max_run_duration_sec
        self._now_fn = now_fn
        self._started_at = now_fn()

    def should_stop(self) -> bool:
        if self.max_run_duration_sec == 0:
            return False
        return (self._now_fn() - self._started_at) >= self.max_run_duration_sec

    def remaining_sec(self) -> float:
        if self.max_run_duration_sec == 0:
            return float("inf")
        return max(0.0, self.max_run_duration_sec - (self._now_fn() - self._started_at))


class ForceStopRequested(BaseException):
    """Raised from the SIGINT handler on a second Ctrl+C to interrupt a blocking wait."""


class StopController:
    """Ctrl+C handling (SPEC.md 25章): first press is graceful, second forces.

    The first press must NOT raise (it would abort a blocking
    ``LaunchedProcess.wait()`` mid-CLI-run instead of waiting up to
    cli_timeout as SPEC.md requires); the second press raises so it can
    interrupt that same blocking call.
    """

    def __init__(self) -> None:
        self.stop_requested = False
        self.force_requested = False

    def handle_sigint(self, signum: int, frame: Any) -> None:
        if self.stop_requested:
            self.force_requested = True
            raise ForceStopRequested()
        self.stop_requested = True

    def install(self) -> None:
        import signal

        signal.signal(signal.SIGINT, self.handle_sigint)


class RecoveryActionKind(Enum):
    REQUEUE = "REQUEUE"
    NOTIFY_ORIGIN_SENDER = "NOTIFY_ORIGIN_SENDER"
    MARK_RESULT = "MARK_RESULT"
    MARK_COMPLETED = "MARK_COMPLETED"  # legacy compatibility


@dataclass(frozen=True)
class RecoveryAction:
    kind: RecoveryActionKind
    state: RunningAgentState
    origin_mail: dict | None = None
    reply_to_uid: str | None = None
    reply_mail_id: int | None = None
    invocation_result: str | None = None
    result_mail_uid: int | None = None
    duplicate_mail_uids: tuple[int, ...] = ()


class StaleRecoveryService:
    """Startup STALE detection and triage (SPEC.md 24章, SEQUENCE.md 4章)."""

    def __init__(
        self,
        runtime_store: RuntimeStateStore,
        mail_adapter: MailPort,
        reply_query: MailReplyQuery,
    ) -> None:
        self._runtime_store = runtime_store
        self._mail = mail_adapter
        self._query = reply_query

    def recover_on_startup(self) -> list[RecoveryAction]:
        actions: list[RecoveryAction] = []
        for state in self._runtime_store.load_all():
            if is_same_running_process(state.pid, state.process_start_time_iso):
                continue  # genuinely still running; leave it alone
            actions.append(self._triage(state))
            self._runtime_store.remove(state.job_id)
        return actions

    def _triage(self, state: RunningAgentState) -> RecoveryAction:
        origin = self._query.get_origin_mail_state(state.origin_mail_id, state.agent_uid)
        if origin is None or not origin["is_read"]:
            return RecoveryAction(kind=RecoveryActionKind.REQUEUE, state=state, origin_mail=origin)

        expected = ExpectedReply(
            job_id=state.job_id,
            sender_uid=state.agent_uid,
            recipient_uid="",
            origin_mail_id=state.origin_mail_id,
            # Shift 1ms earlier for the same reason DispatchCycle does
            # (mail_adapter/dispatch.py): find_mails' "sent_at > sent_after"
            # is strict, so a reply landing in the same millisecond as the
            # recorded launch would otherwise be spuriously excluded.
            not_before_iso=shift_ms(state.recorded_at_iso, -1),
            invocation_id=state.invocation_id,
            max_mail_id=state.origin_mail_max_id or state.origin_mail_id,
            decision_id=state.decision_id,
            parent_invocation_id=state.parent_invocation_id,
            root_invocation_id=state.root_invocation_id,
            trigger_mail_uid=state.trigger_mail_uid,
            require_structured_context=bool(
                state.invocation_id
                and state.root_invocation_id
                and state.trigger_mail_uid is not None
            ),
        )
        reply = self._query.find_terminal_reply(expected)
        if reply.found:
            return RecoveryAction(
                kind=RecoveryActionKind.MARK_RESULT,
                state=state,
                origin_mail=origin,
                reply_to_uid=origin["sender_uid"],
                reply_mail_id=reply.reply_mail_id,
                invocation_result=(
                    reply.invocation_result.value
                    if reply.invocation_result is not None
                    else None
                ),
                result_mail_uid=reply.result_mail_uid,
                duplicate_mail_uids=reply.duplicate_mail_uids,
            )
        return RecoveryAction(
            kind=RecoveryActionKind.NOTIFY_ORIGIN_SENDER,
            state=state,
            origin_mail=origin,
            reply_to_uid=origin["sender_uid"],
        )
