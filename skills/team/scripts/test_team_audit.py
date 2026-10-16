"""Tests for team_audit.py on synthetic runs. Run: python3 -m unittest test_team_audit"""
import json
import tempfile
import unittest
from pathlib import Path

import team_audit as ta

TASK = "Add a --widget flag that prints widget counts with unittest coverage"
ROUTE = {
    "lane": "standard", "summary": "widget flag",
    "stages": {
        "plan": {"by": "agent", "model": "sonnet", "provider": "claude", "agent_model": "sonnet"},
        "plan_review": {"provider": "codex", "model": "gpt-6.1-sol", "effort": "medium", "model_id": "gpt-6.1-sol"},
        "build": {"provider": "codex", "model": "gpt-6.1-sol", "effort": "medium", "model_id": "gpt-6.1-sol"},
        "inspect": {"provider": "claude", "model": "opus", "effort": "medium", "model_id": "claude-opus-5-5"},
    },
    "grunt_tasks": [{"task": "Create fixtures/widgets/sample_01.txt through sample_09.txt",
                     "provider": "claude", "model": "haiku", "model_id": "claude-haiku-5-5"}],
}


def agent(kind, model, prompt, final="", tools=()):
    return {"file": f"/x/{kind}.jsonl", "type": kind, "models": [model] if model else [],
            "tools": set(tools), "prompt": prompt, "final": final, "ts": "t"}


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = Path(self.tmp.name) / "proj-run"
        (self.run / "artifacts").mkdir(parents=True)
        (self.run / "route.ok.json").write_text(json.dumps(ROUTE))
        (self.run / "plan.md").write_text("# Plan: widget flag\nsteps\n")
        (self.run / "log.md").write_text(f"task: {TASK}\n")
        self.result("aaa", mode="review", provider="codex", model="gpt-6.1-sol")
        self.result("bbb", mode="build", provider="codex", model="gpt-6.1-sol")
        self.result("ccc", mode="inspect", provider="claude", model="claude-opus-5-5")
        prompt = f'Route this /team task.\n\nTask (verbatim): "{TASK}"\n'
        self.agents = [
            agent("team-router", "claude-sonnet-5-5", prompt, final=json.dumps(ROUTE)),
            agent("team-planner", "claude-sonnet-5-5", prompt.replace("Route", "Plan"), final="# Plan: widget flag"),
            agent("team-grunt", "claude-haiku-5-5", "Repo: /r\nTask: Create fixtures/widgets/sample_01.txt to sample_09.txt"),
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def result(self, name, mode, provider, model, effective=None, observed=None, stdout=""):
        art = self.run / "artifacts" / f"claudex-{name}"
        art.mkdir(exist_ok=True)
        rec = {"status": "completed", "mode": mode, "provider": provider, "plan": str(self.run / "plan.md"),
               "requested_model": model, "requested_effort": "medium",
               "effective_model": (effective or model) if provider == "codex" else None,
               "effective_effort": "medium" if provider == "codex" else None,
               "observed_models": observed if observed is not None else ([model] if provider == "claude" else []),
               "response": {"verdict": "APPROVED"} if mode != "build" else "done"}
        (art / "result.json").write_text(json.dumps(rec))
        (art / "stdout.txt").write_text(stdout)

    def statuses(self):
        return {(r["stage"], r["status"]) for r in ta.audit_run(self.run, self.agents)["rows"]}

    def test_clean_run_is_all_ok(self):
        rows = ta.audit_run(self.run, self.agents)["rows"]
        self.assertEqual({r["stage"] for r in rows}, {"route", "plan", "plan_review", "build", "inspect", "grunt"})
        self.assertTrue(all(r["status"] == "OK" for r in rows), rows)

    def test_codex_effective_model_mismatch(self):
        self.result("bbb", mode="build", provider="codex", model="gpt-6.1-sol", effective="gpt-6-luna")
        self.assertIn(("build", "MISMATCH"), self.statuses())

    def test_claude_extra_observed_model(self):
        self.result("ccc", mode="inspect", provider="claude", model="claude-opus-5-5",
                    observed=["claude-opus-5-5", "claude-haiku-5-5"])
        self.assertIn(("inspect", "MISMATCH"), self.statuses())

    def test_codex_collab_event_is_delegation(self):
        self.result("bbb", mode="build", provider="codex", model="gpt-6.1-sol",
                    stdout='{"type":"item.started","item":{"type":"collab_tool_call"}}\n')
        self.assertIn(("build", "DELEGATED"), self.statuses())

    def test_agent_using_agent_tool_is_delegation(self):
        self.agents[1]["tools"] = {"Read", "Agent"}
        self.assertIn(("plan", "DELEGATED"), self.statuses())

    def test_wrong_agent_model(self):
        self.agents[2]["models"] = ["claude-sonnet-5-5"]
        self.assertIn(("grunt", "MISMATCH"), self.statuses())

    def test_missing_stage(self):
        for f in (self.run / "artifacts" / "claudex-ccc").iterdir():
            f.unlink()
        (self.run / "artifacts" / "claudex-ccc").rmdir()
        self.assertIn(("inspect", "MISSING"), self.statuses())

    def test_requested_effort_mismatch(self):
        rec_path = self.run / "artifacts" / "claudex-aaa" / "result.json"
        rec = json.loads(rec_path.read_text())
        rec["requested_effort"] = "low"
        rec_path.write_text(json.dumps(rec))
        self.assertIn(("plan_review", "MISMATCH"), self.statuses())

    def test_superseded_stage_is_not_a_problem(self):
        self.result("bbb", mode="build", provider="codex", model="gpt-6.1-sol",
                    stdout='{"type":"item.started","item":{"type":"collab_tool_call"}}\n')
        (self.run / "artifacts" / "claudex-bbb" / "superseded").write_text("delegated; re-run\n")
        self.result("bbc", mode="build", provider="codex", model="gpt-6.1-sol")
        rows = ta.audit_run(self.run, self.agents)["rows"]
        old = next(r for r in rows if r["evidence"] == "artifacts/claudex-bbb")
        self.assertEqual(old["status"], "SUPERSEDED")
        self.assertIn("was DELEGATED: delegated; re-run", old["notes"])
        self.assertFalse(any(r["status"] in ta.BAD for r in rows), rows)

    def test_superseded_without_replacement_is_missing(self):
        (self.run / "artifacts" / "claudex-bbb" / "superseded").write_text("bad build")
        self.assertIn(("build", "MISSING"), self.statuses())

    def test_empty_superseded_marker_is_ignored(self):
        self.result("bbb", mode="build", provider="codex", model="gpt-6.1-sol", effective="gpt-6-luna")
        (self.run / "artifacts" / "claudex-bbb" / "superseded").write_text("  \n")
        self.assertIn(("build", "MISMATCH"), self.statuses())

    def test_runner_tokens_and_run_total(self):
        rec_path = self.run / "artifacts" / "claudex-aaa" / "result.json"
        rec = json.loads(rec_path.read_text())
        rec["usage"] = {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 5}
        rec_path.write_text(json.dumps(rec))
        rec_path = self.run / "artifacts" / "claudex-ccc" / "result.json"
        rec = json.loads(rec_path.read_text())
        rec["usage"] = {"input_tokens": 2, "cache_creation_input_tokens": 8, "cache_read_input_tokens": 30,
                        "output_tokens": 7}
        rec["total_cost_usd"] = 0.5
        rec_path.write_text(json.dumps(rec))
        report = ta.audit_run(self.run, self.agents)
        rows = {r["stage"]: r for r in report["rows"]}
        self.assertEqual(rows["plan_review"]["tokens"], {"new": 40, "cached": 60, "out": 5})
        self.assertEqual(rows["inspect"]["tokens"], {"new": 10, "cached": 30, "out": 7, "cost_usd": 0.5})
        self.assertEqual(report["tokens"]["new"], 50)
        self.assertEqual(report["tokens"]["cost_usd"], 0.5)

    def test_transcript_usage_dedupes_and_windows(self):
        f = Path(self.tmp.name) / "s.jsonl"
        u = {"input_tokens": 1, "cache_creation_input_tokens": 9, "cache_read_input_tokens": 100, "output_tokens": 3}
        lines = [{"timestamp": "2026-01-01T00:00:01", "message": {"id": "m1", "usage": u}},
                 {"timestamp": "2026-01-01T00:00:02", "message": {"id": "m1", "usage": u}},
                 {"timestamp": "2026-01-01T00:00:03", "message": {"id": "m2", "usage": u}},
                 {"timestamp": "2026-01-01T09:00:00", "message": {"id": "m3", "usage": u}}]
        f.write_text("\n".join(json.dumps(x) for x in lines))
        self.assertEqual(ta.transcript_usage(f, "2026-01-01T00:00:00", "2026-01-01T01:00:00"),
                         {"new": 20, "cached": 200, "out": 6})

    def test_coordinator_row_from_host_session(self):
        proj = Path(self.tmp.name) / "proj"
        (proj / "sess" / "subagents").mkdir(parents=True)
        (proj / "sess.jsonl").write_text(json.dumps(
            {"timestamp": "2026-01-01T00:00:05", "message": {"id": "h1", "usage": {"input_tokens": 4,
             "cache_read_input_tokens": 1000, "output_tokens": 2}}}))
        for a in self.agents:
            a["file"], a["ts"] = str(proj / "sess" / "subagents" / "agent-x.jsonl"), "2026-01-01T00:00:00"
        from unittest.mock import patch
        with patch.object(ta, "run_started", return_value="2025-12-31T00:00:00"):
            rows = {r["stage"]: r for r in ta.audit_run(self.run, self.agents)["rows"]}
        self.assertEqual(rows["coordinator"]["status"], "INFO")
        self.assertEqual(rows["coordinator"]["tokens"], {"new": 4, "cached": 1000, "out": 2})

    def set_usage(self, name, new):
        path = self.run / "artifacts" / f"claudex-{name}" / "result.json"
        rec = json.loads(path.read_text())
        rec["usage"] = {"input_tokens": new + 10, "cached_input_tokens": 10, "output_tokens": 1}
        path.write_text(json.dumps(rec))

    def set_route(self, **extra):
        (self.run / "route.ok.json").write_text(json.dumps({**ROUTE, **extra}))

    def test_budget_counts_stage_tokens(self):
        self.set_route(budgets={"max_new_tokens": 100})
        self.set_usage("aaa", 60)
        self.assertEqual(ta.audit_run(self.run, self.agents)["budget"],
                         {"used": 60, "limit": 100, "exceeded": False})
        self.set_usage("bbb", 50)
        self.assertTrue(ta.audit_run(self.run, self.agents)["budget"]["exceeded"])

    def test_budget_absent_without_limit(self):
        self.assertIsNone(ta.audit_run(self.run, self.agents)["budget"])

    def test_budget_mode_exit_code(self):
        import contextlib, io, sys
        self.set_route(budgets={"max_new_tokens": 100})
        self.set_usage("aaa", 150)
        argv, out = sys.argv, io.StringIO()
        try:
            sys.argv = ["team_audit.py", str(self.run), "--budget", "--projects", self.tmp.name]
            with contextlib.redirect_stdout(out):
                code = ta.main()
        finally:
            sys.argv = argv
        self.assertEqual(code, 3)
        self.assertIn("EXCEEDED used=150.0K".replace("150.0K", "0.1K"), out.getvalue())

    def test_fast_path_route_needs_no_router(self):
        self.set_route(routed_by="fast_path")
        agents = [a for a in self.agents if a["type"] != "team-router"]
        rows = {r["stage"]: r for r in ta.audit_run(self.run, agents)["rows"]}
        self.assertEqual(rows["route"]["status"], "OK")
        self.assertIn("fast path", rows["route"]["evidence"])

    def test_fast_path_ignores_matching_old_router(self):
        self.set_route(routed_by="fast_path")
        routers, _ = ta.agents_for_run(self.run, json.loads((self.run / "route.ok.json").read_text()), self.agents)
        self.assertEqual(routers, [])

    def test_agents_older_than_the_run_are_ignored(self):
        old = agent("team-router", "claude-sonnet-5-5", f'Task (verbatim): "{TASK}"', final=json.dumps(ROUTE))
        old["ts"] = "2000-01-01T00:00:00"
        planner = [a for a in self.agents if a["type"] == "team-planner"][0]
        planner["prompt"] = "Plan something unrelated"
        routers, _ = ta.agents_for_run(self.run, ROUTE, [old, planner])
        self.assertEqual(routers, [])
        self.assertIsNone(ta.coordinator_row(self.run, [old]))

    def test_routing_only_run_is_skipped(self):
        (self.run / "route.ok.json").unlink()
        self.assertIn("skipped", ta.audit_run(self.run, self.agents))


if __name__ == "__main__":
    unittest.main()
