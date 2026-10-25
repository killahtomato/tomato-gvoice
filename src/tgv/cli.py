"""Argparse entry point for the tgv (Tomato-gVoice) CLI."""

from __future__ import annotations

import argparse
import json
import re
import sys

from playwright.sync_api import Error as PlaywrightError

from . import contacts, reader, sender, session

_DURATION_RE = re.compile(r"^(\d+)([smhdw])$")
_UNIT_MS = {
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
    "w": 604_800_000,
}
_BIDI_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069\u200e\u200f\ufeff]")


def clean(text: str | None) -> str | None:
    """Strip invisible bidi/control characters the web app inserts."""
    if text is None:
        return None
    return _BIDI_RE.sub("", text).strip()


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
    send.add_argument("--refresh", action="store_true", help="re-fetch contacts first")
    send.add_argument("--json", action="store_true")

    list_contacts = sub.add_parser("contacts", help="list or search known contacts")
    list_contacts.add_argument("query", nargs="?", help="name to search for")
    list_contacts.add_argument(
        "--refresh", action="store_true", help="re-fetch instead of using the cache"
    )
    list_contacts.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "login":
        return 0 if session.login() else 1

    try:
        with session.browser(headless=not args.headed) as page:
            if args.command == "status":
                data = session.status(page)
                _emit(data, args.json, lambda d: print(
                    f"authenticated: {d['authenticated']}\n"
                    f"url: {d['url']}\n"
                    f"account: {d.get('accountLabel') or 'unknown'}"
                ))
            elif args.command == "threads":
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
                known = contacts.load(page, refresh=args.refresh)
                number, kind = contacts.resolve(args.recipient, known)
                if number is None:
                    raise session.VoiceError(
                        f"No contact named {args.recipient!r}. "
                        "Try 'tgv contacts' to see known contacts."
                    )
                sender.send(page, number, args.text)
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
                known = contacts.load(page, refresh=args.refresh)
                results = contacts.search(args.query, known)
                _emit(results, args.json, _print_contacts)
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
