import sys
import tempfile
import unittest
from pathlib import Path

import launcher as launcher_module
from config import AgentDefinition
from launcher import CliLauncher, CliNotFoundError, CliPathResolver, redact_command
from tests.fakes import FakeCliAdapter

_STUBS_DIR = Path(__file__).resolve().parent / "stubs"


class CliPathResolverTests(unittest.TestCase):
    def test_explicit_config_command_wins(self) -> None:
        resolver = CliPathResolver({"codex": FakeCliAdapter("codex")})
        agent = AgentDefinition(
            name="a", uid="UID000001", cli_type="codex", command=["C:/tools/codex.exe", "--flag"]
        )
        self.assertEqual(resolver.resolve(agent), ["C:/tools/codex.exe", "--flag"])

    def test_unknown_cli_type_without_explicit_command_raises(self) -> None:
        resolver = CliPathResolver({})
        agent = AgentDefinition(name="a", uid="UID000001", cli_type="ghost", command=[])
        with self.assertRaises(CliNotFoundError):
            resolver.resolve(agent)

    def test_default_name_not_on_path_raises(self) -> None:
        resolver = CliPathResolver({"codex": FakeCliAdapter("definitely-not-a-real-executable-xyz")})
        agent = AgentDefinition(name="a", uid="UID000001", cli_type="codex", command=[])
        with self.assertRaises(CliNotFoundError):
            resolver.resolve(agent)

    def test_default_name_resolved_via_path(self) -> None:
        # python itself is guaranteed to be on PATH in this test environment.
        resolver = CliPathResolver({"py": FakeCliAdapter("python")})
        agent = AgentDefinition(name="a", uid="UID000001", cli_type="py", command=[])
        resolved = resolver.resolve(agent)
        self.assertEqual(len(resolved), 1)
        self.assertTrue(Path(resolved[0]).name.lower().startswith("python"))


class CliLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_path = Path(tempfile.mkdtemp())
        self.adapters = {"fake": FakeCliAdapter()}

    def _launcher_for(self, stub_name: str) -> CliLauncher:
        resolver = CliPathResolver(self.adapters)
        return CliLauncher(resolver, self.adapters)

    def _agent(self, stub_name: str) -> AgentDefinition:
        return AgentDefinition(
            name="worker",
            uid="UID000002",
            cli_type="fake",
            command=[sys.executable, str(_STUBS_DIR / stub_name)],
        )

    def test_success_process_reports_exit_code_zero(self) -> None:
        launcher = self._launcher_for("exit_success.py")
        launched = launcher.launch(self._agent("exit_success.py"), "JOB-1", 1, self.project_path)
        result = launched.wait(timeout_sec=10)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)

    def test_each_cli_launch_carries_a_distinct_invocation_id(self) -> None:
        launcher = self._launcher_for("exit_success.py")
        first = launcher.launch(self._agent("exit_success.py"), "JOB-1", 1, self.project_path, invocation_id="INV-1")
        second = launcher.launch(self._agent("exit_success.py"), "JOB-1", 2, self.project_path, invocation_id="INV-2")
        self.assertEqual(first.invocation_id, "INV-1")
        self.assertEqual(second.invocation_id, "INV-2")
        self.assertNotEqual(first.invocation_id, second.invocation_id)
        self.assertEqual(first.wait(timeout_sec=10).exit_code, 0)
        self.assertEqual(second.wait(timeout_sec=10).exit_code, 0)

    def test_failure_process_reports_nonzero_exit_code(self) -> None:
        launcher = self._launcher_for("exit_fail.py")
        launched = launcher.launch(self._agent("exit_fail.py"), "JOB-1", 1, self.project_path)
        result = launched.wait(timeout_sec=10)
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.timed_out)

    def test_timeout_terminates_process(self) -> None:
        launcher = self._launcher_for("sleep_forever.py")
        launched = launcher.launch(self._agent("sleep_forever.py"), "JOB-1", 1, self.project_path)
        result = launched.wait(timeout_sec=1)
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.exit_code)

    def test_waiting_notification_stops_without_timeout(self) -> None:
        launcher = self._launcher_for("sleep_forever.py")
        launched = launcher.launch(self._agent("sleep_forever.py"), "JOB-1", 1, self.project_path)
        result = launched.wait(
            timeout_sec=10,
            terminal_reply_check=lambda: ("WAITING_FOR_DECISION", 42),
            poll_interval_sec=0.05,
            terminal_grace_sec=0.05,
        )
        self.assertEqual(result.terminal_status, "WAITING_FOR_DECISION")
        self.assertEqual(result.terminal_mail_id, 42)
        self.assertFalse(result.timed_out)

    def test_waiting_notification_allows_natural_exit(self) -> None:
        launcher = self._launcher_for("sleep_then_exit.py")
        launched = launcher.launch(self._agent("sleep_then_exit.py"), "JOB-1", 1, self.project_path)
        result = launched.wait(
            timeout_sec=10,
            terminal_reply_check=lambda: ("WAITING_FOR_DECISION", 43),
            poll_interval_sec=0.05,
            terminal_grace_sec=1,
        )
        self.assertEqual(result.terminal_status, "WAITING_FOR_DECISION")
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)

    def test_instruction_delivered_via_stdin_not_argv(self) -> None:
        out_path = self.project_path / "captured.txt"
        agent = AgentDefinition(
            name="worker",
            uid="UID000002",
            cli_type="fake",
            command=[sys.executable, str(_STUBS_DIR / "echo_instruction.py"), str(out_path)],
        )
        launcher = self._launcher_for("echo_instruction.py")
        launched = launcher.launch(agent, "JOB-1", 1, self.project_path)
        self.assertNotIn("あなたは", " ".join(launched.launch_command))
        launched.wait(timeout_sec=10)
        captured = out_path.read_text(encoding="utf-8")
        self.assertIn("あなたはworkerです。", captured)
        self.assertIn("UIDはUID000002です。", captured)

    def test_japanese_instruction_round_trips_without_mojibake(self) -> None:
        out_path = self.project_path / "captured_ja.txt"
        agent = AgentDefinition(
            name="日本語エージェント",
            uid="UID000003",
            cli_type="fake",
            command=[sys.executable, str(_STUBS_DIR / "echo_instruction.py"), str(out_path)],
        )
        launcher = self._launcher_for("echo_instruction.py")
        launched = launcher.launch(agent, "JOB-1", 1, self.project_path)
        launched.wait(timeout_sec=10)
        captured = out_path.read_text(encoding="utf-8")
        self.assertIn("あなたは日本語エージェントです。", captured)

    def test_subprocess_env_sets_utf8(self) -> None:
        launcher = CliLauncher(CliPathResolver(self.adapters), self.adapters)
        env = launcher._build_subprocess_env()
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")

    def test_explicit_nonexistent_command_raises_cli_not_found_not_oserror(self) -> None:
        # config.json can name a command directly (SPEC.md 11章 rule 1), so
        # CliPathResolver never checks it exists; Popen itself would raise
        # a raw FileNotFoundError here if launch() did not convert it.
        agent = AgentDefinition(
            name="worker",
            uid="UID000002",
            cli_type="fake",
            command=["definitely-nonexistent-cli-xyz-12345"],
        )
        launcher = CliLauncher(CliPathResolver(self.adapters), self.adapters)
        with self.assertRaises(CliNotFoundError):
            launcher.launch(agent, "JOB-1", 1, self.project_path)

    def test_missing_start_time_is_never_replaced_by_launch_clock(self) -> None:
        # If the OS start-time query fails, launch() must not fall back to
        # its own launched_at clock -- that would let a later
        # is_same_running_process() check wrongly match a fabricated value
        # (Codex review finding #3).
        original = launcher_module.get_process_start_time_iso
        launcher_module.get_process_start_time_iso = lambda pid: None
        try:
            launcher = CliLauncher(CliPathResolver(self.adapters), self.adapters)
            launched = launcher.launch(self._agent("exit_success.py"), "JOB-1", 1, self.project_path)
            self.assertEqual(launched.start_time_iso, "")
            launched.wait(timeout_sec=10)
        finally:
            launcher_module.get_process_start_time_iso = original


class RedactCommandTests(unittest.TestCase):
    def test_secret_flag_value_is_masked(self) -> None:
        for flag in ("--token", "--api-key", "--password", "--auth"):
            with self.subTest(flag=flag):
                argv = ["claude", flag, "s3cr3t-value", "--other", "x"]
                redacted = redact_command(argv)
                self.assertNotIn("s3cr3t-value", redacted)
                self.assertEqual(redacted[-2:], ["--other", "x"])

    def test_secret_flag_equals_value_is_masked(self) -> None:
        redacted = redact_command(["claude", "--api-key=s3cr3t-value"])
        self.assertEqual(redacted[1], "--api-key=[REDACTED]")

    def test_url_embedded_credentials_are_masked(self) -> None:
        redacted = redact_command(["curl", "https://alice:hunter2@example.com/x"])
        self.assertNotIn("hunter2", redacted[1])
        self.assertNotIn("alice", redacted[1])

    def test_ordinary_arguments_are_unchanged(self) -> None:
        argv = ["claude", "-p", "--cd", "C:/project"]
        self.assertEqual(redact_command(argv), argv)


if __name__ == "__main__":
    unittest.main()
