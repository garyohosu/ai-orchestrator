"""One dispatch pass: watch -> launch -> verify -> classify -> retry/notify -> log (SPEC.md 15,16,26,30章)."""

from __future__ import annotations

import re
import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from config import AgentDefinition
from launcher import CliLauncher, CliNotFoundError, ProcessResult, redact_command
from logging_utils import JobLogger, LogEntry
from mail_adapter import (
    ExpectedReply,
    MailPort,
    MailReplyQuery,
    ReplyVerifier,
    resolve_reply_to_uid,
)
from runtime import ForceStopRequested, RunningAgentState, RuntimeStateStore, TerminalStateStore
from timeutil import now_iso, shift_ms
from winproc import is_same_running_process

_JOB_ID_PATTERN = re.compile(r"^\[(JOB-[^\]]+)\]")
_DECISION_ID_PATTERN = re.compile(r"\[(DEC-[A-Za-z0-9._-]+)\]")
# job_id becomes a filename component (checkpoints/{job_id}.json,
# runtime/running-{job_id}.json). A mail subject is attacker/AI-controlled
# text, so anything extracted from it must be restricted to a safe charset
# before being trusted as part of a path -- otherwise a subject like
# "[JOB-..\..\evil]" could write outside those directories.
_SAFE_JOB_ID_PATTERN = re.compile(r"^JOB-[A-Za-z0-9._-]+$")


def extract_job_id(subject: str, fallback_mail_id: int, logger: JobLogger | None = None) -> str:
    match = _JOB_ID_PATTERN.match(subject or "")
    if match and _SAFE_JOB_ID_PATTERN.fullmatch(match.group(1)):
        return match.group(1)
    fallback = f"NOJOB-{fallback_mail_id}"
    if logger is not None:
        logger.log_warning(LogEntry(
            job_id=fallback,
            mail_id=fallback_mail_id,
            agent_name=None,
            command_summary=None,
            started_at=None,
            finished_at=None,
            exit_code=None,
            result="NOJOB_FALLBACK",
            error=f"Subject '{subject}' does not start with [JOB-...]. Fallback to {fallback}"
        ))
    return fallback


class OutcomeStatus(Enum):
    NO_WORK = "NO_WORK"
    DELIVERY_FAILED = "DELIVERY_FAILED"
    FAILED = "FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    NO_REPLY = "NO_REPLY"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    SUCCESS = "SUCCESS"


class OutcomeClassifier:
    """CLI結果とCLI固有の根拠付き証拠から状態を確定."""

    def classify(
        self,
        *,
        cli_launch_failed: bool,
        process_result: ProcessResult | None,
        reply_found: bool,
    ) -> OutcomeStatus:
        if cli_launch_failed:
            return OutcomeStatus.DELIVERY_FAILED
        assert process_result is not None
        if process_result.cli_evidence.rate_limited:
            return OutcomeStatus.RATE_LIMITED
        if process_result.timed_out:
            return OutcomeStatus.TIMEOUT
        if process_result.exit_code != 0:
            return OutcomeStatus.FAILED
        return OutcomeStatus.SUCCESS if reply_found else OutcomeStatus.NO_REPLY


class RetryPolicy:
    """Retries only when the origin mail is clearly still unreceived (SPEC.md 26章)."""

    def __init__(self, max_retries: int) -> None:
        self.max_retries = max_retries

    def should_retry(self, status: OutcomeStatus, attempt: int, mail_received: bool) -> bool:
        if mail_received:
            return False
        if status != OutcomeStatus.DELIVERY_FAILED:
            return False
        return attempt <= self.max_retries


class RoundTripCounter:
    """Count of confirmed round trips per job_id (SPEC.md 10,25章).

    Only a completed leg with a verified reply (SUCCESS) increments the
    counter, since completion vs. a mere status update cannot be
    distinguished from outside the AI (see README「既知の制限」).

    When ``runtime_dir`` is given, counts survive an orchestrator restart
    (SPEC.md 30章の受け入れ条件「無限往復を制限できる」は再起動をまたいで
    も成立する必要がある); without it (e.g. in unit tests), counts are
    in-process only.
    """

    def __init__(self, max_round_trips: int, runtime_dir: Path | None = None) -> None:
        self.max_round_trips = max_round_trips
        self._path = Path(runtime_dir) / "round_trips.json" if runtime_dir is not None else None
        self._counts: dict[str, int] = self._load()

    def _load(self) -> dict[str, int]:
        if self._path is None or not self._path.is_file():
            return {}
        import json

        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): value
            for key, value in data.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }

    def _save(self) -> None:
        if self._path is None:
            return
        import json
        import os

        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._counts, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    def count(self, job_id: str) -> int:
        return self._counts.get(job_id, 0)

    def increment(self, job_id: str) -> int:
        self._counts[job_id] = self._counts.get(job_id, 0) + 1
        self._save()
        return self._counts[job_id]

    def is_exceeded(self, job_id: str) -> bool:
        return self.count(job_id) >= self.max_round_trips


