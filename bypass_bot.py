"""
bypass_bot.py
-------------
Standalone Telethon-based Telegram bot for bypassing ad-shorteners using Playwright.

Configuration can be set via environment variables or a .env file:
- TELEGRAM_API_ID
- TELEGRAM_API_HASH
- TELEGRAM_BOT_TOKEN
"""

import asyncio
import logging
import os
import re
from typing import Optional

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from telethon import TelegramClient, events
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Target domains regex pattern
SHORTENER_PATTERN = re.compile(
    r"(?:linkvertise|speedy\.link|dt-m|fastforward|bit\.ly|tinyurl|is\.gd|goo\.gl|ouo\.io)",
    re.IGNORECASE,
)


class AdBypassBot:
    def __init__(self, api_id: int, api_hash: str, bot_token: str):
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot_token = bot_token
        self.client = TelegramClient("bypass_session", self.api_id, self.api_hash)
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self.client.on(events.NewMessage(pattern=r"^/start"))
        async def start_handler(event: events.NewMessage.Event):
            welcome_text = (
                "👋 **Welcome to Ad Bypass Bot!**\n\n"
                "Send me any shortener link (like Linkvertise, SpeedyLink, etc.) "
                "and I will resolve the direct destination link for you!\n\n"
                "Use /help for more instructions."
            )
            await event.respond(welcome_text)

        @self.client.on(events.NewMessage(pattern=r"^/help"))
        async def help_handler(event: events.NewMessage.Event):
            help_text = (
                "📖 **How to use:**\n\n"
                "1. Send or forward any message containing a shortener link.\n"
                "2. The bot will open the link in a headless browser.\n"
                "3. You will receive the bypassed destination link.\n"
            )
            await event.respond(help_text)

        @self.client.on(events.NewMessage)
        async def message_handler(event: events.NewMessage.Event):
            # Ignore commands
            if event.raw_text.startswith("/"):
                return

            links = self.extract_links(event)
            if not links:
                if event.is_private:
                    await event.respond("⚠️ No valid shortener link detected. Please send a valid link.")
                return

            for link in links:
                status_msg = await event.reply(f"⏳ **Resolving link:** `{link}`\nPlease wait...")
                final_url = await self.get_final_url(link)

                if final_url and final_url != link:
                    await status_msg.edit(
                        f"✅ **Link Resolved Successfully!**\n\n"
                        f"🔗 **Original:** `{link}`\n"
                        f"🎯 **Destination:** `{final_url}`"
                    )
                else:
                    await status_msg.edit(
                        f"❌ **Failed to resolve:** `{link}`\n"
                        "The link might have a captcha or is currently unsupported."
                    )

    def extract_links(self, event: events.NewMessage.Event) -> list[str]:
        """Extract shortener URLs from message entities or plain text."""
        links: list[str] = []
        raw_text = event.raw_text or ""

        # Check Telethon message entities
        if event.message and event.message.entities:
            for entity in event.message.entities:
                if isinstance(entity, MessageEntityTextUrl):
                    url = entity.url
                    if url and url.startswith("http"):
                        links.append(url)
                elif isinstance(entity, MessageEntityUrl):
                    offset = entity.offset
                    length = entity.length
                    url = raw_text[offset : offset + length]
                    if url and url.startswith("http"):
                        links.append(url)

        # Fallback regex if entities didn't find any
        if not links and raw_text:
            found = re.findall(r"https?://[^\s<>\"'`]+", raw_text)
            links.extend(found)

        # Deduplicate preserving order
        seen = set()
        unique_links = []
        for l in links:
            if l not in seen:
                seen.add(l)
                unique_links.append(l)

        return unique_links

    async def extract_shortener_id(self, url: str) -> Optional[str]:
        """Attempts to extract numeric IDs from various shortener patterns."""
        patterns = [
            r"/(\d+)/",
            r"/link/(\d+)",
            r"/s/(\w+)",
            r"/go/(\w+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None

    async def get_final_url(self, short_url: str) -> Optional[str]:
        """
        Uses Playwright (Browser Automation) to resolve links that require
        JavaScript execution, Meta-refreshes, or LocalStorage redirections.
        """
        try:
            logger.info("Resolving link via browser automation: %s", short_url)

            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                page = await browser.new_page()

                try:
                    await page.goto(short_url, timeout=35000, wait_until="domcontentloaded")
                    await asyncio.sleep(2.0)

                    # Strategy A: Check Meta Refresh Tags
                    content = await page.content()
                    meta_refresh = await page.query_selector('meta[http-equiv="refresh"]')
                    if meta_refresh:
                        content_attr = await meta_refresh.get_attribute("content")
                        if content_attr:
                            match = re.search(r"url=(https?://[^\s'\";>]+)", content_attr, re.IGNORECASE)
                            if match:
                                await browser.close()
                                return match.group(1)

                    # Strategy B: Inspect Local Storage & window variables
                    script_to_run_js = """
                        () => {
                            var vars_to_check = ['url', 'link', 'redirectUrl', 'targetUrl', 'destination'];
                            var found = null;
                            try {
                                for (var i = 0; i < localStorage.length; i++) {
                                    var key = localStorage.key(i);
                                    var val = localStorage.getItem(key);
                                    if (val && (val.startsWith('http://') || val.startsWith('https://'))) {
                                        return val;
                                    }
                                }
                            } catch(e) {}
                            return window.location.href;
                        }
                    """
                    evaluated_url = await page.evaluate(script_to_run_js)
                    if evaluated_url and evaluated_url != short_url and evaluated_url.startswith("http"):
                        # Ensure it's not still on the shortener domain
                        if not any(d in evaluated_url for d in ["linkvertise", "speedy.link", "dt-m"]):
                            await browser.close()
                            return evaluated_url

                    # Strategy C: Look for common download/continue destination links in DOM
                    target_element_selectors = [
                        "#download-url",
                        ".main-content > a",
                        "#destination",
                        ".final-link",
                        'a[href*="/check"]',
                        'a.btn-primary[href^="http"]',
                        'a#btn-main[href^="http"]',
                    ]

                    for selector in target_element_selectors:
                        el = await page.query_selector(selector)
                        if el:
                            href = await el.get_attribute("href")
                            if href and href.startswith("http") and href != short_url:
                                await browser.close()
                                return href

                    # If page navigated to a different URL
                    current_url = page.url
                    if current_url and current_url != short_url and current_url.startswith("http"):
                        if not any(d in current_url for d in ["linkvertise", "speedy.link"]):
                            await browser.close()
                            return current_url

                except Exception as e:
                    logger.error("Browser navigation error for %s: %s", short_url, e)
                finally:
                    await browser.close()

        except Exception as e:
            logger.error("Playwright startup error: %s", e)

        return None

    def run(self) -> None:
        """Start the Telethon client with bot token."""
        if not self.bot_token:
            logger.error("TELEGRAM_BOT_TOKEN is not configured.")
            return

        logger.info("Starting Telethon AdBypassBot...")
        self.client.start(bot_token=self.bot_token)
        self.client.run_until_disconnected()


if __name__ == "__main__":
    if not API_ID or not API_HASH or not BOT_TOKEN:
        print("⚠️ Configuration missing in .env:")
        print("Please set TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_BOT_TOKEN in .env")
    else:
        bot = AdBypassBot(api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
        bot.run()
