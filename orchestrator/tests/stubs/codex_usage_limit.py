import sys

sys.stdin.buffer.read()
sys.stderr.write(
    "ERROR: You've hit your usage limit. Upgrade to Pro "
    "(https://chatgpt.com/explore/pro), visit "
    "https://chatgpt.com/codex/settings/usage to purchase more credits "
    "or try again at Aug 8th, 2026 5:17 PM.\n"
)
sys.exit(1)
