"""
resolvers/orchestrator.py
-------------------------
Central dispatcher. Decides which resolver(s) to use for a given domain,
chains fallbacks, and handles retries.

Decision logic:
1. Look up domain in supported_domains.json.
2. If unsupported -> UNSUPPORTED_DOMAIN (no retry).
3. If strategy == "linkvertise_api" -> try API first, then browser, then fallback.
4. If strategy == "browser"          -> try browser first, then fallback.
5. If strategy == "fallback"         -> try fallback only.
6. If any stage returns should_fallback=True, advance to the next stage.
7. Retries (with backoff) are only done for TIMEOUT / INTERNAL_ERROR.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from playwright.async_api import Browser

from config import settings
from resolvers.base import (
    BaseResolver,
    ResolutionMethod,
    ResolutionResult,
    ResolutionStatus,
)
from resolvers.browser_resolver import BrowserResolver
from resolvers.fallback_resolver import FallbackResolver
from resolvers.linkvertise_api import LinkvertiseAPIResolver
from utils.logger import get_logger

logger = get_logger(__name__)

_DOMAINS_FILE = Path(__file__).resolve().parent.parent / "data" / "supported_domains.json"


# ---------- Domain lookup ----------

def _load_domains() -> dict[str, Any]:
    try:
        return json.loads(_DOMAINS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Failed to load supported_domains.json: %s", exc)
        return {"domains": {}}


def _resolve_domain_entry(domain: str) -> dict[str, Any] | None:
    """
    Match the incoming domain against the domain table, including
    alternate_hosts (e.g. link-to.net -> linkvertise.com entry).
    """
    data = _load_domains()
    domains = data.get("domains", {})

    # Direct match
    if domain in domains:
        return {"canonical": domain, **domains[domain]}

    # Alternate-host match
    for canonical, meta in domains.items():
        alternates = meta.get("alternate_hosts", [])
        if domain in alternates:
            return {"canonical": canonical, **meta}

    return None


# ---------- Orchestrator ----------

class ResolverOrchestrator:
    """
    Holds all resolver instances and dispatches based on domain config.
    """

    def __init__(self, browser: Browser) -> None:
        self._browser = browser
        self._linkvertise = LinkvertiseAPIResolver(browser=browser)
        self._browser_resolver = BrowserResolver(browser=browser)
        self._fallback = FallbackResolver()

    # ---------- Public entry ----------

    async def resolve(self, url: str, domain: str) -> ResolutionResult:
        """
        Top-level entry: pick the right resolver chain for `domain`,
        run with retries + fallback, and return the final result.
        """
        entry = _resolve_domain_entry(domain)

        if entry is None:
            logger.info("Unsupported domain: %s", domain)
            return ResolutionResult.failure(
                ResolutionStatus.UNSUPPORTED_DOMAIN,
                error_class="UnsupportedDomain",
                error_message=f"{domain} is not in supported_domains.json",
                domain=domain,
                original_url=url,
            )

        strategy = entry.get("strategy", "browser")
        chain = self._build_chain(strategy)
        logger.info("Resolving %s via chain: %s", domain, [r.name for r in chain])

        last_result: ResolutionResult | None = None

        for resolver in chain:
            result = await self._attempt_with_retries(resolver, url, domain)
            last_result = result

            if result.status.is_success:
                # On success, clear any stale failure storage
                return result

            if not result.should_fallback:
                # Permanent failure — no point trying next resolver
                logger.info(
                    "[%s] Permanent failure (%s), stopping chain",
                    domain, result.status.value,
                )
                return result

            logger.info(
                "[%s] %s returned %s, trying next resolver",
                domain, resolver.name, result.status.value,
            )

        # All resolvers exhausted
        assert last_result is not None
        logger.warning("[%s] All resolvers failed", domain)
        return last_result

    # ---------- Chain building ----------

    def _build_chain(self, strategy: str) -> list[BaseResolver]:
        """
        Build the ordered list of resolvers to try based on strategy string.
        """
        if strategy == "linkvertise_api":
            chain: list[BaseResolver] = [self._linkvertise, self._browser_resolver]
        elif strategy == "browser":
            chain = [self._browser_resolver]
        elif strategy == "fallback":
            chain = [self._fallback]
        else:
            chain = [self._browser_resolver]

        # Always end with fallback unless fallback is already in chain or disabled
        if (
            settings.enable_fallback
            and self._fallback not in chain
            and strategy != "fallback"
        ):
            chain.append(self._fallback)

        return chain

    # ---------- Retry wrapper ----------

    async def _attempt_with_retries(
        self,
        resolver: BaseResolver,
        url: str,
        domain: str,
    ) -> ResolutionResult:
        """
        Run a single resolver with retries + exponential backoff.
        Only retries on TIMEOUT / INTERNAL_ERROR.
        """
        max_attempts = 1 + settings.max_retries
        last: ResolutionResult | None = None

        for attempt in range(1, max_attempts + 1):
            logger.debug("[%s] %s attempt %d/%d",
                         domain, resolver.name, attempt, max_attempts)

            result = await resolver.resolve(url, domain)
            last = result

            if result.status.is_success:
                return result

            retryable = result.status in {
                ResolutionStatus.TIMEOUT,
                ResolutionStatus.INTERNAL_ERROR,
            }
            if not retryable or attempt == max_attempts:
                return result

            backoff = settings.retry_backoff_s * attempt
            logger.info(
                "[%s] %s attempt %d failed (%s), retrying in %.1fs",
                domain, resolver.name, attempt,
                result.status.value, backoff,
            )
            await asyncio.sleep(backoff)

        assert last is not None
        return last


# ---------- Module-level helper ----------

_global_orchestrator: ResolverOrchestrator | None = None


def get_orchestrator(browser: Browser | None = None) -> ResolverOrchestrator:
    """
    Singleton accessor. Pass `browser` on first call; later calls reuse it.
    """
    global _global_orchestrator
    if _global_orchestrator is None:
        if browser is None:
            raise RuntimeError("Orchestrator not initialized; pass browser=")
        _global_orchestrator = ResolverOrchestrator(browser)
        logger.info("ResolverOrchestrator initialized")
    return _global_orchestrator


async def resolve_url(url: str, domain: str) -> ResolutionResult:
    """
    Convenience function for callers who just want 'resolve this URL'.
    Raises if orchestrator has not been initialized yet.
    """
    if _global_orchestrator is None:
        raise RuntimeError("Orchestrator not initialized; call get_orchestrator() first")
    return await _global_orchestrator.resolve(url, domain)