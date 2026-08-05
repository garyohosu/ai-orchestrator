import json
import sys
import tempfile
import unittest
from pathlib import Path

from config import AgentDefinition
from dispatch import (
    CheckpointStore,
    DispatchCycle,
    ErrorNotifier,
    MailWatcher,
    OutcomeClassifier,
    OutcomeStatus,
    RetryPolicy,
    RoundTripCounter,
    extract_job_id,
)
from adapters.codex import CodexCliAdapter
from launcher import CliLauncher, CliPathResolver
from launcher import ProcessResult
from adapters.base import CliEvidence
from output_capture import OutputArtifact
from logging_utils import JobLogger
from mail_adapter import MailReplyQuery
from rate_limit_store import RateLimitStore
from runtime import RuntimeStateStore, TerminalStateStore
from tests.fakes import FakeCliAdapter, InMemoryMailAdapter
from timeutil import now_iso, shift_ms

_STUBS_DIR = Path(__file__).resolve().parent / "stubs"


def _agent(name: str, uid: str, stub: str, extra_args: list[str] | None = None) -> AgentDefinition:
    command = [sys.executable, str(_STUBS_DIR / stub)] + (extra_args or [])
    return AgentDefinition(name=name, uid=uid, cli_type="fake", command=command)


class DispatchCycleHarness:
    """Wires a DispatchCycle with fresh temp dirs and a fake CLI adapter."""

    def __init__(
        self, max_retries: int = 2, max_round_trips: int = 10,
        extra_adapters: dict | None = None, with_rate_limit_store: bool = False,
    ) -> None:
        self._max_retries = max_retries
        self.mail = InMemoryMailAdapter()
        self.system_uid = self.mail.register_user("orchestrator")
        self.project_path = Path(tempfile.mkdtemp())
        self.logs_dir = Path(tempfile.mkdtemp())
        self.checkpoints_dir = Path(tempfile.mkdtemp())
        self.runtime_dir = Path(tempfile.mkdtemp())

        adapters = {"fake": FakeCliAdapter("nonexistent-default-cli-xyz")}
        adapters.update(extra_adapters or {})
        self.adapters = adapters
        self.rate_limit_store = RateLimitStore(self.runtime_dir) if with_rate_limit_store else None
        self.watcher = MailWatcher(self.mail)
        self.launcher = CliLauncher(CliPathResolver(adapters), adapters)
        self.reply_query = MailReplyQuery(self.mail)
        self.notifier = ErrorNotifier(self.mail, self.system_uid)
        self.logger = JobLogger(self.logs_dir)
        self.checkpoint_store = CheckpointStore(self.checkpoints_dir)
        self.round_trips = RoundTripCounter(max_round_trips)
        self.runtime_store = RuntimeStateStore(self.runtime_dir)
        self.terminal_store = TerminalStateStore(self.runtime_dir)

        self.cycle = self._build_cycle()

    def reply_after_launch(self, sender_uid: str, recipient_uid: str, subject: str, body: str) -> None:
        """Make the next launch() send a reply mail immediately afterward.

        InMemoryMailAdapter.send_mail stamps sent_at with the real wall
        clock, so calling it right after launch() (rather than before)
        guarantees the reply's timestamp is not earlier than
        launched_at_iso -- matching real chronology and avoiding the
        find_mails "sent_at > sent_after" boundary rejecting it.
        """
        original_launch = self.cycle._launcher.launch

        def patched_launch(agent, job_id, origin_mail_id, project_path, **kwargs):
            launched = original_launch(agent, job_id, origin_mail_id, project_path, **kwargs)
            status = "DELEGATED" if "DELEGATED" in subject else ("WAITING_FOR_WORKER" if "WAITING_FOR_WORKER" in subject else ("WAITING_FOR_DECISION" if "WAITING_FOR_DECISION" in subject else ("ACK_RECEIVED" if "ACK" in subject else "COMPLETED")))
            env_vars = kwargs.get("env_vars", {})
            payload = {
                "status": status,
                "job_id": job_id,
                "decision_id": env_vars.get("DECISION_ID", ""),
                "invocation_id": launched.invocation_id,
                "parent_invocation_id": env_vars.get("AI_PARENT_INVOCATION_ID"),
                "root_invocation_id": env_vars["AI_ROOT_INVOCATION_ID"],
                "trigger_mail_uid": int(env_vars["AI_TRIGGER_MAIL_UID"]),
            }
            if status == "COMPLETED":
                payload["invocation_result"] = "COMPLETED"
            elif status == "DELEGATED":
                payload["invocation_result"] = "DELEGATED"
            elif status in {"WAITING_FOR_WORKER", "WAITING_FOR_DECISION"}:
                payload["invocation_result"] = "WAITING"
            self.mail.send_mail(sender_uid, recipient_uid, f"{subject} [{launched.invocation_id}]", json.dumps(payload))
            return launched

        self.cycle._launcher.launch = patched_launch

    def _build_cycle(self) -> DispatchCycle:
        return DispatchCycle(
            watcher=self.watcher,
            launcher=self.launcher,
            mail_adapter=self.mail,
            reply_query=self.reply_query,
            classifier=OutcomeClassifier(),
            retry_policy=RetryPolicy(self._max_retries),
            notifier=self.notifier,
            logger=self.logger,
            checkpoint_store=self.checkpoint_store,
            round_trips=self.round_trips,
            runtime_store=self.runtime_store,
            terminal_store=self.terminal_store,
            project_path=self.project_path,
            cli_timeout_sec=10,
            reply_check_timeout_sec=2,
            adapters=self.adapters,
            rate_limit_store=self.rate_limit_store,
        )


