"""Browser pool — Playwright sessions via Browserless.

LinkedIn (sub-project #5) and any other anti-bot platform need real browser
sessions. We don't run headless Chromium inside the worker container — that's
fragile and resource-heavy. Instead, every worker connects to a separately
running Browserless container (`pgvector` style: just another Docker service).

Local dev:
    docker compose --profile browser up postgres browserless
    python -m worker

Configuration (env):
    BROWSERLESS_WS_URL  default: ws://127.0.0.1:3000
    BROWSERLESS_TOKEN   optional auth token

Usage:
    async with browser_session(proxy=p, account=a) as ctx:
        page = await ctx.new_page()
        await page.goto("https://linkedin.com/in/...", wait_until="domcontentloaded")
        ...

The context closes automatically; Browserless reaps the underlying browser.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

    from infra.accounts import LeasedAccount
    from infra.proxies import LeasedProxy


def _ws_url() -> str:
    base = os.environ.get("BROWSERLESS_WS_URL", "ws://127.0.0.1:3000")
    token = os.environ.get("BROWSERLESS_TOKEN")
    if token:
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}token={token}"
    return base


_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


@asynccontextmanager
async def browser_session(
    *,
    proxy: "LeasedProxy | None" = None,
    account: "LeasedAccount | None" = None,
    user_agent: str | None = None,
    viewport: tuple[int, int] = (1366, 768),
) -> "AsyncIterator[BrowserContext]":
    """Yield a fresh Playwright BrowserContext.

    The proxy (if any) is configured on the context, not the launched browser,
    so concurrent contexts on the same Browserless instance can use different
    proxies. The account is currently informational only — sub-project #5 will
    use account.credentials to load LinkedIn cookies into the context.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright is not installed. `pip install playwright` then "
            "(in production) docker compose --profile browser up browserless."
        ) from exc

    proxy_kwargs: dict[str, Any] = {}
    if proxy is not None:
        entry: dict[str, Any] = {"server": f"http://{proxy.host}:{proxy.port}"}
        if proxy.username:
            entry["username"] = proxy.username
        if proxy.password:
            entry["password"] = proxy.password
        proxy_kwargs["proxy"] = entry

    async with async_playwright() as pw:
        browser = await pw.chromium.connect(_ws_url())
        context = await browser.new_context(
            user_agent=user_agent or _DEFAULT_USER_AGENT,
            viewport={"width": viewport[0], "height": viewport[1]},
            **proxy_kwargs,
        )
        # Hook for #5: load account cookies into the context here.
        try:
            yield context
        finally:
            await context.close()
            await browser.close()
