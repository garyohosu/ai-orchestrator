from .base import CliAdapter
from .claude_code import ClaudeCodeCliAdapter
from .codex import CodexCliAdapter
from .director import DirectorCliAdapter


def build_adapters() -> dict[str, CliAdapter]:
    return {
        "codex": CodexCliAdapter(),
        "claude_code": ClaudeCodeCliAdapter(),
        "director": DirectorCliAdapter(),
    }


__all__ = ["CliAdapter", "CodexCliAdapter", "ClaudeCodeCliAdapter", "DirectorCliAdapter", "build_adapters"]
