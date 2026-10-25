"""Contact name resolution for Google Voice SMS.

Google Voice does not expose a public contacts API, but the thread list payload
it already returns contains each thread's contacts and phone numbers. We decode
that into a name -> [numbers] map and use it to resolve names to numbers.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from playwright.sync_api import Page

from . import config
from .reader import capture_thread_list, dom_participants
from .session import VoiceError

_BIDI_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069\u200e\u200f\ufeff]")


def normalize_name(value: str | None) -> str:
    """Lowercase, strip bidi/control chars and collapse whitespace."""
    if not value:
        return ""
    cleaned = _BIDI_RE.sub("", value)
    return " ".join(cleaned.split()).lower()


def normalize_number(value: str | None) -> str:
    """Canonicalize a phone number: US numbers become +1..., short codes stay."""
    if not value:
        return ""
    cleaned = _BIDI_RE.sub("", value).strip()
    has_plus = cleaned.startswith("+")
    digits = re.sub(r"\D", "", cleaned)
    if not digits:
        return ""
    if has_plus:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return digits


def decode_contacts(
    payload: Any, participants: list[str | None]
) -> dict[str, list[str]]:
    """Build a name -> [numbers] map from the thread payload and DOM names.

    The payload's contact "name" is the phone number, but each thread's real
    phone number is in ``thread[8]`` (Thread.phoneNumbers). The display name
    comes from the DOM participant list, which is in the same order as the
    payload's threads.
    """
    contacts: dict[str, list[str]] = {}
    raw_threads = (
        payload[0]
        if isinstance(payload, list) and payload and isinstance(payload[0], list)
        else []
    )

    for idx, thread in enumerate(raw_threads):
        if not isinstance(thread, list):
            continue
        phone_numbers = (
            thread[8] if len(thread) > 8 and isinstance(thread[8], list) else []
        )
        number = next(
            (p for p in phone_numbers if isinstance(p, str) and p), None
        )
        name = participants[idx] if idx < len(participants) else None
        if not name or not number:
            continue
        name = _BIDI_RE.sub("", name).strip()
        norm = normalize_number(number)
        if not name or not norm:
            continue
        bucket = contacts.setdefault(name, [])
        if norm not in bucket:
            bucket.append(norm)

    return contacts


def _dom_contact_list(page: Page) -> list[tuple[str, str]]:
    """Read name/number pairs from the call panel's contact list.

    The SMS recipient picker does not autocomplete names, but the call panel
    renders the full Google contact list (with display names and numbers) in
    the DOM. Reading it is harmless and gives us contacts that were never
    texted.
    """
    return page.evaluate(
        """() => {
            const out = [];
            for (const el of document.querySelectorAll('gv-contact-item')) {
                const name = (el.querySelector('gv-annotation.name')?.textContent || '').trim();
                const number = (el.querySelector('.phone-number')?.textContent || '').replace(/[^\\d]/g, '');
                if (name && number) out.push([name, number]);
            }
            return out;
        }"""
    )


def fetch_contacts(page: Page) -> dict[str, list[str]]:
    """Build a name -> [numbers] map from the thread payload and the contact list."""
    payload, _page_size = capture_thread_list(page)
    participants = dom_participants(page)
    contacts = decode_contacts(payload, participants)

    for name, number in _dom_contact_list(page):
        key = _BIDI_RE.sub("", name).strip()
        norm = normalize_number(number)
        if not key or not norm:
            continue
        bucket = contacts.setdefault(key, [])
        if norm not in bucket:
            bucket.append(norm)

    return contacts


def _cache_is_fresh() -> bool:
    try:
        mtime = config.CONTACTS_CACHE.stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) < config.CONTACTS_CACHE_TTL_SECONDS


def _read_cache() -> dict[str, list[str]]:
    data = json.loads(config.CONTACTS_CACHE.read_text(encoding="utf-8"))
    raw = data.get("contacts", {})
    return {str(k): [str(n) for n in v] for k, v in raw.items()}


def save(contacts: dict[str, list[str]]) -> None:
    config.CONTACTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    config.CONTACTS_CACHE.write_text(
        json.dumps({"updated": time.time(), "contacts": contacts}, indent=2),
        encoding="utf-8",
    )


def load(page: Page, refresh: bool = False) -> dict[str, list[str]]:
    """Load the contact map, using the disk cache when it is fresh."""
    if not refresh and _cache_is_fresh():
        try:
            cached = _read_cache()
            if cached:
                return cached
        except (OSError, ValueError):
            pass
    contacts = fetch_contacts(page)
    save(contacts)
    return contacts


def _index(contacts: dict[str, list[str]]) -> dict[str, list[str]]:
    """Build a normalized-name lookup index."""
    index: dict[str, list[str]] = {}
    for name, numbers in contacts.items():
        key = normalize_name(name)
        if not key:
            continue
        bucket = index.setdefault(key, [])
        for number in numbers:
            if number not in bucket:
                bucket.append(number)
    return index


def resolve(recipient: str, contacts: dict[str, list[str]]) -> tuple[str | None, str]:
    """Resolve a recipient to a phone number.

    Returns (number, kind) where kind is "number", "contact", or "unresolved".
    Names are matched exactly first, then by prefix. Raises VoiceError when a
    name matches several contacts.
    """
    if any(ch.isdigit() for ch in recipient):
        return recipient, "number"

    index = _index(contacts)
    needle = normalize_name(recipient)
    if not needle:
        return None, "unresolved"

    if needle in index:
        matches = list(index[needle])
    else:
        matches = []
        for key, numbers in index.items():
            if key.startswith(needle):
                matches.extend(numbers)
        if not matches and len(needle) >= 3:
            for key, numbers in index.items():
                if needle in key:
                    matches.extend(numbers)

    # Dedupe while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for match in matches:
        if match not in seen:
            seen.add(match)
            unique.append(match)

    if len(unique) == 1:
        return unique[0], "contact"
    if len(unique) > 1:
        raise VoiceError(
            f"Multiple contacts match {recipient!r}: {', '.join(unique)}"
        )
    return None, "unresolved"


def search(query: str | None, contacts: dict[str, list[str]]) -> list[dict]:
    """Return contacts whose name contains the (normalized) query."""
    needle = normalize_name(query) if query else ""
    out = []
    for name, numbers in contacts.items():
        if not needle or needle in normalize_name(name):
            out.append({"name": name, "numbers": numbers})
    out.sort(key=lambda c: normalize_name(c["name"]))
    return out
