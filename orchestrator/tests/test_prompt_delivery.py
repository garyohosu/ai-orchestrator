import os
import stat
import sys
import unittest
from pathlib import Path

from prompt_delivery import delete_prompt_file, write_prompt_file


class WritePromptFileTests(unittest.TestCase):
    def test_writes_utf8_content_exactly(self) -> None:
        instruction = "あなたはgrok_reviewerです。日本語の指示文。"
        path = write_prompt_file(instruction, job_id="JOB-1", agent_name="grok_reviewer", attempt=1)
        try:
            self.assertEqual(path.read_text(encoding="utf-8"), instruction)
        finally:
            delete_prompt_file(path)

    def test_two_calls_never_collide(self) -> None:
        paths = [
            write_prompt_file("x", job_id="JOB-1", agent_name="grok_reviewer", attempt=1)
            for _ in range(20)
        ]
        try:
            self.assertEqual(len(set(paths)), len(paths))
        finally:
            for path in paths:
                delete_prompt_file(path)

    def test_job_id_and_agent_name_are_sanitized_in_filename(self) -> None:
        path = write_prompt_file(
            "x", job_id="../../evil", agent_name="a/b\\c", attempt=1
        )
        try:
            self.assertNotIn("..", path.name)
            self.assertNotIn("/", path.name)
            self.assertNotIn("\\", path.name.replace(str(path.parent), ""))
        finally:
            delete_prompt_file(path)


class DeletePromptFileTests(unittest.TestCase):
    def test_none_is_a_noop(self) -> None:
        delete_prompt_file(None)  # must not raise

    def test_already_missing_file_is_a_noop(self) -> None:
        path = write_prompt_file("x", job_id="JOB-1", agent_name="a", attempt=1)
        delete_prompt_file(path)
        self.assertFalse(path.exists())
        delete_prompt_file(path)  # second delete: still must not raise

    def test_deletion_failure_warns_without_leaking_content(self) -> None:
        if sys.platform != "win32":
            self.skipTest("permission-based deletion failure is Windows-specific here")
        path = write_prompt_file("SECRET_BODY_TEXT", job_id="JOB-1", agent_name="a", attempt=1)
        os.chmod(path, stat.S_IREAD)
        captured = []
        import prompt_delivery as pd

        def fake_print(*args, **kwargs):
            captured.append(" ".join(str(a) for a in args))

        pd.print = fake_print
        try:
            delete_prompt_file(path)
        finally:
            del pd.print
            os.chmod(path, stat.S_IWRITE)
            delete_prompt_file(path)
        if captured:
            self.assertNotIn("SECRET_BODY_TEXT", captured[0])


if __name__ == "__main__":
    unittest.main()
