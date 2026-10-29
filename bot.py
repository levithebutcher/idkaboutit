"""
bot.py
------
Main entry point for the Linkvertise & Shortener Bypass Telegram Bot.

Responsibilities:
- Initialize structured logging.
- Validate configuration & required static data files.
- Launch shared Playwright browser instance.
- Initialize ResolverOrchestrator and QueueManager with MessageSender.
- Register Aiogram command and message handlers.
- Manage graceful startup and shutdown lifecycle.
"""

from __future__ import annotations

import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from playwright.async_api import async_playwright

from config import settings
from modules.message_sender import MessageSender
from modules.queue_manager import get_queue_manager
from modules.url_parser import extract_urls
from resolvers.orchestrator import _load_domains, get_orchestrator
from utils.logger import get_logger, setup_logging
from utils.stealth_config import (
    build_browser_launch_args,
    validate_data_files,
)

logger = get_logger("bot")
dp = Dispatcher()


# =====================================================================
# Command Handlers
# =====================================================================

@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    """Send welcome message and instructions."""
    text = (
        "👋 <b>Namaste! Main Link Bypass Bot hoon.</b>\n\n"
        "Kisi bhi supported ad-shortener link ko bypass karne ke liye:\n"
        "👉 <i>Bas mujhe woh link yahan send karein!</i>\n\n"
        "<b>Available Commands:</b>\n"
        "• /help - Kaise use karein aur tips\n"
        "• /supported - Supported shorteners ki list\n"
        "• /stats - Bot queue aur system status\n"
    )
    await message.reply(text, parse_mode=ParseMode.HTML)


@dp.message(Command("help"))
async def handle_help(message: Message) -> None:
    """Show help documentation."""
    text = (
        "📖 <b>Bot Usage Guide:</b>\n\n"
        "1. Kisi bhi message mein URL paste karein ya forward karein.\n"
        "2. Bot link ko parse karke automatically queue mein add karega.\n"
        "3. Playwright browser automation ya API se ad-wall/timer bypass karke "
        "asli target URL nikaal kar reply karega.\n\n"
        "💡 <b>Tips:</b>\n"
        "• Ek message mein max 5 links extract ho sakte hain.\n"
        "• Agar captcha mila, toh aapko browser mein manually kholna hoga.\n"
        "• /supported command se supported shorteners check karein."
    )
    await message.reply(text, parse_mode=ParseMode.HTML)


@dp.message(Command("supported"))
async def handle_supported(message: Message) -> None:
    """Show list of supported domains and shorteners."""
    domain_data = _load_domains()
    domains = domain_data.get("domains", {})

    if not domains:
        await message.reply("⚠️ Koi supported domains configured nahi hain.")
        return

    lines = ["🌐 <b>Supported Shorteners & Domains:</b>\n"]
    for canonical, meta in domains.items():
        name = meta.get("display_name", canonical)
        strategy = meta.get("strategy", "browser")
        alternates = meta.get("alternate_hosts", [])

        alt_text = f" (Alts: {', '.join(alternates[:3])})" if alternates else ""
        lines.append(f"• <b>{name}</b> (<code>{canonical}</code>){alt_text} — <i>{strategy}</i>")

    lines.append("\n<i>Send any link from these domains to bypass!</i>")
    await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)


@dp.message(Command("stats"))
async def handle_stats(message: Message) -> None:
    """Show current bot queue and performance stats."""
    qm = get_queue_manager()
    stats = qm.stats()
    text = (
        "📊 <b>Bot Statistics:</b>\n\n"
        f"• <b>Queue Size:</b> <code>{stats.get('queue_size', 0)}</code>\n"
        f"• <b>Active Users:</b> <code>{stats.get('active_users', 0)}</code>\n"
        f"• <b>Concurrent Workers:</b> <code>{stats.get('workers', 0)}</code>\n"
    )
    await message.reply(text, parse_mode=ParseMode.HTML)


# =====================================================================
# Link Handler
# =====================================================================

@dp.message(F.text | F.caption)
async def handle_incoming_message(message: Message) -> None:
    """Detect links, validate, and enqueue for processing."""
    parsed_urls = extract_urls(message)

    if not parsed_urls:
        # If in private chat and not a command, guide the user
        if message.chat.type == "private" and not (message.text and message.text.startswith("/")):
            await message.reply(
                "⚠️ Koi valid HTTP/HTTPS link nahi mila.\n"
                "Kripya ek valid shortener link bhejein, jaise Linkvertise ya SpeedyLink."
            )
        return

    qm = get_queue_manager()

    for parsed in parsed_urls:
        accepted, status_msg = await qm.enqueue(
            user_id=message.from_user.id if message.from_user else message.chat.id,
            chat_id=message.chat.id,
            parsed_url=parsed,
        )
        if not accepted:
            await message.reply(status_msg)
        else:
            logger.info("Enqueued link %s for chat %s", parsed.domain, message.chat.id)


# =====================================================================
# Main Application Lifecycle
# =====================================================================

async def main() -> None:
    setup_logging()
    logger.info("Starting Telegram Bypass Bot...")

    # 1. Validate data files
    validate_data_files()

    # 2. Check token
    bot_token = settings.telegram_bot_token.get_secret_value()
    if not bot_token:
        logger.error(
            "TELEGRAM_BOT_TOKEN is empty! Please set it in your .env file or environment variable."
        )
        return

    bot = Bot(token=bot_token)

    # 3. Launch shared Playwright browser
    logger.info("Launching Playwright Chromium browser...")
    pw = await async_playwright().start()
    launch_kwargs: dict = {
        "headless": settings.headless,
        "args": build_browser_launch_args(),
    }
    if settings.proxy_url:
        launch_kwargs["proxy"] = {"server": settings.proxy_url}

    browser = await pw.chromium.launch(**launch_kwargs)

    # 4. Initialize Orchestrator and Queue Manager
    get_orchestrator(browser=browser)
    sender = MessageSender(bot)
    queue_manager = get_queue_manager(on_result=sender)
    await queue_manager.start()

    logger.info("Bot ready and listening for incoming messages!")

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        logger.info("Shutting down bot...")
        await queue_manager.stop()
        await browser.close()
        await pw.stop()
        await bot.session.close()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot exited gracefully.")