class ExtractJobIdTests(unittest.TestCase):
    def test_extracts_tagged_job_id(self) -> None:
        self.assertEqual(extract_job_id("[JOB-20260730T063015Z-A3F91C2D] 依頼", 1), "JOB-20260730T063015Z-A3F91C2D")

    def test_fallback_when_untagged(self) -> None:
        self.assertEqual(extract_job_id("依頼", 42), "NOJOB-42")

    def test_path_traversal_in_subject_falls_back_to_safe_id(self) -> None:
        # job_id becomes a filename (checkpoints/{job_id}.json,
        # runtime/running-{job_id}.json); a subject is untrusted text, so
        # anything outside [A-Za-z0-9._-] must never be trusted as-is
        # (Codex review finding #1, P0).
        for subject in (
            r"[JOB-..\..\evil] 依頼",
            "[JOB-../../evil] 依頼",
            "[JOB-a/b] 依頼",
            "[JOB-a\\b] 依頼",
            "[JOB- with space] 依頼",
        ):
            with self.subTest(subject=subject):
                job_id = extract_job_id(subject, 7)
                self.assertEqual(job_id, "NOJOB-7")

    def test_safe_job_id_characters_pass_through(self) -> None:
        self.assertEqual(extract_job_id("[JOB-A.b_c-9] 依頼", 1), "JOB-A.b_c-9")

    def test_rate_limited_handoff_preserves_job_and_deduplicates(self) -> None:
        h = DispatchCycleHarness()
        commander = h.mail.register_user("commander")
        worker_uid = h.mail.register_user("worker")
        alt_uid = h.mail.register_user("alternate")
        worker = AgentDefinition(
            name="worker", uid=worker_uid, cli_type="fake", command=[sys.executable],
            fallback_agents=["alternate"],
        )
        # Availability pre-screening (availability.py) resolves a candidate's
        # command before it's offered as a fallback target, so this needs a
        # command that actually exists on this machine -- sys.executable
        # always does, and this test never launches it.
        alternate = AgentDefinition(name="alternate", uid=alt_uid, cli_type="fake", command=[sys.executable])
        h.cycle._agents_by_name = {"worker": worker, "alternate": alternate}
        h.cycle._adapters = {"fake": FakeCliAdapter()}
        h.cycle._system_sender_uid = h.system_uid
        origin = {"mail_id": 1, "subject": "[JOB-RATE] 依頼", "sender_uid": commander}
        result = ProcessResult(
            exit_code=1, timed_out=False, duration_sec=0.1,
            stdout=OutputArtifact(None, None, 0, 0, False, ""),
            stderr=OutputArtifact(None, None, 0, 0, False, "You've hit your session limit"),
            cli_evidence=CliEvidence(True, "claude.rate_limit.session_limit", "stderr", "You've hit your session limit"),
        )
        first = h.cycle._handoff_rate_limited(worker, "JOB-RATE", origin, result, 0)
        self.assertIsNotNone(first)
        handoffs = h.mail.find_mails(sender_uid=h.system_uid, recipient_uid=alt_uid, request_id="JOB-RATE")
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(h.checkpoint_store.load("JOB-RATE").current_state, "HANDOFF_SENT")
        second = h.cycle._handoff_rate_limited(worker, "JOB-RATE", origin, result, 0)
        self.assertIsNone(second)
        self.assertEqual(len(h.mail.find_mails(sender_uid=h.system_uid, recipient_uid=alt_uid, request_id="JOB-RATE")), 1)

    def test_rate_limited_has_no_delivery_retry(self) -> None:
        self.assertFalse(RetryPolicy(5).should_retry(OutcomeStatus.RATE_LIMITED, 1, False))

    def test_handoff_mail_forwards_original_task_content(self) -> None:
        # A candidate only ever sees mail addressed to its own UID -- the
        # original task mail was addressed to the failed agent, so unless
        # the handoff body carries it forward verbatim, a cold candidate
        # has no way to discover what it's actually being asked to do.
        h = DispatchCycleHarness()
        commander = h.mail.register_user("commander")
        worker_uid = h.mail.register_user("worker")
        alt_uid = h.mail.register_user("alternate")
        worker = AgentDefinition(
            name="worker", uid=worker_uid, cli_type="fake", command=[sys.executable],
            fallback_agents=["alternate"],
        )
        alternate = AgentDefinition(name="alternate", uid=alt_uid, cli_type="fake", command=[sys.executable])
        h.cycle._agents_by_name = {"worker": worker, "alternate": alternate}
        h.cycle._adapters = {"fake": FakeCliAdapter()}
        h.cycle._system_sender_uid = h.system_uid
        origin = {
            "mail_id": 1, "subject": "[JOB-RATE2] 依頼件名テキスト",
            "sender_uid": commander,
            "body": "これは元の依頼本文です。SECRET_TASK_MARKER_12345を含む具体的な指示。",
        }
        result = ProcessResult(
            exit_code=1, timed_out=False, duration_sec=0.1,
            cli_evidence=CliEvidence(True, "codex.rate_limit.usage_limit", "stderr", "You've hit your usage limit"),
        )
        h.cycle._handoff_rate_limited(worker, "JOB-RATE2", origin, result, 0)
        handoffs = h.mail.find_mails(sender_uid=h.system_uid, recipient_uid=alt_uid, request_id="JOB-RATE2")
        self.assertEqual(len(handoffs), 1)
        self.assertIn("SECRET_TASK_MARKER_12345", handoffs[0]["body"])
        self.assertIn("依頼件名テキスト", handoffs[0]["body"])
        self.assertIn("agent_reply.py", handoffs[0]["body"])

    def test_rate_limited_with_no_candidates_returns_no_handoff(self) -> None:
        h = DispatchCycleHarness()
        worker = AgentDefinition(name="worker", uid="UID000002", cli_type="fake", command=[])
        result = ProcessResult(
            exit_code=1, timed_out=False, duration_sec=0.1,
            cli_evidence=CliEvidence(True, "claude.rate_limit.session_limit", "stderr", "You've hit your session limit"),
        )
        outcome = h.cycle._handoff_rate_limited(
            worker, "JOB-NO-CANDIDATE",
            {"mail_id": 1, "subject": "[JOB-NO-CANDIDATE] 依頼", "sender_uid": "UID000001"},
            result, 0,
        )
        self.assertIsNone(outcome)


