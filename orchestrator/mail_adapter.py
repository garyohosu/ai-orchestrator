"""The single boundary orchestrator uses to talk to the sibling mail package.

SPEC.md 7章: orchestrator must only call mail's public functions and must
never know SQLite table/column names or connect to the database directly.
This module loads the sibling ``mail`` package by file path (never by
mutating PYTHONPATH or relying on the current working directory) and
fails loudly if it is missing -- no silent in-memory fallback in real
runs (see the user's instruction and CLASS.md 3章). An in-memory test
double implementing the same interface lives in tests/fakes.py and is
only ever used when a test explicitly injects it.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class MailPackageNotFoundError(RuntimeError):
    """The sibling mail/ package could not be found or loaded."""


class MailPort(Protocol):
    """The six mail functions orchestrator is allowed to call (SPEC.md 7章)."""

    def register_user(self, name: str) -> str: ...
    def list_users(self) -> list[dict]: ...
    def send_mail(self, sender_uid: str, recipient_uid: str, subject: str, body: str) -> int: ...
    def check_mail(self, uid: str) -> int: ...
    def receive_mail(self, uid: str) -> list[dict]: ...
    def find_mails(
        self,
        *,
        sender_uid: str | None = None,
        recipient_uid: str | None = None,
        request_id: str | None = None,
        after_mail_id: int | None = None,
        sent_after: str | None = None,
        is_read: bool | None = None,
        limit: int | None = None,
    ) -> list[dict]: ...


class MailModuleAdapter:
    """Thin wrapper around the real, sibling ``mail`` package (SPEC.md 7章)."""

    def __init__(self, mail_dir: Path, db_path: Path | None = None) -> None:
        self._mail_dir = Path(mail_dir)
        self._db_path = db_path
        self._module = self._load_module()
        self.errors = types.SimpleNamespace(
            AgentMailError=self._module.AgentMailError,
            InvalidInputError=self._module.InvalidInputError,
            InvalidNameError=self._module.InvalidNameError,
            InvalidUidError=self._module.InvalidUidError,
            UserNotFoundError=self._module.UserNotFoundError,
            DatabaseError=self._module.DatabaseError,
        )

    def _load_module(self) -> Any:
        cached = sys.modules.get("mail")
        if cached is not None and getattr(cached, "__file__", None) == str(
            self._mail_dir / "__init__.py"
        ):
            return cached
        init_path = self._mail_dir / "__init__.py"
        if not init_path.is_file():
            raise MailPackageNotFoundError(
                f"mail package not found at {self._mail_dir}. orchestrator "
                "requires a sibling mail/ folder (SPEC.md 4章, 7章); it does "
                "not fall back to an in-memory implementation at runtime."
            )
        spec = importlib.util.spec_from_file_location(
            "mail", init_path, submodule_search_locations=[str(self._mail_dir)]
        )
        if spec is None or spec.loader is None:
            raise MailPackageNotFoundError(f"could not load mail package from {init_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules["mail"] = module
        try:
            spec.loader.exec_module(module)
        except Exception as err:
            del sys.modules["mail"]
            raise MailPackageNotFoundError(f"failed to import mail package: {err}") from err
        return module

    def register_user(self, name: str) -> str:
        return self._module.register_user(name, db_path=self._db_path)

    def list_users(self) -> list[dict]:
        return self._module.list_users(db_path=self._db_path)

    def send_mail(self, sender_uid: str, recipient_uid: str, subject: str, body: str) -> int:
        return self._module.send_mail(sender_uid, recipient_uid, subject, body, db_path=self._db_path)

    def check_mail(self, uid: str) -> int:
        return self._module.check_mail(uid, db_path=self._db_path)

    def receive_mail(self, uid: str) -> list[dict]:
        # SPEC.md 15章 assigns metadata retrieval for dispatch decisions to
        # find_mails (QandA Q013); this wrapper exists only because SPEC.md
        # 7章 lists receive_mail among the six allowed functions, and the
        # AI CLI process itself calls it after being launched.
        return self._module.receive_mail(uid, db_path=self._db_path)

    def find_mails(
        self,
        *,
        sender_uid: str | None = None,
        recipient_uid: str | None = None,
        request_id: str | None = None,
        after_mail_id: int | None = None,
        sent_after: str | None = None,
        is_read: bool | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        return self._module.find_mails(
            sender_uid=sender_uid,
            recipient_uid=recipient_uid,
            request_id=request_id,
            after_mail_id=after_mail_id,
            sent_after=sent_after,
            is_read=is_read,
            limit=limit,
            db_path=self._db_path,
        )


@dataclass(frozen=True)
class ExpectedReply:
    """Reply-matching criteria for one origin mail (SPEC.md 30章「返信メールの確認」)."""

    job_id: str
    sender_uid: str
    recipient_uid: str
    origin_mail_id: int
    not_before_iso: str
    invocation_id: str = ""
    max_mail_id: int | None = None
    decision_id: str = ""


@dataclass(frozen=True)
class ReplyCheckResult:
    found: bool
    reply_mail_id: int | None = None
    status: str | None = None


class MailReplyQuery:
    """Read-only mail lookups via ``find_mails`` (never receive_mail, never SQL).

    Used by both reply verification (SPEC.md 30章) and STALE recovery
    (SPEC.md 24章), which share the same matching rule (QandA Q012).
    """

    def __init__(self, mail_adapter: MailPort) -> None:
        self._mail = mail_adapter

    def find_reply(self, expected: ExpectedReply) -> ReplyCheckResult:
        matches = self._mail.find_mails(
            sender_uid=expected.sender_uid,
            recipient_uid=expected.recipient_uid,
            request_id=expected.job_id,
            after_mail_id=expected.origin_mail_id,
            sent_after=expected.not_before_iso,
            limit=1,
        )
        if matches:
            return ReplyCheckResult(found=True, reply_mail_id=matches[0]["mail_id"])
        return ReplyCheckResult(found=False)

    @staticmethod
    def _invocation_matches(message: dict, invocation_id: str) -> bool:
        return not invocation_id or invocation_id in (message.get("subject", "") + "\n" + message.get("body", ""))

    def find_terminal_reply(self, expected: ExpectedReply) -> ReplyCheckResult:
        matches = self._mail.find_mails(
            sender_uid=expected.sender_uid,
            recipient_uid=expected.recipient_uid,
            request_id=expected.job_id,
            after_mail_id=expected.max_mail_id if expected.max_mail_id is not None else expected.origin_mail_id,
            sent_after=expected.not_before_iso,
            limit=None,
        )
        terminal = {"WAITING_FOR_DECISION", "COMPLETED", "FAILED", "HUMAN_REQUIRED", "REJECTED", "CANCELLED"}
        import json
        for message in matches:
            if not self._invocation_matches(message, expected.invocation_id):
                continue
            if expected.decision_id and expected.decision_id not in (message.get("subject", "") + "\n" + message.get("body", "")):
                continue
            status = None
            try:
                payload = json.loads(message.get("body", ""))
                if isinstance(payload, dict):
                    status = payload.get("status")
            except (TypeError, ValueError):
                pass
            if status not in terminal:
                subject = message.get("subject", "")
                body = message.get("body", "")
                status = next((candidate for candidate in terminal if candidate in subject or f"status: {candidate}" in body), None)
            if status in terminal:
                return ReplyCheckResult(True, int(message["mail_id"]), status)
        return ReplyCheckResult(False)

    def get_origin_mail_state(self, mail_id: int, recipient_uid: str) -> dict | None:
        """Return the origin mail's own row (is_read/read_at/body/sender_uid).

        ``after_mail_id=mail_id - 1`` combined with ``limit=1`` selects the
        smallest mail ID greater than ``mail_id - 1``, which is exactly
        ``mail_id`` (integers have no value strictly between ``mail_id - 1``
        and ``mail_id``). Filtering on ``recipient_uid`` -- known from
        ``RunningAgentState.agent_uid`` -- is enough to identify the row
        without needing the sender UID as an input.
        """
        matches = self._mail.find_mails(
            recipient_uid=recipient_uid,
            after_mail_id=max(mail_id - 1, 0),
            limit=1,
        )
        if matches and matches[0]["mail_id"] == mail_id:
            return matches[0]
        return None


_REPLY_TO_PATTERN = re.compile(r"^返信先UID:\s*(UID[0-9]{6,})\s*$", re.MULTILINE)


def resolve_reply_to_uid(mail_adapter: MailPort, origin_mail: dict) -> str:
    """Determine the expected reply-to UID for an origin mail (SPEC.md 30章).

    Defaults to the origin mail's sender UID. If the body contains a line
    of the form ``返信先UID: UIDxxxxxx`` naming a different, currently
    registered UID, that UID is used instead; an invalid or unregistered
    override falls back to the sender UID ("無効または存在しない返信先UID
    の場合は元メールの送信者UIDへ戻す").
    """
    default_uid = origin_mail["sender_uid"]
    match = _REPLY_TO_PATTERN.search(origin_mail.get("body") or "")
    if not match:
        return default_uid
    candidate = match.group(1)
    if candidate == default_uid:
        return candidate
    try:
        mail_adapter.check_mail(candidate)
    except Exception:
        return default_uid
    return candidate


class ReplyVerifier:
    """Polls MailReplyQuery until a matching reply appears or the timeout elapses."""

    def __init__(self, query: MailReplyQuery) -> None:
        self._query = query

    def wait_for_reply(
        self,
        expected: ExpectedReply,
        timeout_sec: float,
        sleep_fn: Any = None,
        now_fn: Any = None,
    ) -> ReplyCheckResult:
        import time

        sleep_fn = sleep_fn or time.sleep
        now_fn = now_fn or time.monotonic
        deadline = now_fn() + timeout_sec
        while True:
            result = self._query.find_reply(expected)
            if result.found:
                return result
            remaining = deadline - now_fn()
            if remaining <= 0:
                return result
            sleep_fn(min(1.0, remaining))

    def wait_for_terminal_reply(
        self, expected: ExpectedReply, timeout_sec: float, sleep_fn: Any = None, now_fn: Any = None
    ) -> ReplyCheckResult:
        import time
        sleep_fn = sleep_fn or time.sleep
        now_fn = now_fn or time.monotonic
        deadline = now_fn() + timeout_sec
        while True:
            result = self._query.find_terminal_reply(expected)
            if result.found:
                return result
            remaining = deadline - now_fn()
            if remaining <= 0:
                return result
            sleep_fn(min(1.0, remaining))
