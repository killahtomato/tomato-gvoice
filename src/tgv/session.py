"""Browser session handling: login, authentication checks and context management."""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from typing import Iterator

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    sync_playwright,
)

from . import config


class VoiceError(Exception):
    """Base error for tgv."""


class AuthenticationRequiredError(VoiceError):
    """Raised when the saved session is missing or has expired."""

    def __init__(self) -> None:
        super().__init__(
            "Google Voice is not signed in. Run `tgv login` first."
        )


def _clear_stale_singleton_locks(user_data_dir: str) -> None:
    """Remove Chromium Singleton lock files left behind by a crashed process.

    The SingletonLock symlink encodes the host and PID that owns the profile.
    If that PID is no longer running, the lock is stale and can be safely
    deleted so a new browser instance can start.
    """
    lock = Path(user_data_dir) / "SingletonLock"
    if not lock.exists():
        return
    try:
        target = os.readlink(str(lock))
    except OSError:
        return
    # target is "<hostname>-<pid>"; the original process may also leave
    # SingletonSocket and SingletonCookie alongside it.
    parts = target.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        pid = int(parts[1])
        try:
            os.kill(pid, 0)
            return  # process is still alive — lock is valid
        except (OSError, ProcessLookupError):
            pass  # stale lock
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (Path(user_data_dir) / name).unlink()
        except FileNotFoundError:
            pass


def force_clear_singleton_locks(user_data_dir: str) -> None:
    """Unconditionally remove all Singleton lock files for a profile.

    Use this for profiles that are exclusively managed by a single process
    (like the main tgv profile used only by the watch) — any existing lock
    is by definition stale.
    """
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (Path(user_data_dir) / name).unlink()
        except FileNotFoundError:
            pass


