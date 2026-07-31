import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from adapters.claude_code import ClaudeCodeCliAdapter
from config import AgentDefinition
from dispatch import ErrorNotifier, OutcomeStatus
from launcher import CliLauncher, CliPathResolver

_STUB = Path(__file__).resolve().parent / "stubs" / "burst_output.py"


class OutputCaptureTests(unittest.TestCase):
    def test_large_dual_stream_drains_and_detects_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            agent = AgentDefinition(
                name="worker", uid="UID000002", cli_type="claude_code",
                command=[sys.executable, str(_STUB)],
            )
            adapters = {"claude_code": ClaudeCodeCliAdapter()}
            launcher = CliLauncher(
                CliPathResolver(adapters), adapters, logs_dir=logs,
                output_max_bytes=1024 * 1024, output_ring_bytes=64 * 1024,
            )
            launched = launcher.launch(agent, "JOB-OUTPUT", 1, Path(tmp), attempt=1)
            result = launched.wait(20)
            self.assertEqual(result.exit_code, 0)
            self.assertIsNotNone(result.stdout)
            self.assertIsNotNone(result.stderr)
            assert result.stdout and result.stderr
            self.assertLessEqual(result.stdout.saved_bytes, 1024 * 1024)
            self.assertLessEqual(result.stderr.saved_bytes, 1024 * 1024)
            self.assertGreater(result.stdout.total_read_bytes, 1024 * 1024)
            self.assertGreater(result.stderr.total_read_bytes, 1024 * 1024)
            self.assertTrue(result.stdout.truncated)
            self.assertTrue(result.stderr.truncated)
            self.assertEqual(result.cli_evidence.rule_id, "claude.rate_limit.session_limit")
            self.assertEqual(result.cli_evidence.stream, "stdout")
            self.assertNotIn("super-secret", result.stderr.tail)
            stdout_path = logs / result.stdout.relative_path
            self.assertEqual(
                hashlib.sha256(stdout_path.read_bytes()).hexdigest(), result.stdout.sha256
            )

    def test_notification_tail_is_bounded_and_secrets_are_absent(self) -> None:
        class Mail:
            def __init__(self): self.sent = []
            def send_mail(self, *args): self.sent.append(args)

        mail = Mail()
        notifier = ErrorNotifier(mail, "UID999999", tail_bytes=8192)
        from dispatch import NotificationDetail
        detail = NotificationDetail(
            target_agent_name="worker", target_agent_uid="UID000002",
            failed_stage="CLI実行", reason="失敗", exit_code=1,
            duration_sec=1, retry_count=0, last_attempt_at="now",
            origin_mail_status="処理失敗", recommended_action="確認",
            stdout_tail="X" * 20000,
            stderr_tail="password=[REDACTED]" + "Y" * 20000,
        )
        self.assertTrue(notifier.notify("JOB-1", 1, "UID000001", OutcomeStatus.FAILED, detail))
        body = mail.sent[0][3]
        self.assertLessEqual(len(body.encode("utf-8")), 2 * 8192 + 4096)
        self.assertNotIn("super-secret", body)
        self.assertIn("[TRUNCATED]", body)


if __name__ == "__main__":
    unittest.main()
