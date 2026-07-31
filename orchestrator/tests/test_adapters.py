import unittest
from pathlib import Path

from adapters import build_adapters
from adapters.claude_code import ClaudeCodeCliAdapter
from adapters.codex import CodexCliAdapter
from output_capture import OutputArtifact


class CodexAdapterTests(unittest.TestCase):
    def test_default_command_name(self) -> None:
        self.assertEqual(CodexCliAdapter().default_command_name(), "codex")

    def test_build_argv_includes_workspace_write_and_cd(self) -> None:
        argv = CodexCliAdapter().build_argv(["codex"], Path("C:/proj"))
        self.assertIn("--full-auto", argv)
        self.assertIn("workspace-write", argv)
        self.assertIn(str(Path("C:/proj")), argv)
        self.assertEqual(argv[-1], "-")


class ClaudeCodeAdapterTests(unittest.TestCase):
    def test_default_command_name(self) -> None:
        self.assertEqual(ClaudeCodeCliAdapter().default_command_name(), "claude")

    def test_build_argv_uses_print_mode(self) -> None:
        argv = ClaudeCodeCliAdapter().build_argv(["claude"], Path("C:/proj"))
        self.assertEqual(argv, ["claude", "-p"])

    def test_session_limit_is_rate_limited_without_reset_time_dependency(self) -> None:
        artifact = OutputArtifact(None, None, 0, 0, False, "You've hit your session limit · resets 4pm (UTC)")
        evidence = ClaudeCodeCliAdapter().classify_output(1, False, artifact, OutputArtifact(None, None, 0, 0, False, ""))
        self.assertTrue(evidence.rate_limited)
        self.assertEqual(evidence.rule_id, "claude.rate_limit.session_limit")

    def test_temporarily_unavailable_is_not_rate_limited(self) -> None:
        artifact = OutputArtifact(None, None, 0, 0, False, "claude-sonnet-5 is temporarily unavailable")
        evidence = ClaudeCodeCliAdapter().classify_output(1, False, artifact, OutputArtifact(None, None, 0, 0, False, ""))
        self.assertFalse(evidence.rate_limited)


class RegistryTests(unittest.TestCase):
    def test_build_adapters_has_both_known_types(self) -> None:
        adapters = build_adapters()
        self.assertEqual(set(adapters), {"codex", "claude_code"})


if __name__ == "__main__":
    unittest.main()
