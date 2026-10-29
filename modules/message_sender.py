"""
modules/message_sender.py
-------------------------
Formats and sends resolution results back to the user via Telegram.

Responsibilities:
- Convert a ResolutionResult into a clean, readable Telegram message.
- Escape Markdown safely (user content may contain special characters).
- Split long messages if needed (Telegram limit ~4096 chars).
- Handle Telegram-side send errors gracefully.
"""

from __future__ import annotations

import html
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError

from config import settings
from modules.url_parser import ParsedURL
from resolvers.base import ResolutionMethod, ResolutionResult, ResolutionStatus
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------- Emoji by status ----------

_STATUS_ICON = {
    ResolutionStatus.SUCCESS: "✅",
    ResolutionStatus.TIMEOUT: "⏱️",
    ResolutionStatus.CAPTCHA_GATE: "🤖",
    ResolutionStatus.AD_WALL: "🚧",
    ResolutionStatus.LOGIN_REQUIRED: "🔒",
    ResolutionStatus.UNRESOLVABLE: "❌",
    ResolutionStatus.UNSUPPORTED_DOMAIN: "🚫",
    ResolutionStatus.INVALID_URL: "⚠️",
    ResolutionStatus.INTERNAL_ERROR: "💥",
}

_STATUS_HUMAN = {
    ResolutionStatus.SUCCESS: "Link resolve ho gaya!",
    ResolutionStatus.TIMEOUT: "Timeout — site ne jawab nahi diya.",
    ResolutionStatus.CAPTCHA_GATE: "Captcha mila — bypass nahi ho paya.",
    ResolutionStatus.AD_WALL: "Sirf ad-wall mila, final link nahi.",
    ResolutionStatus.LOGIN_REQUIRED: "Login maang raha hai — bot login nahi kar sakta.",
    ResolutionStatus.UNRESOLVABLE: "Link resolve nahi ho paya.",
    ResolutionStatus.UNSUPPORTED_DOMAIN: "Yeh shortener support nahi hai.",
    ResolutionStatus.INVALID_URL: "Yeh valid URL nahi hai.",
    ResolutionStatus.INTERNAL_ERROR: "Internal error aaya.",
}


# ---------- Formatting helpers ----------

def _esc(text: str | None) -> str:
    """HTML-escape any user-supplied string for ParseMode.HTML."""
    if not text:
        return ""
    return html.escape(str(text), quote=True)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _format_success(parsed: ParsedURL, result: ResolutionResult) -> str:
    method = result.method.value if result.method else "unknown"
    latency = f"{result.latency_ms / 1000:.1f}s"
    return (
        f"{_STATUS_ICON[ResolutionStatus.SUCCESS]} <b>Link resolved!</b>\n\n"
        f"🔗 <b>Source:</b> <code>{_esc(parsed.domain)}</code>\n"
        f"⚙️ <b>Method:</b> <code>{_esc(method)}</code> · "
        f"⏱️ <b>Time:</b> {latency}\n\n"
        f"<b>Final link:</b>\n"
        f"<code>{_esc(result.target_url)}</code>"
    )


def _format_failure(parsed: ParsedURL, result: ResolutionResult) -> str:
    icon = _STATUS_ICON.get(result.status, "❌")
    human = _STATUS_HUMAN.get(result.status, "Resolve nahi ho paya.")
    lines = [
        f"{icon} <b>{_esc(human)}</b>",
        "",
        f"🔗 <b>Domain:</b> <code>{_esc(parsed.domain)}</code>",
    ]
    if result.error_class:
        lines.append(f"⚠️ <b>Reason:</b> <code>{_esc(result.error_class)}</code>")

    # Per-status actionable hint
    if result.status == ResolutionStatus.CAPTCHA_GATE:
        lines.append("")
        lines.append(
            "💡 <i>Is site par captcha hai. "
            "Browser mein manually kholein.</i>"
        )
    elif result.status == ResolutionStatus.UNSUPPORTED_DOMAIN:
        lines.append("")
        lines.append(
            "💡 <i>/supported command se poori list dekhein.</i>"
        )
    elif result.status == ResolutionStatus.TIMEOUT:
        lines.append("")
        lines.append("💡 <i>Thodi der baad dobara try karein.</i>")

    return "\n".join(lines)


def format_result(parsed: ParsedURL, result: ResolutionResult) -> str:
    """Return the HTML-formatted message body for a result."""
    if result.status.is_success:
        return _format_success(parsed, result)
    return _format_failure(parsed, result)


# ---------- Sending ----------

async def send_result(
    bot: Bot,
    chat_id: int,
    parsed: ParsedURL,
    result: ResolutionResult,
    *,
    reply_to: int | None = None,
) -> None:
    """
    Send a single resolution result to a user. Handles formatting,
    length splitting, and Telegram API errors.
    """
    text = format_result(parsed, result)

    # Telegram hard limit is 4096 chars; split if larger (very rare).
    chunks = _split_text(text, limit=4000)

    for i, chunk in enumerate(chunks):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_to_message_id=reply_to if i == 0 else None,
            )
        except TelegramAPIError as exc:
            logger.warning(
                "Failed to send to chat %s: %s", chat_id, exc
            )
            # Final fallback: send as plain text (no HTML) so user still sees it
            if i == 0:
                try:
                    plain = _strip_html(text)
                    await bot.send_message(
                        chat_id=chat_id,
                        text=plain[:4000],
                        disable_web_page_preview=True,
                    )
                except TelegramAPIError as exc2:
                    logger.error(
                        "Plain-text fallback also failed for chat %s: %s",
                        chat_id, exc2,
                    )
            return


def _split_text(text: str, limit: int = 4000) -> list[str]:
    """Split text at line boundaries to stay under Telegram's limit."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > limit and current:
            chunks.append("".join(current))
            current = []
            size = 0
        current.append(line)
        size += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


def _strip_html(text: str) -> str:
    """Ultra-simple HTML tag stripper for plain-text fallback."""
    import re
    no_tags = re.sub(r"<[^>]+>", "", text)
    return html.unescape(no_tags)


# ---------- Batch entry used by QueueManager callback ----------

class MessageSender:
    """
    Wraps `send_result` in the exact signature the QueueManager expects:
        async def (user_id, chat_id, parsed, result) -> None
    """

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def __call__(
        self,
        user_id: int,
        chat_id: int,
        parsed: ParsedURL,
        result: ResolutionResult,
    ) -> None:
        await send_result(self._bot, chat_id, parsed, result)