import json
import tempfile
import unittest
from pathlib import Path

from logging_utils import JobLogger, LogEntry


class JobLoggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logs_dir = Path(tempfile.mkdtemp())
        self.logger = JobLogger(self.logs_dir)

    def test_log_launch_writes_jsonl_line(self) -> None:
        self.logger.log_launch(
            LogEntry(
                job_id="JOB-A", mail_id=1, agent_name="worker",
                command_summary="python x.py", started_at="2026-01-01T00:00:00.000Z",
                finished_at=None, exit_code=None, result="LAUNCHED",
            )
        )
        lines = (self.logs_dir / "orchestrator.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["kind"], "launch")
        self.assertEqual(record["job_id"], "JOB-A")

    def test_secret_marker_in_command_summary_is_redacted(self) -> None:
        self.logger.log_launch(
            LogEntry(
                job_id="JOB-A", mail_id=1, agent_name="worker",
                command_summary="python x.py --api_key=abc123",
                started_at=None, finished_at=None, exit_code=None, result="LAUNCHED",
            )
        )
        record = json.loads((self.logs_dir / "orchestrator.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(record["command_summary"], "[REDACTED]")

    def test_secret_marker_in_error_is_redacted(self) -> None:
        self.logger.log_outcome(
            LogEntry(
                job_id="JOB-A", mail_id=1, agent_name="worker", command_summary=None,
                started_at=None, finished_at=None, exit_code=1, result="FAILED",
                error="auth failed: token=xyz",
            )
        )
        record = json.loads((self.logs_dir / "orchestrator.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(record["error"], "[REDACTED]")

    def test_japanese_fields_are_not_corrupted(self) -> None:
        self.logger.log_outcome(
            LogEntry(
                job_id="JOB-A", mail_id=1, agent_name="作業AI", command_summary=None,
                started_at=None, finished_at=None, exit_code=0, result="SUCCESS",
                error=None,
            )
        )
        record = json.loads((self.logs_dir / "orchestrator.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(record["agent_name"], "作業AI")


if __name__ == "__main__":
    unittest.main()
