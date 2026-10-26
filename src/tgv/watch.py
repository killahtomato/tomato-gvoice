"""Background watcher that notifies about new Google Voice messages."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from . import config, reader, session

_BIDI_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069\u200e\u200f\ufeff]")
_POLL_WINDOW_MS = 5 * 60 * 1000  # look back 5 minutes on every poll
_LOG_PATH = Path(os.environ.get("TGV_WATCH_LOG", config.BASE_DIR / "watch.log"))

def _log() -> object:
    """Return the debug log stream (lazy, opened on first use)."""
    stream = getattr(_send_notification, "_log", None)
    if stream is None:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stream = open(_LOG_PATH, "a", encoding="utf-8")
        setattr(_send_notification, "_log", stream)
    return stream

TERMINAL_CANDIDATES = (
    os.environ.get("TGV_TERMINAL"),
    "foot",
    "kitty",
    "alacritty",
    "ghostty",
    "wezterm",
    "konsole",
)


def _clean(text: str | None) -> str:
    return _BIDI_RE.sub("", text or "").strip()


def resolve_terminal(explicit: str | None = None) -> str | None:
    """Return a terminal executable to open clicked notifications in."""
    candidates = (explicit,) if explicit else TERMINAL_CANDIDATES
    for candidate in candidates:
        if not candidate:
            continue
        path = shutil.which(candidate) or (
            candidate if os.path.exists(candidate) else None
        )
        if path:
            return path
    return None


def _terminal_command(terminal: str, thread: int) -> list[str]:
    name = os.path.basename(terminal)
    tgv = str(config.LAUNCHER)
    if name == "foot":
        return [terminal, "--hold", tgv, "read", str(thread)]
    if name == "alacritty":
        return [terminal, "--hold", "-e", tgv, "read", str(thread)]
    if name == "kitty":
        return [terminal, tgv, "read", str(thread)]
    if name == "wezterm":
        return [terminal, "start", "--", tgv, "read", str(thread)]
    return [terminal, tgv, "read", str(thread)]


def _open_terminal(thread: int | None, terminal: str | None) -> None:
    if not thread or not terminal:
        return
    try:
        subprocess.Popen(
            _terminal_command(terminal, thread),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def _load_state() -> set[str]:
    """Return the set of message IDs we've already notified about."""
    try:
        data = json.loads(config.WATCH_STATE.read_text(encoding="utf-8"))
        return set(data.get("notified_ids", []))
    except (OSError, ValueError):
        return set()


def _save_state(notified: set[str]) -> None:
    # Keep the set bounded so the file doesn't grow forever.
    recent = sorted(notified)[-1000:]
    config.WATCH_STATE.parent.mkdir(parents=True, exist_ok=True)
    config.WATCH_STATE.write_text(
        json.dumps({"notified_ids": recent, "updated": time.time()}),
        encoding="utf-8",
    )


