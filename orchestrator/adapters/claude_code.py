"""Claude Code CLI adapter (SPEC.md 9.4, 11章).

The exact non-interactive/permission flags a given environment needs are
not hardcoded here (see README.md「既知の制限」): config.json's
``agents[].command`` can fully override the resolved command prefix, so
operators can add whatever flags their Claude Code installation requires
for unattended runs.
"""

from __future__ import annotations

from pathlib import Path

import error_taxonomy as et
from adapters.base import CliEvidence
from output_capture import OutputArtifact


class ClaudeCodeCliAdapter:
    cli_type = "claude_code"
    prompt_transport = "stdin"

    def default_command_name(self) -> str:
        return "claude"

    def build_argv(
        self, command: list[str], project_path: Path, prompt_path: Path | None = None
    ) -> list[str]:
        # "-p" (print mode) runs one non-interactive turn and exits; the
        # fixed instruction is delivered via stdin by the launcher.
        return [*command, "-p"]

    def classify_output(
        self,
        exit_code: int | None,
        timed_out: bool,
        stdout: OutputArtifact,
        stderr: OutputArtifact,
    ) -> CliEvidence:
        # Claude Code 2.1.220 observed message. The reset time and timezone
        # are intentionally not part of the rule.
        needle = "you've hit your session limit"
        for stream_name, artifact in (("stdout", stdout), ("stderr", stderr)):
            if needle in artifact.tail.lower():
                return CliEvidence(
                    rate_limited=True,
                    rule_id=et.RATE_LIMIT_CLAUDE,
                    stream=stream_name,
                    evidence="You've hit your session limit",
                    category=et.CATEGORY_RATE_LIMIT,
                    provider="claude_code",
                )
        return CliEvidence()
