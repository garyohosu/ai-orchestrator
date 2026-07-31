import unittest
from pathlib import Path

from adapters import build_adapters
from adapters.claude_code import ClaudeCodeCliAdapter
from adapters.codex import CodexCliAdapter


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


class RegistryTests(unittest.TestCase):
    def test_build_adapters_has_both_known_types(self) -> None:
        adapters = build_adapters()
        self.assertEqual(set(adapters), {"codex", "claude_code"})


if __name__ == "__main__":
    unittest.main()
