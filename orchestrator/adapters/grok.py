"""Grok CLI adapter.

Unlike Codex and Claude Code, Grok's non-interactive single-turn flag
(``-p``/``--single``) takes the prompt as a CLI argument, not stdin (verified
2026-08-05: piping text to ``grok -p "..."`` is ignored / triggers unrelated
agentic exploration instead of being read as the prompt). Putting the
Japanese instruction text directly in argv would both violate this project's
"instruction never appears in argv" policy (adapters/base.py) and risk the
Windows argv code-page mangling already seen elsewhere in this project (see
mail/README.md). Grok's ``--prompt-file <PATH>`` reads the prompt from a
file instead, so this adapter uses ``prompt_transport = "prompt_file"``:
the launcher writes the instruction to a private UTF-8 temp file and this
adapter only ever sees that file's path.
"""

from __future__ import annotations

from pathlib import Path

import error_taxonomy as et
from adapters.base import CliEvidence
from output_capture import OutputArtifact


class GrokCliAdapter:
    cli_type = "grok"
    prompt_transport = "prompt_file"

    def default_command_name(self) -> str:
        return "grok"

    def build_argv(
        self, command: list[str], project_path: Path, prompt_path: Path | None = None
    ) -> list[str]:
        if prompt_path is None:
            # Should be unreachable: the launcher always writes a prompt
            # file before calling build_argv() for a prompt_file adapter.
            # Fail loudly rather than silently falling back to an argv
            # form that would violate the "prompt never in argv" policy.
            raise ValueError("GrokCliAdapter requires prompt_path (prompt_transport=prompt_file)")
        return [
            *command,
            "--prompt-file",
            str(prompt_path),
            "--permission-mode",
            "bypassPermissions",
            "--cwd",
            str(project_path),
        ]

    def classify_output(
        self,
        exit_code: int | None,
        timed_out: bool,
        stdout: OutputArtifact,
        stderr: OutputArtifact,
    ) -> CliEvidence:
        # UNCONFIRMED: no live Grok usage-limit failure has been observed in
        # this project. This is a best-effort guess at Grok's wording,
        # modeled on the confirmed Codex/Claude messages ("usage limit" /
        # "rate limit"). Revisit and replace with the real message the
        # first time a live Grok rate-limit failure is captured (see
        # docs/ai-director-report.md for how the Codex/Claude rules were
        # confirmed from real stderr logs).
        needles = ("usage limit", "rate limit", "rate_limit_exceeded")
        for stream_name, artifact in (("stdout", stdout), ("stderr", stderr)):
            tail_lower = artifact.tail.lower()
            for needle in needles:
                if needle in tail_lower:
                    return CliEvidence(
                        rate_limited=True,
                        rule_id=et.RATE_LIMIT_GROK,
                        stream=stream_name,
                        evidence=f"(unconfirmed pattern) matched {needle!r}",
                        category=et.CATEGORY_RATE_LIMIT,
                        provider="grok",
                        retry_at=et.parse_retry_at(artifact.tail),
                    )
        # A nonzero exit with no recognizable pattern could still be an
        # auth problem ("not logged in") -- also unconfirmed, so this is
        # deliberately not auto-classified as auth.* yet. It surfaces as a
        # plain execution failure instead of a guessed category.
        return CliEvidence()