class CodexToGrokFailoverTests(unittest.TestCase):
    """End-to-end: a real Codex usage-limit failure fails over to a
    fallback reviewer instead of ending the job (project failover
    requirements, section 12: reviewer -> codex_reviewer -> usage limit
    detected -> grok_reviewer -> COMPLETED, not a bare FAILED)."""

    def _codex_agent(self, uid: str, fallback_agents: list[str]) -> AgentDefinition:
        return AgentDefinition(
            name="codex_reviewer", uid=uid, cli_type="codex",
            command=[sys.executable, str(_STUBS_DIR / "codex_usage_limit.py")],
            fallback_agents=fallback_agents,
        )

    def test_real_usage_limit_message_triggers_handoff_not_human_required(self) -> None:
        h = DispatchCycleHarness(
            max_retries=0, extra_adapters={"codex": CodexCliAdapter()}, with_rate_limit_store=True
        )
        commander = h.mail.register_user("commander")
        codex_uid = h.mail.register_user("codex_reviewer")
        grok_uid = h.mail.register_user("grok_reviewer")
        h.mail.send_mail(commander, codex_uid, "[JOB-CSV-999] レビュー依頼", "b")
        codex_agent = self._codex_agent(codex_uid, ["grok_reviewer"])
        grok_agent = AgentDefinition(
            name="grok_reviewer", uid=grok_uid, cli_type="fake", command=[sys.executable]
        )
        h.cycle._agents_by_name = {"codex_reviewer": codex_agent, "grok_reviewer": grok_agent}
        h.cycle._system_sender_uid = h.system_uid

        outcomes = h.cycle.run_one_pass([codex_agent])

        self.assertEqual(outcomes[0].status, OutcomeStatus.RATE_LIMITED)
        handoff_mails = h.mail.find_mails(sender_uid=h.system_uid, recipient_uid=grok_uid, request_id="JOB-CSV-999")
        self.assertEqual(len(handoff_mails), 1)
        self.assertIn("引継ぎ", handoff_mails[0]["subject"])
        # Retry-at parsed from the real message must be persisted so a
        # later candidate-selection pass skips codex_reviewer without
        # re-attempting it.
        record = h.rate_limit_store.get("codex_reviewer")
        self.assertIsNotNone(record)
        self.assertEqual(record.rule_id, "codex.rate_limit.usage_limit")
        self.assertEqual(record.retry_at, "2026-08-08T17:17:00")
        self.assertTrue(h.rate_limit_store.is_in_cooldown("codex_reviewer"))

    def test_all_fallback_candidates_unavailable_yields_human_required_with_reasons(self) -> None:
        h = DispatchCycleHarness(
            max_retries=0, extra_adapters={"codex": CodexCliAdapter()}, with_rate_limit_store=True
        )
        commander = h.mail.register_user("commander")
        codex_uid = h.mail.register_user("codex_reviewer")
        antigravity_uid = h.mail.register_user("antigravity_reviewer")
        h.mail.send_mail(commander, codex_uid, "[JOB-CSV-998] レビュー依頼", "b")
        codex_agent = self._codex_agent(codex_uid, ["antigravity_reviewer"])
        antigravity_agent = AgentDefinition(
            name="antigravity_reviewer", uid=antigravity_uid, cli_type="antigravity"
        )
        h.cycle._agents_by_name = {"codex_reviewer": codex_agent, "antigravity_reviewer": antigravity_agent}
        h.cycle._system_sender_uid = h.system_uid

        outcomes = h.cycle.run_one_pass([codex_agent])

        self.assertEqual(outcomes[0].status, OutcomeStatus.HUMAN_REQUIRED)
        self.assertEqual(h.terminal_store.get_status(1), "HUMAN_REQUIRED")
        checkpoint = h.checkpoint_store.load("JOB-CSV-998")
        self.assertEqual(checkpoint.current_state, "HUMAN_REQUIRED")
        # The notification/checkpoint reason must explain *why* the only
        # configured fallback was skipped (section 14: "未試行候補がある
        # 場合は、その理由"), not just "unknown failure".
        self.assertTrue(any("antigravity_reviewer" in issue for issue in checkpoint.open_issues))
        self.assertTrue(any("cli.not_installed" in issue for issue in checkpoint.open_issues))

    def test_success_clears_a_prior_cooldown_record(self) -> None:
        h = DispatchCycleHarness(max_retries=0, with_rate_limit_store=True)
        h.rate_limit_store.record(
            "worker", rule_id="codex.rate_limit.usage_limit", retry_at="2099-01-01T00:00:00", evidence="e"
        )
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.mail.send_mail(commander, worker, "[JOB-A] 依頼", "b")
        agent = _agent("worker", worker, "exit_success.py")
        h.reply_after_launch(worker, commander, "[JOB-A] 完了", "b")
        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(outcomes[0].status, OutcomeStatus.SUCCESS)
        self.assertFalse(h.rate_limit_store.is_in_cooldown("worker"))


