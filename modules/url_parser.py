"""
modules/url_parser.py
---------------------
Extracts and validates URLs from Telegram messages.

Strategy:
1. Prefer Telegram's native message entities (type="url" / "text_link") —
   these are authoritative and never include Markdown artifacts.
2. Fall back to regex on message.text ONLY if no URL entity exists.
3. Strip Markdown/HTML wrappers (<u>, **, __, etc.) before parsing.
4. Validate via urlparse: require http(s) scheme + non-empty netloc.
5. Reject localhost, private IPs, and non-http schemes.
6. Deduplicate while preserving order.
"""

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from aiogram.types import Message

from utils.logger import get_logger

logger = get_logger(__name__)

# ---------- Regexes ----------

# Conservative URL regex — matches http(s) URLs, stops at whitespace
_URL_REGEX = re.compile(
    r"https?://[^\s<>\"'`]+",
    re.IGNORECASE,
)

# Markdown/HTML artifacts we must strip if they leak into a candidate
_MD_ARTIFACT_REGEX = re.compile(
    r"</?(?:u|b|i|s|code|pre|a)\b[^>]*>",  # HTML-ish tags
    re.IGNORECASE,
)

# Trailing punctuation that often follows a URL in prose
_TRAILING_PUNCT = ".,;:!?)]}'\">"


# ---------- Data model ----------

@dataclass(frozen=True)
class ParsedURL:
    """Result of parsing a single URL candidate."""
    original: str       # original text as received
    normalized: str     # cleaned URL (markdown stripped, punctuation removed)
    domain: str         # netloc without "www.", lowercased
    scheme: str         # "http" or "https"


# ---------- Helpers ----------

def _strip_markdown(text: str) -> str:
    """Remove <u>...</u>, **, __, ` wrappers from a candidate string."""
    text = _MD_ARTIFACT_REGEX.sub("", text)
    text = text.strip()
    # Common markdown emphasis markers at the edges
    for marker in ("**", "__", "`", "*", "_"):
        if text.startswith(marker) and text.endswith(marker) and len(text) > 2 * len(marker):
            text = text[len(marker):-len(marker)]
    return text.strip()


def _strip_trailing_punct(url: str) -> str:
    """Remove trailing punctuation that's usually prose, not part of the URL."""
    while url and url[-1] in _TRAILING_PUNCT:
        url = url[:-1]
    return url


def _is_public_host(host: str) -> bool:
    """
    Reject localhost and private/reserved IPs.
    Returns True if host looks like a public domain or public IP.
    """
    if not host:
        return False
    host_lower = host.lower()
    if host_lower in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return False

    # Try IPv4 / IPv6
    try:
        ip = ipaddress.ip_address(host_lower)
        return not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        )
    except ValueError:
        pass

    # Domain must contain a dot (rejects bare hostnames)
    return "." in host_lower and not host_lower.endswith(".local")


def _normalize_domain(netloc: str) -> str:
    """Lowercase and strip 'www.' and port from netloc."""
    domain = netloc.lower()
    if "@" in domain:          # strip userinfo
        domain = domain.rsplit("@", 1)[-1]
    if domain.startswith("www."):
        domain = domain[4:]
    if ":" in domain:          # strip port
        domain = domain.split(":", 1)[0]
    return domain


def _validate(raw: str) -> ParsedURL | None:
    """Return a ParsedURL if `raw` is a valid public http(s) URL, else None."""
    candidate = _strip_trailing_punct(_strip_markdown(raw))
    if not candidate:
        return None

    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None

    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.netloc:
        return None
    if not _is_public_host(parsed.hostname or ""):
        return None

    domain = _normalize_domain(parsed.netloc)
    if not domain:
        return None

    return ParsedURL(
        original=raw,
        normalized=candidate,
        domain=domain,
        scheme=parsed.scheme,
    )


# ---------- Main extraction ----------

def extract_urls(message: Message, *, max_urls: int = 5) -> list[ParsedURL]:
    """
    Extract valid URLs from a Telegram message.

    Priority:
      1. message.entities of type "url"       → slice from message.text
      2. message.entities of type "text_link" → use entity.url
      3. regex on message.text as last resort

    Returns de-duplicated list of ParsedURL, preserving order.
    """
    if not message.text and not message.caption:
        return []

    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []

    candidates: list[str] = []

    # --- 1. From entities ---
    for ent in entities:
        etype = getattr(ent, "type", None)
        if etype == "url":
            try:
                slice_ = text[ent.offset: ent.offset + ent.length]
            except Exception:
                continue
            candidates.append(slice_)
        elif etype == "text_link":
            url = getattr(ent, "url", None)
            if url:
                candidates.append(url)

    # --- 2. Regex fallback ---
    if not candidates:
        candidates = _URL_REGEX.findall(text)

    # --- 3. Validate + dedup ---
    results: list[ParsedURL] = []
    seen: set[str] = set()

    for raw in candidates:
        parsed = _validate(raw)
        if not parsed:
            logger.debug("Rejected candidate URL: %r", raw[:120])
            continue
        if parsed.normalized in seen:
            continue
        seen.add(parsed.normalized)
        results.append(parsed)
        if len(results) >= max_urls:
            break

    logger.info("Extracted %d URL(s) from message", len(results))
    return results


# ---------- Standalone test ----------

if __name__ == "__main__":
    # Quick self-test without Telegram — run: python -m modules.url_parser
    class _FakeEnt:
        def __init__(self, type_, offset, length, url=None):
            self.type = type_
            self.offset = offset
            self.length = length
            self.url = url

    class _FakeMsg:
        def __init__(self, text, entities):
            self.text = text
            self.caption = None
            self.entities = entities
            self.caption_entities = None

    sample_text = "Try https://linkvertise.com/12345/abc and <u>https://speedy.link/xyz</u>"
    start = sample_text.index("https://linkvertise")
    ent = _FakeEnt("url", start, len("https://linkvertise.com/12345/abc"))

    msg = _FakeMsg(sample_text, [ent])
    for p in extract_urls(msg):
        print(f"{p.domain:30} -> {p.normalized}")