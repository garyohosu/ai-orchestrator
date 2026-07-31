"""Claude Code CLI adapter (SPEC.md 9.4, 11章).

The exact non-interactive/permission flags a given environment needs are
not hardcoded here (see README.md「既知の制限」): config.json's
``agents[].command`` can fully override the resolved command prefix, so
operators can add whatever flags their Claude Code installation requires
for unattended runs.
"""

from __future__ import annotations

from pathlib import Path


class ClaudeCodeCliAdapter:
    cli_type = "claude_code"

    def default_command_name(self) -> str:
        return "claude"

    def build_argv(self, command: list[str], project_path: Path) -> list[str]:
        # "-p" (print mode) runs one non-interactive turn and exits; the
        # fixed instruction is delivered via stdin by the launcher.
        return [*command, "-p"]