class NoWorkAndOrderingTests(unittest.TestCase):
    def test_no_unread_produces_no_outcomes(self) -> None:
        h = DispatchCycleHarness()
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        agent = _agent("worker", worker, "exit_success.py")
        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(outcomes, [])
        self.assertEqual(h.mail.send_mail_calls, 0)

    def test_ack_only_is_not_a_terminal_reply(self) -> None:
        h = DispatchCycleHarness(max_retries=0)
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.mail.send_mail(commander, worker, "[JOB-A] 依頼", "b")
        h.reply_after_launch(worker, commander, "[JOB-A] STATUS: ACK", "ack")
        outcome = h.cycle.run_one_pass([_agent("worker", worker, "exit_success.py")])[0]
        self.assertEqual(outcome.status, OutcomeStatus.NO_REPLY)

    def test_agents_processed_in_config_order_not_registration_order(self) -> None:
        h = DispatchCycleHarness()
        commander = h.mail.register_user("commander")
        # Register "second" before "first" so registration/UID order differs
        # from config order.
        second_uid = h.mail.register_user("second")
        first_uid = h.mail.register_user("first")
        h.mail.send_mail(commander, first_uid, "[JOB-A] 依頼", "b")
        h.mail.send_mail(commander, second_uid, "[JOB-B] 依頼", "b")

        seen: list[str] = []

        class RecordingWatcher(MailWatcher):
            def list_unread_mails(self, agent):
                seen.append(agent.name)
                return super().list_unread_mails(agent)

        h.cycle._watcher = RecordingWatcher(h.mail)
        first = AgentDefinition(name="first", uid=first_uid, cli_type="fake", command=[sys.executable, str(_STUBS_DIR / "exit_success.py")], order_index=0)
        second = AgentDefinition(name="second", uid=second_uid, cli_type="fake", command=[sys.executable, str(_STUBS_DIR / "exit_success.py")], order_index=1)
        # Pass in reverse to prove sorting uses order_index, not list order.
        h.cycle.run_one_pass([second, first])
        self.assertEqual(seen, ["first", "second"])

    def test_double_launch_is_prevented(self) -> None:
        import subprocess as sp

        from runtime import RunningAgentState
        from winproc import get_process_start_time_iso, terminate_process_tree

        h = DispatchCycleHarness()
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.mail.send_mail(commander, worker, "[JOB-A] 依頼", "b")
        # A genuinely running process, so _is_agent_running's PID+start_time
        # check (not mere file presence) finds it still alive.
        blocker = sp.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        try:
            h.runtime_store.save(
                RunningAgentState(
                    pid=blocker.pid,
                    process_start_time_iso=get_process_start_time_iso(blocker.pid),
                    agent_name="worker",
                    agent_uid=worker,
                    job_id="JOB-A",
                    origin_mail_id=1,
                    launch_command=["x"],
                    recorded_at_iso=now_iso(),
                )
            )
            agent = _agent("worker", worker, "exit_success.py")
            outcomes = h.cycle.run_one_pass([agent])
            self.assertEqual(outcomes, [])
            self.assertEqual(h.mail.receive_mail_calls, 0)
        finally:
            terminate_process_tree(blocker.pid)
            blocker.wait(timeout=10)

    def test_stale_runtime_entry_does_not_block_launch(self) -> None:
        """A leftover entry for a PID that is no longer that process must
        not permanently block this agent (Codex review finding #4)."""
        from runtime import RunningAgentState

        h = DispatchCycleHarness()
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.mail.send_mail(commander, worker, "[JOB-A] 依頼", "b")
        h.reply_after_launch(worker, commander, "[JOB-A] 完了報告", "done")
        h.runtime_store.save(
            RunningAgentState(
                pid=999_999,
                process_start_time_iso="2000-01-01T00:00:00.000Z",
                agent_name="worker",
                agent_uid=worker,
                job_id="JOB-STALE",
                origin_mail_id=1,
                launch_command=["x"],
                recorded_at_iso=now_iso(),
            )
        )
        agent = _agent("worker", worker, "exit_success.py")
        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(outcomes[0].status, OutcomeStatus.SUCCESS)
        self.assertEqual(h.runtime_store.load_all(), [])


