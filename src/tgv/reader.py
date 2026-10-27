"""Reading Google Voice SMS: thread list, single threads and recent messages."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Response

from . import config
from .session import VoiceError, navigate_to_messages


def _first_thread_page_size(request) -> int | None:
    """Mirror the web app's list request so we only capture the full fetch."""
    try:
        data = request.post_data_json
    except Exception:
        return None
    if not isinstance(data, list) or len(data) < 4:
        return None
    if data[0] != 2 or data[3] is not None:
        return None
    if isinstance(data[1], int) and data[1] > 0:
        return data[1]
    return None


def _is_thread_list_response(response: Response) -> bool:
    if config.THREAD_LIST_ENDPOINT not in response.url:
        return False
    return _first_thread_page_size(response.request) is not None


def capture_thread_list(page: Page, timeout_ms: int = 15_000) -> tuple[Any, int]:
    """Navigate to the inbox and capture the api2thread/list payload."""
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with page.expect_response(_is_thread_list_response, timeout=timeout_ms) as info:
                navigate_to_messages(page)
            response = info.value
            payload = response.json()
            page_size = _first_thread_page_size(response.request) or 100
            return payload, page_size
        except PlaywrightError as exc:
            last_error = exc
            if attempt == 0:
                page.reload(wait_until="domcontentloaded")
    raise VoiceError(
        "Could not read the message list. Try again, or use `tgv threads`."
    ) from last_error


def dom_participants(page: Page) -> list[str | None]:
    return page.evaluate(
        """(sel) => {
            const clean = (v) => (v ? v.replace(/\\s+/g, ' ').trim() : null);
            return [...document.querySelectorAll(sel)].map(
                (el) => clean(el.querySelector('gv-annotation.participants')?.textContent)
            );
        }""",
        config.THREAD_ITEM_SELECTOR,
    )


def decode_messages(
    payload: Any,
    participants: list[str | None],
    *,
    since_ms: int,
    incoming_only: bool = False,
    now_ms: float | None = None,
) -> list[dict]:
    """Decode the api2thread/list payload into a flat message list.

    Returned dicts carry an internal "_ts" (epoch ms) used for sorting and
    de-duplication; callers strip it before returning data to the user.
    Messages are sorted newest-first.
    """
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    cutoff = now_ms - since_ms
    raw_threads = (
        payload[0]
        if isinstance(payload, list) and payload and isinstance(payload[0], list)
        else []
    )

    messages: list[dict] = []
    for idx, thread in enumerate(raw_threads):
        raw_messages = (
            thread[2]
            if isinstance(thread, list) and len(thread) > 2 and isinstance(thread[2], list)
            else []
        )
        for m in raw_messages:
            if not isinstance(m, list) or len(m) < 10:
                continue
            ts = m[1]
            text = m[9] if isinstance(m[9], str) else ""
            direction = (
                "incoming"
                if m[4] == config.DIRECTION_INCOMING
                else "outgoing"
                if m[4] == config.DIRECTION_OUTGOING
                else "unknown"
            )
            if not isinstance(ts, (int, float)) or ts < cutoff or not text:
                continue
            if incoming_only and direction != "incoming":
                continue
            messages.append(
                {
                    "id": m[0],
                    "direction": direction,
                    "participant": participants[idx] if idx < len(participants) else None,
                    "text": " ".join(text.split()),
                    "thread": idx + 1,
                    "timestamp": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(),
                    "_ts": ts,
                }
            )

    messages.sort(key=lambda m: m["_ts"], reverse=True)
    return messages


def all_messages(page: Page) -> list[dict]:
    """Return every message in the loaded threads (no time filter)."""
    payload, _page_size = capture_thread_list(page)
    participants = dom_participants(page)
    return decode_messages(payload, participants, since_ms=10 * 365 * 86_400_000)


def decode_recent(
    payload: Any,
    participants: list[str | None],
    *,
    since_ms: int,
    incoming_only: bool = False,
    limit: int = 100,
    now_ms: float | None = None,
) -> dict:
    """Decode the api2thread/list payload into a flat message list."""
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    raw_threads = (
        payload[0]
        if isinstance(payload, list) and payload and isinstance(payload[0], list)
        else []
    )

    latest_thread_timestamps: list[float] = []
    for thread in raw_threads:
        raw_messages = (
            thread[2]
            if isinstance(thread, list) and len(thread) > 2 and isinstance(thread[2], list)
            else []
        )
        latest_ts = next(
            (
                m[1]
                for m in raw_messages
                if isinstance(m, list) and len(m) > 1 and isinstance(m[1], (int, float))
            ),
            None,
        )
        if latest_ts is not None:
            latest_thread_timestamps.append(latest_ts)

    all_messages = decode_messages(
        payload,
        participants,
        since_ms=since_ms,
        incoming_only=incoming_only,
        now_ms=now_ms,
    )
    selected = all_messages[:limit]
    for m in selected:
        del m["_ts"]

    cutoff = now_ms - since_ms
    oldest_loaded = min(latest_thread_timestamps) if latest_thread_timestamps else None
    return {
        "count": len(selected),
        "cutoff": datetime.fromtimestamp(cutoff / 1000, tz=timezone.utc).isoformat(),
        "generatedAt": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat(),
        "messages": selected,
        "sourceTruncated": bool(
            raw_threads
            and oldest_loaded is not None
            and oldest_loaded >= cutoff
            and len(raw_threads) >= 100
        ),
        "truncated": len(all_messages) > len(selected),
        "windowMs": since_ms,
    }


