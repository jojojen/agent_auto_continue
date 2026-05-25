#!/bin/bash
# install.sh — one-shot setup for agent_auto_continue.
#
# What it does:
#   1. Verify prerequisites (python3, tmux)
#   2. Make the watcher scripts executable
#   3. Print the suggested launch command (does NOT start it for you —
#      starting in the wrong tmux pane / wrong workspace is annoying to undo,
#      so we leave that as an explicit user step)
#
# Usage:
#   ./install.sh                # check + print suggested command
#   ./install.sh --start        # also launch the watcher in the background
#
# After installation, attach to your tmux session (default ``claude:0.0``) and
# run Claude Code as usual. When you hit the usage limit, the watcher waits
# for the reset moment, then types ``continue`` and presses Enter for you.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

err() { printf "✗ %s\n" "$*" >&2; }
ok()  { printf "✓ %s\n" "$*"; }

# --- prerequisite checks --------------------------------------------------

if ! command -v python3 >/dev/null 2>&1; then
    err "python3 not found in PATH. Install Python 3.9+ and retry."
    exit 1
fi
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
ok "python3 ${PY_VERSION}"

if ! command -v tmux >/dev/null 2>&1; then
    err "tmux not found in PATH. Install tmux (brew install tmux / apt install tmux) and retry."
    exit 1
fi
ok "tmux $(tmux -V | awk '{print $2}')"

CLAUDE_PROJECTS_DIR="$HOME/.claude/projects"
if [ ! -d "$CLAUDE_PROJECTS_DIR" ]; then
    err "Claude Code projects dir not found at $CLAUDE_PROJECTS_DIR."
    err "Open Claude Code at least once in your workspace first, then re-run."
    exit 1
fi
WORKSPACE_COUNT=$(find "$CLAUDE_PROJECTS_DIR" -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ')
ok "Claude Code workspaces detected: ${WORKSPACE_COUNT}"

# --- make scripts executable ----------------------------------------------

chmod +x "$SCRIPT_DIR/watch-claude-ratelimit.py"
chmod +x "$SCRIPT_DIR/watch-claude-ratelimit.sh"
ok "scripts marked executable"

# --- print suggested launch command ---------------------------------------

echo ""
echo "═══ Setup complete ═══"
echo ""
echo "To start the watcher in the background:"
echo ""
echo "  nohup \"$SCRIPT_DIR/watch-claude-ratelimit.sh\" </dev/null \\"
echo "    >/tmp/agent-auto-continue.stdout 2>&1 &"
echo ""
echo "The debug log is written to: \$HOME/.cache/agent-auto-continue/watch.log"
echo "(env: CLAUDE_WATCH_LOG to override)"
echo ""
echo "Env vars you may want to set BEFORE launching:"
echo "  export CLAUDE_WATCH_PANE=claude:0.0         # default; tmux target"
echo "  export CLAUDE_WATCH_DEFAULT_TZ=Asia/Tokyo   # fallback timezone"
echo "  export CLAUDE_WATCH_BUFFER=10               # seconds past reset"
echo ""

# --- optionally auto-start ------------------------------------------------

if [ "${1:-}" = "--start" ]; then
    echo "Starting watcher in background..."
    nohup "$SCRIPT_DIR/watch-claude-ratelimit.sh" </dev/null \
        >/tmp/agent-auto-continue.stdout 2>&1 &
    pid=$!
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        ok "Watcher started (PID ${pid}). Tail the debug log to see activity."
    else
        err "Watcher exited immediately. Check /tmp/agent-auto-continue.stdout for details."
        exit 1
    fi
fi
