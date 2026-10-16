#!/usr/bin/env python3
"""Audit /team runs: did each stage run on the model the router chose?

Evidence per stage:
  plan_review / build / inspect (runner stages)
      artifacts/claudex-*/result.json: requested_model, effective_model (Codex
      session settings), observed_models (Claude modelUsage), plus a rescan of
      stdout.txt for Codex collab_tool_call events (delegation).
  router / plan / Claude build / Claude grunt (agent stages)
      Claude Code subagent transcripts (~/.claude/projects/*/*/subagents/*.jsonl):
      the model on every assistant message, and any Agent/Task tool use.

Usage: team_audit.py [RUN_DIR ...] [--json] [--projects DIR] [--budget]
With no RUN_DIR, audits every run under team.json's runs_dir.
Exit 0 when every stage is OK, 1 when any stage is MISMATCH/DELEGATED/MISSING.
--budget prints only each run's token budget line and exits 3 when any run has
used more new tokens across its stages (coordinator excluded) than the route's
budgets.max_new_tokens; use it mid-run, before starting another paid stage.

A route with "routed_by": "fast_path" was written by the coordinator from
team.json routing.fast_path, so it has no router transcript by design.

A runner stage that was deliberately discarded and re-run can be marked by
writing a reason to artifacts/claudex-*/superseded. It then reports SUPERSEDED
(not a failure, original status kept in notes) and no longer counts as covering
its stage, so a missing replacement still shows as MISSING.

Tokens: every row also carries tokens = {new, cached, out[, cost_usd]}; new is
uncached input (Codex input - cached; Claude input + cache writes). Agent
stages sum their transcript (deduped by message id). A "coordinator" INFO row
estimates the host session's own spend: its messages between the run's first
stage and the last file written in the run dir (+5 min for the final report).
"""
import argparse
import glob
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
BAD = {"MISMATCH", "DELEGATED", "MISSING"}
DELEGATING_TOOLS = {"Agent", "Task"}


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def task_key(prompt):
    """The verbatim task a router/planner prompt quotes, normalised for matching."""
    m = re.search(r'Task \(verbatim[^)]*\):\s*"(.{20,200}?)', prompt or "", re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def tok_codex(u):
    u = u or {}
    return {"new": u.get("input_tokens", 0) - u.get("cached_input_tokens", 0),
            "cached": u.get("cached_input_tokens", 0), "out": u.get("output_tokens", 0)}


def tok_claude(u):
    u = u or {}
    return {"new": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
            "cached": u.get("cache_read_input_tokens", 0), "out": u.get("output_tokens", 0)}


def tok_add(a, b):
    out = {k: a.get(k, 0) + b.get(k, 0) for k in ("new", "cached", "out")}
    if "cost_usd" in a or "cost_usd" in b:
        out["cost_usd"] = round(a.get("cost_usd", 0) + b.get("cost_usd", 0), 4)
    return out


def transcript_usage(path, start=None, end=None):
    """Sum assistant usage in a transcript, one entry per message id, within [start, end]."""
    seen = {}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            msg = e.get("message") or {}
            ts = e.get("timestamp") or ""
            if not msg.get("usage") or (start and ts < start) or (end and ts > end):
                continue
            seen[msg.get("id") or len(seen)] = tok_claude(msg["usage"])
    total = {"new": 0, "cached": 0, "out": 0}
    for t in seen.values():
        total = tok_add(total, t)
    return total


def load_subagents(projects_dir):
    by_id = {}
    for f in sorted(glob.glob(os.path.join(projects_dir, "*", "*", "subagents", "agent-*.jsonl")),
                    key=os.path.getmtime):
        meta = load_json(f[:-len(".jsonl")] + ".meta.json") or {}
        models, tools, prompt, final, ts = set(), set(), None, None, None
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                ts = ts or e.get("timestamp")
                msg = e.get("message") or {}
                content = msg.get("content")
                if msg.get("role") == "user" and prompt is None:
                    prompt = content if isinstance(content, str) else " ".join(
                        c.get("text", "") for c in content or [] if isinstance(c, dict))
                if msg.get("role") == "assistant":
                    if msg.get("model") and not msg["model"].startswith("<"):
                        models.add(msg["model"])
                    for c in content if isinstance(content, list) else []:
                        if c.get("type") == "tool_use":
                            tools.add(c.get("name"))
                        elif c.get("type") == "text" and c.get("text", "").strip():
                            final = c["text"]
        aid = os.path.basename(f)
        if aid in by_id:
            # A resumed host session copies subagent transcripts into its own dir; count once.
            by_id[aid]["copies"].append(f)
            continue
        by_id[aid] = {"file": f, "copies": [f], "type": meta.get("agentType"), "models": sorted(models),
                       "tokens": transcript_usage(f),
                       "tools": tools, "prompt": prompt or "", "final": final or "", "ts": ts or ""}
    return sorted(by_id.values(), key=lambda a: a["ts"])


def words(text):
    return {w.lower() for w in re.findall(r"[A-Za-z0-9_\-.]{4,}", html.unescape(text or ""))}


def overlap(needle, haystack):
    """Share of the needle's distinctive words that occur in the haystack."""
    n = words(needle)
    return len(n & words(haystack)) / len(n) if n else 0.0


def first_line(text):
    for line in html.unescape(text or "").splitlines():
        if line.strip():
            return line.strip()
    return None


def head_key(text):
    line = first_line(text)
    return " ".join(re.findall(r"[a-z0-9]+", line.lower())) if line else None


def router_task(agent):
    m = re.search(r'Task \(verbatim[^)]*\):\s*"(.*?)"\s*\n', agent["prompt"], re.S)
    return m.group(1) if m else agent["prompt"][:600]


def run_started(run):
    """When the run dir was created (UTC, transcript format), or "" if unknown.

    The router and every stage start after it, so earlier transcripts belong to other runs.
    """
    try:
        born = run.stat().st_birthtime
    except (OSError, AttributeError):
        return ""
    return datetime.fromtimestamp(born, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def agents_for_run(run, route, agents):
    """Attribute subagent transcripts to this run.

    builder / planner revisions: the run dir path is in the prompt.
    planner: same verbatim task as the run's router, or its output heads plan*.md.
    router: same verbatim task as a matched planner, else best task-word overlap with
            the run's plan and log (the coordinator may shorten the route summary).
    grunt: best overlap between its prompt and a routed grunt task.
    """
    path = str(run)
    mine = [a for a in agents if path in a["prompt"]]
    run_text = " ".join(p.read_text(encoding="utf-8", errors="replace")
                        for p in list(run.glob("plan*.md")) + [run / "log.md"] if p.exists())
    plan_heads = {head_key(p.read_text(encoding="utf-8", errors="replace"))
                  for p in run.glob("plan*.md")} - {None}
    for a in agents:
        if a not in mine and a["type"] == "team-planner" and head_key(a["final"]) in plan_heads:
            mine.append(a)
    keys = {task_key(a["prompt"]) for a in mine if a["type"] == "team-planner"} - {None}
    routers = [a for a in agents if a["type"] == "team-router" and task_key(a["prompt"]) in keys]
    if route.get("routed_by") == "fast_path":
        routers = []
    elif not routers:
        born = run_started(run)
        sig = route_signature(route.get("stages"), route.get("grunt_tasks"))
        same = [a for a in agents if a["type"] == "team-router" and a["ts"] >= born
                and route_signature(*[(router_route(a) or {}).get(k) for k in ("stages", "grunt_tasks")]) == sig]
        if same:
            routers = same[:1]  # the first one after the run dir was created
    if not routers and route.get("routed_by") != "fast_path":
        born = run_started(run)
        scored = [(overlap(router_task(a), run_text), a) for a in agents
                  if a["type"] == "team-router" and a["ts"] >= born]
        scored = [x for x in scored if x[0] >= 0.6]
        if scored:
            routers = [max(scored, key=lambda x: x[0])[1]]
    keys |= {task_key(a["prompt"]) for a in routers} - {None}
    for a in agents:
        if (a["type"] == "team-planner" and a not in mine and keys and task_key(a["prompt"]) in keys
                and a["ts"] >= run_started(run)):
            mine.append(a)
    for g in route.get("grunt_tasks", []):
        scored = [(overlap(g.get("task"), a["prompt"]), a) for a in agents
                  if a["type"] == "team-grunt" and a not in mine]
        scored = [x for x in scored if x[0] >= 0.5]
        if scored:
            mine.append(max(scored, key=lambda x: x[0])[1])
    return routers, mine


def route_signature(stages, grunts):
    # route_check fills provider "claude" for agent-planned stages; match that.
    sig = {k: (v.get("provider") or ("claude" if v.get("by") == "agent" else None), v.get("model"), v.get("effort"), v.get("by"), v.get("skip"))
           for k, v in (stages or {}).items()}
    return sig, sorted((g.get("provider"), g.get("model")) for g in grunts or [])


def router_route(agent):
    m = re.search(r"\{.*\}", agent["final"], re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


def check_models(expected, observed):
    if not observed:
        return "MISSING"
    if observed == [expected]:
        return "OK"
    return "MISMATCH"


def audit_agent(stage, expected, agent):
    status = check_models(expected, agent["models"])
    if agent["tools"] & DELEGATING_TOOLS:
        status = "DELEGATED"
    return {"stage": stage, "routed": expected, "actual": agent["models"], "status": status,
            "evidence": os.path.relpath(agent["file"], os.path.expanduser("~")),
            "tokens": agent.get("tokens")}


def audit_runner(stage, spec, rec, art):
    expected, effort = spec.get("model_id"), spec.get("effort")
    provider = rec.get("provider")
    if provider == "codex":
        actual = [rec["effective_model"]] if rec.get("effective_model") else []
    else:
        actual = rec.get("observed_models") or []
    status = check_models(expected, actual)
    notes = []
    if rec.get("requested_model") != expected:
        status, _ = "MISMATCH", notes.append(f"requested {rec.get('requested_model')}")
    if provider != spec.get("provider"):
        status, _ = "MISMATCH", notes.append(f"provider {provider}")
    if effort and rec.get("requested_effort") != effort:
        status, _ = "MISMATCH", notes.append(f"requested effort {rec.get('requested_effort')}")
    if provider == "codex" and effort and rec.get("effective_effort") not in (None, effort):
        status, _ = "MISMATCH", notes.append(f"effective effort {rec.get('effective_effort')}")
    stdout = art / "stdout.txt"
    if stdout.exists() and "collab_tool_call" in stdout.read_text(encoding="utf-8", errors="replace"):
        status, _ = "DELEGATED", notes.append("collab_tool_call in stdout")
    if rec.get("status") != "completed":
        notes.append(f"run {rec.get('status')}")
    verdict = rec.get("response", {}).get("verdict") if isinstance(rec.get("response"), dict) else None
    if verdict:
        notes.append(verdict)
    marker = art / "superseded"
    if marker.exists():
        reason = marker.read_text(encoding="utf-8", errors="replace").strip()
        if reason:
            notes.insert(0, f"was {status}: {reason}")
            status = "SUPERSEDED"
        else:
            notes.insert(0, "empty superseded marker ignored (needs a reason)")
    tokens = (tok_codex if provider == "codex" else tok_claude)(rec.get("usage"))
    if rec.get("total_cost_usd") is not None:
        tokens["cost_usd"] = round(rec["total_cost_usd"], 4)
    return {"stage": stage, "routed": expected, "actual": actual, "status": status,
            "evidence": f"artifacts/{art.name}", "notes": notes, "tokens": tokens}


def audit_run(run, agents):
    route = load_json(run / "route.ok.json")
    rows = []
    if not route:
        return {"run": run.name, "rows": rows,
                "skipped": "no route.ok.json (routing-only test or rejected route)"}
    stages = route.get("stages", {})
    routers, mine = agents_for_run(run, route, agents)
    roster = (load_json(SKILL / "team.json") or {}).get("roster", {}).get("claude", {})
    router_model = roster.get("sonnet", {}).get("id", "claude-sonnet-5-5")

    if route.get("routed_by") == "fast_path":
        rows.append({"stage": "route", "routed": "fast_path", "actual": ["coordinator"], "status": "OK",
                     "evidence": "route: team.json fast path (router skipped)"})
    elif routers:
        rows.append(audit_agent("route", router_model, routers[-1]))
        notes = rows[-1].setdefault("notes", [])
        if len(routers) > 1:
            notes.append(f"{len(routers)} router transcripts for this task; newest used")
        said = router_route(routers[-1]) or {}
        if route_signature(said.get("stages"), said.get("grunt_tasks")) != route_signature(
                stages, route.get("grunt_tasks")):
            notes.append("route differs from router output (override or retry; check log.md)")
    else:
        rows.append({"stage": "route", "routed": router_model, "actual": [], "status": "MISSING",
                     "evidence": "no team-router transcript matches this run"})

    def claude_agent_stage(stage, kind, spec, pred=lambda a: True):
        expected = spec.get("model_id") or roster.get(spec.get("agent_model") or spec.get("model"), {}).get("id")
        hits = [a for a in mine if a["type"] == kind and pred(a)]
        if not hits:
            rows.append({"stage": stage, "routed": expected, "actual": [], "status": "MISSING",
                         "evidence": f"no {kind} transcript for this run"})
        for a in hits:
            rows.append(audit_agent(stage, expected, a))

    plan = stages.get("plan", {})
    if plan.get("by") == "agent":
        claude_agent_stage("plan", "team-planner", plan)
    else:
        rows.append({"stage": "plan", "routed": plan.get("by"), "actual": [plan.get("by")],
                     "status": "OK", "evidence": "route: written by the coordinator"})

    records = []
    for res in sorted(run.glob("artifacts/claudex-*/result.json"), key=os.path.getmtime):
        rec = load_json(res)
        if rec:
            records.append((rec, res.parent))
    by_mode = {"review": "plan_review", "build": "build", "inspect": "inspect"}
    codex_grunts = [g for g in route.get("grunt_tasks", []) if g.get("provider") == "codex"]
    build = stages.get("build", {})
    for rec, art in records:
        stage = by_mode.get(rec.get("mode"), rec.get("mode"))
        spec = stages.get(stage, {})
        if stage == "build":
            is_grunt = Path(rec.get("plan") or "").name.startswith("grunt") or (
                rec.get("requested_model") != build.get("model_id")
                and any(g.get("model_id") == rec.get("requested_model") for g in codex_grunts))
            if is_grunt:
                stage, spec = "grunt", next(g for g in codex_grunts
                                            if g.get("model_id") == rec.get("requested_model"))
        rows.append(audit_runner(stage, spec, rec, art))

    for stage in ("plan_review", "build", "inspect"):
        spec = stages.get(stage, {})
        if spec.get("skip") or any(r["stage"] == stage and r["status"] != "SUPERSEDED" for r in rows):
            continue
        if stage == "build" and spec.get("provider") == "claude":
            claude_agent_stage("build", "team-builder", spec)
        else:
            rows.append({"stage": stage, "routed": spec.get("model_id"), "actual": [],
                         "status": "MISSING", "evidence": "no runner artifact for this stage"})

    claude_grunts = [g for g in route.get("grunt_tasks", []) if g.get("provider") == "claude"]
    grunt_agents = [a for a in mine if a["type"] == "team-grunt"]
    for i, g in enumerate(claude_grunts):
        if i < len(grunt_agents):
            rows.append(audit_agent("grunt", g.get("model_id"), grunt_agents[i]))
        else:
            rows.append({"stage": "grunt", "routed": g.get("model_id"), "actual": [],
                         "status": "MISSING", "evidence": "no team-grunt transcript for this run"})

    coord = coordinator_row(run, routers + mine)
    if coord:
        rows.append(coord)
    total = {"new": 0, "cached": 0, "out": 0}
    for row in rows:
        if row.get("tokens"):
            total = tok_add(total, row["tokens"])
    used = sum(r["tokens"]["new"] for r in rows if r.get("tokens") and r["stage"] != "coordinator")
    limit = (route.get("budgets") or {}).get("max_new_tokens")
    budget = ({"used": used, "limit": limit, "exceeded": used > limit}
              if isinstance(limit, int) and not isinstance(limit, bool) else None)
    return {"run": run.name, "rows": rows, "tokens": total, "budget": budget}


def coordinator_row(run, agents):
    """The host session's own spend during this run (time-window estimate)."""
    born = run_started(run)
    agents = [a for a in agents if a["ts"] and a["ts"] >= born]
    if not agents:
        return None
    sessions = {str(Path(f).parent.parent) + ".jsonl" for a in agents for f in a.get("copies", [a["file"]])}
    sessions = {s for s in sessions if os.path.exists(s)}
    if not sessions:
        return None
    start = min(a["ts"] for a in agents)
    last = max((p.stat().st_mtime for p in run.rglob("*") if p.is_file()), default=None)
    if not start or last is None:
        return None
    end = (datetime.fromtimestamp(last, timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    total = {"new": 0, "cached": 0, "out": 0}
    for s in sorted(sessions):
        t = transcript_usage(s, start, end)
        if t:
            total = tok_add(total, t)
    return {"stage": "coordinator", "routed": "-", "actual": [], "status": "INFO",
            "evidence": f"{len(sessions)} host session(s), {start[:19]}..{end}", "tokens": total}


def fmt_tok(t):
    if not t:
        return ""
    k = lambda n: f"{n / 1000:.1f}K"
    s = f"tok new={k(t['new'])} cached={k(t['cached'])} out={k(t['out'])}"
    return s + (f" ${t['cost_usd']:.2f}" if "cost_usd" in t else "")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="*")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--budget", action="store_true", help="only check token budgets; exit 3 if exceeded")
    args = ap.parse_args()
    if args.runs:
        runs = [Path(r).expanduser().resolve() for r in args.runs]
    else:
        runs_dir = (load_json(SKILL / "team.json") or {}).get("runs_dir", "~/.claude/team-runs")
        # Dot-dirs hold shared state (e.g. the runner's lean .codex-home), not runs.
        runs = sorted(p for p in Path(os.path.expanduser(runs_dir)).iterdir()
                      if p.is_dir() and not p.name.startswith("."))
    agents = load_subagents(args.projects)
    report = [audit_run(r, agents) for r in runs]
    bad = sum(row["status"] in BAD for r in report for row in r["rows"])
    if args.budget:
        over = 0
        for r in report:
            b = r.get("budget")
            if not b:
                print(f"{r['run']}: BUDGET none (no route.ok.json or no max_new_tokens)")
                continue
            over += b["exceeded"]
            print(f"{r['run']}: BUDGET {'EXCEEDED' if b['exceeded'] else 'OK'} used={b['used'] / 1000:.1f}K "
                  f"limit={b['limit'] / 1000:.1f}K left={(b['limit'] - b['used']) / 1000:.1f}K")
        return 3 if over else 0
    if args.json:
        print(json.dumps({"runs": report, "problems": bad}, indent=2))
    else:
        for r in report:
            print(f"== {r['run']}")
            if r.get("skipped"):
                print(f"   skipped: {r['skipped']}")
            for row in r["rows"]:
                actual = ",".join(row["actual"]) or "-"
                notes = "; ".join(row.get("notes", []))
                tok = fmt_tok(row.get("tokens"))
                print(f"   {row['status']:<9} {row['stage']:<11} routed={row['routed']} actual={actual}"
                      f"  [{row['evidence']}]{'  ' + tok if tok else ''}{'  ' + notes if notes else ''}")
            if r.get("tokens"):
                print(f"   TOTAL     {fmt_tok(r['tokens'])}")
            b = r.get("budget")
            if b:
                print(f"   BUDGET    {'EXCEEDED' if b['exceeded'] else 'OK'} stages new={b['used'] / 1000:.1f}K"
                      f" of {b['limit'] / 1000:.1f}K")
        print(f"\n{bad} problem stage(s) across {len(report)} run(s).")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