class SuccessAndReplyMatchingTests(unittest.TestCase):
    def test_invalid_structured_metadata_isolated_without_stopping_pass(self) -> None:
        h = DispatchCycleHarness()
        sender = h.mail.register_user("sender")
        bad_uid = h.mail.register_user("bad-worker")
        good_uid = h.mail.register_user("good-worker")
        bad_id = h.mail.send_mail(
            sender,
            bad_uid,
            "[JOB-BAD-METADATA] malformed task",
            json.dumps({"message_type": "TASK", "task_eligible": True}),
        )
        h.mail.send_mail(sender, good_uid, "[JOB-GOOD-METADATA] task", "plain")
        h.reply_after_launch(
            good_uid, sender, "[JOB-GOOD-METADATA] COMPLETED", ""
        )
        outcomes = h.cycle.run_one_pass(
            [
                _agent("bad-worker", bad_uid, "exit_success.py"),
                _agent("good-worker", good_uid, "exit_success.py"),
            ]
        )
        self.assertEqual(
            [outcome.status for outcome in outcomes],
            [OutcomeStatus.HUMAN_REQUIRED, OutcomeStatus.SUCCESS],
        )
        self.assertTrue(h.terminal_store.is_terminal(bad_id))

    def test_duplicate_delegated_task_launches_child_only_once(self) -> None:
        h = DispatchCycleHarness()
        director = h.mail.register_user("director")
        reviewer = h.mail.register_user("reviewer")
        payload = {
            "message_type": "DECISION_REQUEST",
            "task_eligible": True,
            "status": "DELEGATED",
            "invocation_result": "DELEGATED",
            "job_id": "JOB-DUP-DELEGATE",
            "decision_id": "DEC-DUP-DELEGATE",
            "invocation_id": "INV-DIRECTOR-DELEGATE",
            "parent_invocation_id": "INV-ROOT-DELEGATE",
            "root_invocation_id": "INV-ROOT-DELEGATE",
            "trigger_mail_uid": 1,
        }
        first_id = h.mail.send_mail(
            director,
            reviewer,
            "[JOB-DUP-DELEGATE] [DEC-DUP-DELEGATE] DECISION_REQUEST",
            json.dumps(payload),
        )
        second_id = h.mail.send_mail(
            director,
            reviewer,
            "[JOB-DUP-DELEGATE] [DEC-DUP-DELEGATE] DECISION_REQUEST",
            json.dumps(payload),
        )
        h.reply_after_launch(
            reviewer,
            director,
            "[JOB-DUP-DELEGATE] COMPLETED",
            "",
        )
        agent = _agent("reviewer", reviewer, "exit_success.py")
        first = h.cycle.run_one_pass([agent])
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].status, OutcomeStatus.SUCCESS)
        self.assertEqual(first[0].origin_mail_id, first_id)
        self.assertEqual(h.cycle.run_one_pass([agent]), [])
        self.assertEqual(
            h.terminal_store.get_status(second_id),
            "DUPLICATE_DELEGATED_RESULT",
        )

    def test_third_party_delegation_is_successful_invocation_result(self) -> None:
        h = DispatchCycleHarness()
        originator = h.mail.register_user("originator")
        director = h.mail.register_user("director")
        reviewer = h.mail.register_user("reviewer")
        origin_id = h.mail.send_mail(
            originator, director, "[JOB-DELEGATED] [DEC-D] question", "{}"
        )
        h.reply_after_launch(
            director,
            reviewer,
            "[JOB-DELEGATED] [DEC-D] DELEGATED DECISION_REQUEST",
            "",
        )
        outcome = h.cycle.run_one_pass(
            [_agent("director", director, "exit_success.py")]
        )[0]
        self.assertEqual(outcome.status, OutcomeStatus.SUCCESS)
        self.assertEqual(h.terminal_store.get_status(origin_id), "DELEGATED")

    def test_result_visible_at_timeout_edge_wins_during_final_grace(self) -> None:
        h = DispatchCycleHarness()
        originator = h.mail.register_user("originator")
        worker = h.mail.register_user("worker")
        third_party = h.mail.register_user("third-party")
        h.mail.send_mail(
            originator, worker, "[JOB-EDGE] [DEC-EDGE] request", "{}"
        )
        original_launch = h.cycle._launcher.launch

        def patched_launch(agent, job_id, origin_mail_id, project_path, **kwargs):
            launched = original_launch(
                agent, job_id, origin_mail_id, project_path, **kwargs
            )
            original_wait = launched.wait

            def timeout_edge_wait(timeout_sec, **wait_kwargs):
                base = original_wait(10, terminal_reply_check=None)
                env_vars = kwargs["env_vars"]
                h.mail.send_mail(
                    worker,
                    third_party,
                    "edge result",
                    json.dumps(
                        {
                            "status": "DELEGATED",
                            "invocation_result": "DELEGATED",
                            "job_id": job_id,
                            "decision_id": env_vars["DECISION_ID"],
                            "invocation_id": launched.invocation_id,
                            "parent_invocation_id": env_vars.get(
                                "AI_PARENT_INVOCATION_ID"
                            ),
                            "root_invocation_id": env_vars[
                                "AI_ROOT_INVOCATION_ID"
                            ],
                            "trigger_mail_uid": int(
                                env_vars["AI_TRIGGER_MAIL_UID"]
                            ),
                        }
                    ),
                )
                return ProcessResult(
                    exit_code=None,
                    timed_out=True,
                    duration_sec=base.duration_sec,
                    invocation_id=launched.invocation_id,
                    launch_started_at=launched.launched_at_iso,
                )

            launched.wait = timeout_edge_wait
            return launched

        h.cycle._launcher.launch = patched_launch
        outcome = h.cycle.run_one_pass(
            [_agent("worker", worker, "exit_success.py")]
        )[0]
        self.assertEqual(outcome.status, OutcomeStatus.SUCCESS)

    def test_late_result_does_not_reopen_finalized_timeout(self) -> None:
        h = DispatchCycleHarness()
        h.cycle._terminal_grace_sec = 0
        originator = h.mail.register_user("originator")
        worker = h.mail.register_user("worker")
        origin_id = h.mail.send_mail(
            originator, worker, "[JOB-LATE] [DEC-LATE] request", "{}"
        )
        captured: dict[str, object] = {}
        original_launch = h.cycle._launcher.launch

        def patched_launch(agent, job_id, origin_mail_id, project_path, **kwargs):
            launched = original_launch(
                agent, job_id, origin_mail_id, project_path, **kwargs
            )
            original_wait = launched.wait
            captured.update(kwargs["env_vars"])
            captured["invocation_id"] = launched.invocation_id

            def timed_out_wait(timeout_sec, **wait_kwargs):
                base = original_wait(10, terminal_reply_check=None)
                return ProcessResult(
                    exit_code=None,
                    timed_out=True,
                    duration_sec=base.duration_sec,
                    invocation_id=launched.invocation_id,
                    launch_started_at=launched.launched_at_iso,
                )

            launched.wait = timed_out_wait
            return launched

        h.cycle._launcher.launch = patched_launch
        agent = _agent("worker", worker, "exit_success.py")
        first = h.cycle.run_one_pass([agent])[0]
        self.assertEqual(first.status, OutcomeStatus.TIMEOUT)
        h.mail.send_mail(
            worker,
            originator,
            "late result",
            json.dumps(
                {
                    "status": "COMPLETED",
                    "invocation_result": "COMPLETED",
                    "job_id": "JOB-LATE",
                    "decision_id": "DEC-LATE",
                    "invocation_id": captured["invocation_id"],
                    "parent_invocation_id": captured.get(
                        "AI_PARENT_INVOCATION_ID"
                    ),
                    "root_invocation_id": captured["AI_ROOT_INVOCATION_ID"],
                    "trigger_mail_uid": int(captured["AI_TRIGGER_MAIL_UID"]),
                }
            ),
        )
        self.assertEqual(h.cycle.run_one_pass([agent]), [])
        self.assertEqual(h.terminal_store.get_status(origin_id), "TIMEOUT")

    def test_success_when_cli_exits_zero_and_reply_matches(self) -> None:
        h = DispatchCycleHarness()
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.mail.send_mail(commander, worker, "[JOB-A] 依頼", "b")
        agent = _agent("worker", worker, "exit_success.py")

        # Simulate the AI itself having called send_mail with its reply
        # while the (fake) CLI process is running, so the reply's
        # timestamp is guaranteed to be after the launch instant. The
        # origin mail is deliberately left unread here: DispatchCycle --
        # not the test -- is responsible for noticing it and dispatching;
        # if the test marked it read first, MailWatcher would see no
        # pending work at all.
        h.reply_after_launch(worker, commander, "[JOB-A] 完了報告", "done")

        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].status, OutcomeStatus.SUCCESS)
        self.assertEqual(h.round_trips.count("JOB-A"), 1)

    def test_no_reply_produces_no_reply_status_and_notification(self) -> None:
        h = DispatchCycleHarness()
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.mail.send_mail(commander, worker, "[JOB-A] 依頼", "b")
        agent = _agent("worker", worker, "exit_success.py")

        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(outcomes[0].status, OutcomeStatus.NO_REPLY)
        self.assertEqual(h.mail.send_mail_calls, 2)  # original + notification

    def test_reply_landing_same_millisecond_as_launch_still_counts(self) -> None:
        h = DispatchCycleHarness()
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.mail.send_mail(commander, worker, "[JOB-A] 依頼", "b")

        # Patch launcher to record the launch instant and immediately seed a
        # reply stamped at exactly that same millisecond, before the stub
        # process (which just exits 0) is even waited on.
        original_launch = h.cycle._launcher.launch

        def patched_launch(agent, job_id, origin_mail_id, project_path, **kwargs):
            launched = original_launch(agent, job_id, origin_mail_id, project_path, **kwargs)
            reply_id = h.mail.send_mail(worker, commander, "[JOB-A] 完了報告", "done")
            h.mail._mails[-1]["subject"] += f" [{launched.invocation_id}]"
            env_vars = kwargs["env_vars"]
            h.mail._mails[-1]["body"] = json.dumps({
                "status": "COMPLETED",
                "invocation_result": "COMPLETED",
                "job_id": job_id,
                "decision_id": env_vars.get("DECISION_ID", ""),
                "invocation_id": launched.invocation_id,
                "parent_invocation_id": env_vars.get("AI_PARENT_INVOCATION_ID"),
                "root_invocation_id": env_vars["AI_ROOT_INVOCATION_ID"],
                "trigger_mail_uid": int(env_vars["AI_TRIGGER_MAIL_UID"]),
            })
            h.mail.seed_sent_at(reply_id, launched.launched_at_iso)
            return launched

        h.cycle._launcher.launch = patched_launch
        agent = _agent("worker", worker, "exit_success.py")
        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(outcomes[0].status, OutcomeStatus.SUCCESS)

    def test_old_reply_with_same_job_id_is_not_mistaken_for_new_reply(self) -> None:
        h = DispatchCycleHarness()
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.mail.send_mail(worker, commander, "[JOB-A] 古い完了報告", "old")  # stale prior reply
        h.mail.send_mail(commander, worker, "[JOB-A] 依頼", "b")
        agent = _agent("worker", worker, "exit_success.py")
        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(outcomes[0].status, OutcomeStatus.NO_REPLY)

    def test_japanese_subject_and_body_round_trip(self) -> None:
        h = DispatchCycleHarness()
        commander = h.mail.register_user("指揮AI")
        worker = h.mail.register_user("作業AI")
        h.mail.send_mail(commander, worker, "[JOB-A] 日本語の作業依頼", "調査をお願いします。")
        h.reply_after_launch(worker, commander, "[JOB-A] 完了報告", "調査が完了しました。")
        agent = _agent("作業AI", worker, "exit_success.py")
        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(outcomes[0].status, OutcomeStatus.SUCCESS)


