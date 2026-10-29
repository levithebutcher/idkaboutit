"""
resolvers/linkvertise_api.py
----------------------------
Linkvertise resolver using the publisher API replay method.

How it works (reverse-engineered, no official API key needed):
1. Extract the link ID from the path: /123456/abc -> "123456"
   (some paths use a slug like /abc123/xyz; the static endpoint still
   returns the numeric link id in data.link.id)
2. GET  /api/v1/redirect/link/static/{path}?X-Linkvertise-UT={token}
   -> returns data.link.id and data.tokens.TARGET
3. Build a serial JSON: {timestamp, random, link_id}
   - timestamp = current epoch ms
   - random    = 6548307 (constant, observed)
   - link_id   = from step 2
   Base64-encode it.
4. POST /api/v1/redirect/link/{path}/target?serial={b64}
   -> returns data.target = final URL

Token strategy:
- If a saved storage_state exists for linkvertise.com, read the
  X-LINKVERTISE-UT value from localStorage via Playwright.
- Otherwise fall back to a minimal Playwright warm-up that just
  reads the token (no DOM interaction needed).

If the API returns an error (403/404/500) or the token is missing,
return a failure with should_fallback=True so the orchestrator can
try the browser resolver instead.
"""

from __future__ import annotations

import base64
import json
import random
import time
from pathlib import Path

import httpx
from playwright.async_api import Browser

from config import settings
from resolvers.base import (
    BaseResolver,
    ResolutionMethod,
    ResolutionResult,
    ResolutionStatus,
    classify_error,
    clean_target_url,
)
from utils.logger import get_logger

logger = get_logger(__name__)

_API_BASE = "https://publisher.linkvertise.com/api/v1/redirect/link"
_DEFAULT_RANDOM = 6548307
_UT_STORAGE_KEY = "X-LINKVERTISE-UT"


