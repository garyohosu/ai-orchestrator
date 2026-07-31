from .base import CliAdapter
from .claude_code import ClaudeCodeCliAdapter
from .codex import CodexCliAdapter


def build_adapters() -> dict[str, CliAdapter]:
    return {
        "codex": CodexCliAdapter(),
        "claude_code": ClaudeCodeCliAdapter(),
    }


__all__ = ["CliAdapter", "CodexCliAdapter", "ClaudeCodeCliAdapter", "build_adapters"]
