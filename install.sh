#!/usr/bin/env bash
# Installs the /team skill and its four agents into ~/.claude.
#   ./install.sh            install (existing files are backed up first)
#   ./install.sh --test     install, then run the bundled unit tests
#   ./install.sh --uninstall
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SKILL_DST="$CLAUDE_DIR/skills/team"
AGENT_DST="$CLAUDE_DIR/agents"
AGENTS=(team-router team-planner team-builder team-grunt)
STAMP="$(date +%Y%m%d-%H%M%S)"

say()  { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }

if [[ "${1:-}" == "--uninstall" ]]; then
  [[ -d "$SKILL_DST" ]] && mv "$SKILL_DST" "$SKILL_DST.removed-$STAMP" && say "Moved $SKILL_DST aside (.removed-$STAMP)"
  for a in "${AGENTS[@]}"; do
    f="$AGENT_DST/$a.md"; [[ -f "$f" ]] && mv "$f" "$f.removed-$STAMP" && say "Moved $f aside"
  done
  say "Uninstalled. Run logs in ~/.claude/team-runs were left alone."
  exit 0
fi

# --- prerequisites -----------------------------------------------------------
command -v python3 >/dev/null || { echo "python3 (3.10+) is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "python3 must be 3.10 or newer" >&2; exit 1; }
command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
command -v claude >/dev/null || warn "claude CLI not on PATH (Claude Code is needed to run /team)"
if command -v codex >/dev/null; then
  codex login status >/dev/null 2>&1 || warn "codex is installed but not logged in: run 'codex login'"
else
  warn "codex CLI not on PATH: install it (npm i -g @openai/codex) and run 'codex login'"
fi

# --- install -----------------------------------------------------------------
mkdir -p "$CLAUDE_DIR/skills" "$AGENT_DST"
if [[ -d "$SKILL_DST" ]]; then
  mv "$SKILL_DST" "$SKILL_DST.bak-$STAMP"; say "Backed up existing skill to $SKILL_DST.bak-$STAMP"
fi
cp -R "$SRC/skills/team" "$SKILL_DST"
for a in "${AGENTS[@]}"; do
  f="$AGENT_DST/$a.md"
  [[ -f "$f" ]] && cp "$f" "$f.bak-$STAMP" && say "Backed up $f"
  cp "$SRC/agents/$a.md" "$f"
done
chmod +x "$SKILL_DST"/scripts/*.py
mkdir -p "$HOME/.claude/team-runs"
say "Installed /team skill -> $SKILL_DST"
say "Installed agents      -> $AGENT_DST/{$(IFS=,; echo "${AGENTS[*]}")}.md"

if [[ "${1:-}" == "--test" ]]; then
  say "Running unit tests..."
  ( cd "$SKILL_DST/scripts" && for t in test_*.py; do
      printf '  %-22s ' "$t"; python3 -B -m unittest -q "${t%.py}" 2>&1 | tail -n 1; done )
fi

say ""
say "Done. Restart Claude Code (or start a new session), cd into a git repo and run:"
say "  /team <describe the task>"
say "Check the model names in $SKILL_DST/team.json match what your accounts can use."