class LinkvertiseAPIResolver(BaseResolver):
    """Resolves Linkvertise links via API replay."""

    name = "linkvertise_api"

    def __init__(self, browser: Browser | None = None) -> None:
        # Browser is optional — only used to extract the UT token if
        # storage_state is not available or stale.
        self._browser = browser

    # ---------- Main entry ----------

    async def resolve_impl(self, url: str, domain: str) -> ResolutionResult:
        # Normalise: strip query string, keep path only
        path = self._extract_path(url)
        if not path:
            return ResolutionResult.failure(
                ResolutionStatus.INVALID_URL,
                method=ResolutionMethod.API,
                error_class="InvalidPath",
                error_message=f"Could not extract path from {url}",
            )

        token = await self._get_token(url, domain)
        if not token:
            logger.warning("No X-LINKVERTISE-UT token available")
            return ResolutionResult.failure(
                ResolutionStatus.UNRESOLVABLE,
                method=ResolutionMethod.API,
                error_class="MissingToken",
                error_message="Could not obtain X-Linkvertise-UT token",
            )

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
                headers=self._headers(token),
            ) as client:
                # Step 1: static info (link id + target token)
                static = await self._fetch_static(client, path, token)
                if isinstance(static, ResolutionResult):
                    return static  # already a failure result

                link_id = static.get("link_id")
                if not link_id:
                    return ResolutionResult.failure(
                        ResolutionStatus.UNRESOLVABLE,
                        method=ResolutionMethod.API,
                        error_class="MissingLinkId",
                        error_message="Static endpoint did not return link id",
                    )

                # Step 2: build serial + POST target
                serial = self._build_serial(link_id)
                target = await self._fetch_target(client, path, serial, token)
                if isinstance(target, ResolutionResult):
                    return target

                if not target.startswith("http"):
                    return ResolutionResult.failure(
                        ResolutionStatus.UNRESOLVABLE,
                        method=ResolutionMethod.API,
                        error_class="InvalidTarget",
                        error_message=f"Target is not a URL: {target[:100]}",
                    )

                cleaned = clean_target_url(target)
                logger.info("Linkvertise API resolved -> %s", cleaned[:120])
                return ResolutionResult.success(
                    cleaned,
                    ResolutionMethod.API,
                    link_id=link_id,
                )

        except Exception as exc:
            status = classify_error(exc)
            logger.warning("Linkvertise API error: %s", exc)
            return ResolutionResult.failure(
                status,
                method=ResolutionMethod.API,
                error_class=type(exc).__name__,
                error_message=str(exc)[:300],
            )

    # ---------- Helpers ----------

    @staticmethod
    def _extract_path(url: str) -> str | None:
        """Return the path component without leading/trailing slashes."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        if not path:
            return None
        # Path can be "123456/abc" or "abc123/xyz" — keep as-is
        return path

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/129.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://linkvertise.com",
            "Referer": "https://linkvertise.com/",
            "X-Linkvertise-UT": token,
        }

    # ---------- Token acquisition ----------

    async def _get_token(self, url: str, domain: str) -> str | None:
        """
        Try two sources for the UT token:
        1. Saved storage_state JSON on disk (fast, no browser).
        2. Live Playwright read from linkvertise.com root (fallback).
        """
        # --- 1. Disk ---
        token = self._read_token_from_disk(domain)
        if token:
            logger.debug("Read UT token from storage_state on disk")
            return token

        # --- 2. Live browser ---
        if self._browser is not None:
            token = await self._read_token_from_browser(domain)
            if token:
                logger.debug("Read UT token via live Playwright")
                return token

        return None

    def _read_token_from_disk(self, domain: str) -> str | None:
        """Parse the saved storage_state JSON and pull X-LINKVERTISE-UT."""
        safe = domain.replace("/", "_").replace(":", "_")
        path = Path(settings.storage_state_dir) / f"{safe}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        for origin in data.get("origins", []):
            for item in origin.get("localStorage", []):
                if item.get("name") == _UT_STORAGE_KEY:
                    return item.get("value")
        return None

    async def _read_token_from_browser(self, domain: str) -> str | None:
        """Open linkvertise.com root, read localStorage, close page."""
        if self._browser is None:
            return None
        context = None
        try:
            from utils.session_factory import create_context, warmup_domain
            context = await create_context(
                self._browser, domain, use_saved_state=True
            )
            await warmup_domain(context, "https://linkvertise.com/")
            page = await context.new_page()
            token = await page.evaluate(
                f"() => window.localStorage.getItem('{_UT_STORAGE_KEY}')"
            )
            await page.close()
            return token or None
        except Exception as exc:
            logger.debug("Live token read failed: %s", exc)
            return None
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass

    # ---------- API calls ----------

    async def _fetch_static(
        self,
        client: httpx.AsyncClient,
        path: str,
        token: str,
    ) -> dict | ResolutionResult:
        """GET /static/{path} -> {'link_id': int} or failure result."""
        url = f"{_API_BASE}/static/{path}"
        params = {"X-Linkvertise-UT": token}
        resp = await client.get(url, params=params)

        if resp.status_code == 401 or resp.status_code == 403:
            return ResolutionResult.failure(
                ResolutionStatus.UNRESOLVABLE,
                method=ResolutionMethod.API,
                error_class="AuthRejected",
                error_message=f"Static endpoint returned {resp.status_code}",
            )
        if resp.status_code != 200:
            return ResolutionResult.failure(
                ResolutionStatus.UNRESOLVABLE,
                method=ResolutionMethod.API,
                error_class=f"HTTP{resp.status_code}",
                error_message=resp.text[:200],
            )

        try:
            body = resp.json()
            link_id = body.get("data", {}).get("link", {}).get("id")
            return {"link_id": link_id}
        except (json.JSONDecodeError, AttributeError) as exc:
            return ResolutionResult.failure(
                ResolutionStatus.UNRESOLVABLE,
                method=ResolutionMethod.API,
                error_class="BadJSON",
                error_message=str(exc)[:200],
            )

    async def _fetch_target(
        self,
        client: httpx.AsyncClient,
        path: str,
        serial_b64: str,
        token: str,
    ) -> str | ResolutionResult:
        """POST /{path}/target?serial={b64} -> target string or failure."""
        url = f"{_API_BASE}/{path}/target"
        params = {"serial": serial_b64, "X-Linkvertise-UT": token}
        resp = await client.post(url, params=params)

        if resp.status_code != 200:
            return ResolutionResult.failure(
                ResolutionStatus.UNRESOLVABLE,
                method=ResolutionMethod.API,
                error_class=f"HTTP{resp.status_code}",
                error_message=resp.text[:200],
            )

        try:
            body = resp.json()
            target = body.get("data", {}).get("target")
            if not target:
                return ResolutionResult.failure(
                    ResolutionStatus.UNRESOLVABLE,
                    method=ResolutionMethod.API,
                    error_class="MissingTarget",
                    error_message="Target endpoint returned no target",
                )
            return target
        except (json.JSONDecodeError, AttributeError) as exc:
            return ResolutionResult.failure(
                ResolutionStatus.UNRESOLVABLE,
                method=ResolutionMethod.API,
                error_class="BadJSON",
                error_message=str(exc)[:200],
            )

    @staticmethod
    def _build_serial(link_id: int | str) -> str:
        """Build base64-encoded serial JSON."""
        payload = {
            "timestamp": int(time.time() * 1000),
            "random": _DEFAULT_RANDOM,
            "link_id": link_id,
        }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")