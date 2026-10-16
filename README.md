# Claude x Codex: `/team`

`/team` is a Claude Code skill. It runs one coding task through a team of models from **two companies**: Claude (Fable, Opus, Sonnet, Haiku) and OpenAI Codex (Astra, Sol, Luna).

It's fully automatic. A router agent reads the task and the repo, then picks which model plans, reviews, builds and inspects, and at what effort. A Python checker validates that choice. The coordinator (your Claude Code session) runs every stage on the model the route names, and an audit proves afterwards that each stage really ran on that model.

```
/team add a --stats-only flag to the CLI and golden tests for it
/team build with sol: make input streaming with byte-identical output
/team fast: add a --version flag
/team heavy: rework the parser for CRLF and invalid UTF-8
```

## How it works

```
 task ──► 0b ROUTE ──► 1 PLAN ──► 2 PLAN REVIEW ──► 3 BUILD ──► 4 VERIFY ──► 5 INSPECT ──► 6 REPORT
          team-router   planner    other company    builder     coordinator   other company  + audit
          (Sonnet)      (picked)   (picked)         (+ grunts)  re-runs proof than builder
```

- **Cross-company review is enforced.** The plan is reviewed, and the code inspected, by the company that didn't write it. Claude builds → Codex inspects; Codex builds → Claude inspects.
- **Routing.** `team-router` picks from the `roster` in `team.json` (cost, and what each model is good for). `route_check.py` rejects a bad route: an unknown model, a missing cross-company check, a Codex grunt beside a Codex builder, or a budget over the lane cap. Your constraints (`build with sol`, `fast`, `heavy`) are hard requirements.
- **Fast path.** For tiny tasks, or when you say `fast`, the router is skipped: the coordinator plans, Haiku builds, Astra inspects.
- **Bounded loops.** Plan review runs at most `plan_review_rounds` revisions and build fixes at most `fix_rounds`. When a budget runs out, it stops and asks you.
- **Nothing is committed.** The result stays as uncommitted changes in your working tree. Plans, logs and model outputs go to `~/.claude/team-runs/<repo>-<timestamp>/`, outside the repo.
- **Proof, not trust.** Every plan has proof commands. The coordinator re-runs them itself, and the final report starts with `team_audit.py`. The audit checks each stage's *observed* model against the requested one, using Codex result records and Claude subagent transcripts. Any mismatch, delegation or missing stage fails it.

## Token savings built in

| Measure | What it does | Measured effect |
|---|---|---|
| Lean Codex home | Codex runs with only your login and default model: no global skills, AGENTS.md or MCP servers | about −13% new tokens per Codex stage |
| `--fresh` rounds | Review and fix rounds start a new session from a short brief instead of resuming a growing one | about −37% new tokens on round 2 |
| Lane budgets | `max_new_tokens` per lane (fast 200K / standard 600K / heavy 1.2M); `team_audit.py --budget` runs before each paid stage | stops runaway runs |
| Fast path | No router call for tiny tasks | about −13% on a whole fast run |
| `verdict.py` | The coordinator reads a one-screen summary instead of raw JSON | smaller coordinator turns |
| No delegation | Codex runs with `multi_agent` disabled; any subagent call fails the stage | the stage runs only on the model you paid for |

## Requirements

- **Claude Code**, on a plan that can use the Claude models in `team.json`. Fable is used for hard planning and inspection.
- **Codex CLI**, installed and logged in (`npm i -g @openai/codex`, then `codex login`), with access to the Codex models in `team.json`.
- **Python 3.10+** (standard library only) and **git**. `/team` only runs inside a git repo.

## Install

```bash
git clone https://github.com/Wasif-Karim03/Claude-x-Codex.git
cd Claude-x-Codex
./install.sh --test      # or ./install.sh to skip the tests
```

This copies:

| From | To |
|---|---|
| `skills/team/` (SKILL.md, team.json, scripts) | `~/.claude/skills/team/` |
| `agents/team-{router,planner,builder,grunt}.md` | `~/.claude/agents/` |

Existing files are backed up with a `.bak-<timestamp>` suffix first. Start a new Claude Code session afterwards. `./install.sh --uninstall` moves everything aside again.

<details><summary>Manual install</summary>

```bash
mkdir -p ~/.claude/skills ~/.claude/agents
cp -R skills/team ~/.claude/skills/team
cp agents/team-*.md ~/.claude/agents/
```
</details>

## Configure

Everything is in `~/.claude/skills/team/team.json`:

- `roster`: the models the router may pick, with ids, cost and strengths. **If your accounts use different model names, edit them here.** Claude entries need `agent_model` (an agent alias: `fable`, `opus`, `sonnet` or `haiku`); Codex entries need the exact `id` your Codex CLI accepts.
- `budgets`: `plan_review_rounds`, `fix_rounds`, `timeout_seconds` and `max_new_tokens` per lane.
- `routing.mode`: `auto` (the router decides) or `fixed` (use the role maps `planner`, `plan_reviewer`, `builders`, `inspectors` and `grunts`). You can also say `fixed` in the command for one run.
- `routing.fast_path`: the fast-lane template.

The agent files in `~/.claude/agents/` set each role's default model and instructions. The router overrides the model per task.

## Arguments

| You say | Effect |
|---|---|
| `build with sol` / `opus` / `sonnet` | forces the builder (and so the inspector's company) |
| `fast` | fast lane / fast path |
| `heavy` | heavy lane: strongest models, higher effort, bigger budget |
| `fixed` | skip the router and use the fixed role maps |

## Check a run yourself

```bash
S=~/.claude/skills/team/scripts
python3 $S/team_audit.py ~/.claude/team-runs/<run>        # per-stage requested vs observed model; exit 1 on problems
python3 $S/team_audit.py ~/.claude/team-runs/<run> --budget
python3 $S/verdict.py   ~/.claude/team-runs/<run> --all   # every stage's verdict and findings
```

To discard a stage on purpose, write a one-line reason to `<run>/artifacts/claudex-<id>/superseded`. The audit then reports that stage as SUPERSEDED, not as a failure.

## Repo layout

```
skills/team/SKILL.md            the coordinator's instructions (the pipeline)
skills/team/team.json           roster, role maps, budgets, routing
skills/team/scripts/runner.py   runs one Codex/Claude stage via CLI, records requested/effective/observed model
skills/team/scripts/route_check.py   validates and normalizes a route
skills/team/scripts/team_audit.py    proves which model ran each stage; budget check
skills/team/scripts/verdict.py       one-screen verdict for a stage
skills/team/scripts/test_*.py        unit tests (python3 -m unittest, run from scripts/)
agents/team-*.md                 router, planner, builder and grunt subagents
docs/HOW-IT-WORKS.md             design decisions, test history, gotchas
```

## Credits

`runner.py` and `test_runner.py` started from [chaseai-yt/claudex-loop](https://github.com/chaseai-yt/claudex-loop) v2.1.0 (MIT, © 2026 Chase AI). See `THIRD-PARTY-LICENSE-claudex-loop.txt`. They've since been extended with the lean Codex home, `--fresh` rounds, the delegation block and token accounting. Everything else here is MIT-licensed (see `LICENSE`).