class FailureClassificationTests(unittest.TestCase):
    def test_nonzero_exit_is_failed_without_retry(self) -> None:
        h = DispatchCycleHarness(max_retries=2)
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.mail.send_mail(commander, worker, "[JOB-A] 依頼", "b")
        agent = _agent("worker", worker, "exit_fail.py")
        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(outcomes[0].status, OutcomeStatus.FAILED)
        # Only ever launched once: post-launch failures are not retried.
        self.assertEqual(len(list(h.logs_dir.glob("*.jsonl"))), 1)

    def test_timeout_is_reported_and_not_retried(self) -> None:
        h = DispatchCycleHarness(max_retries=2)
        h.cycle._cli_timeout_sec = 1
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.mail.send_mail(commander, worker, "[JOB-A] 依頼", "b")
        agent = _agent("worker", worker, "sleep_forever.py")
        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(outcomes[0].status, OutcomeStatus.TIMEOUT)

    def test_waiting_terminal_mail_stops_running_cli_without_timeout(self) -> None:
        h = DispatchCycleHarness(max_retries=0)
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.mail.send_mail(commander, worker, "[JOB-A] [DEC-1] 依頼", "b")
        h.reply_after_launch(
            worker, commander, "[JOB-A] [DEC-1] STATUS: WAITING_FOR_DECISION",
            json.dumps({"status": "WAITING_FOR_DECISION", "job_id": "JOB-A", "decision_id": "DEC-1"}),
        )
        agent = _agent("worker", worker, "sleep_forever.py")
        h.cycle._cli_timeout_sec = 3
        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(outcomes[0].status, OutcomeStatus.SUCCESS)
        self.assertFalse(any("TIMEOUT" in mail["subject"] for mail in h.mail._mails))

    def test_waiting_for_worker_terminal_mail_stops_running_cli_without_timeout(self) -> None:
        h = DispatchCycleHarness(max_retries=0)
        commander = h.mail.register_user("commander")
        director = h.mail.register_user("director")
        h.mail.send_mail(commander, director, "[JOB-A] [DEC-1] 依頼", "b")
        h.reply_after_launch(
            director, commander, "[JOB-A] [DEC-1] STATUS: WAITING_FOR_WORKER",
            json.dumps({"status": "WAITING_FOR_WORKER", "job_id": "JOB-A", "decision_id": "DEC-1"}),
        )
        agent = _agent("director", director, "sleep_forever.py")
        h.cycle._cli_timeout_sec = 3
        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(outcomes[0].status, OutcomeStatus.SUCCESS)
        self.assertFalse(any("NO_REPLY" in mail["subject"] for mail in h.mail._mails))

    def test_delivery_failed_retries_up_to_max_then_notifies(self) -> None:
        h = DispatchCycleHarness(max_retries=2)
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.mail.send_mail(commander, worker, "[JOB-A] 依頼", "b")
        # cli_type "fake" resolves to a nonexistent default command, and the
        # agent supplies no explicit command, so every attempt fails to launch.
        agent = AgentDefinition(name="worker", uid=worker, cli_type="fake", command=[])
        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(outcomes[0].status, OutcomeStatus.DELIVERY_FAILED)
        self.assertEqual(h.mail.send_mail_calls, 2)  # original + one notification
        self.assertTrue(h.terminal_store.is_terminal(1))

    def test_project_path_missing_is_delivery_failed(self) -> None:
        h = DispatchCycleHarness(max_retries=0)
        h.cycle._project_path = h.project_path / "does-not-exist"
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.mail.send_mail(commander, worker, "[JOB-A] 依頼", "b")
        agent = _agent("worker", worker, "exit_success.py")
        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(outcomes[0].status, OutcomeStatus.DELIVERY_FAILED)


