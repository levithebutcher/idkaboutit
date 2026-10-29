"""
utils/session_factory.py
------------------------
Creates and manages Playwright browser contexts.

Responsibilities:
- Launch a shared Chromium browser instance (managed by the caller).
- Create per-job BrowserContexts with stealth options.
- Load/save storage_state (cookies + localStorage) per domain, so subsequent
  visits look like a returning user instead of a fresh profile.
- Perform a warm-up navigation to the shortener's root domain to seed
  cookies/tokens before hitting the actual target link.
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    async_playwright,
)

from config import settings
from utils.logger import get_logger
from utils.stealth_config import (
    apply_navigator_overrides,
    build_browser_launch_args,
    build_context_options,
)

logger = get_logger(__name__)

_STORAGE_DIR = Path(settings.storage_state_dir)


# ---------- Browser lifecycle ----------

@asynccontextmanager
async def launch_browser() -> AsyncIterator[Browser]:
    """
    Async context manager that launches Chromium and cleans up on exit.

    Usage:
        async with launch_browser() as browser:
            context = await browser.new_context(...)
            ...

    Note: In production (bot.py), launch ONE browser for the entire bot
    lifetime and share it across jobs. This helper is mainly useful for
    standalone tests or one-shot resolutions.
    """
    pw: Playwright | None = None
    browser: Browser | None = None
    try:
        pw = await async_playwright().start()

        launch_kwargs: dict = {
            "headless": settings.headless,
            "args": build_browser_launch_args(),
        }
        if settings.proxy_url:
            launch_kwargs["proxy"] = {"server": settings.proxy_url}

        browser = await pw.chromium.launch(**launch_kwargs)
        logger.info(
            "Chromium launched (headless=%s, proxy=%s)",
            settings.headless,
            "yes" if settings.proxy_url else "no",
        )
        yield browser
    finally:
        if browser:
            await browser.close()
        if pw:
            await pw.stop()
        logger.debug("Chromium shut down")


# ---------- Storage state helpers ----------

def _storage_path(domain: str) -> Path:
    """Return the per-domain storage_state file path."""
    safe = domain.replace("/", "_").replace(":", "_")
    return _STORAGE_DIR / f"{safe}.json"


def load_storage_state(domain: str) -> str | None:
    """
    Return the storage_state file path if it exists, else None.
    Playwright accepts the path directly in `new_context(storage_state=...)`.
    """
    path = _storage_path(domain)
    if path.exists() and path.stat().st_size > 0:
        logger.debug("Loaded storage state for %s (%d bytes)",
                     domain, path.stat().st_size)
        return str(path)
    return None


async def save_storage_state(context: BrowserContext, domain: str) -> None:
    """Persist the current context state (cookies + localStorage) to disk."""
    _STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    path = _storage_path(domain)
    try:
        await context.storage_state(path=str(path))
        logger.debug("Saved storage state for %s", domain)
    except Exception as e:
        logger.warning("Failed to save storage state for %s: %s", domain, e)


def clear_storage_state(domain: str) -> None:
    """Delete a domain's saved state (use after CAPTCHA_GATE failures)."""
    path = _storage_path(domain)
    if path.exists():
        path.unlink()
        logger.info("Cleared storage state for %s", domain)


# ---------- Context creation ----------

async def create_context(
    browser: Browser,
    domain: str,
    *,
    device_type: str | None = None,
    use_saved_state: bool = True,
) -> BrowserContext:
    """
    Create a fresh BrowserContext with stealth options.

    If `use_saved_state` is True and a storage file exists for `domain`,
    cookies/localStorage from the previous session will be pre-loaded.
    """
    opts = build_context_options(device_type=device_type)

    if use_saved_state:
        saved = load_storage_state(domain)
        if saved:
            opts["storage_state"] = saved

    context = await browser.new_context(**opts)
    context.set_default_timeout(settings.nav_timeout_ms)
    context.set_default_navigation_timeout(settings.nav_timeout_ms)

    # Block images/ads to speed up resolution; keep JS and XHR.
    # Comment out if a site needs image loading to trigger its countdown.
    try:
        await context.route(
            "**/*",
            _route_handler,
        )
    except Exception as e:
        logger.debug("Route handler setup skipped: %s", e)

    # Close all pages automatically when context is closed
    context.on("close", lambda _: logger.debug("Context closed for %s", domain))

    logger.debug("Created context for %s (device=%s)",
                 domain, device_type or "random")
    return context


async def _route_handler(route) -> None:
    """
    Block intrusive ad trackers and heavy media while allowing fonts, styles,
    and standard images so bot-detection scripts (BotD, FingerprintJS) are not tripped.
    """
    try:
        req = route.request
        res_type = req.resource_type
        url = req.url.lower()

        # Block massive media/video/audio downloads
        if res_type in ("media",):
            await route.abort()
            return

        # Block known ad and tracking domains
        ad_domains = (
            "doubleclick.net", "googleadservices.com", "googlesyndication.com",
            "adservice.google.com", "adsterra.com", "propellerads.com",
            "popads.net", "exoclick.com", "adcash.com", "criteo.com",
            "taboola.com", "outbrain.com", "ad-server.linkvertise.com",
        )
        if any(ad in url for ad in ad_domains):
            await route.abort()
            return

        await route.continue_()
    except Exception:
        pass


# ---------- Warm-up navigation ----------

async def warmup_domain(
    context: BrowserContext,
    warmup_url: str,
    delay_ms: int | None = None,
) -> None:
    """
    Navigate to the shortener's root domain to seed cookies/session tokens
    BEFORE hitting the actual target link.

    Args:
        context: The BrowserContext to use.
        warmup_url: Root URL (from supported_domains.json).
        delay_ms: How long to linger on the page (default from settings).
    """
    if not warmup_url:
        return
    delay_ms = delay_ms if delay_ms is not None else settings.warmup_delay_ms

    page = await context.new_page()
    try:
        await apply_navigator_overrides(page)
        logger.debug("Warm-up navigating to %s", warmup_url)
        await page.goto(warmup_url, wait_until="domcontentloaded")
        await asyncio.sleep(delay_ms / 1000.0)
        logger.debug("Warm-up complete for %s", warmup_url)
    except Exception as e:
        # Warm-up failure is non-fatal; continue anyway.
        logger.warning("Warm-up navigation failed for %s: %s", warmup_url, e)
    finally:
        await page.close()


# ---------- Full resolution context ----------

@asynccontextmanager
async def resolution_context(
    browser: Browser,
    domain: str,
    warmup_url: str | None = None,
    *,
    device_type: str | None = None,
) -> AsyncIterator[BrowserContext]:
    """
    High-level helper: create a context, warm it up, yield it, and
    persist its state on exit.

    Usage:
        async with resolution_context(browser, "speedy.link",
                                       "https://speedy.link/") as ctx:
            page = await ctx.new_page()
            await page.goto(target_url)
            ...
    """
    context = await create_context(
        browser, domain, device_type=device_type, use_saved_state=True
    )
    try:
        if warmup_url:
            await warmup_domain(context, warmup_url)
        yield context
    finally:
        try:
            await save_storage_state(context, domain)
        finally:
            await context.close()
