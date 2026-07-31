"""T-079b (TESTCASE.md 7.5節): orchestrator never touches SQLite directly."""

import re
import unittest
from pathlib import Path

_ORCHESTRATOR_DIR = Path(__file__).resolve().parent.parent
_THIS_FILE = Path(__file__).resolve()

_FORBIDDEN_PATTERNS = [
    re.compile(r"^\s*import sqlite3", re.MULTILINE),
    re.compile(r"^\s*from sqlite3", re.MULTILINE),
    re.compile(r"sqlite3\.connect"),
    re.compile(r"sqlite3\.Connection"),
    re.compile(r"FROM\s+mails\b", re.IGNORECASE),
    re.compile(r"FROM\s+users\b", re.IGNORECASE),
    re.compile(r"agent_mail\.db"),
]


class NoDirectSqlTests(unittest.TestCase):
    def test_no_sqlite_usage_anywhere_under_orchestrator(self) -> None:
        offenders: list[str] = []
        for path in _ORCHESTRATOR_DIR.rglob("*.py"):
            if path.resolve() == _THIS_FILE:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in _FORBIDDEN_PATTERNS:
                if pattern.search(text):
                    offenders.append(f"{path.relative_to(_ORCHESTRATOR_DIR)}: {pattern.pattern}")
        self.assertEqual(offenders, [], f"found forbidden direct-SQL usage: {offenders}")

    def test_mail_module_adapter_is_the_only_mail_import_boundary(self) -> None:
        """Only mail_adapter.py may import the sibling mail package by path."""
        offenders: list[str] = []
        for path in _ORCHESTRATOR_DIR.rglob("*.py"):
            if path.name in ("mail_adapter.py",) or "tests" in path.relative_to(_ORCHESTRATOR_DIR).parts:
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(r"^\s*(import mail\b|from mail\b)", text, re.MULTILINE):
                offenders.append(str(path.relative_to(_ORCHESTRATOR_DIR)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