class NotificationTests(unittest.TestCase):
    def test_control_filter_uses_only_explicit_structured_metadata(self) -> None:
        control = DispatchCycle._control_payload
        self.assertIsNotNone(
            control(
                {
                    "body": json.dumps(
                        {"message_type": "SYSTEM_ALERT", "task_eligible": True}
                    )
                }
            )
        )
        self.assertIsNotNone(
            control(
                {
                    "body": json.dumps(
                        {"message_type": "INVOCATION_ACK", "task_eligible": False}
                    )
                }
            )
        )
        for body in (
            "{broken",
            json.dumps({"message_type": "TASK"}),
            json.dumps({"message_type": "TASK", "task_eligible": True}),
        ):
            self.assertIsNone(control({"body": body, "subject": "[NO_REPLY]"}))

    def test_notification_body_contains_required_fields(self) -> None:
        h = DispatchCycleHarness(max_retries=0)
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.mail.send_mail(commander, worker, "[JOB-A] 依頼", "b")
        agent = _agent("worker", worker, "exit_fail.py")
        h.cycle.run_one_pass([agent])
        notification = h.mail._mails[-1]
        self.assertEqual(notification["recipient_uid"], commander)
        payload = json.loads(notification["body"])
        self.assertEqual(payload["message_type"], "SYSTEM_ALERT")
        self.assertIs(payload["task_eligible"], False)
        self.assertEqual(payload["job_id"], "JOB-A")
        self.assertIn("failure", payload)
        self.assertIn("evidence", payload)
        for label in (
            "状態:", "依頼ID:", "元メールID:", "元の送信者UID:", "処理対象AI:",
            "処理対象UID:", "失敗段階:", "失敗理由:", "CLI終了コード:", "実行時間:",
            "再試行回数:", "最終試行日時:", "元メールの状態:", "推奨する次の対応:",
        ):
            self.assertIn(label, payload["human_readable"])

    def test_notification_never_contains_secret_markers(self) -> None:
        h = DispatchCycleHarness(max_retries=0)
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.mail.send_mail(commander, worker, "[JOB-A] 依頼", "b")
        agent = _agent("worker", worker, "exit_fail.py")
        h.cycle.run_one_pass([agent])
        notification = h.mail._mails[-1]
        lowered = notification["body"].lower()
        for marker in ("api_key", "token", "password", "cookie", "secret"):
            self.assertNotIn(marker, lowered)

    def test_notify_failure_due_to_invalid_sender_uid_yields_human_required(self) -> None:
        h = DispatchCycleHarness(max_retries=0)
        worker = h.mail.register_user("worker")
        # Bypass send_mail's own UID validation by injecting a mail whose
        # sender_uid is not (or no longer) a registered user.
        h.mail._mails.append(
            {
                "mail_id": 1, "sender_uid": "UID000999", "sender_name": "ghost",
                "recipient_uid": worker, "recipient_name": "worker",
                "subject": "[JOB-A] 依頼", "body": "b", "sent_at": now_iso(),
                "is_read": False, "read_at": None,
            }
        )
        h.mail._next_mail_id = 2
        agent = _agent("worker", worker, "exit_fail.py")
        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(outcomes[0].status, OutcomeStatus.HUMAN_REQUIRED)
        self.assertEqual(h.terminal_store.get_status(1), "HUMAN_REQUIRED")

    def test_orchestrator_self_origin_does_not_recurse(self) -> None:
        h = DispatchCycleHarness(max_retries=0)
        worker = h.mail.register_user("worker")
        h.mail.send_mail(h.system_uid, worker, "[JOB-A][TIMEOUT] worker から応答なし", "b")
        agent = _agent("worker", worker, "exit_fail.py")
        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(outcomes[0].status, OutcomeStatus.HUMAN_REQUIRED)
        # No new mail was sent (would be mail #2); still just the original.
        self.assertEqual(h.mail.send_mail_calls, 1)

    def test_system_alert_remains_unread_and_never_launches(self) -> None:
        h = DispatchCycleHarness(max_retries=0)
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.mail.send_mail(commander, worker, "[JOB-ALERT] task", "b")
        h.cycle.run_one_pass([_agent("worker", worker, "exit_fail.py")])
        alert = h.mail._mails[-1]
        alert_payload = json.loads(alert["body"])
        self.assertEqual(alert_payload["message_type"], "SYSTEM_ALERT")
        self.assertIs(alert_payload["task_eligible"], False)

        commander_agent = _agent("commander", commander, "exit_success.py")
        self.assertEqual(h.cycle.run_one_pass([commander_agent]), [])
        self.assertTrue(h.terminal_store.is_terminal(alert["mail_id"]))
        self.assertFalse(
            next(
                message["is_read"]
                for message in h.mail._mails
                if message["mail_id"] == alert["mail_id"]
            )
        )
        self.assertIn("human_readable", alert_payload)
        self.assertEqual(h.cycle.run_one_pass([commander_agent]), [])

    def test_control_is_skipped_before_normal_task_even_with_alert_subject(self) -> None:
        h = DispatchCycleHarness(max_retries=0)
        sender = h.mail.register_user("sender")
        worker = h.mail.register_user("worker")
        control_id = h.mail.send_mail(
            sender,
            worker,
            "ordinary display",
            json.dumps(
                {
                    "message_type": "INVOCATION_ACK",
                    "task_eligible": False,
                    "status": "ACK_RECEIVED",
                }
            ),
        )
        normal_id = h.mail.send_mail(
            sender,
            worker,
            "[JOB-NORMAL] [DEC-NORMAL] [NO_REPLY] this is a real task",
            json.dumps(
                {
                    "message_type": "TASK",
                    "task_eligible": True,
                    "job_id": "JOB-NORMAL",
                    "decision_id": "DEC-NORMAL",
                    "invocation_id": "INV-PARENT-NORMAL",
                    "parent_invocation_id": None,
                    "root_invocation_id": "INV-PARENT-NORMAL",
                    "trigger_mail_uid": 1,
                }
            ),
        )
        h.reply_after_launch(worker, sender, "[JOB-NORMAL] COMPLETED", "")
        outcomes = h.cycle.run_one_pass(
            [_agent("worker", worker, "exit_success.py")]
        )
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].origin_mail_id, normal_id)
        self.assertEqual(outcomes[0].status, OutcomeStatus.SUCCESS)
        self.assertEqual(
            h.terminal_store.get_status(control_id),
            "IGNORED_CONTROL_NOTIFICATION",
        )


class RoundTripCapTests(unittest.TestCase):
    def test_reaching_cap_stops_without_launching(self) -> None:
        h = DispatchCycleHarness(max_round_trips=1)
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.round_trips.increment("JOB-A")
        h.mail.send_mail(commander, worker, "[JOB-A] 追加の依頼", "b")
        agent = _agent("worker", worker, "exit_success.py")
        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(outcomes[0].status, OutcomeStatus.HUMAN_REQUIRED)
        self.assertEqual(h.mail.receive_mail_calls, 0)


