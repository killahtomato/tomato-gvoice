"""Terminal UI for Tomato-gVoice, built with Textual.

The UI runs Playwright in a dedicated worker thread (the sync API is not
thread-safe), communicating through request/response queues so the interface
never blocks.
"""

from __future__ import annotations

import atexit
import os
import queue
import re
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, ListItem, ListView, Static

from . import config, contacts, reader, sender, session
from .session import AuthenticationRequiredError

_BIDI_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069\u200e\u200f\ufeff]")


def _clean(text: str | None) -> str:
    return _BIDI_RE.sub("", text or "").strip()


# --------------------------------------------------------------------------- #
# Browser worker
# --------------------------------------------------------------------------- #
class BrowserWorker:
    """Owns the Playwright page in a dedicated thread."""

    def __init__(self, profile_dir: Path | None = None, headless: bool = True) -> None:
        self._profile_dir = profile_dir or config.TUI_PROFILE_DIR
        self._headless = headless
        self._requests: queue.Queue = queue.Queue()
        self._responses: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as playwright:
                context = session.launch_context(
                    playwright, headless=self._headless, profile_dir=self._profile_dir
                )
                try:
                    page = session.get_page(context)
                    if not session.is_authenticated(page):
                        self._responses.put(("auth_required", None))
                    else:
                        self._responses.put(("ready", None))
                    while True:
                        item = self._requests.get()
                        if item is None:
                            break
                        fn, args, kwargs = item
                        try:
                            result = fn(page, *args, **kwargs)
                            self._responses.put(("ok", result))
                        except Exception as exc:  # noqa: BLE001
                            self._responses.put(("err", exc))
                finally:
                    context.close()
        except Exception as exc:  # noqa: BLE001
            self._responses.put(("error", exc))

    def start(self) -> None:
        status, payload = self._responses.get()
        if status == "error":
            raise payload
        if status == "auth_required":
            raise AuthenticationRequiredError()

    def call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        self._requests.put((fn, args, kwargs))
        status, result = self._responses.get()
        if status == "err":
            raise result
        return result

    def stop(self) -> None:
        self._requests.put(None)
        self._thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# Widgets
# --------------------------------------------------------------------------- #
class MessageRow(Horizontal):
    """A single message bubble: incoming left, outgoing right."""

    def __init__(self, message: dict) -> None:
        super().__init__(classes=f"row {message.get('direction', 'incoming')}")
        self.message = message

    def compose(self) -> ComposeResult:
        incoming = self.message.get("direction") != "outgoing"
        who = self.message.get("participant") or "unknown"
        text = self.message.get("text") or ""
        ts = self.message.get("timestamp") or ""
        content = f"{text}\n[dim]{ts}[/]" if not incoming else f"{who}\n{text}\n[dim]{ts}[/]"
        bubble = Static(content, classes="bubble")
        spacer = Static("", classes="spacer")
        if incoming:
            yield bubble
            yield spacer
        else:
            yield spacer
            yield bubble


class MessagePane(VerticalScroll):
    """The scrollable conversation pane."""

    def set_messages(self, messages: list[dict]) -> None:
        self.remove_children()
        for msg in messages:
            self.mount(MessageRow(msg))
        if messages:
            self.scroll_end(animate=False)


class ThreadList(ListView):
    """The sidebar thread list."""


class ComposeBar(Horizontal):
    """The compose input at the bottom."""

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Type a message…", id="compose-input")
        yield Static("i: type  Esc: list", classes="send-hint")


# --------------------------------------------------------------------------- #
# Screens
# --------------------------------------------------------------------------- #
class HelpScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        yield Static(
            "[b]Tomato-gVoice — keys[/b]\n\n"
            "  j / ↓      next thread\n"
            "  k / ↑      previous thread\n"
            "  Enter      open thread\n"
            "  r          refresh\n"
            "  c          compose new message\n"
            "  /          search\n"
            "  g          contacts\n"
            "  m          mark thread read\n"
            "  ?          this help\n"
            "  q          quit\n",
            id="help",
        )


class SearchScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Search messages…", id="search-input")
        yield Static("Searching…", id="search-status")
        yield ListView(id="search-results")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.input.value.strip()
        if not query:
            return
        status = self.query_one("#search-status", Static)
        status.update(f"Searching for: {query}")
        results = self.query_one("#search-results", ListView)
        results.clear()
        # Run search in a worker via the app's worker
        app = self.app
        assert isinstance(app, TuiApp)

        def do_search():
            try:
                messages = app._worker.call(reader.search, query, limit=20)
            except Exception:
                messages = []
            app.call_from_thread(self._show_results, messages, query)

        import threading
        threading.Thread(target=do_search, daemon=True).start()

    def _show_results(self, messages: list[dict], query: str) -> None:
        status = self.query_one("#search-status", Static)
        status.update(f"{len(messages)} result(s) for: {query}")
        results = self.query_one("#search-results", ListView)
        for i, msg in enumerate(messages):
            who = _clean(msg.get("participant")) or "?"
            text = (msg.get("text") or "").splitlines()[0]
            thread_idx = msg.get("thread", 0) - 1
            label = f"{who}: {text}\n     [thread {thread_idx + 1}]"
            results.append(ListItem(Static(label), id=f"result-{i}"))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if item_id.startswith("result-"):
            idx = int(item_id.split("-", 1)[1])
            results = self.query_one("#search-results", ListView)
            # get thread index from the result widget's data
            # We stored it in the label; extract from the id mapping
            self.dismiss()
            # Switch to that thread
            assert isinstance(self.app, TuiApp)
            thread_idx = getattr(results, "_thread_indices", {}).get(idx, 0)
            sidebar = self.app.query_one("#sidebar", ListView)
            sidebar.index = thread_idx
            self.app._selected = thread_idx
            self.app.load_thread(thread_idx)


class ComposeScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Name or number", id="compose-recipient")
        yield Input(placeholder="Message", id="compose-text")
        yield Static("Enter on the message field sends. Esc cancels.", id="compose-hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "compose-text":
            return
        recipient = self.query_one("#compose-recipient", Input).value.strip()
        text = event.input.value.strip()
        if not text or not recipient:
            return
        self.dismiss()
        assert isinstance(self.app, TuiApp)
        def do_send():
            try:
                known = self.app._worker.call(contacts.load)
                number, kind = contacts.resolve(recipient, known)
                if number is None:
                    self.app.call_from_thread(
                        self.app.notify, f"No contact named {recipient!r}", severity="warning"
                    )
                    return
                self.app._worker.call(sender.send, number, text)
                self.app.call_from_thread(
                    self.app.notify, f"Sent to {recipient}", severity="information"
                )
            except Exception as exc:
                self.app.call_from_thread(self.app.notify, str(exc), severity="error")
            self.app.call_from_thread(self.app.refresh_threads)
        import threading
        threading.Thread(target=do_send, daemon=True).start()


class ContactsScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Filter…", id="contacts-filter")
        yield ListView(id="contacts-list")

    def on_mount(self) -> None:
        assert isinstance(self.app, TuiApp)

        def do_load():
            try:
                contacts_map = self.app._worker.call(contacts.load)
            except Exception:
                contacts_map = {}
            self.app.call_from_thread(self._show, contacts_map)

        import threading
        threading.Thread(target=do_load, daemon=True).start()

    def _show(self, contacts_map: dict) -> None:
        lst = self.query_one("#contacts-list", ListView)
        for i, (name, numbers) in enumerate(sorted(contacts_map.items())):
            label = f"{name}  {', '.join(numbers[:2])}"
            lst.append(ListItem(Static(label), id=f"contact-{i}"))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss()
        # Focus the compose screen with this contact pre-filled
        assert isinstance(self.app, TuiApp)
        idx = int((event.item.id or "contact-0").split("-", 1)[1])
        lst = self.query_one("#contacts-list", ListView)
        # We need the name; re-derive from the list
        # For simplicity, open a compose screen
        self.app.action_compose()
        screen = self.app.screen_stack[-1] if self.app.screen_stack else None
        if isinstance(screen, ComposeScreen):
            inp = screen.query_one("#compose-recipient", Input)
            # Set the contact name
            label = event.query_one(Static).renderable
            if hasattr(label, "plain"):
                name = str(label.plain).split("  ")[0]
            else:
                name = str(label).split("  ")[0]
            inp.value = name


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
class TuiApp(App):
    CSS = """
    Screen { layout: vertical; }

    #main { height: 1fr; }

    #sidebar {
        width: 32%;
        min-width: 22;
        border-right: solid $primary;
    }

    #sidebar ListView { height: 1fr; }

    #conversation { width: 1fr; layout: grid; grid-rows: 67% 33%; grid-gutter: 0; }

    #messages {
        height: 100%;
        overflow-y: auto;
        padding: 0 1;
    }

    #compose {
        height: 100%;
        border-top: solid $primary;
        padding: 0 1;
        align: center middle;
    }

    #compose Input { width: 1fr; }

    .send-hint { width: auto; padding: 1 1 0 0; color: $text-muted; }

    MessageRow { height: auto; width: 100%; }

    MessageRow .spacer { width: 1fr; }

    MessageRow .bubble {
        width: auto;
        max-width: 75%;
        height: auto;
        padding: 0 1;
        margin-bottom: 1;
    }

    MessageRow.incoming .bubble {
        background: $surface;
        border-left: thick $primary;
    }

    MessageRow.outgoing .bubble {
        background: $primary;
        color: $text;
    }

    .thread-unread { color: $warning; }
    .thread-read { color: $text-muted; }

    #help { padding: 1 2; }
    #search-input { margin: 1; }
    #search-results { height: 1fr; }
    #search-status { padding: 0 1; color: $text-muted; }
    #compose-recipient { margin: 1 1 0 1; }
    #compose-text { margin: 1; }
    #compose-hint { padding: 0 1; color: $text-muted; }
    #contacts-filter { margin: 1; }
    #contacts-list { height: 1fr; }
    .send-hint { width: auto; padding: 1 1 0 0; color: $text-muted; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("c", "compose", "Compose"),
        ("/", "search", "Search"),
        ("g", "contacts", "Contacts"),
        ("m", "mark_read", "Mark read"),
        ("?", "help", "Help"),
        ("j", "cursor_down", "Next"),
        ("k", "cursor_up", "Previous"),
        ("i", "focus_compose", "Type"),
        ("escape", "focus_sidebar", "Back to list"),
    ]

    def __init__(self, *, headless: bool = True, interval: float = 30.0) -> None:
        super().__init__()
        self._worker: BrowserWorker | None = None
        self._headless = headless
        self._interval = interval
        self._threads: list[dict] = []
        self._selected: int | None = None
        self._last_ts: float = 0.0
        self._lock_created = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            yield ThreadList(id="sidebar")
            with Vertical(id="conversation"):
                yield MessagePane(id="messages")
                yield ComposeBar(id="compose")
        yield Footer()

    def _create_lock(self) -> None:
        if self._lock_created:
            return
        config.TUI_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.TUI_LOCK_FILE.write_text(str(os.getpid()))
        self._lock_created = True

    def _remove_lock(self) -> None:
        if not self._lock_created:
            return
        try:
            config.TUI_LOCK_FILE.unlink()
        except FileNotFoundError:
            pass
        self._lock_created = False

    def on_mount(self) -> None:
        self._create_lock()
        self._install_exit_handlers()
        self._start()

    def _install_exit_handlers(self) -> None:
        atexit.register(self._remove_lock)
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._signal_handler)
            except (OSError, ValueError):
                pass

    def _signal_handler(self, signum: int, frame: object) -> None:
        if signum == signal.SIGHUP:
            # Ignore SIGHUP — we were launched from a notification action and
            # the parent process group may have exited. Stay alive.
            return
        self._remove_lock()
        raise SystemExit(0)

    @work(thread=True)
    def _start(self) -> None:
        try:
            self._worker = BrowserWorker(headless=self._headless)
            self._worker.start()
        except AuthenticationRequiredError:
            self.call_from_thread(
                self.notify,
                "Not signed in. Run `tgv tui-login` first.",
                severity="error",
                timeout=10,
            )
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.notify, f"Could not start the browser: {exc}", severity="error"
            )
            return
        self.call_from_thread(self._worker_ready)

    def _worker_ready(self) -> None:
        # Seed the poll cursor so we don't toast every old message.
        self._last_ts = time.time() * 1000
        # The widget tree may not be fully composed yet when the worker
        # signals ready. Retry on the next refresh if the sidebar is missing.
        try:
            self.refresh_threads()
        except NoMatches:
            self.call_after_refresh(self._refresh_on_mount)
        try:
            self.query_one("#sidebar").focus()
        except NoMatches:
            self.call_after_refresh(self._focus_sidebar)
        self.set_interval(self._interval, self.poll)

    def _refresh_on_mount(self) -> None:
        try:
            self.refresh_threads()
        except NoMatches:
            pass

    def _focus_sidebar(self) -> None:
        try:
            self.query_one("#sidebar").focus()
        except NoMatches:
            pass

    def on_unmount(self) -> None:
        self._remove_lock()
        if self._worker:
            self._worker.stop()

    # -- data -------------------------------------------------------------- #
    @work(thread=True)
    def refresh_threads(self) -> None:
        assert self._worker is not None
        try:
            threads = self._worker.call(reader.list_threads, limit=100)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.notify, str(exc), severity="error")
            return
        self.call_from_thread(self._set_threads, threads)

    def _set_threads(self, threads: list[dict]) -> None:
        self._threads = threads
        try:
            sidebar = self.query_one("#sidebar", ThreadList)
        except NoMatches:
            return
        sidebar.clear()
        if not threads:
            sidebar.append(ListItem(Static("No conversations.")))
            return
        for thread in threads:
            marker = "●" if thread.get("unread") else " "
            preview = (thread.get("preview") or "").splitlines()[0]
            label = f"{marker} {_clean(thread.get('participant')) or 'unknown'}\n     {preview}"
            sidebar.append(ListItem(Static(label)))
        if threads:
            sidebar.index = 0
            self._selected = 0
            self.load_thread(0)

    @work(thread=True)
    def load_thread(self, index: int) -> None:
        assert self._worker is not None
        try:
            thread = self._worker.call(reader.read_thread, str(index + 1))
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.notify, str(exc), severity="error")
            return
        self.call_from_thread(self._set_messages, thread)

    def _set_messages(self, thread: dict) -> None:
        pane = self.query_one("#messages", MessagePane)
        pane.set_messages(thread.get("messages", []))

    # -- events ------------------------------------------------------------ #
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._selected = event.index
        self.load_thread(event.index)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "compose-input":
            return
        text = event.input.value.strip()
        event.input.value = ""
        if not text:
            return
        if self._selected is None:
            self.notify("Open a thread first", severity="warning")
            return
        self.send_to_selected(text)

    @work(thread=True)
    def send_to_selected(self, text: str) -> None:
        assert self._worker is not None
        if not self._threads or self._selected is None:
            return
        thread = self._threads[self._selected]
        recipient = _clean(thread.get("participant")) or "unknown"
        known = self._worker.call(contacts.load)
        number, kind = contacts.resolve(recipient, known)
        if number is None:
            self.call_from_thread(
                self.notify, f"No contact named {recipient!r}", severity="warning"
            )
            return
        try:
            self._worker.call(sender.send, number, text)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.notify, str(exc), severity="error")
            return
        self.call_from_thread(self.notify, f"Sent to {recipient}", severity="information")
        self.refresh_threads()

    # -- polling (Phase 3) ------------------------------------------------- #
    @work(thread=True)
    def poll(self) -> None:
        assert self._worker is not None
        try:
            now_ms = time.time() * 1000
            since_ms = max(1.0, now_ms - self._last_ts)
            messages = self._worker.call(reader.messages_since, since_ms, now_ms=now_ms)
            if messages:
                self._last_ts = max(m["_ts"] for m in messages)
        except Exception:  # noqa: BLE001
            return
        self.call_from_thread(self.refresh_threads)
        for msg in messages:
            self.call_from_thread(
                self.notify,
                f"{msg.get('participant') or 'unknown'}: {msg.get('text') or ''}",
                title="New message",
                severity="information",
            )

    # -- actions ----------------------------------------------------------- #
    def action_refresh(self) -> None:
        self.refresh_threads()

    def action_focus(self) -> None:
        sidebar = self.query_one("#sidebar")
        compose = self.query_one("#compose-input")
        if self.compose_has_focus():
            sidebar.focus()
        else:
            compose.focus()

    def action_focus_sidebar(self) -> None:
        self.query_one("#sidebar").focus()

    def action_focus_compose(self) -> None:
        self.query_one("#compose-input").focus()

    def compose_has_focus(self) -> bool:
        from textual.widget import Widget
        widget = self.focused
        while widget is not None:
            if getattr(widget, "id", None) == "compose-input":
                return True
            if isinstance(widget, Widget):
                widget = widget.parent
            else:
                break
        return False

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_search(self) -> None:
        self.push_screen(SearchScreen())

    def action_compose(self) -> None:
        self.push_screen(ComposeScreen())

    def action_contacts(self) -> None:
        self.push_screen(ContactsScreen())

    def action_mark_read(self) -> None:
        if self._selected is None:
            return
        thread = self._threads[self._selected]
        number = _clean(thread.get("participant")) or "unknown"
        def do_mark():
            try:
                self._worker.call(reader.list_threads, limit=1)  # refresh session
                # Mark read via the API would go here; for now update local state
                thread["unread"] = False
                self.call_from_thread(self.refresh_threads)
            except Exception:
                pass
        import threading
        threading.Thread(target=do_mark, daemon=True).start()

    def action_quit(self) -> None:
        self.exit()


def run_tui(*, headless: bool = True, interval: float = 30.0) -> None:
    TuiApp(headless=headless, interval=interval).run()
