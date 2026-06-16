# agent_auto_continue

Auto-resume Claude Code after a usage-limit cooldown. Drop this repo next to
any project, run `./install.sh`, and your Claude Code session will type
`continue` for you the moment your quota resets — even if you walked away.

## What it does

When Claude Code hits a usage limit, the TUI shows something like:

```
You've hit your limit · resets 1am (America/Los_Angeles)
```

…and refuses to send more requests until that time. Without this watcher you
have to sit there and type `continue` yourself at the reset moment.

This watcher:

1. **Detects** the rate-limit event via Claude Code's own JSONL session log
   (`~/.claude/projects/<workspace>/<session>.jsonl`), not by scraping the
   TUI. The log carries structured `"error":"rate_limit"` events with
   `apiErrorStatus: 429` — stable regardless of TUI cosmetic changes.
2. **Parses the reset time** from the message body. Handles every variant
   observed in the wild: `am`/`pm`, 24-hour `HH:MM`, relative `in 2h` /
   `in 90m`, and Chinese `重置時間 14:30` / `於 09:00 重置`. Timezone is
   pulled from a trailing `(Region/City)` tag; falls back to `Asia/Tokyo`
   (override with `CLAUDE_WATCH_DEFAULT_TZ`).
3. **Sleeps** until the reset moment + a 10-second buffer.
4. **Types `continue` and Enter** into the correct Claude tmux pane via `tmux send-keys`.
5. **Recovers on restart**: if the watcher restarts while a rate-limit banner is
   still visible in a Claude pane, it reconstructs the wait from that banner and
   sends the missed `continue` instead of waiting for a brand-new JSONL event.

Detection ↔ injection are decoupled: detection works without tmux (just log
tailing), but injection currently relies on tmux because Claude Code's TUI
runs in your terminal and the only safe way to inject text is through tmux's
keystroke API.

## Requirements

