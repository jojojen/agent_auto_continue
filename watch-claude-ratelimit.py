#!/usr/bin/env python3
"""
Auto-continue Claude Code after a usage-limit cooldown.

The script tails Claude Code's JSONL session log under
``~/.claude/projects/<workspace>/*.jsonl``, watches for structured rate-limit
events (``"error":"rate_limit"`` + ``apiErrorStatus 429``), parses the reset
time from the message body, sleeps until then + a buffer, and then sends
``continue`` to the configured tmux pane.

The JSONL detection replaces older ``tmux capture-pane | grep`` approaches
that break whenever Claude Code's TUI wording changes. The reset-time parser
handles every variant observed so far: am/pm, 24-hour ``HH:MM``, relative
``in 2h`` / ``in 90m``, and Chinese ``重置時間 14:30`` / ``於 09:00 重置``.
Timezone is read from the trailing ``(Region/City)`` tag in the message and
falls back to ``CLAUDE_WATCH_DEFAULT_TZ`` (default ``Asia/Tokyo``).

Environment overrides (all optional):
    CLAUDE_WATCH_PANE         tmux target (default ``claude:0.0``)
    CLAUDE_WATCH_PROJECT_DIR  Claude Code projects dir for THIS workspace.
                              When unset, auto-detected: the directory under
                              ``~/.claude/projects/`` that owns the most-recent
                              ``.jsonl`` file.
    CLAUDE_WATCH_BUFFER       Extra seconds past reset before sending continue
                              (default ``10``).
    CLAUDE_WATCH_DEBOUNCE     Suppress duplicate triggers within N seconds
                              (default ``90``).
    CLAUDE_WATCH_DEFAULT_TZ   Fallback timezone when the rate-limit message
                              lacks an explicit ``(Region/City)`` tag.
    CLAUDE_WATCH_LOG          Debug log path. Default:
                              ``~/.cache/agent-auto-continue/watch.log``.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from pytz import timezone as ZoneInfo  # type: ignore[assignment]


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_DEFAULT_TZ_NAME = os.environ.get("CLAUDE_WATCH_DEFAULT_TZ", "Asia/Tokyo")
DEFAULT_TZ = ZoneInfo(_DEFAULT_TZ_NAME)
PANE_FALLBACK = "claude:0.0"
PANE = os.environ.get("CLAUDE_WATCH_PANE", "")  # empty => auto-detect at send time
PROJECTS_ROOT = Path.home() / ".claude" / "projects"
BUFFER_SECS = int(os.environ.get("CLAUDE_WATCH_BUFFER", "10"))
DEBOUNCE_SECS = int(os.environ.get("CLAUDE_WATCH_DEBOUNCE", "90"))
# After sending 'continue', ignore further rate-limit hits for this long. The
# debounce above is keyed to DETECTION time, but a trigger sleeps for hours
# before sending; by the time the just-fired banner is re-read a second later,
# the debounce has expired, so without this cooldown the stale re-read parses
# its just-passed reset as TOMORROW and locks the watcher asleep ~24h.
POST_SEND_COOLDOWN_SECS = int(os.environ.get("CLAUDE_WATCH_POST_SEND_COOLDOWN", "300"))
# Sanity cap on a single wait. This must be ABOVE the longest legitimate reset:
# Claude has a weekly usage limit whose reset can be several DAYS out, and the
# absolute-date parser ("resets Jun 6 at 7am") exists precisely to handle that.
# An earlier 6h cap here was a bug: it assumed "a reset is never more than a few
# hours out" and so clamped every genuine multi-day/weekly reset down to a 300s
# re-check, making the watcher fire `continue` while still rate-limited, in a loop
# — i.e. unusable for multi-day limits. The 24h re-read lockout is NOT handled by
# this cap; it is handled precisely by POST_SEND_COOLDOWN_SECS above (which
# suppresses the just-fired banner). This cap only catches a truly absurd
# mis-parse (e.g. a cross-year rollover ~365d out). 8 days = weekly limit + margin.
MAX_WAIT_SECS = int(os.environ.get("CLAUDE_WATCH_MAX_WAIT", str(8 * 86400)))
# Never block on a single opaque time.sleep(): a mis-parsed far-future reset must
# not be able to put the watcher to sleep for hours/a day (the "weird sleep"
# failure that recurred). Every wait is split into <= WAIT_CHUNK_SECS naps and
# hard-capped by MAX_WAIT_SECS, so the process stays observable, interruptible,
# and can never sleep past the cap regardless of any parse bug.
WAIT_CHUNK_SECS = int(os.environ.get("CLAUDE_WATCH_WAIT_CHUNK", "60"))
FALLBACK_SLEEP_SECS = 300
# How often to log "still waiting" progress during a long (multi-day) wait so the
# watcher is visibly alive rather than an opaque blackout.
PROGRESS_LOG_SECS = int(os.environ.get("CLAUDE_WATCH_PROGRESS_LOG", "1800"))
DEFAULT_LOG_PATH = Path.home() / ".cache" / "agent-auto-continue" / "watch.log"
LOG_PATH = Path(os.environ.get("CLAUDE_WATCH_LOG", str(DEFAULT_LOG_PATH)))
SESSION_POLL_SECS = 60
PANE_CAPTURE_LINES = 120
RATE_LIMIT_PANE_MARKERS = (
    "you've hit your session limit",
    "you've hit your weekly limit",
    "you've hit your limit",
    "usage limit",
    "/upgrade to increase your usage limit",
)
EMPTY_SESSION_MARKERS = (
    "there's nothing in progress to continue",
    "there's no prior context to continue",
    "there's still no task in progress to continue",
    "i don't have anything to continue",
    "nothing to continue here",
    "conversation was just cleared",
    "what would you like to work on?",
)


def log(msg: str) -> None:
    stamped = f"[{_dt.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(stamped, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(stamped + "\n")
    except Exception:
        pass


def resolve_project_dir() -> Path | None:
    """Honor the env override; otherwise auto-detect the workspace whose
    .jsonl files were touched most recently. This makes the script usable
    against any Claude Code workspace without manual configuration."""
    override = os.environ.get("CLAUDE_WATCH_PROJECT_DIR")
    if override:
        path = Path(override).expanduser()
        return path if path.is_dir() else None
    if not PROJECTS_ROOT.is_dir():
        return None
    best: tuple[float, Path] | None = None
    for child in PROJECTS_ROOT.iterdir():
        if not child.is_dir():
            continue
        jsonls = list(child.glob("*.jsonl"))
        if not jsonls:
            continue
        mtime = max(p.stat().st_mtime for p in jsonls)
        if best is None or mtime > best[0]:
            best = (mtime, child)
    return best[1] if best else None


def latest_session_log(project_dir: Path) -> Path | None:
    candidates = sorted(
        project_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def parse_reset_at(
    text: str,
    now_utc: _dt.datetime,
    *,
    roll_forward_time_only: bool = True,
) -> _dt.datetime | None:
    """Return absolute reset time as a UTC-aware datetime, or None on failure."""
    tz_match = re.search(r"\(([A-Za-z]+/[A-Za-z_]+)\)", text)
    if tz_match:
        try:
            tz = ZoneInfo(tz_match.group(1))
        except Exception:
            tz = DEFAULT_TZ
    else:
        tz = DEFAULT_TZ
    now_local = now_utc.astimezone(tz)
    lowered = text.lower()

    # "resets May 31 at 7am" / "resets on Jun 3 at 7:30 am" / "resets Dec 1 at 14:30"
    # (absolute date — the reset may be several days out, so we parse month+day,
    # not just the time-of-day. This is the format the live TUI actually emits.)
    m = re.search(
        r"resets?\s+(?:on\s+)?([a-z]{3,9})\.?\s+(\d{1,2})\s+at\s+"
        r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)?",
        lowered,
    )
    if m and m.group(1)[:3] in _MONTHS:
        mon = _MONTHS[m.group(1)[:3]]
        day = int(m.group(2))
        h = int(m.group(3))
        mi = int(m.group(4)) if m.group(4) else 0
        ap = m.group(5)
        if ap == "pm" and h != 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        try:
            target = now_local.replace(
                month=mon, day=day, hour=h, minute=mi, second=0, microsecond=0
            )
            # Cross-year: a month/day already behind us means next year.
            if target < now_local - _dt.timedelta(days=1):
                target = target.replace(year=target.year + 1)
            return target.astimezone(_dt.timezone.utc)
        except ValueError:
            pass

    # "resets in 2h" / "resets in 90m"
    m = re.search(r"resets?\s+in\s+(\d+)\s*([hm])", lowered)
    if m:
        delta = int(m.group(1)) * (3600 if m.group(2) == "h" else 60)
        return now_utc + _dt.timedelta(seconds=delta)

    # "resets 14:30" (24-hour with colon, no am/pm)
    m = re.search(r"resets?\s+(\d{1,2}):(\d{2})(?!\s*[ap]m)", lowered)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        target = now_local.replace(hour=h, minute=mi, second=0, microsecond=0)
        if roll_forward_time_only and target <= now_local:
            target += _dt.timedelta(days=1)
        return target.astimezone(_dt.timezone.utc)

    # "resets 1am" / "resets 1:30am" / "resets 1 am"
    m = re.search(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)", lowered)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2)) if m.group(2) else 0
        ap = m.group(3)
        if ap == "pm" and h != 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        target = now_local.replace(hour=h, minute=mi, second=0, microsecond=0)
        if roll_forward_time_only and target <= now_local:
            target += _dt.timedelta(days=1)
        return target.astimezone(_dt.timezone.utc)

    # Chinese: "重置時間 14:30" / "於 14:30 重置"
    m = re.search(r"(?:重置時間|於)\s*(\d{1,2}):(\d{2})", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        target = now_local.replace(hour=h, minute=mi, second=0, microsecond=0)
        if roll_forward_time_only and target <= now_local:
            target += _dt.timedelta(days=1)
        return target.astimezone(_dt.timezone.utc)

    return None


def extract_rate_limit_text(line: str) -> str | None:
    """Quick string filter then JSON parse; return message text on hit."""
    if (
        '"rate_limit"' not in line
        and '"apiErrorStatus":429' not in line
        and '"apiErrorStatus": 429' not in line
    ):
        return None
    try:
        obj = json.loads(line)
    except Exception:
        return None
    if obj.get("error") != "rate_limit" and obj.get("apiErrorStatus") != 429:
        return None
    content = obj.get("message", {}).get("content", [])
    parts: list[str] = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text", ""))
    text = " ".join(parts).strip()
    return text or "(rate_limit event without text body)"


def _claude_panes_by_activity() -> list[tuple[str, int]]:
    """[(pane_id, window_activity_epoch), ...] for panes running ``claude``,
    most-recently-active first. ``window_activity`` is a tmux epoch that bumps
    whenever the pane prints output (incl. the rate-limit banner), so it ranks
    the actively-driven session above long-idle ones — and claude never holds its
    jsonl open, so this activity proxy is the only reliable pane↔session link."""
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_id}\t#{window_activity}\t#{pane_current_command}"],
            check=False, timeout=10, capture_output=True, text=True,
        ).stdout
    except Exception as exc:
        log(f"resolve_pane: tmux list-panes failed ({exc})")
        return []
    rows: list[tuple[str, int]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].strip() == "claude":
            act = int(parts[1]) if parts[1].strip().isdigit() else 0
            rows.append((parts[0].strip(), act))
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows


def _pane_runs_claude(pane_id: str) -> bool:
    if not pane_id:
        return False
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_current_command}"],
            check=False,
            timeout=10,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except Exception as exc:
        log(f"resolve_pane: could not inspect {pane_id} ({exc})")
        return False
    return out == "claude"


def _capture_pane_tail(pane_id: str, *, lines: int = PANE_CAPTURE_LINES) -> str:
    try:
        return subprocess.run(
            ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", f"-{lines}"],
            check=False,
            timeout=10,
            capture_output=True,
            text=True,
        ).stdout
    except Exception as exc:
        log(f"resolve_pane: could not capture {pane_id} ({exc})")
        return ""


def _pane_text_has_any(pane_text: str, markers: tuple[str, ...]) -> bool:
    lowered = pane_text.lower()
    return any(marker in lowered for marker in markers)


def _extract_visible_rate_limit_banner(pane_text: str) -> str | None:
    for line in reversed(pane_text.splitlines()):
        stripped = line.strip()
        if (
            stripped
            and _pane_text_has_any(stripped, RATE_LIMIT_PANE_MARKERS)
            and "reset" in stripped.lower()
        ):
            return stripped
    return None


def _select_best_claude_pane() -> tuple[str | None, str]:
    panes = _claude_panes_by_activity()
    if not panes:
        return None, "no-claude-panes"

    pane_tails = {
        pane_id: _capture_pane_tail(pane_id)
        for pane_id, _activity in panes
    }

    rate_limited = [
        (pane_id, activity)
        for pane_id, activity in panes
        if _pane_text_has_any(pane_tails.get(pane_id, ""), RATE_LIMIT_PANE_MARKERS)
    ]
    if rate_limited:
        return rate_limited[0][0], "rate-limit-banner-visible"

    non_empty = [
        (pane_id, activity)
        for pane_id, activity in panes
        if not _pane_text_has_any(pane_tails.get(pane_id, ""), EMPTY_SESSION_MARKERS)
    ]
    if non_empty:
        return non_empty[0][0], "most-active-non-empty-pane"

    return panes[0][0], "fallback-most-active-pane"


def recover_visible_rate_limit_state() -> bool:
    pane_id, reason = _select_best_claude_pane()
    if not pane_id or reason != "rate-limit-banner-visible":
        return False
    pane_text = _capture_pane_tail(pane_id)
    banner = _extract_visible_rate_limit_banner(pane_text)
    if not banner:
        return False

    now_utc = _dt.datetime.now(tz=_dt.timezone.utc)
    reset_utc = parse_reset_at(
        banner,
        now_utc,
        roll_forward_time_only=False,
    )
    if reset_utc is None:
        log(f"recover_visible_rate_limit_state: could not parse banner {banner!r}")
        return False

    raw_wait = max(0, int((reset_utc - now_utc).total_seconds())) + BUFFER_SECS
    if raw_wait > MAX_WAIT_SECS:
        log(
            f"recover_visible_rate_limit_state: computed wait {raw_wait}s exceeds "
            f"sanity cap {MAX_WAIT_SECS}s for banner {banner!r}; skipping recovery"
        )
        return False

    log(
        f"recover_visible_rate_limit_state: recovered {pane_id} from visible banner "
        f"{banner!r}; waiting {raw_wait}s before continue"
    )
    sleep_in_chunks(raw_wait)
    send_continue()
    return True


def resolve_pane(current=None) -> str:
    """Pick the tmux pane to send ``continue`` to. With several ``claude`` panes
    open, the old "first claude pane" heuristic silently hit the wrong one (an idle
    ``%0`` while the working session ran in ``%7``). Instead choose the MOST-
    RECENTLY-ACTIVE claude pane: that is the session being actively driven — the
    same one whose newest-mtime jsonl we tail and which therefore hits the limit.
    A working-then-idle pane still outranks a long-idle one, so this holds through
    the rate-limit cooldown until send time.

    Order: (1) CLAUDE_WATCH_PANE if set; (2) among live Claude panes, prefer one
    whose visible content still shows a rate-limit banner; (3) otherwise the most-
    recently-active Claude pane that is NOT an empty "nothing to continue" session;
    (4) fallback to the most-recently-active Claude pane; (5) ``claude:0.0``."""
    if PANE:
        return PANE
    pane_id, reason = _select_best_claude_pane()
    if pane_id:
        log(f"resolve_pane: selected {pane_id} ({reason})")
        return pane_id
    log(f"resolve_pane: no pane running 'claude'; using {PANE_FALLBACK}")
    return PANE_FALLBACK


def sleep_in_chunks(total_secs: int) -> None:
    """Wait total_secs, but in <= WAIT_CHUNK_SECS naps and hard-capped by
    MAX_WAIT_SECS. Long (multi-day/weekly) waits are fine and supported; chunking
    just keeps the wait interruptible and logs progress so it's never an opaque
    blackout. The cap only ever trims a truly absurd mis-parse."""
    capped = max(0, min(int(total_secs), MAX_WAIT_SECS))
    deadline = time.monotonic() + capped
    next_progress = time.monotonic() + PROGRESS_LOG_SECS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if time.monotonic() >= next_progress and remaining > PROGRESS_LOG_SECS:
            log(f"...still waiting, ~{int(remaining)}s ({remaining/3600:.1f}h) left")
            next_progress = time.monotonic() + PROGRESS_LOG_SECS
        time.sleep(min(remaining, WAIT_CHUNK_SECS))


def send_continue(current=None) -> None:
    target = resolve_pane(current)
    log(f"Sending 'continue' to tmux pane {target}")
    try:
        # Send text and Enter as SEPARATE events with a pause between. Claude
        # Code's Ink TUI debounces input; a combined "continue" + "Enter" often
        # lands the Enter before the text registers, leaving it unsubmitted.
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "-l", "continue"],
            check=False, timeout=10,
        )
        time.sleep(0.8)
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "Enter"],
            check=False, timeout=10,
        )
    except Exception as exc:
        log(f"tmux send-keys failed: {exc}")


class TailFollower:
    """tail -n 0 -F <path>; yield lines; can be stopped externally."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.proc: subprocess.Popen[str] | None = None
        self._stop = False

    def start(self) -> None:
        self.proc = subprocess.Popen(
            ["tail", "-n", "0", "-F", str(self.path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def stop(self) -> None:
        self._stop = True
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass

    def lines(self):
        assert self.proc is not None and self.proc.stdout is not None
        for raw in self.proc.stdout:
            if self._stop:
                break
            yield raw


def watch_loop() -> int:
    project_dir = resolve_project_dir()
    if project_dir is None:
        log(
            f"No Claude Code workspace found. Looked under {PROJECTS_ROOT}; "
            f"set CLAUDE_WATCH_PROJECT_DIR to point to the right one."
        )
        return 1
    current = latest_session_log(project_dir)
    if current is None:
        log(f"No .jsonl under {project_dir}; aborting.")
        return 1
    log(f"Workspace: {project_dir}")
    log(
        f"Watching {current} (tmux pane: {PANE or 'auto:'+resolve_pane(current)}, "
        f"tz fallback: {_DEFAULT_TZ_NAME})"
    )
    follower = TailFollower(current)
    follower.start()

    # Background thread: every SESSION_POLL_SECS seconds, see whether a newer
    # session log exists and switch over. Survives Claude Code restarts.
    def session_poller() -> None:
        nonlocal current, follower
        while True:
            time.sleep(SESSION_POLL_SECS)
            latest = latest_session_log(project_dir)
            if latest is not None and latest != current:
                log(f"New session log detected: {latest} — switching tail")
                old = follower
                follower = TailFollower(latest)
                follower.start()
                current = latest
                try:
                    old.stop()
                except Exception:
                    pass

    poller = threading.Thread(target=session_poller, daemon=True)
    poller.start()

    last_triggered = 0.0
    last_send_mono = -float("inf")
    if recover_visible_rate_limit_state():
        last_send_mono = time.monotonic()
    while True:
        try:
            for raw in follower.lines():
                text = extract_rate_limit_text(raw)
                if text is None:
                    continue

                now_mono = time.monotonic()
                if now_mono - last_send_mono < POST_SEND_COOLDOWN_SECS:
                    log(f"Suppressed (post-send cooldown "
                        f"{int(now_mono - last_send_mono)}s): {text!r}")
                    continue
                if now_mono - last_triggered < DEBOUNCE_SECS:
                    log(f"Suppressed (debounce): {text!r}")
                    continue
                last_triggered = now_mono

                log(f"Rate-limit detected: {text!r}")
                now_utc = _dt.datetime.now(tz=_dt.timezone.utc)
                reset_utc = parse_reset_at(text, now_utc)
                if reset_utc is None:
                    log(f"Could not parse reset time; fallback wait {FALLBACK_SLEEP_SECS}s")
                    wait_secs = FALLBACK_SLEEP_SECS
                else:
                    raw_wait = max(0, int((reset_utc - now_utc).total_seconds())) + BUFFER_SECS
                    if raw_wait > MAX_WAIT_SECS:
                        log(f"Computed wait {raw_wait}s exceeds sanity cap {MAX_WAIT_SECS}s "
                            f"(~{MAX_WAIT_SECS/86400:.0f}d) — absurd, likely a mis-parse of "
                            f"{text!r}; re-checking in {FALLBACK_SLEEP_SECS}s instead")
                        wait_secs = FALLBACK_SLEEP_SECS
                    else:
                        log(f"Reset at {reset_utc.isoformat()}; waiting {raw_wait}s "
                            f"(~{raw_wait/3600:.1f}h, in <= {WAIT_CHUNK_SECS}s chunks)")
                        wait_secs = raw_wait
                sleep_in_chunks(wait_secs)
                send_continue(current)
                last_send_mono = time.monotonic()
        except Exception as exc:
            log(f"Tail iterator failed ({exc}); restarting tail in 5s")
            try:
                follower.stop()
            except Exception:
                pass
            time.sleep(5)
            latest = latest_session_log(project_dir) or current
            follower = TailFollower(latest)
            follower.start()
            current = latest


def main() -> int:
    log("agent_auto_continue watcher starting")
    try:
        return watch_loop()
    except KeyboardInterrupt:
        log("interrupted, exiting")
        return 0


if __name__ == "__main__":
    sys.exit(main())
