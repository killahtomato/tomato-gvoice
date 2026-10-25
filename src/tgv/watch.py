"""Background watcher that notifies about new Google Voice messages."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from . import config, reader

_BIDI_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069\u200e\u200f\ufeff]")


def _clean(text: str | None) -> str:
    return _BIDI_RE.sub("", text or "").strip()


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


def _notify(message: dict, as_json: bool) -> None:
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
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", title, text], check=False)
    else:
        print(f"{title}\n  {text}", file=sys.stderr)


def watch(page: Page, *, interval: float, once: bool, as_json: bool) -> None:
    """Poll for new messages and notify about them until interrupted."""
    last_ts = _load_state()
    seeded = last_ts > 0
    print(
        f"Watching for new messages every {interval:g}s (Ctrl+C to stop).",
        file=sys.stderr,
    )

    while True:
        try:
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
                    _notify(m, as_json)
                if messages:
                    last_ts = max(m["_ts"] for m in messages)
                    _save_state(last_ts)
        except KeyboardInterrupt:
            break
        except PlaywrightError as exc:
            message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
            print(
                f"watch stopped: {message}\n"
                "If the session expired, run `tgv login` again.",
                file=sys.stderr,
            )
            break

        if once:
            break
        time.sleep(interval)
