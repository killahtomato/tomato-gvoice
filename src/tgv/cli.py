"""Argparse entry point for the tgv (Tomato-gVoice) CLI."""

from __future__ import annotations

import argparse
import json
import re
import sys

from playwright.sync_api import Error as PlaywrightError

from . import contacts, reader, sender, session, watch
from .text_utils import clean
from .session import AuthenticationRequiredError

_DURATION_RE = re.compile(r"^(\d+)([smhdw])$")
_UNIT_MS = {
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
    "w": 604_800_000,
}

def parse_duration(value: str) -> int:
    match = _DURATION_RE.match(value.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError(
            "duration must look like 30m, 24h, 7d or 2w"
        )
    amount, unit = int(match.group(1)), match.group(2)
    if amount < 1:
        raise argparse.ArgumentTypeError("duration must be at least one unit")
    if amount * _UNIT_MS[unit] > 365 * 86_400_000:
        raise argparse.ArgumentTypeError("duration must be at most 365 days")
    return amount * _UNIT_MS[unit]


def _emit(data, as_json: bool, human: callable) -> None:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        human(data)


def _print_threads(items: list[dict]) -> None:
    if not items:
        print("No message threads.")
        return
    for i, item in enumerate(items, 1):
        marker = "*" if item.get("unread") else " "
        who = clean(item.get("participant")) or "unknown"
        ts = clean(item.get("timestamp")) or ""
        print(f"{i:>3}{marker} {who}  {ts}")
        print(f"      {clean(item.get('preview')) or ''}")


def _print_thread(thread: dict) -> None:
    title = clean(thread.get("title")) or clean(thread.get("participant")) or "(unknown)"
    print(f"{title}  ({thread.get('totalMessages', 0)} messages)")
    for msg in thread.get("messages", []):
        direction = ">>" if msg.get("direction") == "outgoing" else "<<"
        ts = clean(msg.get("timestamp")) or "?"
        print(f"  [{ts}] {direction} {clean(msg.get('text')) or ''}")


def _print_recent(result: dict) -> None:
    if not result["messages"]:
        print("No messages in that window.")
        return
    for msg in result["messages"]:
        direction = ">>" if msg["direction"] == "outgoing" else "<<"
        print(
            f"[{msg['timestamp']}] {direction} {clean(msg.get('participant')) or 'unknown'}: "
            f"{clean(msg.get('text')) or ''}"
        )


def _print_contacts(items: list[dict]) -> None:
    if not items:
        print("No contacts found.")
        return
    for item in items:
        numbers = ", ".join(item.get("numbers", []))
        print(f"{item.get('name', '?')}  {numbers}")


def _print_search(messages: list[dict]) -> None:
    if not messages:
        print("No messages match.")
        return
    for msg in messages:
        direction = ">>" if msg.get("direction") == "outgoing" else "<<"
        print(
            f"[{msg.get('timestamp')}] {direction} "
            f"{clean(msg.get('participant')) or 'unknown'}: {clean(msg.get('text')) or ''}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tgv",
        description="Send and read Google Voice SMS from the terminal.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="run the browser in a visible window instead of headless",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="sign in interactively (one-time)")

    status = sub.add_parser("status", help="check the saved session")
    status.add_argument("--json", action="store_true")

    threads = sub.add_parser("threads", help="list conversation previews")
    threads.add_argument("-n", "--limit", type=int, default=20)
    threads.add_argument("--json", action="store_true")

    read = sub.add_parser("read", help="read one thread by index or preview text")
    read.add_argument("ref")
    read.add_argument("-n", "--limit", type=int, default=100)
    read.add_argument("--json", action="store_true")

    recent = sub.add_parser("recent", help="list messages in a recent time window")
    recent.add_argument("duration", type=parse_duration)
    recent.add_argument("--incoming", action="store_true")
    recent.add_argument("-n", "--limit", type=int, default=100)
    recent.add_argument("--json", action="store_true")

    send = sub.add_parser(
        "send", help="send a text message to a phone number or contact name"
    )
    send.add_argument("recipient")
    send.add_argument("text")
    send.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve the recipient and show where it would go without sending",
    )
    send.add_argument("--refresh", action="store_true", help="re-fetch contacts first")
    send.add_argument("--json", action="store_true")

    list_contacts = sub.add_parser("contacts", help="list or search known contacts")
    list_contacts.add_argument("query", nargs="?", help="name to search for")
    list_contacts.add_argument(
        "--refresh", action="store_true", help="re-fetch instead of using the cache"
    )
    list_contacts.add_argument("--json", action="store_true")

    search = sub.add_parser("search", help="search messages by text")
    search.add_argument("query", help="text to search for")
    search.add_argument("--thread", help="limit results to a contact/thread name")
    search.add_argument("-n", "--limit", type=int, default=100)
    search.add_argument("--json", action="store_true")

    watch_cmd = sub.add_parser(
        "watch", help="notify about new messages until interrupted"
    )
    watch_cmd.add_argument(
        "--interval", type=float, default=30.0, help="seconds between checks"
    )
    watch_cmd.add_argument("--once", action="store_true", help="check once and exit")
    watch_cmd.add_argument(
        "--json", action="store_true", help="print notifications as JSON"
    )
    watch_cmd.add_argument(
        "--terminal",
        help="terminal to open clicked notifications in (default: auto-detect)",
    )
    watch_cmd.add_argument(
        "--background",
        action="store_true",
        help="detach into the background (survives terminal close)",
    )
    watch_cmd.add_argument(
        "--status", action="store_true",
        help="show watch service status and exit"
    )
    watch_cmd.add_argument(
        "--click-cmd",
        nargs="+",
        default=None,
        help="command to run when a notification is clicked (default: open TUI in foot)",
    )

    tui_cmd = sub.add_parser("tui", help="open the interactive terminal UI")
    tui_cmd.add_argument(
        "--headed", action="store_true", default=False,
        help="run the browser in a visible window",
    )
    tui_cmd.add_argument(
        "--interval", type=float, default=30.0, help="seconds between polls"
    )
    tui_cmd.add_argument(
        "--verbose", action="store_true", default=False,
        help="print debug logs to stderr",
    )

    sub.add_parser(
        "tui-login", help="sign in to the TUI's separate browser profile (one-time)"
    )
    watchdog_cmd = sub.add_parser(
        "watchdog", help="manage the TUI lock file (run as a separate service)"
    )
    watchdog_cmd.add_argument(
        "--interval", type=float, default=5.0, help="seconds between polls (default: 5)"
    )
    watchdog_cmd.add_argument(
        "--status", action="store_true",
        help="show watchdog service status and exit"
    )
    watchdog_cmd.add_argument(
        "--background",
        action="store_true",
        help="detach into the background (survives terminal close)",
    )

    sub.add_parser(
        "kill", help="kill all tgv processes (watch, watchdog, tui, chromium)"
    )

    return parser


def handle_status(as_json: bool = False) -> None:
    """Print full system status for all tgv processes and services."""
    from . import watch as watch_module, config

    watch_s = watch_module.watch_status()
    watchdog_s = watch_module.watchdog_status()
    tui_s = watch_module._process_status("tgv-tui", ["tgv.cli tui"])
    worker_s = watch_module.tui_worker_status()
    chromium_main = watch_module._find_tgv_pids(
        f"user-data-dir={config.PROFILE_DIR}"
    )
    chromium_tui = watch_module._find_tgv_pids(
        f"user-data-dir={config.TUI_PROFILE_DIR}"
    )

    if as_json:
        print(json.dumps({
            "watch": watch_s,
            "watchdog": watchdog_s,
            "tui": tui_s,
            "browser_worker": worker_s,
            "chromium_main": len(chromium_main),
            "chromium_tui": len(chromium_tui),
        }, indent=2))
        return

    print("Tomato-gVoice System Status")
    print("=" * 40)
    print()

    # Watch
    w_mode = watch_s["mode"]
    if watch_s["running"]:
        w_uptime = f"uptime {watch_module.fmt_uptime(watch_s.get('uptime_seconds'))}"
    else:
        w_uptime = f"stopped {watch_module.fmt_uptime(watch_s.get('stopped_since_seconds'))}"
    w_pids = ", ".join(str(p) for p in watch_s["pids"]) if watch_s["pids"] else ""
    watch_line = f"  watch     : {w_mode:8s} | {w_uptime} | {watch_s['notified_count']} notified"
    if w_pids:
        watch_line += f" | PIDs {w_pids}"
    print(watch_line)

    # Watchdog
    d_mode = watchdog_s["mode"]
    if watchdog_s["running"]:
        d_uptime = f"uptime {watch_module.fmt_uptime(watchdog_s.get('uptime_seconds'))}"
    else:
        d_uptime = f"stopped {watch_module.fmt_uptime(watchdog_s.get('stopped_since_seconds'))}"
    d_pids = ", ".join(str(p) for p in watchdog_s["pids"]) if watchdog_s["pids"] else ""
    dog_line = f"  watchdog  : {d_mode:8s} | {d_uptime} | {watchdog_s['lock_creations']} on, {watchdog_s['lock_removals']} off"
    if d_pids:
        dog_line += f" | PIDs {d_pids}"
    print(dog_line)

    # TUI
    tui_pids = ", ".join(str(p) for p in tui_s["pids"]) if tui_s["pids"] else ""
    tui_line = f"  tui       : {tui_s['mode']:8s}"
    if tui_pids:
        tui_line += f" | PIDs {tui_pids}"
    print(tui_line)

    # Chromium
    print(f"  chromium  : {len(chromium_main)} main, {len(chromium_tui)} tui")

    # BrowserWorker
    if worker_s["running"]:
        if worker_s["stale"]:
            status_label = "STALE"
        elif not worker_s["thread_alive"]:
            status_label = "DEAD"
        else:
            status_label = "running"
        activity = f"{worker_s['last_task']} ({watch_module.fmt_uptime(worker_s.get('last_activity_seconds_ago'))} ago)"
        auth = worker_s.get("auth_status", "unknown")
        queue = worker_s.get("queue_depth", 0)
        print(f"  worker    : {status_label:8s} | {activity} | auth: {auth} | queue: {queue}")
    else:
        last = worker_s.get("last_task", "unknown")
        ago = watch_module.fmt_uptime(worker_s.get("last_activity_seconds_ago"))
        print(f"  worker    : stopped  | last: {last} ({ago} ago)" if ago else f"  worker    : stopped")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "login":
        return 0 if session.login() else 1

    if args.command == "kill":
        from . import watch as watch_module
        watch_module.kill_all()
        return 0

    if args.command == "watch":
        if args.status:
            from . import watch as watch_module
            s = watch_module.watch_status()
            if s["running"]:
                uptime = f"uptime {watch_module.fmt_uptime(s.get('uptime_seconds'))}"
            else:
                uptime = f"stopped {watch_module.fmt_uptime(s.get('stopped_since_seconds'))}"
            pids = ", ".join(str(p) for p in s["pids"]) if s["pids"] else ""
            line = f"watch: {s['mode']} | {uptime} | {s['notified_count']} notified"
            if pids:
                line += f" | PIDs {pids}"
            print(line)
            return 0
        # The watcher manages its own browser (launch/close per poll) so it
        # doesn't hold the profile lock open.
        try:
            watch.watch(
                interval=args.interval,
                once=args.once,
                as_json=args.json,
                headless=not args.headed,
                terminal=args.terminal,
                click_cmd=args.click_cmd,
                background=args.background,
            )
        except KeyboardInterrupt:
            pass
        return 0

    if args.command == "tui-login":
        return 0 if session.login_tui() else 1

    if args.command == "tui":
        from . import tui as tui_module

        tui_module.run_tui(headless=not args.headed, interval=args.interval, verbose=args.verbose)
        return 0

    if args.command == "watchdog":
        from . import watch as watch_module

        if args.status:
            from . import watch as watch_module
            s = watch_module.watchdog_status()
            if s["running"]:
                uptime = f"uptime {watch_module.fmt_uptime(s.get('uptime_seconds'))}"
            else:
                uptime = f"stopped {watch_module.fmt_uptime(s.get('stopped_since_seconds'))}"
            pids = ", ".join(str(p) for p in s["pids"]) if s["pids"] else ""
            line = f"watchdog: {s['mode']} | {uptime} | {s['lock_creations']} on, {s['lock_removals']} off"
            if pids:
                line += f" | PIDs {pids}"
            print(line)
            return 0

        if args.background:
            watch_module._daemonize()
        watch_module.watchdog(interval=args.interval)
        return 0

    if args.command == "status":
        handle_status(as_json=args.json)
        print()
        try:
            with session.browser(headless=not args.headed) as page:
                data = session.status(page)
                print("Google Voice:")
                _emit(data, args.json, lambda d: print(
                    f"  authenticated: {d['authenticated']}\n"
                    f"  account: {d.get('accountLabel') or 'unknown'}\n"
                    f"  url: {d['url']}"
                ))
        except AuthenticationRequiredError:
            print("Google Voice: not signed in. Run `tgv login` first.")
        except Exception as exc:
            print(f"Google Voice: could not check session ({exc.__class__.__name__})")
        return 0

    try:
        with session.browser(headless=not args.headed) as page:
            if args.command == "threads":
                items = reader.list_threads(page, args.limit)
                _emit(items, args.json, _print_threads)
            elif args.command == "read":
                thread = reader.read_thread(page, args.ref, args.limit)
                _emit(thread, args.json, _print_thread)
            elif args.command == "recent":
                result = reader.recent(
                    page,
                    since_ms=args.duration,
                    incoming_only=args.incoming,
                    limit=args.limit,
                )
                _emit(result, args.json, _print_recent)
            elif args.command == "send":
                known = contacts.load_cached_contacts() if not args.refresh else None
                if known is None:
                    known = contacts.load(page, refresh=args.refresh)
                number, kind = contacts.resolve(args.recipient, known)
                if number is None:
                    raise session.VoiceError(
                        f"No contact named {args.recipient!r}. "
                        "Try 'tgv contacts' to see known contacts."
                    )
                if args.dry_run:
                    if args.json:
                        print(json.dumps({
                            "dry_run": True,
                            "to": number,
                            "resolved_from": args.recipient,
                        }))
                    elif kind == "contact":
                        print(f"Would send to {args.recipient} ({number})")
                    else:
                        print(f"Would send to {number}")
                    return 0
                try:
                    sender.send(page, number, args.text)
                except Exception as exc:
                    raise session.VoiceError(str(exc)) from exc
                if args.json:
                    print(json.dumps({
                        "sent": True,
                        "to": number,
                        "resolved_from": args.recipient,
                    }))
                elif kind == "contact":
                    print(f"Sent to {args.recipient} ({number})")
                else:
                    print(f"Sent to {number}")
            elif args.command == "contacts":
                known = contacts.load_cached_contacts() if not args.refresh else None
                if known is None:
                    known = contacts.load(page, refresh=args.refresh)
                results = contacts.search(args.query, known)
                _emit(results, args.json, _print_contacts)
            elif args.command == "search":
                results = reader.search(
                    page, args.query, thread_name=args.thread, limit=args.limit
                )
                _emit(results, args.json, _print_search)
    except session.AuthenticationRequiredError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except session.VoiceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except PlaywrightError as exc:
        message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        print(
            f"browser error: {message}\n"
            "If Google is blocking automation, try again with --headed.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
