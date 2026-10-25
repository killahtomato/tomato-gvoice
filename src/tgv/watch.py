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

from playwright.sync_api import Error as PlaywrightError

from . import config, reader, session

_BIDI_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069\u200e\u200f\ufeff]")

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


def _load_state() -> float:
    """Return the last-seen timestamp (epoch ms), or 0 if none."""
    try:
        data = json.loads(config.WATCH_STATE.read_text(encoding="utf-8"))
        return float(data.get("last_timestamp", 0))
    except (OSError, ValueError):
        return 0.0


def _save_state(ts: float) -> None:
    config.WATCH_STATE.parent.mkdir(parents=True, exist_ok=True)
    config.WATCH_STATE.write_text(
        json.dumps({"last_timestamp": ts, "updated": time.time()}),
        encoding="utf-8",
    )


def _send_notification(
    args: list[str],
    thread: int | None,
    terminal: str | None,
) -> None:
    """Run a blocking notify-send and open the thread if its action is chosen."""
    proc = subprocess.run(args, capture_output=True, text=True)
    # Daemons without the exec-argv hint report the "default" action instead.
    if proc.stdout.strip() == "default":
        _open_terminal(thread, terminal)


def _notify(message: dict, as_json: bool, terminal: str | None) -> None:
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

    # Two click paths, whichever the daemon supports:
    #  - Omarchy/quickshell runs the `omarchy-exec-argv` hint directly.
    #  - Standard daemons invoke the libnotify "default" action (sender is live).
    cmd = _terminal_command(terminal, thread) if (terminal and thread) else None
    args = ["notify-send", "--wait", "-t", "0", "-a", "tgv"]
    if cmd:
        args += ["-h", f"string:omarchy-exec-argv:{json.dumps(cmd)}"]
    args += ["-A", "default=Open in terminal", title, text]
    threading.Thread(
        target=_send_notification,
        args=(args, thread, terminal),
        daemon=True,
    ).start()


def watch(
    *,
    interval: float,
    once: bool,
    as_json: bool,
    headless: bool = True,
    terminal: str | None = None,
) -> None:
    """Poll for new messages and notify about them until interrupted.

    The browser is launched and closed for each poll so the profile is not held
    open, letting other tgv commands run between checks. Clicking a
    notification opens the thread in a terminal.
    """
    last_ts = _load_state()
    seeded = last_ts > 0
    terminal = resolve_terminal(terminal)
    print(
        f"Watching for new messages every {interval:g}s (Ctrl+C to stop).",
        file=sys.stderr,
    )

    while True:
        try:
            with session.browser(headless=headless) as page:
                now_ms = time.time() * 1000
                if not seeded:
                    # Seed to the newest message so old ones don't spam us.
                    messages = reader.messages_since(page, since_ms=0, now_ms=now_ms)
                    last_ts = max((m["_ts"] for m in messages), default=now_ms)
                    _save_state(last_ts)
                    seeded = True
                    print(f"Seeded at {last_ts:.0f}; watching from now.", file=sys.stderr)
                else:
                    since_ms = max(1.0, now_ms - last_ts)
                    messages = reader.messages_since(page, since_ms=since_ms, now_ms=now_ms)
                    for m in messages:
                        _notify(m, as_json, terminal)
                    if messages:
                        last_ts = max(m["_ts"] for m in messages)
                        _save_state(last_ts)
        except KeyboardInterrupt:
            break
        except session.AuthenticationRequiredError:
            # Don't exit: keep polling so the watcher recovers after a re-login.
            print(
                "Session expired. Run `tgv login` again; will keep retrying.",
                file=sys.stderr,
            )
        except PlaywrightError as exc:
            message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
            print(f"watch stopped: {message}", file=sys.stderr)
            break

        if once:
            break
        time.sleep(interval)
