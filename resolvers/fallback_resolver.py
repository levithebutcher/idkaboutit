"""
resolvers/fallback_resolver.py
------------------------------
Last-resort resolver that delegates to an external bypass service
when browser automation fails (CAPTCHA_GATE / AD_WALL / TIMEOUT).

Supports two integration modes:

1. **Generic HTTP API** (recommended):
   - Configurable via FALLBACK_API_URL in .env
   - Expects: POST {url} with optional Bearer auth
   - Handles common response shapes:
       {"status": "success", "result": "https://..."}
       {"result": "https://..."}
       {"url": "https://..."}
       {"destination": "https://..."}
   - Compatible with bypass.vip, custom self-hosted bypass APIs, etc.

2. **Crowd-bypass endpoint** (FastForward):
   - GET https://api.fastforward.cc/bypass?url={url} or similar
   - Detected by hostname in FALLBACK_API_URL.

If no FALLBACK_API_URL is configured, this resolver is a no-op and
returns UNRESOLVABLE (so the orchestrator can decide what to do).
"""

from __future__ import annotations

from urllib.parse import quote, urlparse

import httpx

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


class FallbackResolver(BaseResolver):
    """Delegates resolution to an external bypass API."""

    name = "fallback"

    def __init__(self) -> None:
        self._api_url = settings.fallback_api_url
        self._api_key = (
            settings.fallback_api_key.get_secret_value()
            if settings.fallback_api_key else None
        )

    # ---------- Main entry ----------

    async def resolve_impl(self, url: str, domain: str) -> ResolutionResult:
        if not settings.enable_fallback:
            return ResolutionResult.failure(
                ResolutionStatus.UNRESOLVABLE,
                method=ResolutionMethod.FALLBACK_API,
                error_class="FallbackDisabled",
                error_message="Fallback resolver disabled in settings",
            )

        if not self._api_url:
            return ResolutionResult.failure(
                ResolutionStatus.UNRESOLVABLE,
                method=ResolutionMethod.FALLBACK_API,
                error_class="NoFallbackAPI",
                error_message="FALLBACK_API_URL not configured",
            )

        try:
            if self._is_fastforward_mode():
                return await self._call_fastforward(url, domain)
            return await self._call_generic(url, domain)
        except Exception as exc:
            status = classify_error(exc)
            logger.warning("Fallback API error: %s", exc)
            return ResolutionResult.failure(
                status,
                method=ResolutionMethod.FALLBACK_API,
                error_class=type(exc).__name__,
                error_message=str(exc)[:300],
            )

    # ---------- Mode detection ----------

    def _is_fastforward_mode(self) -> bool:
        """Detect if the configured URL is FastForward's crowd-bypass API."""
        if not self._api_url:
            return False
        return "fastforward" in self._api_url.lower()

    # ---------- Generic JSON API ----------

    async def _call_generic(self, url: str, domain: str) -> ResolutionResult:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; ResolverBot/1.0)",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        payload = {"url": url}

        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0)) as client:
            logger.debug("POST %s (fallback)", self._api_url)
            resp = await client.post(self._api_url, json=payload, headers=headers)

        if resp.status_code in (401, 403):
            return ResolutionResult.failure(
                ResolutionStatus.UNRESOLVABLE,
                method=ResolutionMethod.FALLBACK_API,
                error_class="AuthRejected",
                error_message=f"Fallback API returned {resp.status_code}",
            )

        if resp.status_code != 200:
            return ResolutionResult.failure(
                ResolutionStatus.UNRESOLVABLE,
                method=ResolutionMethod.FALLBACK_API,
                error_class=f"HTTP{resp.status_code}",
                error_message=resp.text[:200],
            )

        # Try JSON first, then plain text
        try:
            data = resp.json()
        except Exception:
            text = resp.text.strip()
            if text.startswith("http"):
                return self._success_from_url(text, domain)
            return ResolutionResult.failure(
                ResolutionStatus.UNRESOLVABLE,
                method=ResolutionMethod.FALLBACK_API,
                error_class="BadResponse",
                error_message=text[:200],
            )

        target = self._extract_target_from_json(data)
        if target:
            return self._success_from_url(target, domain)

        return ResolutionResult.failure(
            ResolutionStatus.UNRESOLVABLE,
            method=ResolutionMethod.FALLBACK_API,
            error_class="NoTargetInResponse",
            error_message=str(data)[:200],
        )

    # ---------- FastForward crowd-bypass ----------

    async def _call_fastforward(self, url: str, domain: str) -> ResolutionResult:
        encoded = quote(url, safe="")
        endpoint = f"{self._api_url}?url={encoded}"
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; ResolverBot/1.0)",
            "Accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0)) as client:
            logger.debug("GET %s (fastforward fallback)", endpoint)
            resp = await client.get(endpoint, headers=headers)

        if resp.status_code != 200:
            return ResolutionResult.failure(
                ResolutionStatus.UNRESOLVABLE,
                method=ResolutionMethod.FALLBACK_API,
                error_class=f"HTTP{resp.status_code}",
                error_message=resp.text[:200],
            )

        try:
            data = resp.json()
        except Exception:
            text = resp.text.strip()
            if text.startswith("http"):
                return self._success_from_url(text, domain)
            return ResolutionResult.failure(
                ResolutionStatus.UNRESOLVABLE,
                method=ResolutionMethod.FALLBACK_API,
                error_class="BadResponse",
                error_message=text[:200],
            )

        target = self._extract_target_from_json(data)
        if target:
            return self._success_from_url(target, domain)

        return ResolutionResult.failure(
            ResolutionStatus.UNRESOLVABLE,
            method=ResolutionMethod.FALLBACK_API,
            error_class="NoTargetInResponse",
            error_message=str(data)[:200],
        )

    # ---------- Response parsing ----------

    @staticmethod
    def _extract_target_from_json(data: dict) -> str | None:
        """
        Try common response shapes across bypass APIs:
          - {"status": "success", "result": "https://..."}
          - {"result": {"url": "https://..."}}
          - {"destination": "https://..."}
          - {"url": "https://..."}
          - {"bypassed": "https://..."}
        """
        if not isinstance(data, dict):
            return None

        # Direct string values
        for key in ("result", "destination", "url", "bypassed", "link", "target"):
            val = data.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
            if isinstance(val, dict):
                inner = FallbackResolver._extract_target_from_json(val)
                if inner:
                    return inner

        # Nested "data" object
        if isinstance(data.get("data"), dict):
            inner = FallbackResolver._extract_target_from_json(data["data"])
            if inner:
                return inner

        return None

    @staticmethod
    def _success_from_url(target: str, domain: str) -> ResolutionResult:
        cleaned = clean_target_url(target)
        logger.info("Fallback resolved %s -> %s", domain, cleaned[:120])
        return ResolutionResult.success(cleaned, ResolutionMethod.FALLBACK_API)