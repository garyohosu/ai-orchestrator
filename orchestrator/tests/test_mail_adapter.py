import tempfile
import unittest
from pathlib import Path

from mail_adapter import (
    ExpectedReply,
    MailModuleAdapter,
    MailPackageNotFoundError,
    MailReplyQuery,
    ReplyVerifier,
    resolve_reply_to_uid,
)
from tests.fakes import InMemoryMailAdapter
from timeutil import now_iso, shift_ms


class MailModuleAdapterMissingPackageTests(unittest.TestCase):
    def test_raises_clear_error_when_mail_dir_missing(self) -> None:
        missing_dir = Path(tempfile.mkdtemp()) / "mail"
        with self.assertRaises(MailPackageNotFoundError):
            MailModuleAdapter(missing_dir)


class ReplyToResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mail = InMemoryMailAdapter()
        self.commander = self.mail.register_user("commander")
        self.worker = self.mail.register_user("worker")
        self.other = self.mail.register_user("other")

    def test_defaults_to_sender_uid(self) -> None:
        origin = {"sender_uid": self.commander, "body": "作業をお願いします"}
        self.assertEqual(resolve_reply_to_uid(self.mail, origin), self.commander)

    def test_valid_override_in_body_is_used(self) -> None:
        origin = {
            "sender_uid": self.commander,
            "body": f"作業をお願いします\n返信先UID: {self.other}\n",
        }
        self.assertEqual(resolve_reply_to_uid(self.mail, origin), self.other)

    def test_unregistered_override_falls_back_to_sender(self) -> None:
        origin = {
            "sender_uid": self.commander,
            "body": "返信先UID: UID000999\n",
        }
        self.assertEqual(resolve_reply_to_uid(self.mail, origin), self.commander)

    def test_malformed_override_falls_back_to_sender(self) -> None:
        origin = {"sender_uid": self.commander, "body": "返信先UID: not-a-uid\n"}
        self.assertEqual(resolve_reply_to_uid(self.mail, origin), self.commander)


class MailReplyQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mail = InMemoryMailAdapter()
        self.commander = self.mail.register_user("commander")
        self.worker = self.mail.register_user("worker")
        self.stranger = self.mail.register_user("stranger")
        self.query = MailReplyQuery(self.mail)

    def test_get_origin_mail_state_selects_exact_mail_by_id(self) -> None:
        earlier = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼1", "b1")
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼2", "b2")
        later = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼3", "b3")

        state = self.query.get_origin_mail_state(origin_id, self.worker)

        self.assertIsNotNone(state)
        self.assertEqual(state["mail_id"], origin_id)
        self.assertFalse(state["is_read"])

    def test_get_origin_mail_state_reflects_is_read(self) -> None:
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼", "b")
        self.mail.receive_mail(self.worker)
        state = self.query.get_origin_mail_state(origin_id, self.worker)
        self.assertTrue(state["is_read"])

    def test_find_reply_matches_all_conditions(self) -> None:
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼", "b")
        reply_id = self.mail.send_mail(self.worker, self.commander, "[JOB-A] 完了報告", "done")
        expected = ExpectedReply(
            job_id="JOB-A",
            sender_uid=self.worker,
            recipient_uid=self.commander,
            origin_mail_id=origin_id,
            not_before_iso=shift_ms(now_iso(), -60_000),
        )
        result = self.query.find_reply(expected)
        self.assertTrue(result.found)
        self.assertEqual(result.reply_mail_id, reply_id)

    def test_find_reply_rejects_wrong_sender(self) -> None:
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼", "b")
        self.mail.send_mail(self.stranger, self.commander, "[JOB-A] 完了報告", "done")
        expected = ExpectedReply(
            job_id="JOB-A",
            sender_uid=self.worker,
            recipient_uid=self.commander,
            origin_mail_id=origin_id,
            not_before_iso=shift_ms(now_iso(), -60_000),
        )
        self.assertFalse(self.query.find_reply(expected).found)

    def test_find_reply_rejects_wrong_recipient(self) -> None:
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼", "b")
        self.mail.send_mail(self.worker, self.stranger, "[JOB-A] 完了報告", "done")
        expected = ExpectedReply(
            job_id="JOB-A",
            sender_uid=self.worker,
            recipient_uid=self.commander,
            origin_mail_id=origin_id,
            not_before_iso=shift_ms(now_iso(), -60_000),
        )
        self.assertFalse(self.query.find_reply(expected).found)

    def test_find_reply_rejects_different_job_id(self) -> None:
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼", "b")
        self.mail.send_mail(self.worker, self.commander, "[JOB-B] 完了報告", "done")
        expected = ExpectedReply(
            job_id="JOB-A",
            sender_uid=self.worker,
            recipient_uid=self.commander,
            origin_mail_id=origin_id,
            not_before_iso=shift_ms(now_iso(), -60_000),
        )
        self.assertFalse(self.query.find_reply(expected).found)

    def test_find_reply_rejects_mail_id_at_or_below_origin(self) -> None:
        earlier_id = self.mail.send_mail(self.worker, self.commander, "[JOB-A] 先行メール", "x")
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼", "b")
        expected = ExpectedReply(
            job_id="JOB-A",
            sender_uid=self.worker,
            recipient_uid=self.commander,
            origin_mail_id=origin_id,
            not_before_iso=shift_ms(now_iso(), -60_000),
        )
        self.assertFalse(self.query.find_reply(expected).found)
        self.assertLess(earlier_id, origin_id)

    def test_find_reply_rejects_reply_created_before_launch(self) -> None:
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼", "b")
        reply_id = self.mail.send_mail(self.worker, self.commander, "[JOB-A] 完了報告", "done")
        self.mail.seed_sent_at(reply_id, shift_ms(now_iso(), -10_000))
        expected = ExpectedReply(
            job_id="JOB-A",
            sender_uid=self.worker,
            recipient_uid=self.commander,
            origin_mail_id=origin_id,
            not_before_iso=now_iso(),  # launch happened "now", reply predates it
        )
        self.assertFalse(self.query.find_reply(expected).found)

    def test_find_reply_does_not_change_read_state_of_other_mail(self) -> None:
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼", "b")
        self.mail.send_mail(self.worker, self.commander, "[JOB-A] 完了報告", "done")
        before = self.mail.check_mail(self.commander)
        expected = ExpectedReply(
            job_id="JOB-A",
            sender_uid=self.worker,
            recipient_uid=self.commander,
            origin_mail_id=origin_id,
            not_before_iso=shift_ms(now_iso(), -60_000),
        )
        self.query.find_reply(expected)
        after = self.mail.check_mail(self.commander)
        self.assertEqual(before, after)
        self.assertEqual(self.mail.receive_mail_calls, 0)

    def test_terminal_reply_requires_matching_invocation_and_status(self) -> None:
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] [DEC-1] 依頼", "b")
        self.mail.send_mail(self.worker, self.commander, "[JOB-A] [DEC-1] [INV-OLD] STATUS: COMPLETED", '{"status":"COMPLETED","invocation_id":"INV-OLD"}')
        self.mail.send_mail(self.worker, self.commander, "[JOB-A] [DEC-1] [INV-NEW] STATUS: ACK", '{"status":"ACK_RECEIVED","invocation_id":"INV-NEW"}')
        expected = ExpectedReply(
            job_id="JOB-A", sender_uid=self.worker, recipient_uid=self.commander,
            origin_mail_id=origin_id, not_before_iso=shift_ms(now_iso(), -60_000),
            invocation_id="INV-NEW", max_mail_id=origin_id, decision_id="DEC-1",
        )
        self.assertFalse(self.query.find_terminal_reply(expected).found)
        completed_id = self.mail.send_mail(self.worker, self.commander, "[JOB-A] [DEC-1] [INV-NEW] STATUS: COMPLETED", '{"status":"COMPLETED","invocation_id":"INV-NEW"}')
        result = self.query.find_terminal_reply(expected)
        self.assertTrue(result.found)
        self.assertEqual(result.reply_mail_id, completed_id)

    def test_waiting_for_worker_is_a_terminal_invocation_status(self) -> None:
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] [DEC-1] request", "b")
        self.mail.send_mail(self.worker, self.commander, "[JOB-A] [DEC-1] [INV-1] STATUS: WAITING_FOR_WORKER", '{"status":"WAITING_FOR_WORKER","job_id":"JOB-A","decision_id":"DEC-1","invocation_id":"INV-1"}')
        expected = ExpectedReply(
            job_id="JOB-A", sender_uid=self.worker, recipient_uid=self.commander,
            origin_mail_id=origin_id, not_before_iso=shift_ms(now_iso(), -60_000),
            invocation_id="INV-1", max_mail_id=origin_id, decision_id="DEC-1",
        )
        result = self.query.find_terminal_reply(expected)
        self.assertTrue(result.found)
        self.assertEqual(result.status, "WAITING_FOR_WORKER")

    def test_terminal_reply_requires_body_metadata_match(self) -> None:
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] [DEC-1] request", "b")
        # 1. Subject matches but body has mismatched invocation_id (Should be ignored)
        self.mail.send_mail(self.worker, self.commander, "[JOB-A] [DEC-1] [INV-MATCH] STATUS: COMPLETED", '{"status":"COMPLETED","invocation_id":"INV-MISMATCH"}')
        expected = ExpectedReply(
            job_id="JOB-A", sender_uid=self.worker, recipient_uid=self.commander,
            origin_mail_id=origin_id, not_before_iso=shift_ms(now_iso(), -60_000),
            invocation_id="INV-MATCH", max_mail_id=origin_id, decision_id="DEC-1",
        )
        self.assertFalse(self.query.find_terminal_reply(expected).found)

        # 2. Correct invocation_id in body
        self.mail.send_mail(self.worker, self.commander, "[JOB-A] [DEC-1] [INV-MATCH] STATUS: COMPLETED", '{"status":"COMPLETED","invocation_id":"INV-MATCH"}')
        self.assertTrue(self.query.find_terminal_reply(expected).found)

    def test_terminal_reply_rejects_subject_body_mismatch(self) -> None:
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] [DEC-1] request", "b")
        # Subject specifies INV-A, but body JSON specifies INV-B (Should be ignored as a mismatch)
        self.mail.send_mail(self.worker, self.commander, "[JOB-A] [DEC-1] [INV-A] STATUS: COMPLETED", '{"status":"COMPLETED","invocation_id":"INV-B"}')
        expected = ExpectedReply(
            job_id="JOB-A", sender_uid=self.worker, recipient_uid=self.commander,
            origin_mail_id=origin_id, not_before_iso=shift_ms(now_iso(), -60_000),
            invocation_id="INV-A", max_mail_id=origin_id, decision_id="DEC-1",
        )
        self.assertFalse(self.query.find_terminal_reply(expected).found)

    def test_terminal_reply_uses_structured_status_as_truth(self) -> None:
        origin_id = self.mail.send_mail(
            self.commander, self.worker, "[JOB-A] [DEC-1] request", "b"
        )
        expected = ExpectedReply(
            job_id="JOB-A",
            sender_uid=self.worker,
            recipient_uid=self.commander,
            origin_mail_id=origin_id,
            not_before_iso=shift_ms(now_iso(), -60_000),
            invocation_id="INV-STRICT",
            max_mail_id=origin_id,
            decision_id="DEC-1",
        )
        self.mail.send_mail(
            self.worker,
            self.commander,
            "[JOB-A] [DEC-1] [INV-STRICT] STATUS: COMPLETED",
            '{"status":"ACK_RECEIVED","invocation_id":"INV-STRICT"}',
        )
        self.assertFalse(self.query.find_terminal_reply(expected).found)

    def test_terminal_reply_rejects_non_string_structured_status(self) -> None:
        origin_id = self.mail.send_mail(
            self.commander, self.worker, "[JOB-A] [DEC-1] request", "b"
        )
        self.mail.send_mail(
            self.worker,
            self.commander,
            "[JOB-A] [DEC-1] [INV-STRICT] STATUS: COMPLETED",
            '{"status":[],"invocation_id":"INV-STRICT"}',
        )
        expected = ExpectedReply(
            job_id="JOB-A",
            sender_uid=self.worker,
            recipient_uid=self.commander,
            origin_mail_id=origin_id,
            not_before_iso=shift_ms(now_iso(), -60_000),
            invocation_id="INV-STRICT",
            max_mail_id=origin_id,
            decision_id="DEC-1",
        )
        self.assertFalse(self.query.find_terminal_reply(expected).found)

    def test_terminal_reply_requires_expected_and_body_invocation_ids(self) -> None:
        origin_id = self.mail.send_mail(
            self.commander, self.worker, "[JOB-A] [DEC-1] request", "b"
        )
        self.mail.send_mail(
            self.worker,
            self.commander,
            "[JOB-A] [DEC-1] STATUS: COMPLETED",
            '{"status":"COMPLETED"}',
        )
        expected = ExpectedReply(
            job_id="JOB-A",
            sender_uid=self.worker,
            recipient_uid=self.commander,
            origin_mail_id=origin_id,
            not_before_iso=shift_ms(now_iso(), -60_000),
            invocation_id="",
            max_mail_id=origin_id,
            decision_id="DEC-1",
        )
        self.assertFalse(self.query.find_terminal_reply(expected).found)