@dataclass(frozen=True)
class NotificationDetail:
    target_agent_name: str
    target_agent_uid: str
    failed_stage: str
    reason: str
    exit_code: int | None
    duration_sec: float | None
    retry_count: int
    last_attempt_at: str
    origin_mail_status: str
    recommended_action: str
    stdout_tail: str = ""
    stderr_tail: str = ""
    stdout_artifact: dict[str, object] | None = None
    stderr_artifact: dict[str, object] | None = None
    classification: dict[str, object] | None = None
    handoff_count: int = 0
    visited_agents: list[str] = field(default_factory=list)
    handoff_reason: str | None = None
    decision_id: str | None = None
    timeout_sec: int | None = None
    invocation_id: str = ""
    launch_started_at: str = ""


_UID_PATTERN = re.compile(r"^UID[0-9]{6,}$")

_SUBJECT_TAGS = {
    OutcomeStatus.DELIVERY_FAILED: "DELIVERY_FAILED",
    OutcomeStatus.FAILED: "FAILED",
    OutcomeStatus.RATE_LIMITED: "RATE_LIMITED",
    OutcomeStatus.TIMEOUT: "TIMEOUT",
    OutcomeStatus.NO_REPLY: "NO_REPLY",
    OutcomeStatus.HUMAN_REQUIRED: "HUMAN_REQUIRED",
}


class ErrorNotifier:
    """System-sender error notification with no recursive notification (SPEC.md 30章)."""

    def __init__(self, mail_adapter: MailPort, system_sender_uid: str, tail_bytes: int = 8 * 1024) -> None:
        self._mail = mail_adapter
        self._system_sender_uid = system_sender_uid
        self._tail_bytes = tail_bytes

    def is_terminal_recipient_invalid(self, recipient_uid: str | None) -> bool:
        if not recipient_uid or not _UID_PATTERN.fullmatch(recipient_uid):
            return True
        if recipient_uid == self._system_sender_uid:
            return True
        return False

    def notify(
        self,
        job_id: str,
        origin_mail_id: int,
        origin_sender_uid: str,
        status: OutcomeStatus,
        detail: NotificationDetail,
    ) -> bool:
        if self.is_terminal_recipient_invalid(origin_sender_uid):
            return False
        subject = self._build_subject(job_id, status, detail.target_agent_name)
        body = self._build_body(job_id, origin_mail_id, origin_sender_uid, status, detail)
        try:
            self._mail.send_mail(self._system_sender_uid, origin_sender_uid, subject, body)
        except Exception:
            return False
        return True

    def _build_subject(self, job_id: str, status: OutcomeStatus, agent_name: str) -> str:
        tag = _SUBJECT_TAGS.get(status, status.value)
        return f"[{job_id}][{tag}] {agent_name}の処理に失敗"

    def _build_body(
        self,
        job_id: str,
        origin_mail_id: int,
        origin_sender_uid: str,
        status: OutcomeStatus,
        detail: NotificationDetail,
    ) -> str:
        lines = [
            f"status: {'TIMED_OUT' if status == OutcomeStatus.TIMEOUT else status.value}",
            f"job_id: {job_id}",
            f"decision_id: {detail.decision_id or 'unknown'}",
            f"invocation_id: {detail.invocation_id or 'unknown'}",
            f"cli_started_at: {detail.launch_started_at or 'unknown'}",
            f"agent_uid: {detail.target_agent_uid}",
            f"exit_code: {detail.exit_code if detail.exit_code is not None else 'unknown'}",
            f"timeout_sec: {detail.timeout_sec if detail.timeout_sec is not None else 'unknown'}",
            f"stdout_log: {(detail.stdout_artifact or {}).get('relative_path', 'none')}",
            f"stderr_log: {(detail.stderr_artifact or {}).get('relative_path', 'none')}",
            f"occurred_at: {detail.last_attempt_at}",
            f"状態: {status.value}",
            f"依頼ID: {job_id}",
            f"元メールID: {origin_mail_id}",
            f"元の送信者UID: {origin_sender_uid}",
            f"処理対象AI: {detail.target_agent_name}",
            f"処理対象UID: {detail.target_agent_uid}",
            f"失敗段階: {detail.failed_stage}",
            f"失敗理由: {detail.reason}",
            f"CLI終了コード: {detail.exit_code if detail.exit_code is not None else '取得不可'}",
            f"実行時間: {detail.duration_sec if detail.duration_sec is not None else '不明'}秒",
            f"再試行回数: {detail.retry_count}",
            f"最終試行日時: {detail.last_attempt_at}",
            f"元メールの状態: {detail.origin_mail_status}",
            f"判定根拠: {detail.classification or 'なし'}",
            f"stdout証跡: {detail.stdout_artifact or 'なし'}",
            f"stderr証跡: {detail.stderr_artifact or 'なし'}",
            f"引継ぎ回数: {detail.handoff_count}",
            f"担当履歴: {detail.visited_agents}",
            "stdout末尾:",
            self._bounded_tail(detail.stdout_tail),
            "stderr末尾:",
            self._bounded_tail(detail.stderr_tail),
            "",
            "推奨する次の対応:",
            detail.recommended_action,
        ]
        return "\n".join(lines)

    def _bounded_tail(self, text: str) -> str:
        data = text.encode("utf-8")
        if len(data) <= self._tail_bytes:
            return text
        return "[TRUNCATED]\n" + data[-self._tail_bytes :].decode("utf-8", errors="replace")


