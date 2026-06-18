---
name: agent_auto_continue
description: When the user clones this repo and asks you to set it up, follow the steps below to install and launch the rate-limit auto-continue watcher for Claude Code.
tools: Bash, Read
---

# Claude-facing setup guide

This file is for Claude (or any LLM-style assistant) to read when the user
clones the `agent_auto_continue` repo and asks you to set it up. Follow the
steps below **in order**. Do not skip the verification step — silent
misconfiguration is the most common failure mode.

## What you are setting up

A background watcher that:
- tails the user's Claude Code JSONL session log
- detects `"error":"rate_limit"` events
- parses the reset time
- sleeps until then + a buffer
- sends `continue` to the user's tmux pane via `tmux send-keys`

The user wants this to "just work" — they should not have to type
`continue` themselves when their quota resets.

## Step 1 — Verify prerequisites

Run these checks (read-only). If any fail, stop and tell the user what's
missing:

```bash
command -v python3 && python3 -c 'import sys; assert sys.version_info >= (3, 9), sys.version_info'
command -v tmux
[ -d "$HOME/.claude/projects" ] && echo OK || echo "no Claude Code workspaces yet"
```

If `python3 < 3.9`, the `zoneinfo` import fails — recommend an upgrade.

## Step 2 — Inspect the user's tmux panes

The watcher injects through tmux, so confirm Claude is actually running there:

```bash
tmux list-panes -a 2>&1 | head -20
```

Note any panes where `claude` is the current command. The watcher can now
auto-select among multiple live Claude panes, so you usually do **not** need
to set `CLAUDE_WATCH_PANE`. Only set it when the user explicitly wants a hard
pin to one pane.

## Step 3 — Make scripts executable + run installer

From the cloned repo directory:

```bash
./install.sh
```

This checks prerequisites and prints the suggested launch command. **It does
NOT start the watcher by default** — starting in the wrong tmux pane is
annoying to undo, so it's explicit.

## Step 4 — Launch the watcher

Default launch (recommended; puts the watcher in its own detached tmux
session so it is not reaped when an agent / IDE / managed parent shell exits):

```bash
tmux has-session -t agent-auto-continue 2>/dev/null && tmux kill-session -t agent-auto-continue
tmux new-session -d -s agent-auto-continue './watch-claude-ratelimit.sh'
```

Only hard-pin a pane when the user asks for it (e.g. `mywork:1.0`):

```bash
tmux has-session -t agent-auto-continue 2>/dev/null && tmux kill-session -t agent-auto-continue
tmux new-session -d -s agent-auto-continue \
    "env CLAUDE_WATCH_PANE=mywork:1.0 ./watch-claude-ratelimit.sh"
```

Use plain `nohup ... &` only from a normal interactive login shell. Do not
use it as the default launch path when another agent or tool runner is
starting the watcher, because parent-process cleanup often kills the child.

If the user's machine isn't on Asia/Tokyo and the rate-limit messages they
see don't carry a `(Region/City)` tag, also set
`CLAUDE_WATCH_DEFAULT_TZ=<their-tz>` (e.g. `America/Los_Angeles`).

## Step 5 — Verify it's running

```bash
tmux list-sessions | grep agent-auto-continue
tmux capture-pane -pt agent-auto-continue | tail -20
tail -10 "$HOME/.cache/agent-auto-continue/watch.log"
```

The log should show:

```
[…] agent_auto_continue watcher starting
[…] Workspace: /Users/<user>/.claude/projects/-…
[…] resolve_pane: selected %7 (rate-limit-banner-visible)
[…] Watching /…/<session>.jsonl (tmux pane: auto:%7, tz fallback: Asia/Tokyo)
```

If the tmux session is missing or the log is missing, re-run the launch
command in the foreground once to capture the startup error, then report it
to the user.
If the watcher restarts while a rate-limit banner is still visible, you should
also see a recovery line such as:

```
[…] recover_visible_rate_limit_state: recovered %7 from visible banner "You've hit your session limit · resets …"; waiting 10s before continue
```

## Step 6 — Report back to the user

Tell them:

1. The watcher PID and tmux pane it's watching
   Also report the detached tmux session name: `agent-auto-continue`
2. The exact `watch.log` path so they can `tail -f` it later if curious
3. That it'll auto-restart-tail when Claude Code starts a new session
   (background poller checks for newer .jsonl files every 60s)
4. That pane selection is heuristic unless they explicitly set `CLAUDE_WATCH_PANE`
5. How to stop it: `tmux kill-session -t agent-auto-continue`

## Common adjustments

If the user is in a different timezone than Asia/Tokyo and their rate-limit
messages don't carry the `(Region/City)` tag, **prefer setting
`CLAUDE_WATCH_DEFAULT_TZ`** rather than editing the .py file. Examples:

```bash
# US users
CLAUDE_WATCH_DEFAULT_TZ=America/Los_Angeles ./watch-claude-ratelimit.sh

# UK users
CLAUDE_WATCH_DEFAULT_TZ=Europe/London ./watch-claude-ratelimit.sh
```

If the user runs Claude Code in a sub-pane (e.g. inside a `nvim` floating
terminal or a Zellij/screen replacement), tmux send-keys won't reach it.
Tell them they need plain tmux for the injection step. Detection still
works — the log will record what it would have done.

## Out of scope (don't offer these unless asked)

- launchd / systemd auto-startup — keep it manual for now
- non-tmux injection (AppleScript, expect, etc.) — fragile, not portable
- Multi-workspace concurrent watchers — works but still requires explicit
  `CLAUDE_WATCH_PROJECT_DIR` per workspace; mention only if user has >1 active
  Claude Code workspace

## If something breaks

The debug log at `$HOME/.cache/agent-auto-continue/watch.log` records every
detection / parse attempt / debounce skip / continue send. Ask the user to
share the last 30 lines if they report an issue. If the watcher dies, the new
startup / signal / exit logs will usually show whether it received `SIGTERM`,
`SIGHUP`, or another external shutdown.
