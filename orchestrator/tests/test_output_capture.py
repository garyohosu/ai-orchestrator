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
from output_capture import StreamCapture, mask_text

_STUB = Path(__file__).resolve().parent / "stubs" / "burst_output.py"


class OutputCaptureTests(unittest.TestCase):
    def test_common_environment_credential_assignments_are_redacted(self) -> None:
        secret_values = (
            "github-value",
            "aws-value",
            "cookie-value",
            "api-value",
            "json-token-value",
            "json-secret-value",
        )
        text = "\n".join(
            (
                f"GITHUB_TOKEN={secret_values[0]}",
                f"AWS_SECRET_ACCESS_KEY='{secret_values[1]}'",
                f'SESSION_COOKIE_VALUE="{secret_values[2]}"',
                f"SERVICE_API_KEY: {secret_values[3]}",
                f'{{"GITHUB_TOKEN": "{secret_values[4]}"}}',
                f'{{"AWS_SECRET_ACCESS_KEY":"{secret_values[5]}"}}',
                r'{"GITHUB_TOKEN":"prefix\"json-escaped-tail"}',
                r'GITHUB_TOKEN="prefix\"shell-escaped-tail"',
                r"$env:GITHUB_TOKEN='prefix''powershell-single-tail'",
                r'$env:GITHUB_TOKEN="prefix`"powershell-double-tail"',
            )
        )
        masked = mask_text(text)
        for value in secret_values:
            self.assertNotIn(value, masked)
        self.assertNotIn("json-escaped-tail", masked)
        self.assertNotIn("shell-escaped-tail", masked)
        self.assertNotIn("powershell-single-tail", masked)
        self.assertNotIn("powershell-double-tail", masked)
        self.assertEqual(masked.count("[REDACTED]"), 10)

    def test_stream_redaction_reaches_system_alert_body(self) -> None:
        class Mail:
            def __init__(self): self.sent = []
            def send_mail(self, *args): self.sent.append(args)

        capture = StreamCapture(
            stream_name="stdout",
            output_path=None,
            max_file_bytes=1024,
            ring_bytes=64 * 1024,
            logs_root=None,
        )
        capture.feed(b"GITHUB_TO")
        capture.feed(
            b'KEN=github-value\nAWS_SECRET_ACCESS_KEY=aws-value\n'
            b'{"SESSION_COOKIE_VALUE":"cookie-value"}\n'
        )
        capture.feed(
            (
                r'{"GITHUB_TOKEN":"prefix\"json-escaped-tail"}' + "\n"
                + r'$env:GITHUB_TOKEN="prefix`"powershell-double-tail"'
                + "\n"
            ).encode("utf-8")
        )
        artifact = capture.finish()
        mail = Mail()
        notifier = ErrorNotifier(mail, "UID999999")
        from dispatch import NotificationDetail
        detail = NotificationDetail(
            target_agent_name="worker",
            target_agent_uid="UID000002",
            failed_stage="CLI",
            reason="failed",
            exit_code=1,
            duration_sec=1,
            retry_count=0,
            last_attempt_at="now",
            origin_mail_status="failed",
            recommended_action="inspect",
            stdout_tail=artifact.tail,
            stdout_artifact=artifact.as_dict(),
        )
        self.assertTrue(
            notifier.notify("JOB-SECRET", 1, "UID000001", OutcomeStatus.FAILED, detail)
        )
        body = mail.sent[0][3]
        self.assertNotIn("github-value", body)
        self.assertNotIn("aws-value", body)
        self.assertNotIn("cookie-value", body)
        self.assertNotIn("json-escaped-tail", body)
        self.assertNotIn("powershell-double-tail", body)
        self.assertIn("[REDACTED]", body)
        self.assertIn("credential_assignment", body)

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
            stdout_tail=("\n\\\"" * 10000),
            stderr_tail="password=[REDACTED]" + ("\r\\" * 10000),
        )
        self.assertTrue(notifier.notify("JOB-1", 1, "UID000001", OutcomeStatus.FAILED, detail))
        body = mail.sent[0][3]
        self.assertLessEqual(len(body.encode("utf-8")), 2 * 8192 + 4096)
        self.assertNotIn("super-secret", body)
        self.assertIn("[TRUNCATED]", body)


if __name__ == "__main__":
    unittest.main()
