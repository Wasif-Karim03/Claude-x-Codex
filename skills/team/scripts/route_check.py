#!/usr/bin/env python3
"""Validate a team-router decision against team.json and the /team hard rules.

Usage:
  route_check.py ROUTE_JSON [--team TEAM_JSON] [--clean|--dirty] [--require stage.field=value ...]
  route_check.py --fast-path [--team TEAM_JSON] [--clean|--dirty] [--require ...]
      (validates team.json routing.fast_path instead of a router decision)

Prints the normalized route (with resolved model ids) on success, exit 0.
Prints a JSON list of errors on failure, exit 1.
"""
import argparse
import json
import os
import sys

STAGES = ("plan", "plan_review", "build", "inspect")
LANES = ("fast", "standard", "heavy")


def load_route(path):
    text = open(path, encoding="utf-8").read().strip()
    # Tolerate a fenced block or prose around the object.
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object found")
    return json.loads(text[start:end + 1])


def resolve(roster, provider, model, errors, where):
    models = roster.get(provider)
    if models is None:
        errors.append(f"{where}: unknown provider {provider!r}")
        return None
    if model in models:
        return models[model]
    for entry in models.values():
        if model == entry.get("id"):
            return entry
    errors.append(f"{where}: model {model!r} is not in the {provider} roster ({', '.join(models)})")
    return None


def check(route, team, clean, requires):
    errors = []
    roster = team["roster"]
    efforts = roster.get("efforts", [])
    budgets_cap = team.get("budgets", {})

    lane = route.get("lane")
    if lane not in LANES:
        errors.append(f"lane must be one of {LANES}, got {lane!r}")
    stages = route.get("stages") or {}
    for name in STAGES:
        if not isinstance(stages.get(name), dict):
            errors.append(f"stages.{name} is missing")
    if errors:
        return errors, None

    for key, want in requires:
        stage, _, field = key.partition(".")
        got = (stages.get(stage) or {}).get(field)
        if str(got) != want:
            errors.append(f"user override: {key} must be {want!r}, router chose {got!r}")

    plan, review, build, inspect = (stages[n] for n in STAGES)

    # Plan: written by the coordinator (fast lane only) or a Claude planner agent.
    if plan.get("by") == "coordinator":
        if lane != "fast":
            errors.append("only the fast lane may let the coordinator write the plan")
    else:
        plan["provider"] = "claude"
        entry = resolve(roster, "claude", plan.get("model"), errors, "plan")
        if entry:
            plan["agent_model"] = entry.get("agent_model")

    # Plan review: skippable only in the fast lane; must be the other company.
    if review.get("skip"):
        if lane != "fast":
            errors.append("plan_review may only be skipped in the fast lane")
    else:
        if review.get("provider") == plan.get("provider", "claude"):
            errors.append("plan_review must come from a different company than the planner")
        entry = resolve(roster, review.get("provider"), review.get("model"), errors, "plan_review")
        if entry:
            review["model_id"] = entry["id"]
        if review.get("effort") not in efforts:
            errors.append(f"plan_review.effort must be one of {efforts}")

    # Build.
    entry = resolve(roster, build.get("provider"), build.get("model"), errors, "build")
    if entry:
        build["model_id"] = entry["id"]
        build["agent_model"] = entry.get("agent_model")
    if build.get("provider") == "codex":
        if not clean:
            errors.append("a Codex builder needs a clean working tree; it is dirty")
        if build.get("effort") not in efforts:
            errors.append(f"build.effort must be one of {efforts}")

    # Inspect: never skipped, always the company that didn't build.
    if inspect.get("skip"):
        errors.append("inspection is never skipped")
    if inspect.get("provider") == build.get("provider"):
        errors.append("inspect must come from a different company than the builder")
    entry = resolve(roster, inspect.get("provider"), inspect.get("model"), errors, "inspect")
    if entry:
        inspect["model_id"] = entry["id"]
    if inspect.get("effort") not in efforts:
        errors.append(f"inspect.effort must be one of {efforts}")

    # Grunt subtasks.
    for i, task in enumerate(route.get("grunt_tasks") or []):
        where = f"grunt_tasks[{i}]"
        entry = resolve(roster, task.get("provider"), task.get("model"), errors, where)
        if entry:
            task["model_id"] = entry["id"]
            task["agent_model"] = entry.get("agent_model")
        if task.get("provider") == "codex":
            if not clean:
                errors.append(f"{where}: Codex grunts need a clean working tree")
            # Each Codex write needs a clean tree and leaves it dirty, so only one fits per run.
            if build.get("provider") == "codex":
                errors.append(f"{where}: a Codex grunt can't run alongside a Codex builder (both need a clean tree); use a Claude grunt or fold it into the build")
            elif sum(t.get("provider") == "codex" for t in route.get("grunt_tasks") or []) > 1:
                errors.append(f"{where}: at most one Codex grunt per run (each needs a clean tree)")
        if not task.get("check"):
            errors.append(f"{where}: every grunt task needs an explicit check")

    # Budgets: router may lower them, never exceed team.json.
    budgets = route.get("budgets") or {}
    for key in ("plan_review_rounds", "fix_rounds"):
        cap = budgets_cap.get(key)
        val = budgets.get(key, cap)
        if not isinstance(val, int) or val < 0:
            errors.append(f"budgets.{key} must be a non-negative integer")
        elif cap is not None and val > cap:
            budgets[key] = cap
    token_caps = budgets_cap.get("max_new_tokens") or {}
    cap = token_caps.get(lane) if isinstance(token_caps, dict) else None
    val = budgets.get("max_new_tokens", cap)
    if val is not None:
        if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
            errors.append("budgets.max_new_tokens must be a positive integer")
        else:
            budgets["max_new_tokens"] = min(val, cap) if isinstance(cap, int) else val
    budgets["timeout_seconds"] = budgets_cap.get("timeout_seconds", 900)
    route["budgets"] = budgets

    for name in STAGES:
        stage = stages[name]
        if not (stage.get("skip") or stage.get("by") == "coordinator") and not stage.get("reason"):
            errors.append(f"stages.{name}.reason is missing")

    return errors, route


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("route", nargs="?")
    ap.add_argument("--fast-path", action="store_true", help="validate team.json routing.fast_path")
    ap.add_argument("--team", default=os.path.expanduser("~/.claude/skills/team/team.json"))
    tree = ap.add_mutually_exclusive_group(required=True)
    tree.add_argument("--clean", action="store_true")
    tree.add_argument("--dirty", action="store_true")
    ap.add_argument("--require", action="append", default=[], metavar="stage.field=value")
    args = ap.parse_args()

    requires = []
    for item in args.require:
        key, sep, val = item.partition("=")
        if not sep or "." not in key:
            ap.error(f"bad --require {item!r}")
        requires.append((key, val))

    try:
        team = json.load(open(args.team, encoding="utf-8"))
        if args.fast_path == bool(args.route):
            raise ValueError("give exactly one of ROUTE_JSON or --fast-path")
        route = (json.loads(json.dumps(team["routing"]["fast_path"])) if args.fast_path
                 else load_route(args.route))
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps([f"could not read input: {exc}"]))
        return 1

    errors, normalized = check(route, team, args.clean, requires)
    if errors:
        print(json.dumps(errors, indent=2))
        return 1
    print(json.dumps(normalized, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
