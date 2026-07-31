"""Test doubles. Only ever used when a test explicitly injects them (never at runtime)."""

from __future__ import annotations

from typing import Any

from timeutil import now_iso


class InMemoryMailAdapter:
    """A minimal in-process stand-in for the mail package's six functions.

    Semantics mirror mail/agent_mail.py closely enough for orchestrator's
    own logic to be tested: UID format, is_read/read_at tracking,
    find_mails' AND-combined filters and ascending mail_id order,
    request_id as a "[<id>]" substring match on the subject.
    """

    def __init__(self) -> None:
        self._users: dict[str, str] = {}
        self._next_uid_num = 1
        self._mails: list[dict[str, Any]] = []
        self._next_mail_id = 1
        self.receive_mail_calls = 0
        self.send_mail_calls = 0

    def register_user(self, name: str) -> str:
        for uid, existing in self._users.items():
            if existing == name:
                return uid
        uid = f"UID{self._next_uid_num:06d}"
        self._next_uid_num += 1
        self._users[uid] = name
        return uid

    def list_users(self) -> list[dict]:
        return [{"uid": uid, "name": name} for uid, name in self._users.items()]

    def send_mail(self, sender_uid: str, recipient_uid: str, subject: str, body: str) -> int:
        self.send_mail_calls += 1
        if sender_uid not in self._users:
            raise ValueError(f"unknown sender_uid: {sender_uid}")
        if recipient_uid not in self._users:
            raise ValueError(f"unknown recipient_uid: {recipient_uid}")
        mail_id = self._next_mail_id
        self._next_mail_id += 1
        mail = {
            "mail_id": mail_id,
            "sender_uid": sender_uid,
            "sender_name": self._users[sender_uid],
            "recipient_uid": recipient_uid,
            "recipient_name": self._users[recipient_uid],
            "subject": subject,
            "body": body,
            "sent_at": now_iso(),
            "is_read": False,
            "read_at": None,
        }
        self._mails.append(mail)
        return mail_id

    def check_mail(self, uid: str) -> int:
        if uid not in self._users:
            raise ValueError(f"unknown uid: {uid}")
        return sum(1 for m in self._mails if m["recipient_uid"] == uid and not m["is_read"])

    def receive_mail(self, uid: str) -> list[dict]:
        self.receive_mail_calls += 1
        if uid not in self._users:
            raise ValueError(f"unknown uid: {uid}")
        result = []
        read_at = now_iso()
        for mail in self._mails:
            if mail["recipient_uid"] == uid and not mail["is_read"]:
                mail["is_read"] = True
                mail["read_at"] = read_at
                result.append(
                    {
                        "mail_id": mail["mail_id"],
                        "sender_uid": mail["sender_uid"],
                        "sender_name": mail["sender_name"],
                        "recipient_uid": mail["recipient_uid"],
                        "subject": mail["subject"],
                        "body": mail["body"],
                        "sent_at": mail["sent_at"],
                    }
                )
        return result

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
        results = []
        for mail in self._mails:
            if sender_uid is not None and mail["sender_uid"] != sender_uid:
                continue
            if recipient_uid is not None and mail["recipient_uid"] != recipient_uid:
                continue
            if request_id is not None and f"[{request_id}]" not in mail["subject"]:
                continue
            if after_mail_id is not None and not (mail["mail_id"] > after_mail_id):
                continue
            if sent_after is not None and not (mail["sent_at"] > sent_after):
                continue
            if is_read is not None and mail["is_read"] != is_read:
                continue
            results.append(dict(mail))
        results.sort(key=lambda m: m["mail_id"])
        if limit is not None:
            results = results[:limit]
        return results

    # --- test helpers, not part of MailPort ---

    def seed_sent_at(self, mail_id: int, sent_at_iso: str) -> None:
        for mail in self._mails:
            if mail["mail_id"] == mail_id:
                mail["sent_at"] = sent_at_iso
                return
        raise KeyError(mail_id)


class FakeCliAdapter:
    """A CliAdapter that runs a test stub verbatim, with no product-specific flags."""

    def __init__(self, default_name: str = "fake-cli") -> None:
        self.cli_type = "fake"
        self._default_name = default_name

    def default_command_name(self) -> str:
        return self._default_name

    def build_argv(self, command: list[str], project_path) -> list[str]:
        return list(command)
