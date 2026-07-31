import json
import tempfile
import unittest
from pathlib import Path

import config


class ConfigDefaultsTests(unittest.TestCase):
    def test_output_and_handoff_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"agents": []}', encoding="utf-8")
            cfg = config.load(path)
        self.assertEqual(cfg.cli_output_max_bytes, 1024 * 1024)
        self.assertEqual(cfg.cli_output_ring_bytes, 64 * 1024)
        self.assertEqual(cfg.notification_tail_bytes, 8 * 1024)
        self.assertEqual(cfg.max_handoffs, 3)

    def test_fallback_agents_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"agents": [
                {"name": "a", "uid": "UID000001", "cli_type": "claude_code",
                 "fallback_agents": ["b"]},
                {"name": "b", "uid": "UID000002", "cli_type": "codex"},
            ]}), encoding="utf-8")
            cfg = config.load(path)
        self.assertEqual(cfg.agents[0].fallback_agents, ["b"])
    def _write(self, data: dict) -> Path:
        tmpdir = Path(tempfile.mkdtemp())
        path = tmpdir / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_defaults_applied_when_omitted(self) -> None:
        path = self._write({})
        cfg = config.load(path)
        self.assertEqual(cfg.mail_check_interval_sec, config.DEFAULT_MAIL_CHECK_INTERVAL_SEC)
        self.assertEqual(cfg.cli_timeout_sec, config.DEFAULT_CLI_TIMEOUT_SEC)
        self.assertEqual(cfg.reply_check_timeout_sec, config.DEFAULT_REPLY_CHECK_TIMEOUT_SEC)
        self.assertEqual(cfg.max_round_trips, config.DEFAULT_MAX_ROUND_TRIPS)
        self.assertEqual(cfg.max_retries, config.DEFAULT_MAX_RETRIES)
        self.assertEqual(cfg.max_run_duration_sec, config.DEFAULT_MAX_RUN_DURATION_SEC)
        self.assertEqual(cfg.agents, [])

    def test_explicit_values_override_defaults(self) -> None:
        path = self._write({"mail_check_interval_sec": 9, "max_retries": 0})
        cfg = config.load(path)
        self.assertEqual(cfg.mail_check_interval_sec, 9)
        self.assertEqual(cfg.max_retries, 0)

    def test_max_run_duration_sec_zero_is_allowed(self) -> None:
        path = self._write({"max_run_duration_sec": 0})
        cfg = config.load(path)
        self.assertEqual(cfg.max_run_duration_sec, 0)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(config.ConfigValidationError):
            config.load(Path(tempfile.mkdtemp()) / "missing.json")

    def test_invalid_json_raises(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        path = tmpdir / "config.json"
        path.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(config.ConfigValidationError):
            config.load(path)

    def test_non_positive_interval_raises(self) -> None:
        path = self._write({"mail_check_interval_sec": 0})
        with self.assertRaises(config.ConfigValidationError):
            config.load(path)

    def test_negative_max_run_duration_raises(self) -> None:
        path = self._write({"max_run_duration_sec": -1})
        with self.assertRaises(config.ConfigValidationError):
            config.load(path)

    def test_invalid_agent_uid_raises(self) -> None:
        path = self._write(
            {"agents": [{"name": "a", "uid": "not-a-uid", "cli_type": "codex"}]}
        )
        with self.assertRaises(config.ConfigValidationError):
            config.load(path)

    def test_director_auto_uid_is_allowed_only_for_director(self) -> None:
        path = self._write({"agents": [{"name": "director", "uid": "AUTO", "cli_type": "director"}]})
        self.assertEqual(config.load(path).agents[0].uid, "AUTO")
        bad = self._write({"agents": [{"name": "worker", "uid": "AUTO", "cli_type": "claude_code"}]})
        with self.assertRaises(config.ConfigValidationError):
            config.load(bad)

    def test_unknown_cli_type_raises(self) -> None:
        path = self._write(
            {"agents": [{"name": "a", "uid": "UID000001", "cli_type": "bogus"}]}
        )
        with self.assertRaises(config.ConfigValidationError):
            config.load(path)

    def test_duplicate_agent_name_raises(self) -> None:
        path = self._write(
            {
                "agents": [
                    {"name": "a", "uid": "UID000001", "cli_type": "codex"},
                    {"name": "a", "uid": "UID000002", "cli_type": "codex"},
                ]
            }
        )
        with self.assertRaises(config.ConfigValidationError):
            config.load(path)

    def test_duplicate_agent_uid_raises(self) -> None:
        path = self._write(
            {
                "agents": [
                    {"name": "a", "uid": "UID000001", "cli_type": "codex"},
                    {"name": "b", "uid": "UID000001", "cli_type": "codex"},
                ]
            }
        )
        with self.assertRaises(config.ConfigValidationError):
            config.load(path)

    def test_agent_order_index_matches_list_order(self) -> None:
        path = self._write(
            {
                "agents": [
                    {"name": "second", "uid": "UID000002", "cli_type": "codex"},
                    {"name": "first", "uid": "UID000001", "cli_type": "claude_code"},
                ]
            }
        )
        cfg = config.load(path)
        self.assertEqual(cfg.agents[0].name, "second")
        self.assertEqual(cfg.agents[0].order_index, 0)
        self.assertEqual(cfg.agents[1].name, "first")
        self.assertEqual(cfg.agents[1].order_index, 1)


if __name__ == "__main__":
    unittest.main()
