"""
resolvers/base.py
-----------------
Shared contracts for all resolvers.

Defines:
- ResolutionStatus  : enum of possible outcomes.
- ResolutionResult  : structured result with helpers.
- BaseResolver      : abstract class every resolver must implement.
- classify_error()  : maps raw exceptions to ResolutionStatus.

All resolvers (linkvertise_api, browser_resolver, fallback_resolver) return
a ResolutionResult, and the orchestrator decides which one to call next.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------- Status enum ----------

class ResolutionStatus(str, Enum):
    """Possible outcomes of a resolution attempt."""

    SUCCESS = "success"                    # We got a valid target URL.
    TIMEOUT = "timeout"                    # Page/navigation exceeded timeout.
    CAPTCHA_GATE = "captcha_gate"          # reCAPTCHA/hCaptcha detected.
    AD_WALL = "ad_wall"                    # Money page with no off-site link.
    LOGIN_REQUIRED = "login_required"      # Site demands account login.
    UNRESOLVABLE = "unresolvable"          # No method could produce a link.
    UNSUPPORTED_DOMAIN = "unsupported_domain"
    INVALID_URL = "invalid_url"            # Input failed validation.
    INTERNAL_ERROR = "internal_error"      # Unexpected exception.

    @property
    def is_success(self) -> bool:
        return self is ResolutionStatus.SUCCESS

    @property
    def should_fallback(self) -> bool:
        """Should the orchestrator try a different strategy after this?"""
        return self in {
            ResolutionStatus.TIMEOUT,
            ResolutionStatus.CAPTCHA_GATE,
            ResolutionStatus.AD_WALL,
            ResolutionStatus.UNRESOLVABLE,
            ResolutionStatus.INTERNAL_ERROR,
        }


# ---------- Resolution method enum ----------

class ResolutionMethod(str, Enum):
    """Which technique produced the result."""

    API = "api"                    # Direct publisher API replay.
    BROWSER = "browser"            # Playwright automation.
    LOCALSTORAGE = "localstorage"  # Extracted from window.localStorage.
    DOM = "dom"                    # Extracted from rendered DOM.
    RAW_HTML = "raw_html"          # Extracted from raw initial HTML.
    HTTP_REDIRECT = "http_redirect"# Plain 301/302 chain.
    FALLBACK_API = "fallback_api"  # External bypass service.
    NONE = "none"                  # No method used (early failure).


# ---------- Result dataclass ----------

@dataclass
class ResolutionResult:
    """
    Outcome of a single resolution attempt.

    All resolvers return this. The orchestrator inspects `.status` and
    `.should_fallback` to decide whether to try another strategy.
    """

    status: ResolutionStatus
    target_url: str | None = None
    method: ResolutionMethod = ResolutionMethod.NONE

    # Diagnostic fields
    domain: str | None = None
    original_url: str | None = None
    error_class: str | None = None   # Short error code, e.g. "TimeoutError"
    error_message: str | None = None # Human-readable detail
    latency_ms: int = 0              # Populated by BaseResolver.timed()
    extra: dict[str, Any] = field(default_factory=dict)

    # ---------- Convenience constructors ----------

    @classmethod
    def success(
        cls,
        target_url: str,
        method: ResolutionMethod,
        *,
        latency_ms: int = 0,
        **extra: Any,
    ) -> "ResolutionResult":
        return cls(
            status=ResolutionStatus.SUCCESS,
            target_url=target_url,
            method=method,
            latency_ms=latency_ms,
            extra=extra,
        )

    @classmethod
    def failure(
        cls,
        status: ResolutionStatus,
        *,
        method: ResolutionMethod = ResolutionMethod.NONE,
        error_class: str | None = None,
        error_message: str | None = None,
        latency_ms: int = 0,
        **extra: Any,
    ) -> "ResolutionResult":
        return cls(
            status=status,
            method=method,
            error_class=error_class,
            error_message=error_message,
            latency_ms=latency_ms,
            extra=extra,
        )

    # ---------- Helpers ----------

    @property
    def should_fallback(self) -> bool:
        return self.status.should_fallback

    def __str__(self) -> str:
        if self.status.is_success:
            return f"[{self.status.value}] {self.target_url} ({self.method.value}, {self.latency_ms}ms)"
        return f"[{self.status.value}] {self.error_class or ''} {self.error_message or ''}".strip()


# ---------- Abstract base class ----------

class BaseResolver(ABC):
    """
    Interface every resolver must implement.

    Subclasses override `resolve_impl()` and get timing + error wrapping
    for free via `resolve()`.
    """

    #: Human-readable identifier for logs.
    name: str = "base"

    async def resolve(self, url: str, domain: str) -> ResolutionResult:
        """
        Public entry point. Times the attempt and catches unexpected
        exceptions, wrapping them as INTERNAL_ERROR.

        Never raises — always returns a ResolutionResult.
        """
        start = time.monotonic()
        try:
            result = await self.resolve_impl(url, domain)
            result.latency_ms = int((time.monotonic() - start) * 1000)
            if not result.domain:
                result.domain = domain
            if not result.original_url:
                result.original_url = url
            return result
        except Exception as exc:
            latency = int((time.monotonic() - start) * 1000)
            return ResolutionResult.failure(
                ResolutionStatus.INTERNAL_ERROR,
                method=ResolutionMethod.NONE,
                error_class=type(exc).__name__,
                error_message=str(exc)[:300],
                latency_ms=latency,
                domain=domain,
                original_url=url,
            )

    @abstractmethod
    async def resolve_impl(self, url: str, domain: str) -> ResolutionResult:
        """
        Subclass implementation. Called by `resolve()`, which adds timing
        and exception safety.
        """
        raise NotImplementedError


# ---------- Error classification ----------

def classify_error(exc: BaseException) -> ResolutionStatus:
    """
    Map a raw exception to a ResolutionStatus so the orchestrator can
    decide whether to retry, fall back, or abort.
    """
    name = type(exc).__name__.lower()
    msg = str(exc).lower()

    if "timeout" in name or "timeout" in msg:
        return ResolutionStatus.TIMEOUT
    if "captcha" in msg or "recaptcha" in msg or "hcaptcha" in msg:
        return ResolutionStatus.CAPTCHA_GATE
    if "login" in msg or "sign in" in msg:
        return ResolutionStatus.LOGIN_REQUIRED
    return ResolutionStatus.INTERNAL_ERROR


# ---------- URL cleanliness helper ----------

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_eid", "ref", "ref_src", "igshid",
}


def clean_target_url(url: str, *, strip_tracking: bool = True) -> str:
    """
    Remove common tracking parameters from a final target URL.
    Used by all resolvers before returning success.
    """
    if not strip_tracking or "?" not in url:
        return url

    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    parsed = urlparse(url)
    if not parsed.query:
        return url

    kept = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    new_query = urlencode(kept, doseq=True)
    cleaned = urlunparse(parsed._replace(query=new_query))
    return cleaned