- macOS or Linux (uses `tail`, `tmux`)
- Python ≥ 3.9 (stdlib only; uses `zoneinfo`, `urllib`, `subprocess`)
- `tmux` ≥ 3.0
- Claude Code (you're already using it)
- You run Claude Code **inside tmux**

## Quick start (Claude can do this for you)

1. Clone this repo somewhere stable:
   ```bash
   git clone https://github.com/jojojen/agent_auto_continue.git ~/agent_auto_continue
   ```
2. Run the installer:
   ```bash
   ~/agent_auto_continue/install.sh
   ```
   It checks prerequisites and prints the suggested launch command.
3. Launch the watcher in the background (run this from any shell on the
   machine where Claude Code lives):
   ```bash
   nohup ~/agent_auto_continue/watch-claude-ratelimit.sh </dev/null \
       >/tmp/agent-auto-continue.stdout 2>&1 &
   ```
   Or do steps 2+3 in one go:
   ```bash
   ~/agent_auto_continue/install.sh --start
   ```
4. Start Claude Code in your tmux pane as usual.

The watcher logs every event it sees to `~/.cache/agent-auto-continue/watch.log`,
so you can audit exactly what it detected, which pane it picked, and when it fired.

## How Claude should set this up for you

If you pulled this repo so Claude can read it, paste this into your chat:

> "I cloned `agent_auto_continue`. Read its README and SKILL.md, verify the
> prerequisites on this machine, and start the watcher in the background.
> Pick a sensible tmux pane name based on what's already running."

The SKILL.md file is written for Claude — it lists the exact checks and
commands to run, in order.

## Configuration

All knobs are environment variables. Set them before launching the watcher.

| env var | default | what it controls |
|---------|---------|------------------|
| `CLAUDE_WATCH_PANE` | *auto-select* | optional hard override for the tmux target; when unset, the watcher inspects live Claude panes and picks the best match |
| `CLAUDE_WATCH_PROJECT_DIR` | *auto-detected* | path to `~/.claude/projects/<workspace>` for THIS session — when unset, the workspace whose `.jsonl` files were touched most recently is used |
| `CLAUDE_WATCH_BUFFER` | `10` | seconds added after the parsed reset moment before sending `continue` |
| `CLAUDE_WATCH_DEBOUNCE` | `90` | suppress duplicate triggers within N seconds (Claude Code sometimes logs the rate-limit twice) |
| `CLAUDE_WATCH_DEFAULT_TZ` | `Asia/Tokyo` | timezone to assume when the rate-limit message doesn't carry a `(Region/City)` tag |
| `CLAUDE_WATCH_LOG` | `~/.cache/agent-auto-continue/watch.log` | debug log path |

## Verifying it's working

After launching, check that the process is alive:

```bash
ps -ef | grep watch-claude-ratelimit | grep -v grep
tail -f ~/.cache/agent-auto-continue/watch.log
```

You should see startup lines like:

```
[2026-05-26T01:23:45] agent_auto_continue watcher starting
[2026-05-26T01:23:45] Workspace: /Users/you/.claude/projects/-Users-you-my-project
[2026-05-26T01:23:45] resolve_pane: selected %7 (rate-limit-banner-visible)
[2026-05-26T01:23:45] Watching /Users/you/.claude/projects/-Users-you-my-project/<session>.jsonl (tmux pane: auto:%7, tz fallback: Asia/Tokyo)
```

The next time you hit a rate limit, the log will show:

```
[…] Rate-limit detected: "You've hit your limit · resets 1am (America/Los_Angeles)"
[…] Reset at 2026-05-26T08:00:00+00:00; sleeping 73215s
[…] resolve_pane: selected %7 (rate-limit-banner-visible)
[…] Sending 'continue' to tmux pane %7
```

Pane selection is no longer "first pane wins". When `CLAUDE_WATCH_PANE` is unset,
the watcher prefers:

1. a Claude pane whose visible content still shows a rate-limit banner
2. otherwise the most recently active Claude pane that is not an empty
   "nothing to continue" session
3. otherwise the most recently active Claude pane

That avoids the common failure mode where one Claude pane is empty after `/clear`
while another pane is the one actually waiting on quota reset.

## Stopping the watcher

```bash
pkill -f watch-claude-ratelimit
```

…or find its PID via `ps` and `kill <pid>`. It writes nothing destructive,
so killing mid-sleep is safe — just relaunch later.

## What's in the repo

| file | purpose |
|------|---------|
| `watch-claude-ratelimit.py` | the watcher (Python, stdlib-only) |
| `watch-claude-ratelimit.sh` | thin bash wrapper that execs the .py |
| `install.sh` | prerequisite checks + suggested launch command (optional `--start` to launch immediately) |
| `SKILL.md` | self-contained instructions for Claude to read and act on |
| `README.md` | this file (human-readable overview) |

## Why structured-log tailing instead of TUI scraping

Earlier iterations tried `tmux capture-pane | grep`, but that approach broke
every time Claude Code changed the rate-limit message wording (e.g. adding
emoji, switching from `resets 1pm` to `Resets at 1pm`, line-wrapping for
narrow terminals, etc.). The JSONL log is the same data the Claude Code
client itself reads — stable across cosmetic UI changes, and the timezone /
reset time arrive as structured fields rather than English prose.

## Limitations

- **tmux-only injection**: the watcher needs to type into your terminal.
  If you run Claude Code outside tmux (e.g. plain Terminal.app), there's
  no portable way to inject keystrokes from another process. AppleScript
  `tell Terminal to do script "continue"` works on macOS but is fragile.
- **No cancellation when sleeping long**: if you fix the rate-limit problem
  yourself (e.g. by upgrading your plan), the watcher will still send
  `continue` later. Restart Claude Code at any time — the worst case is a
  stray `continue` command that does nothing.
- **One workspace log at a time**: the watcher tails the most-recent JSONL log
  inside one Claude workspace directory. If you run multiple Claude Code
  workspaces simultaneously, run multiple watchers, each with its own
  `CLAUDE_WATCH_PROJECT_DIR`. Within one workspace, it can still choose among
  multiple live Claude tmux panes heuristically.

## License

MIT-equivalent — do whatever you like with it. No warranty, etc.
