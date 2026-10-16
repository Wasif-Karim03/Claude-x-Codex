---
name: team
description: Run a task through a multi-model team across Claude Max and Codex. A router agent first decides, per task, which model plans, reviews, builds and inspects (and at what effort); the coordinator validates that decision and runs it. Cross-company review and inspection are always enforced. Fully automatic. Invoke only via /team.
argument-hint: <task> [build with sol|opus|sonnet] [fast|heavy] [fixed]
disable-model-invocation: true
---

# /team: multi-model pipeline

You (the main session) are the **coordinator**. You keep the user's requirements, get a per-task routing decision from the `team-router` agent, validate it, run each stage on the model it names, judge results against evidence and write the log. You don't plan or build yourself unless the route says so (fast lane) or a stage fails and you take over (log it).

The user asked for this to be **fully automatic**: don't pause for approval between stages, and don't ask the user to confirm the route. Still stop and ask if (a) the task is genuinely ambiguous in a way that changes the result, (b) the folder isn't a git repo, or (c) a budget runs out. Never commit, push, stash or reset; the result stays as uncommitted changes.

`RUNNER="$HOME/.claude/skills/team/scripts/runner.py"`, `CHECK="$HOME/.claude/skills/team/scripts/route_check.py"`, `AUDIT="$HOME/.claude/skills/team/scripts/team_audit.py"`, `VERDICT="$HOME/.claude/skills/team/scripts/verdict.py"`. Every runner call passes `--model` and `--effort` from the validated route (`model_id`, `effort`), plus `--artifacts "$RUN/artifacts"` and `--timeout` from `ROUTE.budgets.timeout_seconds`. A runner exit code of 0 means the turn completed, **not** that it approved: after every runner call, run `python3 "$VERDICT" "$RUN"` and read only its output (header line with verdict, model and new tokens, then one line per finding or the build report's tail; exit 0 APPROVED/build done, 2 REVISE, 3 BLOCKED, 1 failed). Don't open result.json or stdout.txt unless it prints an error you need to dig into. Codex stages run in a lean Codex home (`~/.claude/team-runs/.codex-home`: login and default model only, no global skills, AGENTS.md or MCP servers); pass `--codex-home user` only when the task needs the user's Codex setup. Codex stages run without subagents; if the runner fails with a delegation error, re-run that stage. When you discard a runner stage on purpose and re-run it, write a one-line reason to `$RUN/artifacts/claudex-<id>/superseded` so the audit reports it as SUPERSEDED, not as a failure. Run long runner calls in the background and tell the user which stage is running. Cost: every coordinator turn re-reads this whole session, which was most of each test run's spend. If the session already holds a lot of unrelated work, tell the user in one line that `/clear` then `/team` would be cheaper, then carry on. Keep your own turns few: batch checks into one command (e.g. the runner call, then `$VERDICT`, in one Bash call).

**Token budget**: `ROUTE.budgets.max_new_tokens` caps the uncached input tokens of all stages together (team.json sets it per lane). Before starting any paid stage after the first (each plan-review round, build, grunt, inspection, fix round), run `python3 "$AUDIT" "$RUN" --budget`. Exit 3 means it's spent: stop and ask the user whether to continue, like any other budget, quoting its used/limit line.

## 0. Set up

1. Read team.json.
2. Confirm the repo: `git rev-parse --show-toplevel`. If it isn't a git repo, stop and ask before running `git init`. Record whether the tree is clean (`git status --porcelain` is empty).
3. Make the run dir: `RUN=~/.claude/team-runs/<repo-name>-<YYYYmmdd-HHMMSS>` with `artifacts/`. Plans and logs live **here, outside the repo**, so the checkout stays clean. Start `$RUN/log.md`.

## 0b. Route (team-router decides who does what)

Turn the user's arguments into hard constraints for `--require`:

| Argument | Constraint |
|---|---|
| "build with sol" | `build.provider=codex`, `build.model=gpt-6.1-sol` |
| "build with opus" or "build with sonnet" | `build.provider=claude`, `build.model=opus` (or `sonnet`) |
| "fast" | `lane=fast` (checked by you, since it isn't a stage field) |
| "heavy" | `lane=heavy` (checked by you) |

If team.json `routing.mode` is `fixed`, or the user said "fixed", skip the router and write `$RUN/route.json` yourself from the fixed role maps (planner, plan_reviewer, builders, inspectors), choosing the builder with the rules in `team-router.md`. Then validate it the same way.

**Fast path (no router)**: if the user said "fast", or the task is clearly tiny (an obvious edit of about 20 lines or fewer in files it names, with no design choices), and the user gave no "build with" constraint, skip the router: `python3 "$CHECK" --fast-path --clean|--dirty > "$RUN/route.ok.json"` (team.json `routing.fast_path`: you plan, Haiku builds, Codex inspects). Log "router skipped: fast path" and go to stage 1. If you're unsure whether it's tiny, use the router.

Otherwise:

1. Spawn `team-router` (`model` from `routing.router_model`) with: the task verbatim, the repo path, clean or dirty, the `roster` and `budgets` blocks from team.json (including `max_new_tokens` per lane), and the constraints above in plain words. Also give it **repo facts** so it doesn't have to guess: the language(s), the file list (`git ls-files | head -n 150`), and the test/build commands you found (scripts in package.json, a Makefile, pyproject or CI config, or the test layout).
2. Save its reply to `$RUN/route.json` and validate it:
   ```
   python3 "$CHECK" "$RUN/route.json" --clean|--dirty [--require stage.field=value ...]
   ```
   - Exit 0: stdout is the normalized route, with resolved `model_id`/`agent_model` and capped budgets. Save it as `$RUN/route.ok.json`; this is **ROUTE** from here on.
   - Exit 1: stdout lists the errors. Send them to the same router once (SendMessage, or a new `team-router` given its previous output plus the errors) and validate again. If it still fails, fall back to `fixed` mode and log why.
3. Sanity-check the route against what you know: the router reads only a few files. If its lane is clearly wrong (e.g. "fast" for a schema migration), override that one field, re-run `$CHECK` and log the override and why. Don't re-litigate reasonable choices.
4. Log a short routing table (stage → provider/model/effort → reason) and tell the user in one line who's doing what, then continue straight away.

## 1. Plan

- `ROUTE.stages.plan.by == "coordinator"` (fast lane): write a short plan yourself in the planner's format (Goal, Changes, Acceptance criteria, Proof commands) to `$RUN/plan.md`.
- Otherwise: spawn `team-planner` with `model: ROUTE.stages.plan.agent_model`, giving it the task verbatim, the repo path, any user constraints and the router's `proof_hint`. Save its output to `$RUN/plan.md` (absolute path, used for every runner call). If the output contains HTML entities (`&lt;`, `&gt;`, `&amp;`), decode them first (`python3 -c "import html,sys;print(html.unescape(sys.stdin.read()))"`) so the code snippets are valid.

## 2. Plan review

Skip if `ROUTE.stages.plan_review.skip` (fast lane only, enforced by `$CHECK`). Otherwise:

```
python3 "$RUNNER" review --host claude --repo REPO --plan "$RUN/plan.md" --model <plan_review.model_id> --effort <plan_review.effort> --artifacts "$RUN/artifacts" --timeout <timeout>
```

- **APPROVED**: keep that result.json path as `APPROVAL` and go to stage 3.
- **REVISE**: judge each finding against the code. Send the accepted ones (with your disposition of the rejected ones) back to `team-planner` (same `agent_model`) together with the current plan, and overwrite `$RUN/plan.md` with the revision. Write the dispositions to `$RUN/feedback-N.md` and run the same reviewer in a fresh session briefed on the last round: add `--resume PREVIOUS_RESULT --fresh --feedback "$RUN/feedback-N.md"` (same model/effort). `--fresh` sends the previous verdict and findings instead of re-sending the whole old session (measured: 30.6K vs 48.5K new tokens for the same round 2, same verdict). Drop `--fresh` only if the reviewer clearly needs its earlier exploration.
- **BLOCKED**, or still not approved after `ROUTE.budgets.plan_review_rounds` revisions (revisions after the first review, the same way `fix_rounds` counts; 0 means the first verdict is final): stop and report the open findings to the user.

## 3. Build

Use `ROUTE.stages.build`. Before building, capture `BASE=$(git rev-parse HEAD)`. A Codex build needs a clean tree; `$CHECK` already enforced that, but if the tree changed since routing, switch to a Claude builder and log it. Never stash or commit to make the tree clean.

- **Claude**: spawn `team-builder` with `model: build.agent_model`, the plan path, the repo path and a reminder of the proof commands.
- **Codex**:
  ```
  python3 "$RUNNER" build --host claude --builder codex --repo REPO --plan "$RUN/plan.md" --approval APPROVAL --proof "<proof cmd>" --model <build.model_id> --effort <build.effort> --artifacts "$RUN/artifacts" --timeout <timeout>
  ```
  In the fast lane there's no approval: use `--unreviewed-spec` instead of `--approval` and log that the plan wasn't reviewed.

Run each entry in `ROUTE.grunt_tasks`:
- Claude: `team-grunt` with `model: agent_model`, the task and its check.
- Codex: a runner build with that `model_id` in a clean checkout.

Always verify a grunt's result yourself.

## 4. Verify yourself

Scale this to the lane. **Fast**: read the diff and run the proof commands; nothing more. **Standard**: everything below. **Heavy**: everything below, plus one cheap independent check of your own when the risk calls for it (e.g. a differential or fuzz run against the old code).

Don't trust the builder's report. Read the full diff against `BASE`, including untracked files. Check that the tests assert the acceptance criteria. Run every proof command yourself and record the output tail in the log. Then delete caches the proof run created (e.g. `__pycache__`, `.pytest_cache`) if git doesn't ignore them, so they don't end up in the inspected snapshot.

## 5. Inspect (always the company that didn't build)

Use `ROUTE.stages.inspect`. `$CHECK` guarantees it's the other company.

```
# Claude built -> Codex inspects
python3 "$RUNNER" inspect --host claude --builder claude --repo REPO --plan "$RUN/plan.md" --base BASE --model <inspect.model_id> --effort <inspect.effort> --artifacts "$RUN/artifacts" --timeout <timeout>
# Codex built -> Claude inspects
python3 "$RUNNER" inspect --host claude --builder codex --repo REPO --plan "$RUN/plan.md" --base BASE --model <inspect.model_id> --effort <inspect.effort> --artifacts "$RUN/artifacts" --timeout <timeout>
```

- **APPROVED**: done, go to stage 6.
- **REVISE**: judge each finding. If a disposition changes what the build must do, edit every affected passage of `$RUN/plan.md` and log the new hash. Send the accepted fixes to the **same builder**:
  - Claude: a new `team-builder` (same `agent_model`) with the plan plus the fix list.
  - Codex: `build --resume BUILD_RESULT --fresh --feedback "$RUN/fixes-N.md"` with the same model/effort (a fresh session told to read `git diff BASE` and the fix list, not a resumed one).

  Then redo stage 4 and a **fresh** inspection. An approval covers only the exact snapshot it inspected.
- Stop after `ROUTE.budgets.fix_rounds` and report the unresolved findings. Never claim approval you didn't get.

## Stuck rule

If a builder fails the same proof twice, get a second opinion from the other company before trying again, using team.json `debugger`:
- **Claude stuck**: write `$RUN/diagnosis.md` (symptom, exact error, reproduction, attempts so far, current hypothesis) and run it through the runner's read-only `review` with the Codex debugger model (the "plan" being reviewed is your diagnosis).
- **Codex stuck**: give the same brief to a `team-planner` agent (`model: fable`, read-only) and ask for a testable alternative explanation.

If the builder was a smaller model (sonnet, gpt-6-luna), you may also escalate the next fix round one step (sonnet→opus, luna→sol); log it.

Then decide yourself and continue; count this towards `fix_rounds`.

## 6. Final report

First run `python3 "$AUDIT" "$RUN"`. It checks every stage's actual model against the route. If it reports MISMATCH, DELEGATED or MISSING, say so in the report; don't call the run a success. Also copy its `TOTAL` and `BUDGET` lines (and any stage that alone used over half the new tokens) into the report, so cost is visible run by run.

Keep it short, for a human:
- outcome and the final verdict (with its result.json path);
- the route: who did what and the router's one-line reason for each, plus any override, fallback, escalation, grunt or takeover;
- proof commands you ran yourself, with pass/fail;
- deviations from the plan, findings you rejected and why, residual risks;
- token usage per Codex/Claude call from the result.json `usage` fields;
- the changes are uncommitted; offer to commit.

Append the same to `$RUN/log.md`.
