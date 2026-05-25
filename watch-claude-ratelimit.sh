#!/bin/bash
# watch-claude-ratelimit.sh — thin wrapper around watch-claude-ratelimit.py
#
# Detection: tail the JSONL session log under ~/.claude/projects/<workspace>/
# for structured rate-limit events ("error":"rate_limit", apiErrorStatus 429).
# Injection: tmux send-keys "continue" to the configured pane.
#
# Env overrides (see watch-claude-ratelimit.py docstring):
#   CLAUDE_WATCH_PANE         default "claude:0.0"
#   CLAUDE_WATCH_PROJECT_DIR  auto-detected if unset
#   CLAUDE_WATCH_BUFFER       default 10s
#   CLAUDE_WATCH_DEBOUNCE     default 90s
#   CLAUDE_WATCH_DEFAULT_TZ   default "Asia/Tokyo"
#   CLAUDE_WATCH_LOG          default "$HOME/.cache/agent-auto-continue/watch.log"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/env python3 "$SCRIPT_DIR/watch-claude-ratelimit.py" "$@"
