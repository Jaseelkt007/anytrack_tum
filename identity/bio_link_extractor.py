"""Extract GitHub / Twitter / LinkedIn handles from a free-text bio or url field.

Pure functions. No I/O. Stdlib only. Matches:
  - github.com/<handle>           -> ('github', handle)
  - twitter.com/<handle>          -> ('twitter', handle)
  - x.com/<handle>                -> ('twitter', handle)
  - linkedin.com/in/<slug>        -> ('linkedin', slug)
  - markdown links [text](url)    -> URL parsed
  - bare http(s):// or no-scheme  -> both fine

Skips reserved paths like x.com/home, github.com/orgs, linkedin.com/company/...
because those are not Person handles.

Handles returned lowercased for stable keying. Original casing is the caller's
responsibility to preserve in the payload (per the brief's normalization rule).
"""

from __future__ import annotations

import re

# (platform, regex). Each regex captures the handle in group 1.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("github",   re.compile(r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})", re.IGNORECASE)),
    ("twitter",  re.compile(r"(?:https?://)?(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})", re.IGNORECASE)),
    ("linkedin", re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/([A-Za-z0-9_-]{1,100})", re.IGNORECASE)),
]

_GITHUB_RESERVED = {
    "orgs", "settings", "marketplace", "pricing", "features", "topics",
    "trending", "explore", "search", "login", "signup", "about", "contact",
    "security", "site", "enterprise", "issues", "pulls", "notifications",
    "new", "join", "logout", "sponsors",
}
_TWITTER_RESERVED = {
    "home", "explore", "notifications", "messages", "i", "search", "settings",
    "compose", "intent", "share", "logout", "login", "signup", "tos", "privacy",
}
_LINKEDIN_RESERVED: set[str] = set()  # /in/ prefix already filters most


def _is_reserved(platform: str, handle: str) -> bool:
    if platform == "github":
        return handle in _GITHUB_RESERVED
    if platform == "twitter":
        return handle in _TWITTER_RESERVED
    if platform == "linkedin":
        return handle in _LINKEDIN_RESERVED
    return False


def extract_platform_links(text: str | None) -> list[tuple[str, str]]:
    """Return [(platform, handle_lowercase), ...] de-duplicated, source-order.

    GitHub handles can't end with a hyphen, so we strip a trailing hyphen if
    one slipped in via a regex tail.
    """
    if not text:
        return []
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for platform, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            handle = m.group(1).strip().rstrip("/").rstrip("-").lower()
            if not handle or _is_reserved(platform, handle):
                continue
            key = (platform, handle)
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out
