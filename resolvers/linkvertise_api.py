"""
resolvers/linkvertise_api.py
----------------------------
Linkvertise resolver using dynamic param decoding, publisher API replay,
and automated GraphQL task-wall auto-completion.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
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
_GRAPHQL_ENDPOINT = "https://publisher.linkvertise.com/graphql"


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
    """Resolves Linkvertise links via dynamic decoding, API replay, and GraphQL task bypass."""

    name = "linkvertise_api"

    def __init__(self, browser: Browser | None = None) -> None:
        self._browser = browser

    # ---------- Main entry ----------

    async def resolve_impl(self, url: str, domain: str) -> ResolutionResult:
        # 1. Fast Path: Check if this is a dynamic link with ?r= (Base64 target)
        dynamic_dest = _decode_dynamic_r_param(url)
        if dynamic_dest:
            logger.info("[%s] Decoded dynamic destination directly: %s", domain, dynamic_dest)
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
        id_match = re.search(r"/(\d+)/([^/?#]+)", clean_url)
        if not id_match:
            return ResolutionResult.failure(
                ResolutionStatus.INVALID_URL,
                method=ResolutionMethod.API,
                error_class="InvalidPath",
                error_message=f"Could not extract Linkvertise path from {url}",
            )

        user_id = id_match.group(1)
        link_slug = id_match.group(2)
        link_path = f"/{user_id}/{link_slug}"

        # 3. Native Linkvertise API via AppleTV User-Agent impersonation
        try:
            headers = {
                "User-Agent": "AppleTV6,2/11.1",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            static_api = f"{_API_BASE}/static{link_path}"
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
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
            logger.debug("[%s] AppleTV static Linkvertise check failed: %s", domain, exc)

        # 4. GraphQL Task Wall Automated Solver
        try:
            gql_res = await self._solve_graphql_tasks(user_id, link_slug, domain)
            if gql_res:
                logger.info("[%s] Resolved via GraphQL task solver: %s", domain, gql_res)
                return ResolutionResult.success(clean_target_url(gql_res), ResolutionMethod.API)
        except Exception as exc:
            logger.warning("[%s] GraphQL task solver error: %s", domain, exc)

        # 5. Fallback to BrowserResolver
        return ResolutionResult.failure(
            ResolutionStatus.UNRESOLVABLE,
            method=ResolutionMethod.API,
            error_class="APIReplayIncomplete",
            error_message="GraphQL solver did not reveal destination; routing to browser solver",
        )

    async def _solve_graphql_tasks(self, user_id: str, link_slug: str, domain: str) -> str | None:
        """
        Executes Linkvertise's official GraphQL task cycle:
        1. getContent -> inspect tasks
        2. startTask + completeTask for each ad/wait task
        3. Poll getContent until target URL / paste is returned.
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
            "Origin": "https://linkvertise.com",
            "Referer": "https://linkvertise.com/",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        link_input = {
            "userIdAndUrl": {
                "user_id": user_id,
                "url": link_slug,
            }
        }

        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            # Helper to query getContent
            async def query_content():
                payload = {
                    "operationName": "getContent",
                    "variables": {"input": link_input, "origin": "sharing"},
                    "query": """query getContent($input: PublicLinkIdentificationInput!, $origin: String) {
                      getContent(input: $input, origin: $origin) {
                        ... on DetailPageTargetData {
                          type
                          url
                          paste
                        }
                        ... on ContentAccessTaskSet {
                          tasks {
                            id
                            status
                            ... on AdTask {
                              adIndex
                              adsTotal
                            }
                            ... on WaitTask {
                              remainingWaitingTime
                              adsTotal
                            }
                          }
                        }
                      }
                    }""",
                }
                resp = await client.post(_GRAPHQL_ENDPOINT, json=payload, headers=headers)
                if resp.status_code == 200:
                    return resp.json().get("data", {}).get("getContent", {})
                return {}

            # Helper to mutate task (start or complete)
            async def mutate_task(op: str, task_id: str):
                payload = {
                    "operationName": op,
                    "variables": {"input": link_input, "task_id": task_id},
                    "query": f"""mutation {op}($input: PublicLinkIdentificationInput!, $task_id: String!) {{
                      {op}(input: $input, task_id: $task_id) {{
                        id
                        status
                      }}
                    }}""",
                }
                resp = await client.post(_GRAPHQL_ENDPOINT, json=payload, headers=headers)
                if resp.status_code == 200:
                    return resp.json().get("data", {}).get(op, {})
                return {}

            for step in range(8):
                content = await query_content()
                if not content:
                    await asyncio.sleep(0.5)
                    continue

                # Check if target is already revealed
                target_url = content.get("url") or content.get("paste")
                if target_url and isinstance(target_url, str):
                    return target_url

                tasks = content.get("tasks", [])
                if not tasks:
                    break

                for t in tasks:
                    tid = t.get("id")
                    status = t.get("status")
                    if tid and status in ("OPEN", "IN_PROGRESS"):
                        await mutate_task("startTask", tid)
                        await asyncio.sleep(0.3)
                        await mutate_task("completeTask", tid)

                await asyncio.sleep(0.5)

        return None
