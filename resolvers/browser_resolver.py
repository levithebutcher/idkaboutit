"""
resolvers/browser_resolver.py
-----------------------------
The main Playwright-based resolver. Handles JS-heavy gateways
(SpeedyLink, DT-M, Blockfy, MBoost, OUO, AdF.ly, etc.) without any API.

Flow (Step C + Step D from the architecture):

    1. Open context, apply stealth, install page.clock (mock time)
    2. Warm-up root domain (cookies/tokens seeded)
    3. Navigate to target URL (domcontentloaded, NOT load)
    4. Immediately inspect RAW HTML for hidden <a>, meta refresh,
       inline JS string assignments -> if found, short-circuit.
    5. Detect captcha gates -> return CAPTCHA_GATE.
    6. Fast-forward page.clock past countdown timers.
    7. Look for "Continue"/"Get Link" button -> human-like click.
    8. Extract target from (in order):
         a. localStorage / sessionStorage keys
         b. DOM anchors pointing off-site
         c. Intercepted XHR/fetch JSON responses
    9. Clean URL, return SUCCESS.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from config import settings
from resolvers.base import (
    BaseResolver,
    ResolutionMethod,
    ResolutionResult,
    ResolutionStatus,
    clean_target_url,
)
from utils.logger import get_logger
from utils.session_factory import create_context, warmup_domain
from utils.stealth_config import apply_human_like_mouse, apply_navigator_overrides

logger = get_logger(__name__)

_DOMAINS_FILE = Path(__file__).resolve().parent.parent / "data" / "supported_domains.json"

# ---- Regexes ----

# JS inline string assignments: var link = "https://..." ; url: 'https://...'
_JS_STRING_ASSIGN = re.compile(
    r"""(?:var|let|const|window\.\w+|\w+)\s*[:=]\s*['"](https?://[^'"\s<>]+)['"]""",
    re.IGNORECASE,
)

# <a href="https://..."> inside HTML (raw response text)
_ANCHOR_HREF = re.compile(
    r"""<a\b[^>]*\bhref\s*=\s*['"](https?://[^'"\s<>]+)['"]""",
    re.IGNORECASE,
)

# meta refresh: <meta http-equiv="refresh" content="5; url=https://...">
_META_REFRESH = re.compile(
    r"""content\s*=\s*['"]\s*\d+\s*;\s*url\s*=\s*['"]?\s*(https?://[^'"\s>]+)""",
    re.IGNORECASE,
)

# Base64-looking blobs in inline JS (atob("..."))
_ATOB_BLOB = re.compile(r"atob\(\s*['\"]([A-Za-z0-9+/=]{20,})['\"]\s*\)")

# Captcha indicators
_CAPTCHA_HINTS = (
    "recaptcha", "hcaptcha", "cf-turnstile",
    "g-recaptcha", "challenges.cloudflare.com",
)

# Common localStorage keys holding the target URL
_DEST_KEYS = (
    "redirectUrl", "redirect_url", "targetUrl", "target_url",
    "destination", "finalLink", "final_link", "finalUrl",
    "link", "url", "next", "redirect",
)

# Ad/tracker hosts to skip when scanning anchors
_AD_HOSTS = (
    "doubleclick.net", "googlesyndication", "googleadservices",
    "taboola.com", "outbrain.com", "adsterra.com", "propellerads",
    "popads.net", "facebook.com", "twitter.com", "instagram.com",
    "t.me", "telegram.org", "google.com", "gstatic.com",
    "cloudflare.com", "google-analytics.com",
)

# Button labels we try to click after countdown
_BUTTON_LABELS = (
    "Continue", "Continue to Link", "Get Link", "Get link",
    "Skip Ad", "Skip", "Go to Link", "Proceed", "Click Here",
    "Click here", "Open Link", "Download", "Download Now",
)


class BrowserResolver(BaseResolver):
    """Playwright-based resolver for JS-gateway shorteners."""

    name = "browser"

    def __init__(self, browser: Browser) -> None:
        self._browser = browser

    # ---------- Main entry ----------

    async def resolve_impl(self, url: str, domain: str) -> ResolutionResult:
        meta = self._domain_meta(domain)
        warmup_url = meta.get("warmup_url")
        method_used = ResolutionMethod.BROWSER

        context: BrowserContext | None = None
        try:
            context = await create_context(self._browser, domain, use_saved_state=True)

            # Warm-up root domain so cookies/session tokens are seeded
            if warmup_url:
                await warmup_domain(context, warmup_url)

            page = await context.new_page()
            await apply_navigator_overrides(page)

            # Mock the clock BEFORE navigation so timers can be fast-forwarded
            await self._install_clock(page)

            # Intercept XHR/fetch JSON responses that might contain the URL
            intercepted: list[str] = []
            page.on("response", lambda r: asyncio.create_task(
                self._capture_json_response(r, intercepted)
            ))

            logger.debug("[%s] Navigating to %s", domain, url)
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except PlaywrightTimeoutError:
                return ResolutionResult.failure(
                    ResolutionStatus.TIMEOUT,
                    method=ResolutionMethod.BROWSER,
                    error_class="NavTimeout",
                    error_message="goto() exceeded nav_timeout_ms",
                )

            # --- 4. Raw HTML short-circuit ---
            raw_result = await self._try_raw_html(page, domain)
            if raw_result:
                logger.info("[%s] Resolved from raw HTML", domain)
                return ResolutionResult.success(
                    clean_target_url(raw_result),
                    ResolutionMethod.RAW_HTML,
                )

            # --- 5. Captcha check ---
            if await self._has_captcha(page):
                return ResolutionResult.failure(
                    ResolutionStatus.CAPTCHA_GATE,
                    method=ResolutionMethod.BROWSER,
                    error_class="CaptchaDetected",
                )

            # --- 6 + 7. Fast-forward clock, click continue ---
            await self._fast_forward_clock(page)
            await asyncio.sleep(random.uniform(1.5, 3.0))

            clicked = await self._click_continue(page)
            if clicked:
                logger.debug("[%s] Clicked continue button", domain)
                # Wait for either navigation or DOM change
                await self._wait_for_progress(page)

            # --- 8a. localStorage ---
            ls_result = await self._extract_from_storage(page, domain)
            if ls_result:
                logger.info("[%s] Resolved from localStorage", domain)
                return ResolutionResult.success(
                    clean_target_url(ls_result),
                    ResolutionMethod.LOCALSTORAGE,
                )

            # --- 8c. Intercepted XHR JSON ---
            xhr_result = self._extract_from_intercepted(intercepted, domain)
            if xhr_result:
                logger.info("[%s] Resolved from XHR JSON", domain)
                return ResolutionResult.success(
                    clean_target_url(xhr_result),
                    ResolutionMethod.BROWSER,
                )

            # --- 8b. DOM anchors ---
            dom_result = await self._extract_from_dom(page, domain)
            if dom_result:
                logger.info("[%s] Resolved from DOM anchor", domain)
                return ResolutionResult.success(
                    clean_target_url(dom_result),
                    ResolutionMethod.DOM,
                )

            # --- Nothing worked ---
            if await self._is_ad_wall(page, domain):
                return ResolutionResult.failure(
                    ResolutionStatus.AD_WALL,
                    method=ResolutionMethod.BROWSER,
                    error_class="AdWall",
                )

            return ResolutionResult.failure(
                ResolutionStatus.UNRESOLVABLE,
                method=method_used,
                error_class="NoTarget",
                error_message="No destination URL found in storage/DOM/XHR",
            )

        except PlaywrightTimeoutError:
            return ResolutionResult.failure(
                ResolutionStatus.TIMEOUT,
                method=ResolutionMethod.BROWSER,
                error_class="PlaywrightTimeout",
            )
        except Exception as exc:
            logger.warning("[%s] browser resolver error: %s", domain, exc)
            return ResolutionResult.failure(
                ResolutionStatus.INTERNAL_ERROR,
                method=ResolutionMethod.BROWSER,
                error_class=type(exc).__name__,
                error_message=str(exc)[:300],
            )
        finally:
            if context is not None:
                try:
                    from utils.session_factory import save_storage_state
                    await save_storage_state(context, domain)
                    await context.close()
                except Exception:
                    pass

    # ---------- Clock mocking ----------

    @staticmethod
    async def _install_clock(page: Page) -> None:
        """Install page.clock BEFORE navigation to mock Date/setTimeout."""
        try:
            await page.clock.install()
            logger.debug("page.clock installed")
        except Exception as exc:
            logger.debug("clock install failed: %s", exc)

    @staticmethod
    async def _fast_forward_clock(page: Page) -> None:
        """Fast-forward mocked time by 30s to skip countdowns."""
        try:
            await page.clock.fast_forward(30_000)
            logger.debug("Clock fast-forwarded 30s")
        except Exception as exc:
            logger.debug("clock fast_forward failed: %s", exc)

    # ---------- Raw HTML short-circuit ----------

    async def _try_raw_html(self, page: Page, domain: str) -> str | None:
        """Inspect raw initial HTML for the destination URL."""
        try:
            html = await page.content()
        except Exception:
            return None

        # meta refresh
        for m in _META_REFRESH.finditer(html):
            candidate = m.group(1)
            if self._looks_like_target(candidate, domain):
                return candidate

        # inline JS string assignments
        for m in _JS_STRING_ASSIGN.finditer(html):
            candidate = m.group(1)
            if self._looks_like_target(candidate, domain):
                return candidate

        # hidden anchors in raw HTML
        for m in _ANCHOR_HREF.finditer(html):
            candidate = m.group(1)
            if self._looks_like_target(candidate, domain):
                return candidate

        # atob blobs — decode and check
        import base64
        for m in _ATOB_BLOB.finditer(html):
            try:
                decoded = base64.b64decode(m.group(1)).decode("utf-8", "ignore")
                if self._looks_like_target(decoded, domain):
                    return decoded
            except Exception:
                continue

        return None

    # ---------- Captcha detection ----------

    async def _has_captcha(self, page: Page) -> bool:
        """Return True if a captcha widget is present on the page."""
        try:
            html = (await page.content()).lower()
        except Exception:
            return False
        return any(h in html for h in _CAPTCHA_HINTS)

    # ---------- Button clicks ----------

    async def _click_continue(self, page: Page) -> bool:
        """Find and click a 'Continue'-like button using human mouse."""
        for label in _BUTTON_LABELS:
            # Try role=button first
            try:
                locator = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
                if await locator.count() > 0:
                    btn = locator.first
                    if await btn.is_visible():
                        box = await btn.bounding_box()
                        if box:
                            await apply_human_like_mouse(
                                page,
                                box["x"] + box["width"] / 2,
                                box["y"] + box["height"] / 2,
                            )
                            return True
            except Exception:
                pass
            # Then try <a> with the label as text
            try:
                locator = page.get_by_text(re.compile(rf"^{re.escape(label)}$", re.I))
                if await locator.count() > 0:
                    el = locator.first
                    if await el.is_visible():
                        box = await el.bounding_box()
                        if box:
                            await apply_human_like_mouse(
                                page,
                                box["x"] + box["width"] / 2,
                                box["y"] + box["height"] / 2,
                            )
                            return True
            except Exception:
                continue
        return False

    # ---------- Progress / navigation wait ----------

    async def _wait_for_progress(self, page: Page) -> None:
        """
        Wait for either URL change or DOM mutation that suggests resolution
        completed. Capped at ~5 seconds to keep latency low.
        """
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightTimeoutError:
            pass
        except Exception:
            pass
        await asyncio.sleep(random.uniform(1.0, 2.0))

    # ---------- Storage extraction ----------

    async def _extract_from_storage(self, page: Page, domain: str) -> str | None:
        """Read localStorage/sessionStorage and look for a target URL."""
        try:
            data = await page.evaluate(
                """() => {
                    const out = {};
                    for (const k of Object.keys(localStorage)) out[k] = localStorage.getItem(k);
                    for (const k of Object.keys(sessionStorage)) out['s:' + k] = sessionStorage.getItem(k);
                    return out;
                }"""
            )
        except Exception as exc:
            logger.debug("storage read failed: %s", exc)
            return None

        # Direct key match first
        for key in _DEST_KEYS:
            for candidate_key, value in data.items():
                if candidate_key.lower() == key.lower() or candidate_key.lower().endswith(":" + key.lower()):
                    if isinstance(value, str) and self._looks_like_target(value, domain):
                        return value

        # Then scan all string values for embedded http URLs
        for value in data.values():
            if isinstance(value, str):
                for m in re.finditer(r"https?://[^\s'\"<>]+", value):
                    if self._looks_like_target(m.group(0), domain):
                        return m.group(0)
        return None

    # ---------- XHR JSON extraction ----------

    async def _capture_json_response(self, response, sink: list[str]) -> None:
        """Capture response bodies that look like JSON with URLs inside."""
        try:
            ct = response.headers.get("content-type", "")
            if "json" not in ct.lower():
                return
            body = await response.text()
            # Only capture small JSON bodies (< 50 KB)
            if len(body) > 50_000:
                return
            sink.append(body)
        except Exception:
            pass

    def _extract_from_intercepted(self, bodies: list[str], domain: str) -> str | None:
        for body in bodies:
            try:
                obj = json.loads(body)
            except json.JSONDecodeError:
                # Try regex fallback on the raw text
                for m in re.finditer(r"https?://[^\s'\"<>]+", body):
                    if self._looks_like_target(m.group(0), domain):
                        return m.group(0)
                continue
            url = self._walk_json_for_url(obj, domain)
            if url:
                return url
        return None

    def _walk_json_for_url(self, obj: Any, domain: str) -> str | None:
        if isinstance(obj, str):
            if self._looks_like_target(obj, domain):
                return obj
        elif isinstance(obj, dict):
            for v in obj.values():
                found = self._walk_json_for_url(v, domain)
                if found:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = self._walk_json_for_url(v, domain)
                if found:
                    return found
        return None

    # ---------- DOM extraction ----------

    async def _extract_from_dom(self, page: Page, domain: str) -> str | None:
        """Scan <a> tags for an off-site URL."""
        try:
            anchors = await page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.href)"""
            )
        except Exception:
            return None

        for href in anchors or []:
            if self._looks_like_target(href, domain):
                return href
        return None

    # ---------- Ad-wall detection ----------

    async def _is_ad_wall(self, page: Page, domain: str) -> bool:
        """If page has only ad anchors and no off-site link, it's an ad wall."""
        try:
            anchors = await page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"""
            )
        except Exception:
            return False
        external = [a for a in (anchors or []) if self._looks_like_target(a, domain)]
        return len(external) == 0

    # ---------- Filters ----------

    def _looks_like_target(self, url: str, shortener_domain: str) -> bool:
        """
        Return True if `url` is a plausible external destination:
        - starts with http(s)
        - different host from shortener
        - not an ad/tracker/known non-target host
        """
        if not url or not url.startswith("http"):
            return False
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False

        short = shortener_domain.lower()
        if short.startswith("www."):
            short = short[4:]
        if host == short or host.endswith("." + short):
            return False

        for ad in _AD_HOSTS:
            if host == ad or host.endswith("." + ad):
                return False

        # Ignore obviously static resource URLs
        if parsed.path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".css", ".js", ".svg", ".webp", ".ico")):
            return False

        return True

    # ---------- Domain metadata ----------

    @staticmethod
    def _domain_meta(domain: str) -> dict[str, Any]:
        """Lookup domain entry in supported_domains.json."""
        try:
            data = json.loads(_DOMAINS_FILE.read_text(encoding="utf-8"))
            return data.get("domains", {}).get(domain, {})
        except Exception:
            return {}