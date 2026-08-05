from .antigravity import AntigravityCliAdapter
from .base import CliAdapter
from .claude_code import ClaudeCodeCliAdapter
from .codex import CodexCliAdapter
from .director import DirectorCliAdapter
from .grok import GrokCliAdapter


def build_adapters() -> dict[str, CliAdapter]:
    return {
        "codex": CodexCliAdapter(),
        "claude_code": ClaudeCodeCliAdapter(),
        "director": DirectorCliAdapter(),
        "grok": GrokCliAdapter(),
        "antigravity": AntigravityCliAdapter(),
    }


__all__ = [
    "CliAdapter",
    "CodexCliAdapter",
    "ClaudeCodeCliAdapter",
    "DirectorCliAdapter",
    "GrokCliAdapter",
    "AntigravityCliAdapter",
    "build_adapters",
]
