"""Tests for route_check.py budgets and fast path. Run: python3 -m unittest test_route_check"""
import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

import route_check as rc

HERE = Path(__file__).resolve().parent
TEAM = json.loads((HERE.parent / "team.json").read_text())


def fast_route(**budgets):
    route = copy.deepcopy(TEAM["routing"]["fast_path"])
    route["budgets"] = {**route.get("budgets", {}), **budgets}
    return route


class RouteCheckTest(unittest.TestCase):
    def test_fast_path_template_is_valid(self):
        errors, route = rc.check(fast_route(), TEAM, False, [])
        self.assertEqual(errors, [])
        self.assertEqual(route["routed_by"], "fast_path")
        self.assertEqual(route["budgets"]["max_new_tokens"], TEAM["budgets"]["max_new_tokens"]["fast"])

    def test_token_budget_is_capped_by_lane(self):
        _, route = rc.check(fast_route(max_new_tokens=10 ** 9), TEAM, True, [])
        self.assertEqual(route["budgets"]["max_new_tokens"], TEAM["budgets"]["max_new_tokens"]["fast"])
        _, route = rc.check(fast_route(max_new_tokens=5000), TEAM, True, [])
        self.assertEqual(route["budgets"]["max_new_tokens"], 5000)

    def test_bad_token_budget_rejected(self):
        for bad in (0, -1, "lots", True):
            errors, _ = rc.check(fast_route(max_new_tokens=bad), TEAM, True, [])
            self.assertIn("budgets.max_new_tokens must be a positive integer", errors)

    def test_cli_fast_path_and_exclusive_inputs(self):
        script = str(HERE / "route_check.py")
        ok = subprocess.run([sys.executable, script, "--fast-path", "--dirty"], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stdout)
        self.assertEqual(json.loads(ok.stdout)["lane"], "fast")
        both = subprocess.run([sys.executable, script, "x.json", "--fast-path", "--dirty"],
                              capture_output=True, text=True)
        self.assertEqual(both.returncode, 1)
        self.assertIn("exactly one", both.stdout)


if __name__ == "__main__":
    unittest.main()
