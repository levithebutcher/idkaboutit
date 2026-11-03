"""
resolvers/browser_resolver.py
-----------------------------
Robust Playwright-based resolver for JS-heavy gateways and shorteners.

Flow:
1. Fast Query Parameter extraction (?r=, ?url=, ?target=, ?link=, ?dest=).
2. Open Playwright context with anti-detect stealth scripts.
3. Network listener for XHR/Fetch payloads (captures JSON targets like Linkvertise & /links/go).
4. Navigate and run interactive interaction loop:
   - Dismiss GDPR / Cookie consent banners
   - Fast-forward countdown timers
   - Click "Get Link", "Continue", "Direct Access", "Skip Ad" buttons
   - Poll for page navigation to external off-site destination
   - Extract from localStorage and DOM anchors
5. Filter out all tracking/analytics/ad hosts (Google Tag Manager, Google Analytics, etc.).
"""

from __future__ import annotations

import asyncio
import time
import base64
import json
import logging
import random
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

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
from utils.userscript_engine import apply_userscripts

logger = get_logger(__name__)

_DOMAINS_FILE = Path(__file__).resolve().parent.parent / "data" / "supported_domains.json"

# Meta refresh regex
_META_REFRESH = re.compile(
    r"""content\s*=\s*['"]\s*\d+\s*;\s*url\s*=\s*['"]?\s*(https?://[^'"\s>]+)""",
    re.IGNORECASE,
)

# Captcha indicators
_CAPTCHA_HINTS = (
    "recaptcha", "hcaptcha", "cf-turnstile",
    "g-recaptcha", "challenges.cloudflare.com",
)

# Whitelist of trusted file hosts that are ALWAYS accepted as destinations
_TRUSTED_FILE_HOSTS = (
    "mega.nz",
    "drive.google.com",
    "mediafire.com",
    "dropbox.com",
    "pixeldrain.com",
    "gofile.io",
    "1fichier.com",
    "yadi.sk",
    "disk.yandex.com",
)

# Shortener family domains
_SHORTENER_FAMILY = (
    "best-links.org",
    "best-link.org",
    "linkfly.net",
    "clicksfly.com",
    "mboost.me",
    "thetrap.tv",
    "speedy-links.com",
    "speedy.link",
    "speedy-link.com",
)

# Common localStorage keys holding target destination URLs
_DEST_KEYS = (
    "redirectUrl", "redirect_url", "targetUrl", "target_url",
    "destination", "finalLink", "final_link", "finalUrl",
    "link", "url", "next", "redirect",
)

# Comprehensive list of ad, tracking, analytics, and internal shortener networks
_AD_HOSTS = (
    # Google analytics, tags & tracking
    "googletagmanager.com", "google-analytics.com", "analytics.google.com",
    "googleadservices.com", "googlesyndication.com", "doubleclick.net",
    "adservice.google.com", "google.com", "www.google.com",
    # Ad networks
    "taboola.com", "outbrain.com", "adsterra.com", "propellerads.com",
    "popads.net", "popcash.net", "exoclick.com", "juicyads.com",
    "trafficjunky.com", "adnium.com", "hilltopads.net", "mgid.com",
    "adcash.com", "criteo.com", "adnxs.com", "rubiconproject.com",
    "pubmatic.com", "openx.net", "appnexus.com", "amazon-adsystem.com",
    "scorecardresearch.com", "quantserve.com", "moatads.com", "casalemedia.com",
    "bidswitch.net", "smartadserver.com", "yieldmo.com", "inmobi.com",
    "ad-server.linkvertise.com", "integrate.linkvertise.com",
    # Bot challenges / trackers
    "challenges.cloudflare.com",
    # Intermediate shortener mirror & bridge domains (never final destination)
    # External bypass services (never final destinations)
    "bypass.vip", "bypass.city", "adbypass.org",
    "best-links.org", "best-link.org", "speedy.link", "speedy-links.com",
    "speedy-link.com", "speedy-links.net", "linkvertise.com", "linkvertise.net",
    "link-to.net", "up-to-down.net", "direct-link.net", "file-upload.net",
    "link-hub.net", "link-target.net", "link-center.net", "social-unlock.com",
    "dt-m.net", "dtm.net", "dt-m.com", "fastforward.cc", "blockfy.cc", "mboost.me",
    "kishanganjhalchal.com", "kannursalafi.com", "rocklinks.net", "droplink.co",
    "ez4short.com", "shrinkme.io", "gplinks.co", "liteshort.com", "link.liteshort.com",
)


