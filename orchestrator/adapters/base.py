"""Common interface for per-product CLI adapters (SPEC.md 11章, 12章)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from output_capture import OutputArtifact


@dataclass(frozen=True)
class CliEvidence:
    rate_limited: bool = False
    rule_id: str | None = None
    stream: str | None = None
    evidence: str | None = None
    # Broader failure taxonomy (error_taxonomy.py CATEGORY_*), so dispatch
    # can decide "is this failover-eligible?" for reasons beyond rate
    # limiting (auth failure, CLI not installed, client deprecated/migrated,
    # ...) without every caller special-casing `rate_limited` forever.
    # `rate_limited=True` always implies `category == CATEGORY_RATE_LIMIT`;
    # kept as a separate bool for backward compatibility with existing
    # call sites and tests that only check `.rate_limited`.
    category: str | None = None
    provider: str | None = None
    # Best-effort ISO-8601 timestamp parsed from a rate-limit message's
    # "try again at ..." text, when the message contains one. Never
    # guaranteed: absent when unparseable or when the CLI's message omits
    # a concrete time.
    retry_at: str | None = None


class CliAdapter(Protocol):
    cli_type: str

    #: How the launcher delivers the fixed instruction to this CLI.
    #: "stdin" (default): piped to the child process's stdin, never in argv
    #: (SPEC.md 12章/21章; the original two adapters, Claude Code and Codex,
    #: both read the prompt this way).
    #: "prompt_file": some CLIs (e.g. Grok) require the prompt as a CLI
    #: argument rather than reading stdin, and Windows argv has known
    #: code-page problems with non-ASCII text (see mail/README.md「Windowsで
    #: 日本語を含むPython処理を行う場合」). For these, the launcher writes
    #: the instruction to a private, uniquely-named UTF-8 temp file and
    #: passes only its *path* to build_argv() -- the prompt body itself
    #: still never appears in argv, logs, or process listings.
    prompt_transport: str = "stdin"

    def default_command_name(self) -> str:
        """The PATH-resolvable executable name to fall back to (SPEC.md 11章)."""
        ...

    def build_argv(
        self, command: list[str], project_path: Path, prompt_path: Path | None = None
    ) -> list[str]:
        """Build the full argv from the resolved command prefix.

        For "stdin" adapters, ``prompt_path`` is always None and the fixed
        instruction is delivered separately, over stdin, by the launcher.
        For "prompt_file" adapters, ``prompt_path`` is the temp file the
        launcher already wrote the instruction to; the adapter must include
        its path (not the instruction text) in the returned argv.
        """
        ...

    def classify_output(
        self,
        exit_code: int | None,
        timed_out: bool,
        stdout: OutputArtifact,
        stderr: OutputArtifact,
    ) -> CliEvidence:
        ...
