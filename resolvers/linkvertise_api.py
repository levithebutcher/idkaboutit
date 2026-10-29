"""
resolvers/linkvertise_api.py
----------------------------
Linkvertise resolver using dynamic param decoding and publisher API replay.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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


def _decode_dynamic_r_param(url: str) -> str:
    """
    Decodes the ?r= base64 destination parameter found in Linkvertise dynamic links.
    e.g. https://link-to.net/123/456/dynamic?r=aHR0cHM6Ly9leGFtcGxlLmNvbQ==
    """
    try:
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        r_val = query_params.get("r", [None])[0]
        if not r_val:
            m = re.search(r"[?&]r=([a-zA-Z0-9_\-+/=]+)", url)
            if m:
                r_val = m.group(1)

        if r_val:
            padding = len(r_val) % 4
            if padding:
                r_val += "=" * (4 - padding)
            decoded = base64.b64decode(r_val).decode("utf-8", errors="ignore")
            if decoded.startswith("http://") or decoded.startswith("https://"):
                return decoded
    except Exception:
        pass
    return ""


class LinkvertiseAPIResolver(BaseResolver):
    """Resolves Linkvertise links via dynamic decoding and API replay."""

    name = "linkvertise_api"

    def __init__(self, browser: Browser | None = None) -> None:
        self._browser = browser

    # ---------- Main entry ----------

    async def resolve_impl(self, url: str, domain: str) -> ResolutionResult:
        # 1. Fast Path: Check if this is a dynamic link with ?r= (Base64 target)
        dynamic_dest = _decode_dynamic_r_param(url)
        if dynamic_dest:
            logger.info("Decoded dynamic Linkvertise destination directly: %s", dynamic_dest)
            return ResolutionResult.success(
                clean_target_url(dynamic_dest),
                ResolutionMethod.API,
            )

        # 2. Extract Linkvertise link path: /<user_id>/<link_slug>
        clean_url = (
            url.replace("%3D", " ")
            .replace("&o=sharing", "")
            .replace("?o=sharing", "")
            .replace("dynamic?r=", "dynamic/?r=")
        )
        id_match = re.search(r"/\d+/[^/?#]+", clean_url)
        if not id_match:
            return ResolutionResult.failure(
                ResolutionStatus.INVALID_URL,
                method=ResolutionMethod.API,
                error_class="InvalidPath",
                error_message=f"Could not extract Linkvertise path from {url}",
            )

        link_path = id_match.group(0)

        # 3. Native Linkvertise API via AppleTV User-Agent impersonation
        try:
            headers = {
                "User-Agent": "AppleTV6,2/11.1",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            static_api = f"{_API_BASE}/static{link_path}"
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                resp = await client.get(static_api, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict):
                        link_info = data.get("data", {})
                        if isinstance(link_info, dict):
                            link_obj = link_info.get("link", {})
                            if link_obj.get("target_type") == "paste" and link_obj.get("raw"):
                                return ResolutionResult.success(
                                    clean_target_url(link_obj["raw"]),
                                    ResolutionMethod.API,
                                )
                            if link_obj.get("url"):
                                return ResolutionResult.success(
                                    clean_target_url(link_obj["url"]),
                                    ResolutionMethod.API,
                                )
        except Exception as exc:
            logger.debug("AppleTV static Linkvertise check failed: %s", exc)

        # 4. If direct API replay didn't resolve, trigger fallback to BrowserResolver
        return ResolutionResult.failure(
            ResolutionStatus.UNRESOLVABLE,
            method=ResolutionMethod.API,
            error_class="APIReplayIncomplete",
            error_message="Static API did not reveal destination; routing to browser solver",
        )