@dataclass(frozen=True)
class Checkpoint:
    job_id: str
    purpose: str
    current_state: str
    decisions: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    open_issues: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    handoff_count: int = 0
    visited_agents: list[str] = field(default_factory=list)
    handoff_history: list[dict[str, str]] = field(default_factory=list)
    updated_at: str = field(default_factory=now_iso)


class CheckpointStore:
    def __init__(self, checkpoints_dir: Path) -> None:
        self._dir = Path(checkpoints_dir)

    def _path_for(self, job_id: str) -> Path:
        path = (self._dir / f"{job_id}.json").resolve()
        # Defense in depth beyond dispatch.extract_job_id's charset check:
        # never write outside checkpoints_dir regardless of how job_id
        # arrived (see runtime.RuntimeStateStore._path_for for the same
        # reasoning).
        path.relative_to(self._dir.resolve())
        return path

    def save(self, checkpoint: Checkpoint) -> None:
        import json
        import os

        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path_for(checkpoint.job_id)
        tmp = path.with_suffix(".json.tmp")
        data = {
            "job_id": checkpoint.job_id,
            "purpose": checkpoint.purpose,
            "current_state": checkpoint.current_state,
            "decisions": checkpoint.decisions,
            "artifacts": checkpoint.artifacts,
            "open_issues": checkpoint.open_issues,
            "next_actions": checkpoint.next_actions,
            "handoff_count": checkpoint.handoff_count,
            "visited_agents": checkpoint.visited_agents,
            "handoff_history": checkpoint.handoff_history,
            "updated_at": checkpoint.updated_at,
        }
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def load(self, job_id: str) -> Checkpoint | None:
        import json

        path = self._path_for(job_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return Checkpoint(**data)


class MailWatcher:
    """Unread polling and metadata retrieval that never consumes mail (QandA Q013)."""

    def __init__(self, mail_adapter: MailPort) -> None:
        self._mail = mail_adapter

    def has_unread(self, agent: AgentDefinition) -> int:
        return self._mail.check_mail(agent.uid)

    def poll_unread(self, agents: list[AgentDefinition]) -> list[AgentDefinition]:
        return [agent for agent in agents if self.has_unread(agent) > 0]

    def list_unread_mails(self, agent: AgentDefinition) -> list[dict]:
        return self._mail.find_mails(recipient_uid=agent.uid, is_read=False)


@dataclass(frozen=True)
class AgentOutcome:
    agent: AgentDefinition
    status: OutcomeStatus
    job_id: str | None = None
    origin_mail_id: int | None = None


class DispatchCycle:
    """Runs the launch/verify/classify/retry/notify pipeline for one agent."""

    def __init__(
        self,
        *,
        watcher: MailWatcher,
        launcher: CliLauncher,
        mail_adapter: MailPort,
        reply_query: MailReplyQuery,
        classifier: OutcomeClassifier,
        retry_policy: RetryPolicy,
        notifier: ErrorNotifier,
        logger: JobLogger,
        checkpoint_store: CheckpointStore,
        round_trips: RoundTripCounter,
        runtime_store: RuntimeStateStore,
        terminal_store: TerminalStateStore,
        project_path: Path,
        cli_timeout_sec: int,
        reply_check_timeout_sec: int,
        agents: list[AgentDefinition] | None = None,
        system_sender_uid: str | None = None,
        max_handoffs: int = 3,
        terminal_poll_interval_sec: float = 1.0,
        terminal_grace_sec: float = 2.0,
    ) -> None:
        self._watcher = watcher
        self._launcher = launcher
        self._mail = mail_adapter
        self._reply_query = reply_query
        self._reply_verifier = ReplyVerifier(reply_query)
        self._classifier = classifier
        self._retry_policy = retry_policy
        self._notifier = notifier
        self._logger = logger
        self._checkpoint_store = checkpoint_store
        self._round_trips = round_trips
        self._runtime_store = runtime_store
        self._terminal_store = terminal_store
        self._project_path = project_path
        self._cli_timeout_sec = cli_timeout_sec
        self._reply_check_timeout_sec = reply_check_timeout_sec
        self._agents_by_name = {a.name: a for a in (agents or [])}
        self._system_sender_uid = system_sender_uid
        self._max_handoffs = max_handoffs
        self._terminal_poll_interval_sec = terminal_poll_interval_sec
        self._terminal_grace_sec = terminal_grace_sec

    def run_one_pass(
        self, agents: list[AgentDefinition], should_stop_launching: Callable[[], bool] | None = None
    ) -> list[AgentOutcome]:
        outcomes: list[AgentOutcome] = []
        for agent in sorted(agents, key=lambda a: a.order_index):
            if should_stop_launching is not None and should_stop_launching():
                break
            outcome = self.process_agent(agent)
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    def process_agent(self, agent: AgentDefinition) -> AgentOutcome | None:
        if self._is_agent_running(agent):
            return None

        unread_mails = self._watcher.list_unread_mails(agent)
        pending_mails = [m for m in unread_mails if not self._terminal_store.is_terminal(m["mail_id"])]
        if not pending_mails:
            return None

        origin_mail = pending_mails[0]
        job_id = extract_job_id(origin_mail["subject"], origin_mail["mail_id"], logger=self._logger)
        decision_match = _DECISION_ID_PATTERN.search(origin_mail.get("subject", ""))
        decision_id = decision_match.group(1) if decision_match else None

        if self._round_trips.is_exceeded(job_id):
            self._finalize_without_notify(
                agent, job_id, origin_mail, OutcomeStatus.HUMAN_REQUIRED, "最大往復回数へ到達しました"
            )
            return AgentOutcome(agent, OutcomeStatus.HUMAN_REQUIRED, job_id, origin_mail["mail_id"])

        status, process_result, retry_count = self._launch_with_retries(agent, job_id, origin_mail)

        if process_result is not None and process_result.terminal_status is not None:
            terminal_status = process_result.terminal_status
            if status == OutcomeStatus.SUCCESS:
                self._round_trips.increment(job_id)
            self._terminal_store.mark(origin_mail["mail_id"], terminal_status)
            self._checkpoint_store.save(
                Checkpoint(
                    job_id=job_id,
                    purpose=origin_mail["subject"],
                    current_state=terminal_status,
                    next_actions=["directorへ次の処理を委任" if terminal_status == "WAITING_FOR_DECISION" else "終端状態を保持"],
                )
            )
            self._logger.log_outcome(
                LogEntry(
                    job_id=job_id,
                    mail_id=origin_mail["mail_id"],
                    agent_name=agent.name,
                    command_summary=None,
                    started_at=None,
                    finished_at=now_iso(),
                    exit_code=process_result.exit_code,
                    result=status.value,
                    stdout_artifact=self._artifact_dict(process_result, "stdout"),
                    stderr_artifact=self._artifact_dict(process_result, "stderr"),
                    classification=self._classification_dict(process_result),
                )
            )
            return AgentOutcome(agent, status, job_id, origin_mail["mail_id"])

        if status == OutcomeStatus.SUCCESS:
            self._round_trips.increment(job_id)
            self._terminal_store.mark(origin_mail["mail_id"], status.value)
            self._checkpoint_store.save(
                Checkpoint(
                    job_id=job_id,
                    purpose=origin_mail["subject"],
                    current_state="SUCCESS",
                    next_actions=["指揮AI/作業AIによる次の往復を待機"],
                )
            )
            self._logger.log_outcome(
                LogEntry(
                    job_id=job_id,
                    mail_id=origin_mail["mail_id"],
                    agent_name=agent.name,
                    command_summary=None,
                    started_at=None,
                    finished_at=now_iso(),
                    exit_code=process_result.exit_code if process_result else None,
                    result=status.value,
                    next_recipient=None,
                    stdout_artifact=self._artifact_dict(process_result, "stdout"),
                    stderr_artifact=self._artifact_dict(process_result, "stderr"),
                    classification=self._classification_dict(process_result),
                )
            )
            return AgentOutcome(agent, status, job_id, origin_mail["mail_id"])

        if status == OutcomeStatus.RATE_LIMITED:
            handoff_outcome = self._handoff_rate_limited(
                agent, job_id, origin_mail, process_result, retry_count
            )
            if handoff_outcome is not None:
                return handoff_outcome
            status = OutcomeStatus.HUMAN_REQUIRED

        # Terminal failure: notify origin sender, mark terminal, checkpoint, log.
        detail = NotificationDetail(
            target_agent_name=agent.name,
            target_agent_uid=agent.uid,
            failed_stage=self._failed_stage_for(status),
            reason=self._reason_for(status),
            exit_code=process_result.exit_code if process_result else None,
            duration_sec=process_result.duration_sec if process_result else None,
            retry_count=retry_count,
            last_attempt_at=now_iso(),
            origin_mail_status="処理失敗",
            recommended_action=self._recommended_action_for(status, agent),
            stdout_tail=self._notification_tail(process_result, "stdout"),
            stderr_tail=self._notification_tail(process_result, "stderr"),
            stdout_artifact=self._artifact_dict(process_result, "stdout"),
            stderr_artifact=self._artifact_dict(process_result, "stderr"),
            classification=self._classification_dict(process_result),
            decision_id=decision_id,
            timeout_sec=self._cli_timeout_sec if status == OutcomeStatus.TIMEOUT else None,
            invocation_id=getattr(process_result, "invocation_id", ""),
            launch_started_at=getattr(process_result, "launch_started_at", ""),
        )
        notified = self._notifier.notify(
            job_id, origin_mail["mail_id"], origin_mail["sender_uid"], status, detail
        )
        final_status = status if notified else OutcomeStatus.HUMAN_REQUIRED
        self._terminal_store.mark(origin_mail["mail_id"], final_status.value)
        self._checkpoint_store.save(
            Checkpoint(
                job_id=job_id,
                purpose=origin_mail["subject"],
                current_state=final_status.value,
                open_issues=[detail.reason],
                next_actions=["人間による確認が必要です"],
            )
        )
        self._logger.log_outcome(
            LogEntry(
                job_id=job_id,
                mail_id=origin_mail["mail_id"],
                agent_name=agent.name,
                command_summary=None,
                started_at=None,
                finished_at=now_iso(),
                exit_code=process_result.exit_code if process_result else None,
                result=final_status.value,
                error=detail.reason,
                next_recipient=origin_mail["sender_uid"] if notified else None,
                stdout_artifact=detail.stdout_artifact,
                stderr_artifact=detail.stderr_artifact,
                classification=detail.classification,
            )
        )
        return AgentOutcome(agent, final_status, job_id, origin_mail["mail_id"])

    @staticmethod
    def _artifact_dict(result: ProcessResult | None, stream: str) -> dict[str, object] | None:
        artifact = getattr(result, stream, None) if result is not None else None
        return artifact.as_dict() if artifact is not None else None

    @staticmethod
    def _notification_tail(result: ProcessResult | None, stream: str) -> str:
        artifact = getattr(result, stream, None) if result is not None else None
        return artifact.tail if artifact is not None else ""

    @staticmethod
    def _classification_dict(result: ProcessResult | None) -> dict[str, object] | None:
        if result is None:
            return None
        evidence = result.cli_evidence
        return {
            "source": "cli_adapter",
            "rule_id": evidence.rule_id,
            "stream": evidence.stream,
            "evidence": evidence.evidence,
        }

    def _handoff_rate_limited(
        self, agent: AgentDefinition, job_id: str, origin_mail: dict,
        result: ProcessResult | None, retry_count: int,
    ) -> AgentOutcome | None:
        checkpoint = self._checkpoint_store.load(job_id)
        visited = list(checkpoint.visited_agents if checkpoint else [])
        if agent.name not in visited:
            visited.append(agent.name)
        handoff_count = checkpoint.handoff_count if checkpoint else 0
        history = list(checkpoint.handoff_history if checkpoint else [])
        if handoff_count >= self._max_handoffs or self._system_sender_uid is None:
            return None
        candidates = [
            self._agents_by_name[name] for name in agent.fallback_agents
            if name in self._agents_by_name
            and name not in visited
            and self._agents_by_name[name].uid not in {agent.uid}
        ]
        if not candidates:
            return None
        candidate = None
        for candidate_option in candidates:
            body = self._build_handoff_body(
                agent, candidate_option, job_id, origin_mail, result, handoff_count + 1, visited
            )
            subject = f"[{job_id}][HANDOFF] {agent.name}から{candidate_option.name}へ引継ぎ"
            try:
                self._checkpoint_store.save(Checkpoint(
                    job_id=job_id,
                    purpose=origin_mail["subject"],
                    current_state="HANDOFF_PENDING",
                    open_issues=["RATE_LIMITED"],
                    next_actions=[f"{candidate_option.name}へ引継ぎメールを送信"],
                    handoff_count=handoff_count,
                    visited_agents=visited,
                    handoff_history=history,
                ))
                existing = self._mail.find_mails(
                    sender_uid=self._system_sender_uid,
                    recipient_uid=candidate_option.uid,
                    request_id=job_id,
                    limit=1,
                )
                if not existing:
                    self._mail.send_mail(self._system_sender_uid, candidate_option.uid, subject, body)
                candidate = candidate_option
                break
            except (OSError, ValueError, RuntimeError):
                candidate = None
                continue
            except Exception:
                candidate = None
                continue
        if candidate is None:
            return None
        history.append({
            "from_agent": agent.name,
            "to_agent": candidate.name,
            "reason": "RATE_LIMITED",
            "at": now_iso(),
            "classification_rule": result.cli_evidence.rule_id if result else "unknown",
        })
        try:
            self._checkpoint_store.save(Checkpoint(
                job_id=job_id,
                purpose=origin_mail["subject"],
                current_state="HANDOFF_SENT",
                open_issues=["RATE_LIMITED"],
                next_actions=[f"{candidate.name}の引継ぎメール処理を待機"],
                handoff_count=handoff_count + 1,
                visited_agents=visited + [candidate.name],
                handoff_history=history,
            ))
        except OSError:
            return None
        self._terminal_store.mark(origin_mail["mail_id"], OutcomeStatus.RATE_LIMITED.value)
        self._logger.log_outcome(LogEntry(
            job_id=job_id,
            mail_id=origin_mail["mail_id"],
            agent_name=agent.name,
            command_summary=None,
            started_at=None,
            finished_at=now_iso(),
            exit_code=result.exit_code if result else None,
            result=OutcomeStatus.RATE_LIMITED.value,
            next_recipient=candidate.uid,
            stdout_artifact=self._artifact_dict(result, "stdout"),
            stderr_artifact=self._artifact_dict(result, "stderr"),
            classification=self._classification_dict(result),
            handoff_count=handoff_count + 1,
            visited_agents=visited + [candidate.name],
            handoff_reason="RATE_LIMITED",
        ))
        return AgentOutcome(agent, OutcomeStatus.RATE_LIMITED, job_id, origin_mail["mail_id"])

    def _build_handoff_body(self, agent, candidate, job_id, origin_mail, result, count, visited):
        evidence = result.cli_evidence if result else None
        return "\n".join([
            f"状態: RATE_LIMITED",
            f"依頼ID: {job_id}",
            f"元メールID: {origin_mail['mail_id']}",
            f"元の送信者UID: {origin_mail['sender_uid']}",
            f"前担当AI: {agent.name} ({agent.uid})",
            f"次担当AI: {candidate.name} ({candidate.uid})",
            f"引継ぎ回数: {count}",
            f"担当履歴: {visited}",
            f"判定規則: {evidence.rule_id if evidence else 'unknown'}",
            f"判定根拠: {evidence.evidence if evidence else 'unknown'}",
            "引継ぎ情報: checkpointsの依頼IDファイルを確認してください。",
        ])

    def _is_agent_running(self, agent: AgentDefinition) -> bool:
        # A leftover running-*.json should only exist for a genuinely
        # running process (StaleRecoveryService clears everything else at
        # startup, and _attempt_launch removes its own entry when done).
        # Verifying PID+start_time here anyway is cheap and removes the
        # implicit dependency on that call ordering, so a stale entry from
        # any other source can never permanently block this agent.
        still_running = False
        for state in self._runtime_store.load_all():
            if state.agent_uid != agent.uid:
                continue
            if is_same_running_process(state.pid, state.process_start_time_iso):
                still_running = True
            else:
                self._runtime_store.remove(state.job_id)
        return still_running

    def _launch_with_retries(
        self, agent: AgentDefinition, job_id: str, origin_mail: dict
    ) -> tuple[OutcomeStatus, ProcessResult | None, int]:
        attempt = 1
        status = OutcomeStatus.DELIVERY_FAILED
        process_result: ProcessResult | None = None
        while True:
            status, process_result = self._attempt_launch(agent, job_id, origin_mail, attempt)
            mail_received = status != OutcomeStatus.DELIVERY_FAILED
            if self._retry_policy.should_retry(status, attempt, mail_received):
                attempt += 1
                continue
            return status, process_result, attempt - 1

    def _attempt_launch(
        self, agent: AgentDefinition, job_id: str, origin_mail: dict, attempt: int
    ) -> tuple[OutcomeStatus, ProcessResult | None]:
        decision_id = ""
        subj = origin_mail.get("subject", "")
        dec_match = re.search(r"\[(DEC-[A-Za-z0-9._-]+)\]", subj)
        if dec_match:
            decision_id = dec_match.group(1)
        invocation_id = f"INV-{now_iso().replace('-', '').replace(':', '').replace('.', '')}-{attempt:03d}-{uuid.uuid4().hex[:8].upper()}"
        origin_mail_max_id = max(
            (int(mail.get("mail_id", 0)) for mail in self._mail.find_mails(limit=None)), default=0
        )

        env_vars = {
            "AGENT_UID": agent.uid,
            "REPLY_TO_UID": origin_mail.get("sender_uid", ""),
            "JOB_ID": job_id,
            "DECISION_ID": decision_id,
            "INVOCATION_ID": invocation_id,
            "PROJECT_PATH": str(self._project_path),
        }
        if hasattr(self._mail, "_db_path") and getattr(self._mail, "_db_path"):
            env_vars["AGENT_MAIL_DB_PATH"] = str(getattr(self._mail, "_db_path"))

        try:
            try:
                launched = self._launcher.launch(
                    agent, job_id, origin_mail["mail_id"], self._project_path,
                    attempt=attempt, env_vars=env_vars, invocation_id=invocation_id
                )
            except TypeError as err:
                # Preserve compatibility with test/dedicated launchers that
                # implement the pre-output-capture four-argument contract.
                if "attempt" not in str(err) and "env_vars" not in str(err):
                    raise
                launched = self._launcher.launch(
                    agent, job_id, origin_mail["mail_id"], self._project_path
                )
        except CliNotFoundError:
            return OutcomeStatus.DELIVERY_FAILED, None

        # Redact before persisting anywhere: runtime/ and logs/ must never
        # contain secrets (SPEC.md 23章), and this is the one place both
        # storage boundaries derive their command text from.
        redacted_command = redact_command(launched.launch_command)
        state = RunningAgentState(
            pid=launched.pid,
            process_start_time_iso=launched.start_time_iso,
            agent_name=agent.name,
            agent_uid=agent.uid,
            job_id=job_id,
            origin_mail_id=origin_mail["mail_id"],
            invocation_id=invocation_id,
            origin_mail_max_id=origin_mail_max_id,
            decision_id=decision_id,
            launch_command=redacted_command,
            recorded_at_iso=launched.launched_at_iso,
            retry_count=attempt - 1,
        )
        self._runtime_store.save(state)
        self._logger.log_launch(
            LogEntry(
                job_id=job_id,
                mail_id=origin_mail["mail_id"],
                agent_name=agent.name,
                command_summary=" ".join(redacted_command),
                started_at=launched.launched_at_iso,
                finished_at=None,
                exit_code=None,
                result="LAUNCHED",
            )
        )
        try:
            process_result = launched.wait(
                self._cli_timeout_sec,
                terminal_reply_check=self._terminal_reply_checker(agent, origin_mail, job_id, invocation_id, origin_mail_max_id),
                poll_interval_sec=self._terminal_poll_interval_sec,
                terminal_grace_sec=self._terminal_grace_sec,
            )
        except ForceStopRequested:
            # Second Ctrl+C: SPEC.md 25章 requires the process be force-
            # terminated, the fact recorded, and the origin sender notified
            # before propagating the stop.
            launched.terminate()
            confirmed = launched.has_exited()
            try:
                stdout_artifact, stderr_artifact = launched.finish_capture()
            except Exception:
                stdout_artifact = stderr_artifact = None
            detail = NotificationDetail(
                target_agent_name=agent.name,
                target_agent_uid=agent.uid,
                failed_stage="CLI実行中",
                reason="常時監視モードが強制停止されました（二重Ctrl+C）",
                exit_code=None,
                duration_sec=None,
                retry_count=attempt - 1,
                last_attempt_at=now_iso(),
                origin_mail_status="処理中断",
                recommended_action="オーケストレーターを再起動してください。次回起動時にSTALE復旧が行われます。",
                stdout_tail=stdout_artifact.tail if stdout_artifact else "",
                stderr_tail=stderr_artifact.tail if stderr_artifact else "",
                stdout_artifact=stdout_artifact.as_dict() if stdout_artifact else None,
                stderr_artifact=stderr_artifact.as_dict() if stderr_artifact else None,
                invocation_id=invocation_id,
                launch_started_at=launched.launched_at_iso,
            )
            notified = self._notifier.notify(
                job_id, origin_mail["mail_id"], origin_mail["sender_uid"],
                OutcomeStatus.HUMAN_REQUIRED, detail,
            )
            self._terminal_store.mark(origin_mail["mail_id"], OutcomeStatus.HUMAN_REQUIRED.value)
            self._checkpoint_store.save(
                Checkpoint(
                    job_id=job_id,
                    purpose=origin_mail["subject"],
                    current_state=OutcomeStatus.HUMAN_REQUIRED.value,
                    open_issues=[detail.reason],
                    next_actions=(
                        ["人間による確認が必要です"]
                        if notified
                        else ["人間による確認が必要です（通知の送信にも失敗しました）"]
                    ),
                )
            )
            if confirmed:
                self._runtime_store.remove(job_id)
            # else: leave the runtime entry in place -- termination could
            # not be confirmed, so the double-launch guard must stay active
            # until the next startup's STALE recovery re-verifies the PID.
            raise
        else:
            if process_result.terminated_confirmed:
                self._runtime_store.remove(job_id)
            # else: a timeout-triggered terminate() could not be confirmed;
            # keep the runtime entry so this agent is not double-launched
            # while a possibly-still-alive process lingers (SPEC.md 24章).
            # STALE recovery will re-verify and clean it up on next startup.

        reply_found = False
        if not process_result.timed_out and process_result.exit_code == 0:
            reply_to_uid = resolve_reply_to_uid(self._mail, origin_mail)
            # Shift 1ms earlier than the recorded launch instant so a reply
            # landing in the same millisecond is not spuriously excluded by
            # find_mails' strict "sent_at > sent_after" comparison.
            not_before = shift_ms(launched.launched_at_iso, -1)
            expected = ExpectedReply(
                job_id=job_id,
                sender_uid=agent.uid,
                recipient_uid=reply_to_uid,
                origin_mail_id=origin_mail["mail_id"],
                not_before_iso=not_before,
                invocation_id=invocation_id,
                max_mail_id=origin_mail_max_id,
                decision_id=decision_id,
            )
            reply_result = self._reply_verifier.wait_for_terminal_reply(
                expected, self._reply_check_timeout_sec
            )
            reply_found = reply_result.found

        terminal_status = process_result.terminal_status
        if terminal_status in {"WAITING_FOR_DECISION", "COMPLETED"}:
            status = OutcomeStatus.SUCCESS
            reply_found = True
        elif terminal_status == "HUMAN_REQUIRED":
            status = OutcomeStatus.HUMAN_REQUIRED
        elif terminal_status in {"FAILED", "REJECTED", "CANCELLED"}:
            status = OutcomeStatus.FAILED
        else:
            status = self._classifier.classify(
                cli_launch_failed=False, process_result=process_result, reply_found=reply_found
            )
        return status, process_result

    def _terminal_reply_checker(self, agent: AgentDefinition, origin_mail: dict, job_id: str, invocation_id: str, origin_mail_max_id: int):
        reply_to_uid = resolve_reply_to_uid(self._mail, origin_mail)
        not_before = shift_ms(origin_mail.get("sent_at", now_iso()), -1)

        def check():
            result = self._reply_query.find_terminal_reply(ExpectedReply(
                job_id=job_id, sender_uid=agent.uid, recipient_uid=reply_to_uid,
                origin_mail_id=origin_mail["mail_id"], not_before_iso=not_before,
                invocation_id=invocation_id, max_mail_id=origin_mail_max_id,
                decision_id=_DECISION_ID_PATTERN.search(origin_mail.get("subject", "")).group(1) if _DECISION_ID_PATTERN.search(origin_mail.get("subject", "")) else "",
            ))
            return (result.status, result.reply_mail_id) if result.found else None
        return check

    def _finalize_without_notify(
        self, agent: AgentDefinition, job_id: str, origin_mail: dict, status: OutcomeStatus, reason: str
    ) -> None:
        self._terminal_store.mark(origin_mail["mail_id"], status.value)
        self._checkpoint_store.save(
            Checkpoint(
                job_id=job_id,
                purpose=origin_mail["subject"],
                current_state=status.value,
                open_issues=[reason],
                next_actions=["人間による確認が必要です"],
            )
        )
        self._logger.log_outcome(
            LogEntry(
                job_id=job_id,
                mail_id=origin_mail["mail_id"],
                agent_name=agent.name,
                command_summary=None,
                started_at=None,
                finished_at=now_iso(),
                exit_code=None,
                result=status.value,
                error=reason,
            )
        )

    @staticmethod
    def _failed_stage_for(status: OutcomeStatus) -> str:
        return {
            OutcomeStatus.DELIVERY_FAILED: "CLI起動",
            OutcomeStatus.FAILED: "CLI実行",
            OutcomeStatus.TIMEOUT: "CLI実行",
            OutcomeStatus.NO_REPLY: "返信確認",
        }.get(status, "不明")

    @staticmethod
    def _reason_for(status: OutcomeStatus) -> str:
        return {
            OutcomeStatus.DELIVERY_FAILED: "CLIを起動できませんでした",
            OutcomeStatus.FAILED: "CLIが0以外の終了コードで終了しました",
            OutcomeStatus.TIMEOUT: "制限時間内に処理が終了しませんでした",
            OutcomeStatus.NO_REPLY: "CLIは正常終了しましたが、返信メールが確認できませんでした",
        }.get(status, "不明な失敗です")

    @staticmethod
    def _recommended_action_for(status: OutcomeStatus, agent: AgentDefinition) -> str:
        return (
            f"{agent.name}の認証状態とプロセスを確認し、必要であれば同じ依頼IDで再送してください。"
        )