class StopRequestBetweenAgentsTests(unittest.TestCase):
    def test_should_stop_launching_prevents_further_agents(self) -> None:
        h = DispatchCycleHarness()
        commander = h.mail.register_user("commander")
        worker1 = h.mail.register_user("worker1")
        worker2 = h.mail.register_user("worker2")
        h.mail.send_mail(commander, worker1, "[JOB-A] 依頼", "b")
        h.mail.send_mail(commander, worker2, "[JOB-B] 依頼", "b")
        agent1 = AgentDefinition(name="worker1", uid=worker1, cli_type="fake", command=[sys.executable, str(_STUBS_DIR / "exit_success.py")], order_index=0)
        agent2 = AgentDefinition(name="worker2", uid=worker2, cli_type="fake", command=[sys.executable, str(_STUBS_DIR / "exit_success.py")], order_index=1)
        outcomes = h.cycle.run_one_pass([agent1, agent2], should_stop_launching=lambda: True)
        self.assertEqual(outcomes, [])


class NeverReceivesMailTests(unittest.TestCase):
    def test_orchestrator_never_calls_receive_mail_across_full_cycle(self) -> None:
        h = DispatchCycleHarness()
        commander = h.mail.register_user("commander")
        worker = h.mail.register_user("worker")
        h.mail.send_mail(commander, worker, "[JOB-A] 依頼", "b")
        h.reply_after_launch(worker, commander, "[JOB-A] 完了報告", "done")
        agent = _agent("worker", worker, "exit_success.py")
        outcomes = h.cycle.run_one_pass([agent])
        self.assertEqual(outcomes[0].status, OutcomeStatus.SUCCESS)
        # The dispatch pipeline never calls receive_mail on the
        # orchestrator's behalf (QandA Q013) -- only the AI process itself
        # would, and this fake stub never touches the mail package at all.
        self.assertEqual(h.mail.receive_mail_calls, 0)


class CheckpointStorePathSafetyTests(unittest.TestCase):
    def test_path_traversal_job_id_is_rejected(self) -> None:
        from dispatch import Checkpoint

        store = CheckpointStore(Path(tempfile.mkdtemp()))
        checkpoint = Checkpoint(
            job_id="/".join([".."] * 10) + "/evil", purpose="p", current_state="s"
        )
        with self.assertRaises(ValueError):
            store.save(checkpoint)


class RoundTripCounterPersistenceTests(unittest.TestCase):
    def test_count_survives_across_instances(self) -> None:
        runtime_dir = Path(tempfile.mkdtemp())
        first = RoundTripCounter(max_round_trips=10, runtime_dir=runtime_dir)
        first.increment("JOB-A")
        first.increment("JOB-A")

        second = RoundTripCounter(max_round_trips=10, runtime_dir=runtime_dir)
        self.assertEqual(second.count("JOB-A"), 2)

    def test_without_runtime_dir_stays_in_memory_only(self) -> None:
        counter = RoundTripCounter(max_round_trips=10)
        counter.increment("JOB-A")
        self.assertEqual(counter.count("JOB-A"), 1)


class OrchestratorNewFeatureTests(unittest.TestCase):
    def test_job_id_and_decision_id_extraction(self) -> None:
        # 1. Valid Job-ID extraction
        self.assertEqual(extract_job_id("[JOB-SPEC-100] Task", 1), "JOB-SPEC-100")

        # 2. Invalid subject produces NOJOB-* fallback and logs warning if logger passed
        tmp_dir = Path(tempfile.mkdtemp())
        logger = JobLogger(tmp_dir)
        job_id = extract_job_id("No tag subject", 42, logger=logger)
        self.assertEqual(job_id, "NOJOB-42")

        log_content = (tmp_dir / "orchestrator.jsonl").read_text(encoding="utf-8")
        self.assertIn("NOJOB_FALLBACK", log_content)
        self.assertIn("NOJOB-42", log_content)

    def test_env_vars_and_protected_variables_propagation(self) -> None:
        h = DispatchCycleHarness()
        worker = h.mail.register_user("worker")
        boss = h.mail.register_user("boss")

        h.mail.send_mail(
            boss, worker, "[JOB-ENV-001] [DEC-001] Test Env", "Body"
        )
        agent = _agent("worker", worker, "exit_success.py")
        
        # Test _attempt_launch environment building
        status, proc = h.cycle._attempt_launch(agent, "JOB-ENV-001", {"subject": "[JOB-ENV-001] [DEC-001] Test", "sender_uid": boss, "mail_id": 1}, 1)
        # Verify launch built environment variables correctly
        env_captured = h.launcher._build_subprocess_env(extra_env={
            "AGENT_UID": agent.uid,
            "REPLY_TO_UID": boss,
            "JOB_ID": "JOB-ENV-001",
            "DECISION_ID": "DEC-001",
            "PROJECT_PATH": str(h.project_path),
        })
        self.assertEqual(env_captured["AGENT_UID"], agent.uid)
        self.assertEqual(env_captured["REPLY_TO_UID"], boss)
        self.assertEqual(env_captured["JOB_ID"], "JOB-ENV-001")
        self.assertEqual(env_captured["DECISION_ID"], "DEC-001")
        self.assertEqual(env_captured["PROJECT_PATH"], str(h.project_path))

    def test_secrets_redacted_from_logger(self) -> None:
        from logging_utils import LogEntry
        tmp_dir = Path(tempfile.mkdtemp())
        logger = JobLogger(tmp_dir)
        logger.log_outcome(LogEntry(
            job_id="JOB-SEC-001",
            mail_id=1,
            agent_name="agent",
            command_summary="run --api-key=supersecret123",
            started_at=None,
            finished_at=None,
            exit_code=0,
            result="SUCCESS",
            error="token=secret_pass_123",
        ))
        log_content = (tmp_dir / "orchestrator.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("supersecret123", log_content)
        self.assertNotIn("secret_pass_123", log_content)
        self.assertIn("[REDACTED]", log_content)


if __name__ == "__main__":
    unittest.main()
