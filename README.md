# 🚀 Telegram Shortener & Linkvertise Bypass Bot

A high-performance, asynchronous Telegram bot designed to automatically resolve and bypass ad-gateways and URL shorteners (e.g. **Linkvertise**, **SpeedyLink**, **DT-M**, **FastForward**, and more) to retrieve the original destination URL.

---

## ✨ Features

- **⚡ Multi-Strategy Resolution Engine:**
  - **Publisher API Replay:** Reverse-engineered zero-browser API resolver for fast Linkvertise bypasses.
  - **Playwright Headless Browser:** Stealth browser automation with dynamic clock fast-forwarding, DOM scraping, and local storage extraction for heavy JS countdown shorteners.
  - **Fallback External APIs:** Automatically routes tricky links to secondary bypass APIs if browser automation encounters hard barriers (e.g. Cloudflare Turnstile / Captchas).
- **🛡️ Anti-Detect Stealth Engine:**
  - Spoofs WebGL vendor/renderer, navigator permissions, device memory, hardware concurrency, and user-agent fingerprints.
  - WindMouse-style human-like mouse movement and click emulation.
- **🔄 Robust Queue & Rate Limiter:**
  - Async job queue with concurrency limits (`max_concurrent_jobs`) and per-user limits to prevent spam and system overload.
- **🤖 Two Implementation Flavors:**
  - **`bot.py` (Recommended):** Modular, production-grade bot built on **Aiogram 3.x**.
  - **`bypass_bot.py`:** Lightweight, single-file bot built on **Telethon**.

---

## 📁 Project Architecture

```
├── bot.py                  # Main Aiogram 3 bot entrypoint
├── bypass_bot.py           # Standalone Telethon bot entrypoint
├── config.py               # Pydantic-based configuration management
├── requirements.txt        # Python package dependencies
├── .env.example            # Environment variables template
├── .gitignore              # Git ignore rules (protects credentials)
├── README.md               # Documentation
│
├── data/
│   ├── supported_domains.json  # Catalog of supported domains & strategies
│   └── user_agents.json        # Curated desktop & mobile user-agent profiles
│
├── modules/
│   ├── message_sender.py   # Formats and delivers HTML responses
│   ├── queue_manager.py    # Async queue, concurrency semaphore, user limits
│   └── url_parser.py       # Entity & regex URL extraction & validation
│
├── resolvers/
│   ├── base.py             # Abstract resolver class & status definitions
│   ├── browser_resolver.py # Playwright headless browser automation
│   ├── fallback_resolver.py# External fallback API client
│   ├── linkvertise_api.py  # Linkvertise static & target API replay
│   └── orchestrator.py     # Strategy router & fallback dispatcher
│
└── utils/
    ├── logger.py           # Structured ANSI color & rotating file logging
    ├── session_factory.py  # Playwright context pooling & cookie persistence
    └── stealth_config.py   # Anti-bot fingerprint patching & mouse curves
```

---

## 🛠️ Installation & Setup

### 1. Clone the repository
```bash
git clone https://github.com/levithebutcher/idkaboutit.git
cd idkaboutit
```

### 2. Set up a Python Virtual Environment
```bash
python -m venv .venv

# On Windows:
.\.venv\Scripts\activate

# On Linux/macOS:
source .venv/bin/activate
```

### 3. Install Dependencies & Playwright Browser
```bash
pip install -r requirements.txt
playwright install chromium
```

### 4. Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Edit `.env` and fill in your Telegram Bot token:
```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
```
*(If you want to run `bypass_bot.py`, also fill in `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from [my.telegram.org](https://my.telegram.org)).*

---

## 🚀 Running the Bot

### Option A: Aiogram 3 Bot (Recommended)
```bash
python bot.py
```

### Option B: Telethon Bot (Single-file)
```bash
python bypass_bot.py
```

---

## 💬 Bot Commands (Aiogram)

| Command | Description |
|---|---|
| `/start` | Displays the welcome message and instructions |
| `/help` | Detailed instructions on how to submit links |
| `/supported` | Lists all supported shortener domains & their resolution strategy |
| `/stats` | Shows active queue size, workers, and concurrent users |

---

## ⚙️ Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *None* | Bot token obtained from [@BotFather](https://t.me/BotFather) |
| `MAX_CONCURRENT_JOBS` | `3` | Parallel Playwright contexts allowed |
| `MAX_QUEUE_DEPTH` | `20` | Max queued links before rejecting requests |
| `PER_USER_JOB_LIMIT` | `2` | Max links a single user can have in queue simultaneously |
| `NAV_TIMEOUT_MS` | `90000` | Browser navigation timeout in milliseconds |
| `TOTAL_RESOLUTION_TIMEOUT_S` | `180` | Hard cap timeout for a link resolution |
| `HEADLESS` | `true` | Set to `false` for debugging browser visual steps |

---

## ⚠️ Disclaimer

This project is developed for educational and research purposes only, specifically for studying browser automation, asynchronous task queues, and web application patterns. Please ensure compliance with the terms of service of any third-party websites or services you interact with.

