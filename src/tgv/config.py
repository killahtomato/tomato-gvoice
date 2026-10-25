"""Paths, URLs, endpoints and selectors for the Google Voice web app."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

APP_NAME = "tomato-gvoice"
BASE_DIR = Path(os.environ.get("TGV_HOME", Path.home() / ".config" / APP_NAME))
PROFILE_DIR = Path(os.environ.get("TGV_PROFILE_DIR", BASE_DIR / "chrome"))

VOICE_URL = "https://voice.google.com"
MESSAGES_PATH = "messages"
MESSAGES_URL = f"{VOICE_URL}/{MESSAGES_PATH}"

# Internal JSON endpoints the web app itself calls. Reading these is far more
# robust than scraping the DOM, so we intercept them and decode the payload.
API_HOST = "clients6.google.com"
API_BASE = f"https://{API_HOST}/voice/v1/voiceclient"
THREAD_LIST_ENDPOINT = f"{API_BASE}/api2thread/list"
THREAD_GET_ENDPOINT = f"{API_BASE}/api2thread/get"
ACCOUNT_ENDPOINT = f"{API_BASE}/account/get"

# Direction codes used inside the api2thread payload.
DIRECTION_INCOMING = 10
DIRECTION_OUTGOING = 11

# DOM selectors (fallback / for thread previews and reading a single thread).
THREAD_ITEM_SELECTOR = "gv-message-thread-list-item"
THREAD_DETAILS_SELECTOR = "gv-thread-details"
MESSAGE_LIST_SELECTOR = "gv-thread-details gv-message-list"
MESSAGE_ITEM_SELECTOR = "gv-message-item"
PARTICIPANT_SELECTOR = "gv-annotation.participants"
MESSAGE_CONTENT_SELECTOR = "gv-annotation.content"
MESSAGE_SENDER_SELECTOR = ".sender"
MESSAGE_TIMESTAMP_SELECTOR = ".timestamp"
THREAD_HEADER_PARTICIPANT_SELECTOR = "gv-thread-details-header .secondary-text"
THREAD_HEADER_TITLE_SELECTOR = "gv-thread-details-header .header-title"

# Candidate Chromium executables, used when Playwright's bundled build is absent.
CHROME_CANDIDATES = (
    os.environ.get("TGV_CHROME"),
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "chromium",
    "google-chrome",
)


def resolve_chrome_executable() -> str | None:
    """Return a usable Chromium executable path, or None to use Playwright's."""
    for candidate in CHROME_CANDIDATES:
        if not candidate:
            continue
        path = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if path:
            return path
    return None
