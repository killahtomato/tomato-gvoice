"""Sending SMS by driving the Google Voice web composer."""

from __future__ import annotations

import os
import re

from playwright.sync_api import Page

from .session import VoiceError

_DEBUG = bool(os.environ.get("TGV_DEBUG"))


def _lap(label: str) -> None:
    if _DEBUG:
        print(f"[send] {label}", flush=True)


def _wait_for_enabled_send(page: Page, timeout_ms: int) -> None:
    page.wait_for_function(
        """() => {
            const b = document.querySelector('button[aria-label="Send message"]');
            return !!b && !b.disabled;
        }""",
        timeout=timeout_ms,
    )


def send(page: Page, to: str, text: str, timeout_ms: int = 30_000) -> None:
    """Compose and send a text message from the already-loaded inbox page."""
    page.set_default_timeout(timeout_ms)

    compose = page.get_by_role(
        "button", name=re.compile(r"send new message|new message|compose", re.I)
    ).first
    compose.click()
    _lap(f"compose clicked url={page.url}")

    phone_input = page.locator('input[placeholder*="phone number" i]').first
    phone_input.wait_for(state="visible")
    phone_input.click()
    page.keyboard.type(to, delay=10)
    _lap(f"phone typed url={page.url}")

    last_four = "".join(ch for ch in to if ch.isdigit())[-4:]
    suggestion = page.get_by_text(
        re.compile(rf"Send to .*{last_four}", re.I)
    ).first
    suggestion.click()
    _lap(f"suggestion clicked url={page.url}")
    page.keyboard.press("Escape")
    _lap(f"escape pressed url={page.url}")

    body = page.locator(
        'input[placeholder*="type a message" i], textarea[placeholder*="type a message" i]'
    ).first
    body.wait_for(state="visible")
    body.click()
    page.keyboard.type(text, delay=5)
    if _DEBUG:
        value = page.evaluate(
            """() => {
                const t = document.querySelector(
                    'input[placeholder*="type a message" i], textarea[placeholder*="type a message" i]'
                );
                return t ? t.value : null;
            }"""
        )
        _lap(f"body value={value!r}")
    _wait_for_enabled_send(page, timeout_ms)
    # Wait for the actual sendsms API response. The app shows an optimistic
    # "sending" placeholder immediately, so a URL/DOM change is not proof the
    # message went out; closing the browser early aborts the request.
    with page.expect_response(
        lambda response: "api2thread/sendsms" in response.url,
        timeout=15_000,
    ) as resp_info:
        page.locator('button[aria-label="Send message"]').first.click()
    response = resp_info.value
    _lap(f"send clicked url={page.url} status={response.status}")
    if response.status >= 400:
        raise VoiceError(f"Could not send the message (HTTP {response.status}).")