class ReplyVerifierTests(unittest.TestCase):
    def test_returns_immediately_when_reply_already_present(self) -> None:
        mail = InMemoryMailAdapter()
        commander = mail.register_user("commander")
        worker = mail.register_user("worker")
        origin_id = mail.send_mail(commander, worker, "[JOB-A] 依頼", "b")
        mail.send_mail(worker, commander, "[JOB-A] 完了報告", "done")
        verifier = ReplyVerifier(MailReplyQuery(mail))
        expected = ExpectedReply(
            job_id="JOB-A",
            sender_uid=worker,
            recipient_uid=commander,
            origin_mail_id=origin_id,
            not_before_iso=shift_ms(now_iso(), -60_000),
        )
        sleeps: list[float] = []
        clock = {"t": 0.0}
        result = verifier.wait_for_reply(
            expected, timeout_sec=5, sleep_fn=sleeps.append, now_fn=lambda: clock["t"]
        )
        self.assertTrue(result.found)
        self.assertEqual(sleeps, [])

    def test_gives_up_after_timeout_without_real_sleep(self) -> None:
        mail = InMemoryMailAdapter()
        commander = mail.register_user("commander")
        worker = mail.register_user("worker")
        origin_id = mail.send_mail(commander, worker, "[JOB-A] 依頼", "b")
        verifier = ReplyVerifier(MailReplyQuery(mail))
        expected = ExpectedReply(
            job_id="JOB-A",
            sender_uid=worker,
            recipient_uid=commander,
            origin_mail_id=origin_id,
            not_before_iso=shift_ms(now_iso(), -60_000),
        )
        clock = {"t": 0.0}

        def fake_sleep(seconds: float) -> None:
            clock["t"] += seconds

        result = verifier.wait_for_reply(
            expected, timeout_sec=3, sleep_fn=fake_sleep, now_fn=lambda: clock["t"]
        )
        self.assertFalse(result.found)


if __name__ == "__main__":
    unittest.main()
