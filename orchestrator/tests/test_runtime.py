import sys
import tempfile
import time
import unittest
from pathlib import Path

from mail_adapter import MailReplyQuery
from runtime import (
    ForceStopRequested,
    RecoveryActionKind,
    RunDurationGuard,
    RunningAgentState,
    RuntimeStateStore,
    StaleRecoveryService,
    StopController,
    TerminalStateStore,
)
from tests.fakes import InMemoryMailAdapter
from timeutil import now_iso
from winproc import get_process_start_time_iso, is_same_running_process, terminate_process_tree


class RuntimeStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RuntimeStateStore(Path(tempfile.mkdtemp()))

    def _state(self, job_id: str = "JOB-1") -> RunningAgentState:
        return RunningAgentState(
            pid=1234,
            process_start_time_iso=now_iso(),
            agent_name="worker",
            agent_uid="UID000002",
            job_id=job_id,
            origin_mail_id=5,
            launch_command=["python", "x.py"],
            recorded_at_iso=now_iso(),
            retry_count=0,
        )

    def test_save_and_load_all_round_trips(self) -> None:
        state = self._state()
        self.store.save(state)
        loaded = self.store.load_all()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].job_id, "JOB-1")
        self.assertEqual(loaded[0].pid, 1234)

    def test_remove_deletes_entry(self) -> None:
        self.store.save(self._state())
        self.store.remove("JOB-1")
        self.assertEqual(self.store.load_all(), [])

    def test_stop_request_read_and_clear(self) -> None:
        self.assertFalse(self.store.read_stop_request())
        (self.store._runtime_dir).mkdir(parents=True, exist_ok=True)
        (self.store._runtime_dir / "stop.request").write_text("", encoding="utf-8")
        self.assertTrue(self.store.read_stop_request())
        self.store.clear_stop_request()
        self.assertFalse(self.store.read_stop_request())

    def test_path_traversal_job_id_is_rejected(self) -> None:
        # Defense in depth: even if an unsafe job_id ever reached this far
        # (dispatch.extract_job_id should already have sanitized it),
        # RuntimeStateStore must refuse to write outside runtime_dir. Many
        # ".." segments guarantee escape regardless of how the literal
        # "running-" filename prefix interacts with the first segment.
        state = self._state(job_id="/".join([".."] * 10) + "/evil")
        with self.assertRaises(ValueError):
            self.store.save(state)

    def test_corrupt_file_is_skipped_not_fatal(self) -> None:
        self.store.save(self._state("JOB-good"))
        bad_path = self.store._runtime_dir / "running-JOB-bad.json"
        bad_path.write_text("{not json", encoding="utf-8")
        loaded = self.store.load_all()
        self.assertEqual([s.job_id for s in loaded], ["JOB-good"])


class TerminalStateStoreTests(unittest.TestCase):
    def test_mark_and_is_terminal(self) -> None:
        store = TerminalStateStore(Path(tempfile.mkdtemp()))
        self.assertFalse(store.is_terminal(42))
        store.mark(42, "DELIVERY_FAILED")
        self.assertTrue(store.is_terminal(42))
        self.assertEqual(store.get_status(42), "DELIVERY_FAILED")

    def test_survives_process_restart_via_new_store_instance(self) -> None:
        runtime_dir = Path(tempfile.mkdtemp())
        TerminalStateStore(runtime_dir).mark(7, "FAILED")
        reopened = TerminalStateStore(runtime_dir)
        self.assertTrue(reopened.is_terminal(7))


class RunDurationGuardTests(unittest.TestCase):
    def test_zero_means_unlimited(self) -> None:
        clock = {"t": 0.0}
        guard = RunDurationGuard(0, now_fn=lambda: clock["t"])
        clock["t"] = 999_999
        self.assertFalse(guard.should_stop())

    def test_positive_boundary_stops_only_after_exceeding(self) -> None:
        clock = {"t": 0.0}
        guard = RunDurationGuard(10, now_fn=lambda: clock["t"])
        clock["t"] = 9
        self.assertFalse(guard.should_stop())
        clock["t"] = 10
        self.assertTrue(guard.should_stop())


class StopControllerTests(unittest.TestCase):
    def test_first_sigint_sets_flag_without_raising(self) -> None:
        controller = StopController()
        controller.handle_sigint(2, None)
        self.assertTrue(controller.stop_requested)
        self.assertFalse(controller.force_requested)

    def test_second_sigint_raises_and_sets_force(self) -> None:
        controller = StopController()
        controller.handle_sigint(2, None)
        with self.assertRaises(ForceStopRequested):
            controller.handle_sigint(2, None)
        self.assertTrue(controller.force_requested)


class WinProcTests(unittest.TestCase):
    def test_start_time_is_stable_and_matches_same_process(self) -> None:
        proc = __import__("subprocess").Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"]
        )
        try:
            start = get_process_start_time_iso(proc.pid)
            self.assertIsNotNone(start)
            self.assertTrue(is_same_running_process(proc.pid, start))
            self.assertFalse(is_same_running_process(proc.pid, "2000-01-01T00:00:00.000Z"))
        finally:
            terminate_process_tree(proc.pid)
            proc.wait(timeout=10)

    def test_unknown_pid_returns_none_and_is_not_running(self) -> None:
        # A PID astronomically unlikely to exist.
        huge_pid = 999_999
        start = get_process_start_time_iso(huge_pid)
        self.assertIsNone(start)
        self.assertFalse(is_same_running_process(huge_pid, "2000-01-01T00:00:00.000Z"))

    def test_terminate_process_tree_actually_kills(self) -> None:
        proc = __import__("subprocess").Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        terminate_process_tree(proc.pid)
        exit_code = proc.wait(timeout=10)
        self.assertIsNotNone(exit_code)


class StaleRecoveryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mail = InMemoryMailAdapter()
        self.commander = self.mail.register_user("commander")
        self.worker = self.mail.register_user("worker")
        self.runtime_store = RuntimeStateStore(Path(tempfile.mkdtemp()))
        self.query = MailReplyQuery(self.mail)
        self.service = StaleRecoveryService(self.runtime_store, self.mail, self.query)

    def _save_state(self, pid: int, start_time: str, origin_mail_id: int) -> None:
        self.runtime_store.save(
            RunningAgentState(
                pid=pid,
                process_start_time_iso=start_time,
                agent_name="worker",
                agent_uid=self.worker,
                job_id="JOB-A",
                origin_mail_id=origin_mail_id,
                launch_command=["x"],
                recorded_at_iso=now_iso(),
                retry_count=0,
            )
        )

    def test_still_running_process_is_left_alone(self) -> None:
        proc = __import__("subprocess").Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"]
        )
        try:
            start = get_process_start_time_iso(proc.pid)
            origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼", "b")
            self._save_state(proc.pid, start, origin_id)
            actions = self.service.recover_on_startup()
            self.assertEqual(actions, [])
            self.assertEqual(len(self.runtime_store.load_all()), 1)
        finally:
            terminate_process_tree(proc.pid)
            proc.wait(timeout=10)

    def test_unread_origin_mail_is_requeued(self) -> None:
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼", "b")
        self._save_state(pid=999_999, start_time="2000-01-01T00:00:00.000Z", origin_mail_id=origin_id)
        actions = self.service.recover_on_startup()
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, RecoveryActionKind.REQUEUE)
        self.assertEqual(self.runtime_store.load_all(), [])

    def test_read_with_no_reply_yields_notify_action(self) -> None:
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼", "b")
        self.mail.receive_mail(self.worker)
        self._save_state(pid=999_999, start_time="2000-01-01T00:00:00.000Z", origin_mail_id=origin_id)
        actions = self.service.recover_on_startup()
        self.assertEqual(actions[0].kind, RecoveryActionKind.NOTIFY_ORIGIN_SENDER)
        self.assertEqual(actions[0].reply_to_uid, self.commander)

    def test_read_with_reply_yields_mark_completed(self) -> None:
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼", "b")
        self.mail.receive_mail(self.worker)
        # recorded_at_iso (set inside _save_state, below) must predate the
        # reply, matching real chronology: launch is recorded, then the
        # agent replies. Reversing this order would make find_reply's
        # sent_after check reject the reply, defeating the test.
        self._save_state(pid=999_999, start_time="2000-01-01T00:00:00.000Z", origin_mail_id=origin_id)
        self.mail.send_mail(self.worker, self.commander, "[JOB-A] 完了報告", "done")
        actions = self.service.recover_on_startup()
        self.assertEqual(actions[0].kind, RecoveryActionKind.MARK_COMPLETED)

    def test_old_reply_below_origin_mail_id_does_not_mark_completed(self) -> None:
        stale_reply_id = self.mail.send_mail(self.worker, self.commander, "[JOB-A] 前の返信", "old")
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼", "b")
        self.mail.receive_mail(self.worker)
        self._save_state(pid=999_999, start_time="2000-01-01T00:00:00.000Z", origin_mail_id=origin_id)
        actions = self.service.recover_on_startup()
        self.assertEqual(actions[0].kind, RecoveryActionKind.NOTIFY_ORIGIN_SENDER)
        self.assertLess(stale_reply_id, origin_id)

    def test_reply_before_recorded_launch_time_does_not_mark_completed(self) -> None:
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼", "b")
        self.mail.receive_mail(self.worker)
        reply_id = self.mail.send_mail(self.worker, self.commander, "[JOB-A] 完了報告", "done")
        self.mail.seed_sent_at(reply_id, "2000-01-01T00:00:00.000Z")
        self._save_state(pid=999_999, start_time="2020-01-01T00:00:00.000Z", origin_mail_id=origin_id)
        # recorded_at_iso in _save_state defaults to now_iso(), which is after
        # the reply's seeded (older) sent_at.
        actions = self.service.recover_on_startup()
        self.assertEqual(actions[0].kind, RecoveryActionKind.NOTIFY_ORIGIN_SENDER)

    def test_wrong_recipient_reply_does_not_mark_completed(self) -> None:
        stranger = self.mail.register_user("stranger")
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼", "b")
        self.mail.receive_mail(self.worker)
        self._save_state(pid=999_999, start_time="2000-01-01T00:00:00.000Z", origin_mail_id=origin_id)
        self.mail.send_mail(self.worker, stranger, "[JOB-A] 完了報告", "done")
        actions = self.service.recover_on_startup()
        self.assertEqual(actions[0].kind, RecoveryActionKind.NOTIFY_ORIGIN_SENDER)

    def test_recovery_never_calls_receive_mail(self) -> None:
        origin_id = self.mail.send_mail(self.commander, self.worker, "[JOB-A] 依頼", "b")
        self._save_state(pid=999_999, start_time="2000-01-01T00:00:00.000Z", origin_mail_id=origin_id)
        self.service.recover_on_startup()
        self.assertEqual(self.mail.receive_mail_calls, 0)


if __name__ == "__main__":
    unittest.main()
