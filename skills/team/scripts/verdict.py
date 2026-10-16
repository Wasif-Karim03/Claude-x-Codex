#!/usr/bin/env python3
"""Print just what the /team coordinator needs from a runner result.

Usage: verdict.py RUN_DIR|RESULT_JSON [--all]
RUN_DIR picks the newest artifacts/claudex-*/result.json (by started_at).
Prints one header line (id, mode, status, verdict, models, new tokens), then each
finding on one line, or the tail of a build report. --all lists every result.

Exit: 0 APPROVED or completed build, 2 REVISE, 3 BLOCKED, 1 failed/unreadable.
Saves the coordinator from opening whole result.json/stdout files.
"""
import json
import sys
from pathlib import Path

CODES = {"APPROVED": 0, "REVISE": 2, "BLOCKED": 3}


def results(target):
    target = Path(target).expanduser()
    if target.is_file():
        return [target]
    found = []
    for path in (target / "artifacts").glob("claudex-*/result.json"):
        try:
            found.append((json.loads(path.read_text(encoding="utf-8")).get("started_at") or 0, path))
        except (OSError, ValueError):
            continue
    return [p for _, p in sorted(found)]


def new_tokens(rec):
    u = rec.get("usage") or {}
    if "cached_input_tokens" in u:
        n = u.get("input_tokens", 0) - u.get("cached_input_tokens", 0)
    else:
        n = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    return f"{n / 1000:.1f}K"


def show(path):
    rec = json.loads(path.read_text(encoding="utf-8"))
    reply = rec.get("response")
    verdict = reply.get("verdict") if isinstance(reply, dict) else None
    models = rec.get("effective_model") or ",".join(rec.get("observed_models") or []) or rec.get("requested_model")
    head = [path.parent.name, f"mode={rec.get('mode')}", f"status={rec.get('status')}"]
    if verdict:
        head.append(f"verdict={verdict}")
    head += [f"model={models}", f"effort={rec.get('effective_effort') or rec.get('requested_effort')}",
             f"new={new_tokens(rec)}"]
    if rec.get("fresh"):
        head.append("fresh")
    if (path.parent / "superseded").exists():
        head.append("SUPERSEDED")
    print(" ".join(head))
    print(f"  result: {path}")
    if rec.get("status") != "completed":
        print(f"  error: {rec.get('error')}")
        return 1
    if isinstance(reply, dict):
        for f in reply.get("findings") or []:
            print(f"  {f.get('id')} [{f.get('severity')}] {f.get('path')}: {f.get('evidence')} | FIX: {f.get('fix')}")
        for item in reply.get("limitations") or []:
            print(f"  limitation: {item}")
        return CODES.get(verdict, 1)
    print("  report tail: " + (reply or "")[-1500:].replace("\n", "\n  "))
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 1
    paths = results(argv[0])
    if not paths:
        print(f"no result.json under {argv[0]}")
        return 1
    code = 1
    for path in (paths if "--all" in argv else paths[-1:]):
        try:
            code = show(path)
        except (OSError, ValueError) as exc:
            print(f"{path}: unreadable ({exc})")
            code = 1
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
