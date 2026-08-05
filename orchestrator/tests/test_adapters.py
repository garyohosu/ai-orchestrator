import unittest
from pathlib import Path

from adapters import build_adapters
from adapters.antigravity import AntigravityCliAdapter
from adapters.claude_code import ClaudeCodeCliAdapter
from adapters.codex import CodexCliAdapter
from adapters.director import DirectorCliAdapter
from adapters.grok import GrokCliAdapter
from output_capture import OutputArtifact


class CodexAdapterTests(unittest.TestCase):
    def test_default_command_name(self) -> None:
        self.assertEqual(CodexCliAdapter().default_command_name(), "codex")

    def test_prompt_transport_is_stdin(self) -> None:
        self.assertEqual(CodexCliAdapter().prompt_transport, "stdin")

    def test_build_argv_includes_workspace_write_and_cd(self) -> None:
        argv = CodexCliAdapter().build_argv(["codex"], Path("C:/proj"))
        self.assertIn("--full-auto", argv)
        self.assertIn("workspace-write", argv)
        self.assertIn(str(Path("C:/proj")), argv)
        self.assertEqual(argv[-1], "-")

    def test_usage_limit_is_rate_limited_with_retry_at_parsed(self) -> None:
        # Real message observed 2026-08-05 (JOB-CSV-004 review, mail_id=41,
        # Codex CLI v0.146.0), see docs/ai-director-report.md.
        text = (
            "ERROR: You've hit your usage limit. Upgrade to Pro "
            "(https://chatgpt.com/explore/pro), visit "
            "https://chatgpt.com/codex/settings/usage to purchase more "
            "credits or try again at Aug 8th, 2026 5:17 PM."
        )
        artifact = OutputArtifact(None, None, 0, 0, False, text)
        evidence = CodexCliAdapter().classify_output(
            1, False, OutputArtifact(None, None, 0, 0, False, ""), artifact
        )
        self.assertTrue(evidence.rate_limited)
        self.assertEqual(evidence.rule_id, "codex.rate_limit.usage_limit")
        self.assertEqual(evidence.category, "rate_limit")
        self.assertEqual(evidence.stream, "stderr")
        self.assertEqual(evidence.retry_at, "2026-08-08T17:17:00")

    def test_unrelated_nonzero_exit_is_not_rate_limited(self) -> None:
        artifact = OutputArtifact(None, None, 0, 0, False, "some unrelated error")
        evidence = CodexCliAdapter().classify_output(
            1, False, artifact, OutputArtifact(None, None, 0, 0, False, "")
        )
        self.assertFalse(evidence.rate_limited)
        self.assertIsNone(evidence.category)


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


class GrokAdapterTests(unittest.TestCase):
    def test_default_command_name(self) -> None:
        self.assertEqual(GrokCliAdapter().default_command_name(), "grok")

    def test_prompt_transport_is_prompt_file(self) -> None:
        self.assertEqual(GrokCliAdapter().prompt_transport, "prompt_file")

    def test_build_argv_requires_prompt_path(self) -> None:
        with self.assertRaises(ValueError):
            GrokCliAdapter().build_argv(["grok"], Path("C:/proj"), None)

    def test_build_argv_uses_prompt_file_flag_not_stdin(self) -> None:
        prompt_path = Path("C:/temp/orchestrator-prompt-JOB-1-grok_reviewer-a1-xyz.txt")
        argv = GrokCliAdapter().build_argv(["grok"], Path("C:/proj"), prompt_path)
        self.assertIn("--prompt-file", argv)
        self.assertIn(str(prompt_path), argv)
        self.assertIn("--permission-mode", argv)
        self.assertIn("bypassPermissions", argv)
        self.assertIn(str(Path("C:/proj")), argv)
        # The prompt body itself must never appear in argv -- only its path.
        for arg in argv:
            self.assertNotIn("あなたは", arg)

    def test_usage_limit_like_message_is_rate_limited(self) -> None:
        # UNCONFIRMED pattern (see adapters/grok.py docstring) -- this only
        # asserts the classifier's own guessed wording is detected, not
        # that this is Grok's real message.
        artifact = OutputArtifact(None, None, 0, 0, False, "Error: rate limit exceeded, try again later")
        evidence = GrokCliAdapter().classify_output(
            1, False, OutputArtifact(None, None, 0, 0, False, ""), artifact
        )
        self.assertTrue(evidence.rate_limited)
        self.assertEqual(evidence.rule_id, "grok.rate_limit.usage_limit")

    def test_unrelated_failure_is_not_rate_limited(self) -> None:
        artifact = OutputArtifact(None, None, 0, 0, False, "connection refused")
        evidence = GrokCliAdapter().classify_output(
            1, False, artifact, OutputArtifact(None, None, 0, 0, False, "")
        )
        self.assertFalse(evidence.rate_limited)


class AntigravityAdapterTests(unittest.TestCase):
    def test_build_argv_always_raises(self) -> None:
        with self.assertRaises(NotImplementedError):
            AntigravityCliAdapter().build_argv(["antigravity"], Path("C:/proj"))

    def test_classify_output_is_inert(self) -> None:
        empty = OutputArtifact(None, None, 0, 0, False, "")
        evidence = AntigravityCliAdapter().classify_output(1, False, empty, empty)
        self.assertFalse(evidence.rate_limited)


class RegistryTests(unittest.TestCase):
    def test_build_adapters_has_all_known_types(self) -> None:
        adapters = build_adapters()
        self.assertEqual(
            set(adapters), {"codex", "claude_code", "director", "grok", "antigravity"}
        )

    def test_director_adapter_only_runs_one_once_pass(self) -> None:
        self.assertEqual(DirectorCliAdapter().build_argv(["py", "-3", "director/director.py"], Path("C:/proj"))[-1], "--once")


if __name__ == "__main__":
    unittest.main()