def _extract_embedded_destination(url: str) -> Optional[str]:
    """Detects base64 or URL-encoded destination targets inside query parameters."""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        for key in ("r", "url", "link", "target", "dest", "destination", "u", "to"):
            for val in params.get(key, []):
                if not val:
                    continue
                unq = unquote(val).strip()
                if (unq.startswith("http://") or unq.startswith("https://")) and unq != url:
                    return unq
                try:
                    padding = len(val) % 4
                    padded = val + ("=" * (4 - padding)) if padding else val
                    decoded = base64.b64decode(padded).decode("utf-8", errors="ignore").strip()
                    if (decoded.startswith("http://") or decoded.startswith("https://")) and decoded != url:
                        return decoded
                except Exception:
                    pass
    except Exception:
        pass
    return None


class BrowserResolver(BaseResolver):
    """Playwright-based resolver for JS-gateway shorteners."""

    name = "browser"

    def __init__(self, browser: Browser) -> None:
        self._browser = browser

    # ---------- Main entry ----------

    async def resolve_impl(self, url: str, domain: str) -> ResolutionResult:
        # --- Stage 0: Query Parameter Extraction (?r=, ?url=) ---
        embedded = _extract_embedded_destination(url)
        if embedded and self._looks_like_target(embedded, domain):
            logger.info("[%s] Resolved from query parameter", domain)
            return ResolutionResult.success(
                clean_target_url(embedded),
                ResolutionMethod.API,
            )

        meta = self._domain_meta(domain)
        warmup_url = meta.get("warmup_url")

        context: BrowserContext | None = None
        page: Page | None = None
        try:
            context = await create_context(self._browser, domain, use_saved_state=True)

            if warmup_url:
                await warmup_domain(context, warmup_url)

            page = await context.new_page()
            await apply_navigator_overrides(page)
            await apply_userscripts(page)

            # Intercept network responses to capture API targets (e.g. Linkvertise & /links/go)
            intercepted_targets: list[str] = []

            async def on_response(response):
                try:
                    # Linkvertise GraphQL target payload
                    if "publisher.linkvertise.com/graphql" in response.url:
                        res_data = await response.json()
                        if isinstance(res_data, dict):
                            # DetailPageTargetData from getContent
                            gc = res_data.get("data", {}).get("getContent", {})
                            if isinstance(gc, dict):
                                t = gc.get("url") or gc.get("paste")
                                if t and isinstance(t, str) and self._looks_like_target(t, domain):
                                    intercepted_targets.append(t)
                                    return
                            # linkByIdentifier paste / target
                            lbi = res_data.get("data", {}).get("linkByIdentifier", {})
                            if isinstance(lbi, dict):
                                if lbi.get("target_type") == "paste" and lbi.get("raw"):
                                    intercepted_targets.append(lbi["raw"])
                                    return

                    # Linkvertise classic target payload
                    if "publisher.linkvertise.com/api/v1/redirect/link" in response.url:
                        res_data = await response.json()
                        if isinstance(res_data, dict):
                            t = res_data.get("data", {}).get("target")
                            if t and t.startswith("http") and self._looks_like_target(t, domain):
                                intercepted_targets.append(t)
                                return
                            u = res_data.get("data", {}).get("link", {}).get("url")
                            if u and u.startswith("http") and self._looks_like_target(u, domain):
                                intercepted_targets.append(u)
                                return

                    ct = response.headers.get("content-type", "")
                    if "json" in ct.lower() or "links/go" in response.url:
                        res_data = await response.json()
                        if isinstance(res_data, dict):
                            for k in ("url", "destination", "target", "link"):
                                v = res_data.get(k)
                                if isinstance(v, str) and v.startswith("http") and self._looks_like_target(v, domain):
                                    intercepted_targets.append(v)
                                    return
                except Exception:
                    pass

            page.on("response", on_response)

            logger.debug("[%s] Navigating to %s", domain, url)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=settings.nav_timeout_ms)
            except PlaywrightTimeoutError:
                return ResolutionResult.failure(
                    ResolutionStatus.TIMEOUT,
                    method=ResolutionMethod.BROWSER,
                    error_class="NavTimeout",
                    error_message="goto() exceeded nav_timeout_ms",
                )

            # --- Check Meta Refresh Redirect ---
            meta_refresh_dest = await self._try_meta_refresh(page, domain)
            if meta_refresh_dest:
                logger.info("[%s] Resolved from meta refresh", domain)
                return ResolutionResult.success(
                    clean_target_url(meta_refresh_dest),
                    ResolutionMethod.HTTP_REDIRECT,
                )

            # --- Captcha check ---
            if await self._has_captcha(page):
                return ResolutionResult.failure(
                    ResolutionStatus.CAPTCHA_GATE,
                    method=ResolutionMethod.BROWSER,
                    error_class="CaptchaDetected",
                )

            # --- Active Interaction & Polling Loop ---
            deadline = asyncio.get_event_loop().time() + 25.0
            while asyncio.get_event_loop().time() < deadline:
                # 1. Did we capture a target via network response?
                if intercepted_targets:
                    chosen = intercepted_targets[0]
                    logger.info("[%s] Resolved from intercepted network target: %s", domain, chosen)
                    return ResolutionResult.success(clean_target_url(chosen), ResolutionMethod.API)

                # 2. Did the page navigate to a genuine destination URL?
                try:
                    curr_url = page.url
                    if curr_url and curr_url != url and self._looks_like_target(curr_url, domain):
                        logger.info("[%s] Resolved via page navigation: %s", domain, curr_url)
                        return ResolutionResult.success(clean_target_url(curr_url), ResolutionMethod.BROWSER)
                except Exception:
                    pass

                # 3. Dismiss Cookie / GDPR / Consent dialogs
                await self._dismiss_consent_dialogs(page)

                # 4. Click Linkvertise specific "Get access" / "Free Access" buttons
                await self._click_linkvertise_access(page)

                # 5. Click Continue / Get Link buttons
                await self._click_continue_buttons(page)

                # 6. Wait for progress / timers
                await self._wait_for_progress(page)

                # 7. Check Userscript trapped destination URL (Issue 1)
                try:
                    bypassed = await page.evaluate("() => window.__bypassed_url || null")
                    if bypassed and isinstance(bypassed, str) and bypassed.startswith("http") and self._looks_like_target(bypassed, domain):
                        logger.info("[%s] Resolved from userscript __bypassed_url: %s", domain, bypassed)
                        return ResolutionResult.success(clean_target_url(bypassed), ResolutionMethod.USERSCRIPT)
                except Exception:
                    pass

                # 8. Listen for custom "bypassComplete" event dispatched by bypass.city.user.js (Issue 1)
                try:
                    event_bypassed = await page.evaluate("""() => new Promise(resolve => {
                        if (window.__bypassed_url) return resolve(window.__bypassed_url);
                        window.addEventListener('bypassComplete', e => resolve(e.detail?.url || e.detail?.bypassData || (typeof e.detail === 'string' ? e.detail : null)), {once: true});
                        setTimeout(() => resolve(null), 2000);
                    })""")
                    if event_bypassed and isinstance(event_bypassed, str) and event_bypassed.startswith("http") and self._looks_like_target(event_bypassed, domain):
                        logger.info("[%s] Resolved from bypassComplete event: %s", domain, event_bypassed)
                        return ResolutionResult.success(clean_target_url(event_bypassed), ResolutionMethod.USERSCRIPT)
                except Exception:
                    pass

                # 9. Check SpeedyLink / Best-Links embedded destination
                try:
                    conf_link = await page.evaluate("""() => {
                        if (typeof document.verify === 'function') {
                            try { document.verify(); } catch(e) {}
                        }
                        if (window.conf_rew && window.conf_rew.link) {
                            return window.conf_rew.link;
                        }
                        return null;
                    }""")
                    if conf_link and self._looks_like_target(conf_link, domain):
                        logger.info("[%s] Resolved from conf_rew: %s", domain, conf_link)
                        return ResolutionResult.success(clean_target_url(conf_link), ResolutionMethod.DOM)
                except Exception:
                    pass

                # 10. Check LocalStorage for targets
                ls_result = await self._extract_from_storage(page, domain)
                if ls_result and self._looks_like_target(ls_result, domain):
                    logger.info("[%s] Resolved from localStorage: %s", domain, ls_result)
                    return ResolutionResult.success(clean_target_url(ls_result), ResolutionMethod.LOCALSTORAGE)

                await asyncio.sleep(1.0)

            # --- Final DOM inspection check ---
            dom_result = await self._extract_from_dom(page, domain)
            if dom_result and self._looks_like_target(dom_result, domain):
                logger.info("[%s] Resolved from DOM anchor: %s", domain, dom_result)
                return ResolutionResult.success(clean_target_url(dom_result), ResolutionMethod.DOM)

            # Save debug snapshot on failure (Issue 5)
            await self._save_debug_dump(page, domain)

            return ResolutionResult.failure(
                ResolutionStatus.UNRESOLVABLE,
                method=ResolutionMethod.BROWSER,
                error_class="NoTarget",
                error_message="Destination URL could not be retrieved from page",
            )

        except PlaywrightTimeoutError:
            if page is not None:
                await self._save_debug_dump(page, domain)
            return ResolutionResult.failure(
                ResolutionStatus.TIMEOUT,
                method=ResolutionMethod.BROWSER,
                error_class="PlaywrightTimeout",
            )
        except Exception as exc:
            if page is not None:
                await self._save_debug_dump(page, domain)
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
                    await context.close()
                except Exception:
                    pass

    async def _save_debug_dump(self, page: Page, domain: str) -> None:
        """Save screenshot and HTML dump on failure for debugging (Issue 5)."""
        try:
            logs_dir = Path("logs")
            logs_dir.mkdir(parents=True, exist_ok=True)
            clean_domain = re.sub(r"[^\w\-.]", "_", domain)
            timestamp = int(time.time())
            screenshot_path = logs_dir / f"fail_{clean_domain}_{timestamp}.png"
            html_path = logs_dir / f"fail_{clean_domain}_{timestamp}.html"

            await page.screenshot(path=str(screenshot_path), full_page=True)
            html_content = await page.content()
            html_path.write_text(html_content, encoding="utf-8")

            logger.warning(
                "[%s] Resolution failed. Saved debug snapshot: %s and %s",
                domain, screenshot_path, html_path,
            )
        except Exception as exc:
            logger.debug("[%s] Failed to save debug snapshot: %s", domain, exc)

    async def _wait_for_progress(self, page: Page) -> None:
        """Wait briefly for timers, progress bars, or redirect countdowns to advance."""
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=2000)
        except Exception:
            pass
        await asyncio.sleep(1.0)

    # ---------- Interaction Helpers ----------

    async def _try_meta_refresh(self, page: Page, domain: str) -> str | None:
        """Inspect HTML ONLY for legitimate meta refresh redirects."""
        try:
            html = await page.content()
            for m in _META_REFRESH.finditer(html):
                candidate = m.group(1)
                if self._looks_like_target(candidate, domain):
                    return candidate
        except Exception:
            pass
        return None

    async def _has_captcha(self, page: Page) -> bool:
        """Return True if an active blocking captcha widget is visible on the page."""
        try:
            captcha_selectors = [
                "iframe[src*='recaptcha/api2/bframe']",
                "iframe[src*='challenges.cloudflare.com']",
                "iframe[src*='hcaptcha.com/captcha']",
                "#cf-challenge-running",
                "#challenge-form",
            ]
            for sel in captcha_selectors:
                el = page.locator(sel).first
                if await el.is_visible(timeout=300):
                    return True
        except Exception:
            pass
        return False

    async def _dismiss_consent_dialogs(self, page: Page) -> None:
        """Dismiss GDPR / Cookie consent popups (e.g. on Linkvertise)."""
        # Remove InMobi / Quantcast overlay directly via DOM
        try:
            await page.evaluate("""() => {
                document.querySelector('.qc-cmp2-container')?.remove();
                document.querySelector('#qc-cmp2-ui')?.remove();
                if (document.body) document.body.style.overflow = 'auto';
            }""")
        except Exception:
            pass

        consent_selectors = [
            ".qc-cmp2-summary-buttons button[mode='primary']",
            ".qc-cmp2-summary-buttons button:has-text('AGREE')",
            "#qc-cmp2-ui button:has-text('AGREE')",
            "button:has-text('AGREE')",
            "button:has-text('I agree')",
            "button:has-text('Accept All')",
            "button:has-text('Accept')",
            "button:has-text('Consent')",
            "div:has-text('AGREE')",
        ]
        for sel in consent_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible():
                    await btn.click(timeout=1000)
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                pass

    async def _click_linkvertise_access(self, page: Page) -> None:
        """Handle Linkvertise 'Get access', 'Free Access with Ads' & Discovery Modals."""
        access_selectors = [
            "button:has-text('Get access')",
            "a:has-text('Get access')",
            "button:has-text('Free Access')",
            "div:has-text('Free Access with Ads')",
            "button:has-text('Direct Access')",
            "a:has-text('Free Access')",
            "button:has-text('Free Access with Ads')",
            "button:has-text('Skip Ad')",
            "button:has-text('I have read')",
        ]
        for sel in access_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible():
                    await btn.click(timeout=1000)
                    await asyncio.sleep(1.0)
                    break
            except Exception:
                pass

        # Dismiss modal
        dismiss_selectors = [
            "[class*='close']",
            "button:has-text('I have read')",
            "svg[data-icon='xmark']",
            "button:has-text('Dismiss')",
        ]
        for sel in dismiss_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible():
                    await btn.click(timeout=1000)
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                pass

    async def _click_continue_buttons(self, page: Page) -> None:
        """Click standard 'Continue', 'Get Link', Safelink forms or 'Skip' buttons."""
        # Auto-submit WP Safelink / RTG / Indian Shortener forms if present
        try:
            submitted = await page.evaluate("""() => {
                const form = document.querySelector('form#rtg, form:has(input[name*="safelink"])');
                if (form) {
                    form.submit();
                    return true;
                }
                return false;
            }""")
            if submitted:
                await asyncio.sleep(1.0)
                return
        except Exception:
            pass

        button_selectors = [
            "button:has-text('Get Link')",
            "a:has-text('Get Link')",
            "button:has-text('Continue')",
            "a:has-text('Continue')",
            "button:has-text('Proceed')",
            "button:has-text('Skip Ad')",
            "a:has-text('Skip Ad')",
            "button:has-text('Click here to continue')",
            "button:has-text('GO TO LINK')",
            "button:has-text('Dual Tap')",
            "#btn-main",
            "#go-link button",
            "#getlink",
            "#getlink1",
            "#rtg-snp2",
            "#rtg-snp21",
            "form#rtg button",
        ]
        for sel in button_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible():
                    await btn.click(timeout=1000)
                    await asyncio.sleep(1.0)
                    break
            except Exception:
                pass

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
        except Exception:
            return None

        for key in _DEST_KEYS:
            for candidate_key, value in data.items():
                if candidate_key.lower() == key.lower() or candidate_key.lower().endswith(":" + key.lower()):
                    if isinstance(value, str) and self._looks_like_target(value, domain):
                        return value
        return None

    async def _extract_from_dom(self, page: Page, domain: str) -> str | None:
        """Scan <a> tags for an off-site URL."""
        try:
            anchors = await page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"""
            )
        except Exception:
            return None

        for href in anchors or []:
            if self._looks_like_target(href, domain):
                return href
        return None

    # ---------- Filters ----------

    def _looks_like_target(self, url: str, shortener_domain: str) -> bool:
        """
        Return True if `url` is a plausible external destination:
        - starts with http(s)
        - different host from shortener and its mirrors
        - not an ad/tracker/analytics/google tag host
        - not an intermediate redirect script or safelink bridge
        - not a static script, image, or stylesheet
        """
        if not url or not url.startswith("http"):
            return False
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()
        if not host or "." not in host:
            return False

        path_lower = parsed.path.lower()
        static_exts = (
            ".png", ".jpg", ".jpeg", ".gif", ".css", ".js", ".svg",
            ".webp", ".ico", ".woff", ".woff2", ".ttf", ".json", ".xml"
        )

        # Issue 2: Trusted file hosts are ALWAYS accepted
        is_trusted_host = any(host == tfh or host.endswith("." + tfh) for tfh in _TRUSTED_FILE_HOSTS)
        if is_trusted_host:
            return not path_lower.endswith(static_exts)

        short = shortener_domain.lower()
        if short.startswith("www."):
            short = short[4:]
        if host == short or host.endswith("." + short):
            return False

        for ad in _AD_HOSTS:
            if host == ad or host.endswith("." + ad):
                return False

        # Ignore Google search, sorry, or oauth/tag paths
        if "google." in host:
            if any(p in path_lower for p in ("/search", "/sorry", "/url", "/pagead", "/ads")):
                return False

        # Reject URLs that are intermediate shortener bridge scripts
        intermediate_scripts = (
            "no.php", "safe.php", "out.php", "go.php", "link.php",
            "redirect.php", "gateway.php",
        )
        if any(path_lower.endswith(s) or f"/{s}" in path_lower for s in intermediate_scripts):
            return False

        # Issue 2: Only reject intermediate redirects with ?url=, ?link=, ?go= when:
        # (a) points to the same shortener family, OR
        # (b) has a very short path (<=2 chars) AND no file extension AND no known file-host domain
        query_lower = parsed.query.lower()
        intermediate_params = (
            "link=", "url=", "go=", "dest=", "safelink=",
            "newwpsafelink", "newsafelink", "redirect=", "target=",
        )
        if any(p in query_lower for p in intermediate_params):
            clean_path = path_lower.strip("/")
            has_short_path = (len(clean_path) <= 2) and ("." not in clean_path.split("/")[-1])
            is_shortener_family = any(host == sf or host.endswith("." + sf) for sf in _SHORTENER_FAMILY)
            if is_shortener_family or has_short_path:
                return False

        # Ignore static resources
        if path_lower.endswith(static_exts):
            return False

        return True

    @staticmethod
    def _domain_meta(domain: str) -> dict[str, Any]:
        """Lookup domain entry in supported_domains.json."""
        try:
            data = json.loads(_DOMAINS_FILE.read_text(encoding="utf-8"))
            return data.get("domains", {}).get(domain, {})
        except Exception:
            return {}
