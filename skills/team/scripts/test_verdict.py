"""Tests for verdict.py. Run: python3 -m unittest test_verdict"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import verdict


class VerdictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def result(self, name, started, **rec):
        art = self.run / "artifacts" / f"claudex-{name}"
        art.mkdir(parents=True)
        (art / "result.json").write_text(json.dumps({"started_at": started, "status": "completed", **rec}))
        return art / "result.json"

    def call(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = verdict.main(list(argv))
        return code, out.getvalue()

    def test_newest_review_with_findings(self):
        self.result("old", 1, mode="review", response={"verdict": "APPROVED", "findings": []})
        self.result("new", 2, mode="review", requested_model="gpt-x",
                    usage={"input_tokens": 3000, "cached_input_tokens": 1000},
                    response={"verdict": "REVISE", "findings": [
                        {"id": "R1", "severity": "high", "path": "a.py", "evidence": "boom", "fix": "guard"}]})
        code, out = self.call(str(self.run))
        self.assertEqual(code, 2)
        self.assertIn("claudex-new", out)
        self.assertIn("new=2.0K", out)
        self.assertIn("R1 [high] a.py: boom | FIX: guard", out)
        self.assertNotIn("claudex-old", out)

    def test_exit_codes(self):
        path = self.result("a", 1, mode="review", response={"verdict": "BLOCKED"})
        self.assertEqual(self.call(str(path))[0], 3)
        path = self.result("b", 2, mode="build", response="x" * 3000 + "END")
        code, out = self.call(str(path))
        self.assertEqual(code, 0)
        self.assertIn("END", out)
        self.assertLess(len(out), 2000)
        path = self.result("c", 3, mode="build", response=None)
        json.dump({"status": "failed", "error": "timeout"}, path.open("w"))
        code, out = self.call(str(path))
        self.assertEqual(code, 1)
        self.assertIn("timeout", out)

    def test_all_lists_every_result(self):
        self.result("one", 1, mode="review", response={"verdict": "REVISE", "findings": []})
        self.result("two", 2, mode="review", response={"verdict": "APPROVED", "findings": []})
        code, out = self.call(str(self.run), "--all")
        self.assertEqual((code, out.count("result:")), (0, 2))

    def test_empty_run(self):
        self.assertEqual(self.call(str(self.run))[0], 1)


if __name__ == "__main__":
    unittest.main()