def _new_context(
    playwright: Playwright, headless: bool = True, profile_dir: Path | None = None
) -> BrowserContext:
    executable = config.resolve_chrome_executable()
    user_data_dir = str(profile_dir or config.PROFILE_DIR)
    _clear_stale_singleton_locks(user_data_dir)
    kwargs = {
        "user_data_dir": user_data_dir,
        "headless": headless,
        "viewport": {"width": 1280, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if executable:
        kwargs["executable_path"] = executable
    else:
        kwargs["channel"] = "chromium"

    # The profile can be briefly locked by a concurrent tgv process (e.g. the
    # background watcher). Retry for a few seconds before giving up.
    last_error: PlaywrightError | None = None
    for attempt in range(6):
        try:
            return playwright.chromium.launch_persistent_context(**kwargs)
        except PlaywrightError as exc:
            last_error = exc
            if "ProcessSingleton" in str(exc) and attempt < 5:
                time.sleep(2)
                continue
            raise
    raise last_error  # pragma: no cover - unreachable


def launch_context(
    playwright: Playwright, headless: bool = True, profile_dir: Path | None = None
) -> BrowserContext:
    """Public wrapper around _new_context for use by the TUI worker."""
    return _new_context(playwright, headless=headless, profile_dir=profile_dir)


def get_page(context: BrowserContext) -> Page:
    """Public wrapper around _get_page for use by the TUI worker."""
    return _get_page(context)


def _get_page(context: BrowserContext) -> Page:
    pages = context.pages
    if pages:
        return pages[0]
    return context.new_page()


def navigate_to_messages(page: Page) -> None:
    """Navigate to the inbox. Retries through transient Chromium errors."""
    for attempt in range(3):
        try:
            page.goto(config.MESSAGES_URL, wait_until="domcontentloaded", timeout=15_000)
            # The Voice app keeps long-lived connections (websockets/polling) open,
            # so "networkidle" never settles. Give redirects a moment to resolve.
            try:
                page.wait_for_load_state("networkidle", timeout=2_000)
            except PlaywrightError:
                pass
            return
        except PlaywrightError as exc:
            # ERR_ABORTED or "Target page" errors mean Chromium is still
            # settling; retry after a brief pause. Use a fresh page if the
            # current one died.
            if attempt < 2 and ("ERR_ABORTED" in str(exc) or "Target" in str(exc)):
                try:
                    page.wait_for_timeout(1_000)
                except PlaywrightError:
                    pass
                continue
            raise


def is_authenticated(page: Page) -> bool:
    """Return True when the current page shows the signed-in Voice app."""
    navigate_to_messages(page)
    parsed = urlsplit(page.url)
    return parsed.hostname == "voice.google.com" and not parsed.path.startswith("/landing")
    return False


@contextmanager
def browser(headless: bool = True) -> Iterator[Page]:
    """Yield a signed-in page, raising AuthenticationRequiredError if not logged in."""
    with sync_playwright() as playwright:
        context = _new_context(playwright, headless=headless)
        try:
            page = _get_page(context)
            if not is_authenticated(page):
                raise AuthenticationRequiredError()
            yield page
        finally:
            context.close()


def _on_voice_app(page: Page) -> bool:
    parsed = urlsplit(page.url)
    return parsed.hostname == "voice.google.com" and not parsed.path.startswith("/landing")


def login(timeout_seconds: int = 300) -> bool:
    """Open a headed browser and wait for the user to sign in interactively."""
    config.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = _new_context(playwright, headless=False)
        try:
            page = _get_page(context)
            navigate_to_messages(page)

            if not _on_voice_app(page):
                # The inbox redirects to the marketing page when signed out.
                # Follow its "Sign in" link to Google's account chooser; the
                # link is target=_blank/hidden in the nav, so navigate directly.
                href = page.evaluate(
                    """() => {
                        const a = document.querySelector(
                            'a[href*="accounts.google.com"][href*="voice.google.com"]'
                        );
                        return a ? a.href : null;
                    }"""
                )
                if href:
                    page.goto(href)
                    page.wait_for_load_state("networkidle", timeout=10_000)
                else:
                    try:
                        page.get_by_role("link", name="Sign in").first.click()
                        page.wait_for_load_state("networkidle", timeout=10_000)
                    except Exception:
                        pass

            print(
                "A browser window opened to Google Voice.\n"
                "Complete the Google sign-in (password + 2FA if prompted), "
                "then return here.",
                file=sys.stderr,
            )

            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                time.sleep(2)
                if _on_voice_app(page):
                    break
            else:
                print("Timed out waiting for sign-in.", file=sys.stderr)
                return False

            if is_authenticated(page):
                print("Signed in successfully.", file=sys.stderr)
                return True
            print("Sign-in did not complete.", file=sys.stderr)
            return False
        finally:
            context.close()


def login_tui(timeout_seconds: int = 300) -> bool:
    """Sign in to the TUI's separate browser profile."""
    config.TUI_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = _new_context(playwright, headless=False, profile_dir=config.TUI_PROFILE_DIR)
        try:
            page = _get_page(context)
            navigate_to_messages(page)

            if not _on_voice_app(page):
                href = page.evaluate(
                    """() => {
                        const a = document.querySelector(
                            'a[href*="accounts.google.com"][href*="voice.google.com"]'
                        );
                        return a ? a.href : null;
                    }"""
                )
                if href:
                    page.goto(href)
                    page.wait_for_load_state("networkidle", timeout=10_000)

            print(
                "A browser window opened to Google Voice for the TUI.\n"
                "Complete the Google sign-in (password + 2FA if prompted), "
                "then return here.",
                file=sys.stderr,
            )

            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                time.sleep(2)
                if _on_voice_app(page):
                    break
            else:
                print("Timed out waiting for sign-in.", file=sys.stderr)
                return False

            if is_authenticated(page):
                print("TUI signed in successfully.", file=sys.stderr)
                return True
            print("TUI sign-in did not complete.", file=sys.stderr)
            return False
        finally:
            context.close()


def status(page: Page) -> dict:
    """Return basic account/session information."""
    navigate_to_messages(page)
    data = page.evaluate(
        """() => {
            const visible = (el) => {
                if (!(el instanceof HTMLElement)) return false;
                const s = window.getComputedStyle(el);
                return s.visibility !== 'hidden' && s.display !== 'none';
            };
            const labels = [...document.querySelectorAll('[aria-label]')]
                .filter(visible)
                .map(el => el.getAttribute('aria-label'))
                .filter(Boolean);
            const accountLabel = labels.find(l => /google account|account:/i.test(l)) || null;
            // Look for the Google Voice phone number in the header/nav area.
            const phoneRe = /(\\+?1?[-.\\s]?)?\\(?\\d{3}\\)?[-.\\s]?\\d{3}[-.\\s]?\\d{4}/;
            const allText = document.body.innerText || '';
            const phoneMatch = allText.match(phoneRe);
            const phone = phoneMatch ? phoneMatch[0].trim() : null;
            return { accountLabel, phone, title: document.title };
        }"""
    )
    return {"authenticated": True, "url": page.url, **data}
