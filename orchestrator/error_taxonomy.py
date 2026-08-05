"""Shared vocabulary for CliEvidence.rule_id / .category across adapters.

Adapters populate CliEvidence with these constants so dispatch.py can make
failover decisions (and HUMAN_REQUIRED notifications can explain themselves)
from a stable vocabulary instead of ad hoc strings scattered per adapter.
"""

from __future__ import annotations

import re
from datetime import datetime

# --- categories (CliEvidence.category) ---------------------------------
CATEGORY_RATE_LIMIT = "rate_limit"
CATEGORY_AUTH = "auth"
CATEGORY_CLI = "cli"
CATEGORY_EXECUTION = "execution"
CATEGORY_MIGRATION = "migration"

# Categories that make a failed attempt eligible for handoff to a fallback
# agent instead of an immediate terminal FAILED/HUMAN_REQUIRED. Plain
# execution failures (bad exit code with no other signal, timeout) are
# deliberately excluded by default: a generic nonzero exit is not evidence
# that *another* agent would fare any better, whereas rate limits, auth
# failures, missing CLIs, and deprecated/migrated clients are all reasons
# specific to the agent that was tried, not to the task.
FAILOVER_ELIGIBLE_CATEGORIES = frozenset(
    {CATEGORY_RATE_LIMIT, CATEGORY_AUTH, CATEGORY_CLI, CATEGORY_MIGRATION}
)

# --- rate limit ----------------------------------------------------------
RATE_LIMIT_CODEX = "codex.rate_limit.usage_limit"
RATE_LIMIT_CLAUDE = "claude.rate_limit.session_limit"
# Unconfirmed: no live Grok usage-limit failure has been observed in this
# project yet. The pattern in adapters/grok.py is a best-effort guess and
# must be revisited once a real failure is seen (see docs/ai-director-report.md).
RATE_LIMIT_GROK = "grok.rate_limit.usage_limit"
# Placeholder only -- Antigravity has no working adapter at all yet
# (adapters/antigravity.py always reports cli.not_installed), so this rule
# can never actually fire today.
RATE_LIMIT_ANTIGRAVITY = "antigravity.rate_limit.usage_limit"

# --- auth ------------------------------------------------------------------
AUTH_NOT_AUTHENTICATED = "auth.not_authenticated"
AUTH_EXPIRED = "auth.expired"
AUTH_PERMISSION_DENIED = "auth.permission_denied"
AUTH_CLIENT_UNSUPPORTED = "auth.client_unsupported"

# --- execution environment ---------------------------------------------
CLI_NOT_INSTALLED = "cli.not_installed"
CLI_COMMAND_NOT_FOUND = "cli.command_not_found"
CLI_UNSUPPORTED_VERSION = "cli.unsupported_version"
CLI_UNSUPPORTED_PLATFORM = "cli.unsupported_platform"
CLI_PROMPT_TRANSPORT_UNSUPPORTED = "cli.prompt_transport_unsupported"

# --- execution failure ---------------------------------------------------
EXECUTION_TIMEOUT = "execution.timeout"
EXECUTION_NONZERO_EXIT = "execution.nonzero_exit"
EXECUTION_INVALID_RESPONSE = "execution.invalid_response"
EXECUTION_PROTOCOL_VIOLATION = "execution.protocol_violation"

# --- migration -------------------------------------------------------------
CLIENT_DEPRECATED = "client.deprecated"
CLIENT_MIGRATION_REQUIRED = "client.migration_required"
# Gemini CLI's individual-tier auth was retired in favor of Antigravity
# (observed error: "This client is no longer supported for Gemini Code
# Assist for individuals ... migrate to the Antigravity suite of
# products"). Gemini is intentionally NOT a configured agent/fallback
# candidate in this project; this rule id exists so that if Gemini is ever
# probed again (e.g. by a future availability check) the result is recorded
# as a migration event, not chased as a transient auth bug.
GEMINI_CLI_MIGRATION_REQUIRED_ANTIGRAVITY = "gemini_cli.migration_required_antigravity"


_RETRY_AT_PATTERN = re.compile(
    r"try again at ([A-Za-z]+ \d{1,2}(?:st|nd|rd|th)?,? \d{4},? \d{1,2}:\d{2}\s?[AP]M)",
    re.IGNORECASE,
)
_ORDINAL_SUFFIX = re.compile(r"(\d{1,2})(st|nd|rd|th)", re.IGNORECASE)
_RETRY_AT_FORMATS = ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p")


def parse_retry_at(text: str) -> str | None:
    """Best-effort extraction of a "try again at <datetime>" timestamp.

    Returns a naive ISO-8601 string (no timezone -- the source messages
    observed so far do not state one) or None if no such phrase is found
    or it cannot be parsed. Never raises.
    """
    match = _RETRY_AT_PATTERN.search(text)
    if not match:
        return None
    cleaned = _ORDINAL_SUFFIX.sub(r"\1", match.group(1))
    cleaned = cleaned.replace(",", ",").strip()
    for fmt in _RETRY_AT_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).isoformat()
        except ValueError:
            continue
    return None
