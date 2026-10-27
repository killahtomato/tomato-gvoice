"""Shared text utilities for Tomato-gVoice."""

import re

_BIDI_RE = re.compile(r"[‎-‏﻿]")


def clean(text: str | None) -> str:
    """Remove invisible bidi/control characters the web app inserts."""
    return _BIDI_RE.sub("", text or "").strip()