def _send_notification(
    args: list[str],
    thread: int | None,
    terminal: str | None,
    click_cmd: list[str] | None = None,
) -> None:
    """Run a blocking notify-send and run the click action if chosen."""
    proc = subprocess.run(args, capture_output=True, text=True)
    action = proc.stdout.strip()
    if action == "default" and click_cmd:
        log = _log()
        print(f"[tgv-watch] notify-send action=default, running click_cmd={click_cmd}", file=log)
        log.flush()
        try:
            subprocess.Popen(click_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            print(f"[tgv-watch] click_cmd failed: {e}", file=log)
            log.flush()
    elif action == "default":
        log = _log()
        print(f"[tgv-watch] notify-send action=default, no click_cmd — opening terminal", file=log)
        log.flush()
        _open_terminal(thread, terminal)
    else:
        log = _log()
        print(f"[tgv-watch] notify-send action={action!r} (no click)", file=log)
        log.flush()


def _notify(
    message: dict,
    as_json: bool,
    terminal: str | None,
    click_cmd: list[str] | None = None,
) -> None:
    sender = _clean(message.get("participant")) or "unknown"
    text = _clean(message.get("text"))
    if as_json:
        print(json.dumps({
            "sender": sender,
            "direction": message.get("direction"),
            "timestamp": message.get("timestamp"),
            "text": text,
        }))
        return
    title = f"Google Voice — {sender}"
    thread = message.get("thread")
    if not shutil.which("notify-send"):
        print(f"{title}\n  {text}", file=sys.stderr)
        return

    # quickshell handles notification clicks via the omarchy-exec-argv hint,
    # so we do NOT use --wait (it blocks indefinitely with quickshell's daemon).
    args = ["notify-send", "-t", "0", "-a", "tgv"]
    if click_cmd:
        args += ["-h", f"string:omarchy-exec-argv:{json.dumps(click_cmd)}"]
    elif terminal and thread:
        args += ["-h", f"string:omarchy-exec-argv:{json.dumps(_terminal_command(terminal, thread))}"]
    args += ["-A", "default=Open TUI", title, text]
    subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _is_profile_locked(exc: BaseException) -> bool:
    """True when the browser profile is in use by another process."""
    text = str(exc).lower()
    return "processsingleton" in text or "profile" in text and "lock" in text


def _daemonize() -> None:
    """Double-fork into a background process so the watcher survives terminal close."""
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")


def watch(
    *,
    interval: float,
    once: bool,
    as_json: bool,
    headless: bool = True,
    terminal: str | None = None,
    click_cmd: list[str] | None = None,
    background: bool = False,
) -> None:
    """Poll for new messages and notify about them until interrupted."""
    # Default: clicking a notification opens the interactive TUI.
    if click_cmd is None:
        click_cmd = [str(config.OPEN_TUI)]
    """Poll for new messages and notify about them until interrupted.

    Every poll looks back a fixed window (5 minutes) so messages aren't missed
    due to poll timing. Notifications are deduped by message ID, persisted
    across restarts. Clicking a notification runs ``click_cmd`` (default: open
    the thread in a terminal).
    """
    if background:
        _daemonize()
    terminal = resolve_terminal(terminal)
    notified = _load_state()
    if not background:
        print(
            f"Watching for new messages every {interval:g}s (Ctrl+C to stop).",
            file=sys.stderr,
        )

    while True:
        now_ms = time.time() * 1000
        # Skip while the interactive TUI is running so we never contend for
        # the browser profile.
        if config.TUI_LOCK_FILE.exists():
            print("[watch] TUI active; skipping poll.", flush=True)
            if once:
                break
            time.sleep(interval)
            continue
        try:
            with session.browser(headless=headless) as page:
                messages = reader.messages_since(
                    page, since_ms=_POLL_WINDOW_MS, now_ms=now_ms
                )
                new_messages = []
                for m in messages:
                    msg_id = m.get("id")
                    if msg_id and msg_id in notified:
                        continue
                    new_messages.append(m)
                    if msg_id:
                        notified.add(msg_id)

                for m in new_messages:
                    _notify(m, as_json, terminal, click_cmd=click_cmd)
                if new_messages:
                    print(
                        f"Notified about {len(new_messages)} message(s).",
                        file=sys.stderr,
                    )

                if new_messages:
                    _save_state(notified)
        except KeyboardInterrupt:
            break
        except session.AuthenticationRequiredError:
            # Don't exit: keep polling so the watcher recovers after a re-login.
            print(
                "Session expired. Run `tgv login` again; will keep retrying.",
                file=sys.stderr,
            )
        except PlaywrightError as exc:
            if _is_profile_locked(exc):
                # The profile is in use by the TUI or another tgv process;
                # skip this poll and try again after the interval.
                print(
                    f"Profile in use ({str(exc).splitlines()[0][:60]}); skipping.",
                    file=sys.stderr,
                )
            else:
                message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
                print(f"watch stopped: {message}", file=sys.stderr)
                break
        except Exception as exc:  # noqa: BLE001
            print(f"watch error: {type(exc).__name__}: {exc}", file=sys.stderr)
            break

        if once:
            break
        time.sleep(interval)
