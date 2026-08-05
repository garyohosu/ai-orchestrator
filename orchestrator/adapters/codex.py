"""Codex CLI adapter (SPEC.md 9.3, 11章)."""

from __future__ import annotations

from pathlib import Path

import error_taxonomy as et
from adapters.base import CliEvidence
from output_capture import OutputArtifact


class CodexCliAdapter:
    cli_type = "codex"
    prompt_transport = "stdin"

    def default_command_name(self) -> str:
        return "codex"

    def build_argv(
        self, command: list[str], project_path: Path, prompt_path: Path | None = None
    ) -> list[str]:
        # "-" tells Codex to read the prompt from stdin, which avoids
        # putting the instruction in argv/process listings and sidesteps
        # Windows command-line code-page issues (SPEC.md 21章).
        return [
            *command,
            "exec",
            "--full-auto",
            "--sandbox",
            "workspace-write",
            "--cd",
            str(project_path),
            "-",
        ]

    def classify_output(
        self,
        exit_code: int | None,
        timed_out: bool,
        stdout: OutputArtifact,
        stderr: OutputArtifact,
    ) -> CliEvidence:
        # Observed live 2026-08-05 (JOB-CSV-004 review, mail_id=41): Codex
        # CLI v0.146.0 printed this to stderr and exited 1 within seconds.
        # Confirmed real message, unlike the Grok/Antigravity rules.
        needle = "you've hit your usage limit"
        for stream_name, artifact in (("stdout", stdout), ("stderr", stderr)):
            tail = artifact.tail
            if needle in tail.lower():
                return CliEvidence(
                    rate_limited=True,
                    rule_id=et.RATE_LIMIT_CODEX,
                    stream=stream_name,
                    evidence="You've hit your usage limit",
                    category=et.CATEGORY_RATE_LIMIT,
                    provider="codex",
                    retry_at=et.parse_retry_at(tail),
                )
        return CliEvidence()