def search(
    page: Page,
    query: str,
    *,
    thread_name: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Case-insensitive substring search across the loaded threads."""
    payload, _page_size = capture_thread_list(page)
    participants = dom_participants(page)
    messages = decode_messages(payload, participants, since_ms=365 * 86_400_000)
    needle = query.lower()
    thread_needle = thread_name.lower() if thread_name else None

    out: list[dict] = []
    for m in messages:
        if needle not in m["text"].lower():
            continue
        if thread_needle and thread_needle not in (m.get("participant") or "").lower():
            continue
        out.append({k: v for k, v in m.items() if k != "_ts"})
    return out[:limit]


def messages_since(
    page: Page,
    since_ms: int,
    *,
    now_ms: float | None = None,
) -> list[dict]:
    """Return messages strictly newer than an epoch-ms cutoff (oldest first).

    The comparison is exclusive so the last-seen message isn't returned again
    on the next poll. Returned dicts include the internal "_ts" field.
    """
    payload, _page_size = capture_thread_list(page)
    participants = dom_participants(page)
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    cutoff = now_ms - since_ms
    messages = [
        m
        for m in decode_messages(payload, participants, since_ms=since_ms, now_ms=now_ms)
        if m["_ts"] > cutoff
    ]
    messages.reverse()
    return messages


def list_threads(page: Page, limit: int = 20) -> list[dict]:
    """List conversation previews from the DOM."""
    navigate_to_messages(page)
    page.locator(config.THREAD_ITEM_SELECTOR).first.wait_for(state="attached", timeout=15_000)
    items = page.evaluate(
        """(sel) => {
            const clean = (v) => (v ? v.replace(/\\s+/g, ' ').trim() : null);
            const out = [];
            const seen = new Set();
            for (const el of document.querySelectorAll(sel)) {
                if (!(el instanceof HTMLElement)) continue;
                const participant = clean(
                    el.querySelector('gv-annotation.participants')?.textContent
                );
                const preview = clean(
                    el.querySelector('gv-annotation.preview')?.textContent
                );
                const timestamp = clean(el.querySelector('.timestamp')?.textContent);
                const subtitle = el.querySelector('.subtitle');
                const unread = subtitle ? !subtitle.classList.contains('read') : false;
                const key = (participant || '') + '|' + (preview || '');
                if (!preview || seen.has(key)) continue;
                seen.add(key);
                out.push({ participant, preview, timestamp, unread });
            }
            return out;
        }""",
        config.THREAD_ITEM_SELECTOR,
    )
    return items[:limit]


def read_thread(page: Page, ref: str, limit: int = 100) -> dict:
    """Read one thread by 1-based list index or case-insensitive preview text."""
    navigate_to_messages(page)
    threads = page.locator(config.THREAD_ITEM_SELECTOR)
    threads.first.wait_for(state="attached", timeout=15_000)
    count = threads.count()

    if ref.isdigit():
        idx = int(ref) - 1
    else:
        idx = -1
        needle = ref.lower()
        for i in range(count):
            if needle in threads.nth(i).inner_text().lower():
                idx = i
                break

    if idx < 0 or idx >= count:
        raise VoiceError(f"No message thread matches {ref!r}.")

    threads.nth(idx).locator("[role=button]").click()
    page.locator(config.MESSAGE_LIST_SELECTOR).wait_for(state="attached", timeout=15_000)

    return page.evaluate(
        """(limit) => {
            const details = document.querySelector('gv-thread-details');
            const clean = (v) => (v ? v.replace(/\\s+/g, ' ').trim() : null);
            const items = [...details.querySelectorAll('gv-message-item')];
            const messages = items.slice(-limit).map((el) => {
                const full = el.querySelector('.full-container');
                const direction =
                    full && /outgoing/.test(full.className) ? 'outgoing' : 'incoming';
                const tsEl =
                    el.querySelector('.timestamp:not(.status)') ||
                    el.querySelector('.status.timestamp');
                const content = el.querySelector('gv-annotation.content');
                return {
                    direction,
                    timestamp: clean(tsEl ? tsEl.textContent : null),
                    text: clean(content ? content.textContent : null),
                };
            });
            const headerTitle = clean(
                details.querySelector('gv-thread-details-header .header-title')?.textContent
            );
            const secondary = clean(
                details.querySelector('gv-thread-details-header .secondary-text')?.textContent
            );
            return {
                messages,
                participant: headerTitle or secondary,
                title: secondary or headerTitle,
                totalMessages: items.length,
            };
        }""",
        limit,
    )


def recent(
    page: Page,
    *,
    since_ms: int,
    incoming_only: bool = False,
    limit: int = 100,
) -> dict:
    """List individual messages in a recent time window, decoded from the API."""
    payload, _page_size = capture_thread_list(page)
    participants = dom_participants(page)
    return decode_recent(
        payload,
        participants,
        since_ms=since_ms,
        incoming_only=incoming_only,
        limit=limit,
    )
