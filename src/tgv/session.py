"""Browser session handling: login, authentication checks and context management."""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
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


def _new_context(playwright: Playwright, headless: bool) -> BrowserContext:
    executable = config.resolve_chrome_executable()
    kwargs = {
        "user_data_dir": str(config.PROFILE_DIR),
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


def _get_page(context: BrowserContext) -> Page:
    pages = context.pages
    if pages:
        return pages[0]
    return context.new_page()


def navigate_to_messages(page: Page) -> None:
    page.goto(config.MESSAGES_URL, wait_until="domcontentloaded")
    # The Voice app keeps long-lived connections (websockets/polling) open, so
    # "networkidle" never settles. Give redirects a moment to resolve instead.
    try:
        page.wait_for_load_state("networkidle", timeout=2_000)
    except PlaywrightError:
        pass
    page.wait_for_timeout(1_500)


def is_authenticated(page: Page) -> bool:
    """Return True when the current page shows the signed-in Voice app."""
    navigate_to_messages(page)
    parsed = urlsplit(page.url)
    if parsed.hostname != "voice.google.com":
        return False
    if parsed.path.startswith("/landing"):
        return False
    return True


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
            return { accountLabel, title: document.title };
        }"""
    )
    return {"authenticated": True, "url": page.url, **data}
