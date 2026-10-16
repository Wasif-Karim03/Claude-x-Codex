# How it works and why

This doc covers the design notes behind `/team`, how it was tested, and the traps found along the way. For setup, see the README.

## Pieces

| Piece | Role |
|---|---|
| Coordinator (your Claude Code session, `SKILL.md`) | Keeps the requirements, gets and validates the route, runs every stage, re-runs proofs, writes `log.md` and the final report. It doesn't plan or build unless the route says so (fast lane). |
| `team-router` agent (Sonnet, read-only) | Returns one JSON route: lane, and provider/model/effort for each stage, with a reason. |
| `route_check.py` | Validates the route against `team.json`, enforces your constraints, resolves `model_id`/`agent_model`, caps budgets. The result is `route.ok.json` (ROUTE). |
| `team-planner` agent | Writes the plan: acceptance criteria plus proof commands in a code fence. |
| `runner.py` | Runs one Codex or Claude CLI stage (`review`, `build`, `inspect`). It records the requested, effective and observed model plus token usage in `artifacts/claudex-<id>/result.json`. |
| `team-builder` / `team-grunt` agents | Claude-side builder, and the mechanical-subtask worker (fixtures, renames, docs). |
| `verdict.py` | One-screen summary of a stage. Exit codes: 0 APPROVED, 2 REVISE, 3 BLOCKED, 1 failed. |
| `team_audit.py` | Proves which model ran each stage, from Codex result records and Claude subagent transcripts under `~/.claude/projects`. Also reports tokens, budget, delegation and missing stages. |

## Design decisions

- **The other company reviews.** Plan review and final inspection always come from the company that didn't write the work. Codex has blind spots Claude catches, and the reverse is also true.
- **Delegation inside a stage is a failure.** In testing, a user's global Codex skill plus Codex's `multi_agent` feature made the Sol builder hand work to subagents, so another model did the job and nobody recorded it. Now every Codex call gets `--disable multi_agent --disable multi_agent_v2`. The build prompt forbids doing other roles' steps, and any `collab_tool_call` event fails the stage.
- **Lean Codex home.** Codex stages run with `CODEX_HOME=~/.claude/team-runs/.codex-home`. That home holds only an `auth.json` symlink and a `config.toml` with model, effort and service tier. This removes global skills, AGENTS.md and MCP servers, which saves tokens and stops surprise behaviour. Auth refreshes get synced back to `~/.codex` safely. Pass `--codex-home user` when a task really needs your Codex setup.
- **Fresh rounds over resumed sessions.** Resumed Codex sessions re-send their whole history; one review hit 748K input tokens by round 3. A `--resume PREV --fresh` round starts a new session from a short brief (prior verdict and findings, or the base commit plus a diff pointer).
- **Round budgets count revisions after the first pass.** When a budget runs out, the pipeline stops and asks you instead of looping.
- **No Codex grunt next to a Codex builder.** Each Codex write stage needs a clean tree to diff against.
- **Runs live outside the repo,** so the checkout stays clean. The result is left uncommitted for you to review.
- **Overrides are tested at the routing stage.** Every later stage follows ROUTE, so it's enough to prove the route honours a constraint.

## How it was tested

These runs used a small Python CLI as the test repo:

| Test | Route | Outcome |
|---|---|---|
| T1 fast lane | coordinator plans, Haiku builds, Astra inspects | APPROVED |
| T2 clean tree | Sol reviews and builds, Opus inspects | a real bug, one fix round, APPROVED |
| T3 dirty tree | Sonnet builds, Astra inspects | the user's uncommitted file was preserved |
| T4 grunts | Sonnet plans, Sol reviews (REVISE ×2), Sol builds, Haiku grunt makes 30 fixtures, Opus inspects | APPROVED; found the delegation hole above |
| T5 overrides | `build with …`, invalid constraints, `fixed` mode | honoured or rejected correctly |
| T-UI | Sonnet plans and builds a browser page, Sol reviews, Astra inspects | APPROVED; headless-Chrome fallback added to the builder |
| T-heavy | Fable plans, Astra-high reviews (REVISE ×2), Sol-high builds, Fable-high inspects | APPROVED, 0 findings; 348-case old-vs-new fuzz with 0 mismatches |
| fast path | no router; Haiku builds, Astra inspects | 74K new tokens vs 85K for T1 |

Every run passed `team_audit.py`.

## Gotchas

- A runner exit code of 0 means the turn completed, **not** that the stage approved. Use `verdict.py`, or `response.verdict` in the newest `result.json`. The runner's stdout can hold more than one JSON object.
- Planner output can come back HTML-escaped. Decode it before saving `plan.md`.
- When building `--proof` from the plan, take only the lines inside the code fence.
- Avoid `rm -rf` in proof commands, because many permission setups block it. Use `python3 -c "import shutil; shutil.rmtree('x', True)"`.
- The Claude-in-Chrome extension refuses `file://` URLs. For local pages, use headless Chrome with an injected test harness.
- The router writes weak `check` commands for grunts, so the coordinator runs its own check (the plan's post-grunt proof).
- Claude subagent effort can't be observed, only requested. The audit reports the model, not the effort, for Claude stages.
