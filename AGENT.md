# 🤖 AGENT.md - AI Developer & Architecture Documentation

> **Notice for AI Assistants & Maintainers:**  
> This file outlines the complete architecture, problem-solving history, reverse-engineering implementations, and technical design decisions of this repository. Read this file before making modifications or adding new resolvers.
>
> ⚠️ **CRITICAL SECURITY RULE:** Never commit, print, or expose actual credentials (Telegram Bot tokens, API keys, passwords, or personal chat IDs) in code, commit messages, or documentation.

---

## 📌 Project Overview

This repository is a high-performance, asynchronous **Telegram Bypass Bot** built to automatically solve, bypass, and resolve modern link shorteners, ad-gateways, timers, and multi-step redirect networks (such as Linkvertise, Speedy-links, Best-links, Lootlinks, and Indian/Pakistani WordPress Safelink/RTG shortener networks).

- **Primary Entrypoint:** `bot.py` (built on **Aiogram 3.x**)
- **Alternative Lightweight Entrypoint:** `bypass_bot.py` (built on **Telethon**)
- **Colab Runner:** `Colab_Bypass_Suite.ipynb` (Continuous headless runner with anti-sleep protection)
- **Git Repository:** `https://github.com/levithebutcher/idkaboutit.git` (branch `main`)

---

## 🏛️ System Architecture

```
telegram-bypass-bot/
├── bot.py                     # Main Aiogram 3 bot (Worker pool & dispatcher)
├── bypass_bot.py              # Telethon single-file alternative
├── config.py                  # Pydantic v2 settings management
├── requirements.txt           # Python dependencies
├── .env.example               # Configuration template (NO secrets)
├── .gitignore                 # Enforces security (.env and local notebooks)
├── Colab_Bypass_Suite.ipynb   # Standalone Colab execution suite
│
├── data/
│   ├── supported_domains.json # Domain routing table & strategies
│   ├── user_agents.json       # Curated browser profiles & fingerprints
│   ├── bypass_all_shortlinks.user.js # Bloggerpemula universal bypass userscript
│   ├── bypass_city.user.js    # Bypass.city official userscript (v14.0.2)
│   └── bypass_vip.user.js     # Bypass.vip official userscript (v1.4.8)
│
├── modules/
│   ├── url_parser.py          # Fast URL extraction & validation from messages
│   ├── queue_manager.py       # Async job queue with concurrency & user semaphores
│   └── message_sender.py      # Formatted Telegram HTML replies
│
├── resolvers/
│   ├── base.py                # Abstract BaseResolver & ResolutionResult
│   ├── orchestrator.py        # Resolver chain dispatcher & fallback coordinator
│   ├── linkvertise_api.py     # Reverse-engineered Linkvertise GraphQL task solver
│   ├── browser_resolver.py    # Playwright stealth browser with userscript suite
│   └── fallback_resolver.py   # HTTP client for third-party bypass services
│
└── utils/
    ├── logger.py              # Colorized structured logging
    ├── session_factory.py     # Playwright context caching & domain warmups
    ├── stealth_config.py      # Antidetect fingerprint spoofing & human mouse
    └── userscript_engine.py   # Greasemonkey/Tampermonkey polyfill & script injector
```

---

## 🔄 Resolution Flow & Orchestration

When a user sends a message containing links:
1. `modules/url_parser.py` parses and normalizes the link.
2. `modules/queue_manager.py` enqueues the job into an async priority queue (with per-user concurrency limits to prevent abuse).
3. `resolvers/orchestrator.py` checks `data/supported_domains.json`:
   - If domain strategy is `linkvertise_api`: tries `LinkvertiseAPIResolver` ➔ `BrowserResolver` ➔ `FallbackResolver`.
   - If domain strategy is `browser`: tries `BrowserResolver` ➔ `FallbackResolver`.
   - If domain is uncataloged: routes automatically to `BrowserResolver` ➔ `FallbackResolver`.
4. The first resolver to return `ResolutionStatus.SUCCESS` terminates the chain and returns the destination.

---

## 🛠️ Detailed Work Log & Implementations

### 1. Linkvertise Reverse-Engineered GraphQL Task Solver (`resolvers/linkvertise_api.py`)
- **Challenge:** Linkvertise updated their security to mandate an interactive task-wall (`adsTotal: 3` with sponsored articles) and deprecated legacy query endpoints. Headless browsers frequently hung or triggered Cloudflare challenges.
- **Solution:**
  1. **Dynamic Param Fast-Path:** Decodes `?r=` base64 parameter if present (instant 0ms resolution).
  2. **Static AppleTV Endpoint:** Checks for raw paste/url content using AppleTV User-Agent impersonation.
  3. **Official GraphQL Task-Cycle:** Interacts directly with `https://publisher.linkvertise.com/graphql`:
     - Calls `getContent(input: {userIdAndUrl: {user_id, url}}, origin: "sharing")`.
     - Inspects `ContentAccessTaskSet.tasks`.
     - Dispatches `startTask` and `completeTask` mutations for each task (`AdTask` / `WaitTask`).
     - Repeats cycle until `DetailPageTargetData` is returned with `url` or `paste`.
- **Performance:** Resolves `link-target.net`, `direct-link.net`, and `linkvertise.com` links in **3 to 4 seconds** with zero browser overhead and zero captcha triggers.

### 2. Multi-Userscript Engine & GM Polyfill (`utils/userscript_engine.py`)
- **Challenge:** Many shorteners (e.g. Speedy-links, Unlockr, Best-links) require client-side timer manipulation, anti-adblock neutralization, and complex JS execution.
- **Solution:**
  - Built a comprehensive in-memory Greasemonkey / Tampermonkey compatibility layer (`GM_POLYFILL_JS`):
    - `window.unsafeWindow = window`
    - `GM_setValue`, `GM_getValue`, `GM_deleteValue`, `GM_listValues` (sync memory + localStorage backed)
    - `window.GM.*` modern promise-based API
    - `GM_xmlhttpRequest` with full cross-origin fetch emulation
    - `window.MonkeyConfig` and `window.BpNote` mocks
    - Universal timer accelerator (`setTimeout` capped at 80ms, `setInterval` capped at 120ms)
    - `window.open` trap: populates `window.__bypassed_url` when popups or external tabs are spawned
    - `bypassComplete` custom event listener for `bypass.city` compatibility
  - Automated directory loader: Automatically scans `data/*.user.js` and injects:
    - `bypass_all_shortlinks.user.js` (Bloggerpemula universal bypasser v96.8)
    - `bypass_city.user.js` (Official Bypass.city script v14.0.2)
    - `bypass_vip.user.js` (Official Bypass.vip script v1.4.8)

### 3. Playwright Browser Resolver & Intermediary Protection (`resolvers/browser_resolver.py`)
- **Challenge:**
  - Certain multi-step shorteners (e.g. `liteshort.com`) redirect to intermediate WordPress ad-blogs (`kishanganjhalchal.com/no.php?link=...`).
  - Previously, `_looks_like_target()` prematurely treated the intermediate blog URL as the final target because the domain changed.
- **Solution:**
  - **Hardened `_looks_like_target()`:**
    - Rejects intermediate redirect scripts: `no.php`, `safe.php`, `out.php`, `go.php`, `link.php`, `redirect.php`, etc.
    - Rejects intermediate query parameters: `link=`, `url=`, `go=`, `dest=`, `safelink=`, `newwpsafelink=`, `redirect=`, etc.
    - Rejects search engine redirects (`google.com/search`, `google.com/sorry`, `google.com/url`).
    - Blacklists known intermediary shortener bridges from being accepted as targets.
  - **Automated Safelink / RTG Form Stepper:**
    - Detects WP Safelink and RTG forms (`form#rtg`, `input[name*="safelink"]`) and automatically submits them.
    - Clicks multi-step buttons: `"Click here to continue"`, `"GO TO LINK"`, `"Dual Tap"`, `#getlink`, `#rtg-snp2`.
    - Captures `window.__bypassed_url`, `window.conf_rew.link`, and intercepted JSON responses.

### 4. Anti-Detect Stealth Layer (`utils/stealth_config.py`)
- Strips `navigator.webdriver`.
- Emulates Chrome plugins, mime types, and window screen dimensions.
- Randomizes WebGL vendor and renderer strings across authentic GPU configurations.
- Implements WindMouse-based non-linear mouse paths and natural clicks.

### 5. Google Colab Suite (`Colab_Bypass_Suite.ipynb`)
- Self-contained interactive notebook for running the bot 24/7 on Google Colab free or pro tier.
- Includes:
  - Repository git sync (`!git pull origin main`).
  - Dependency & Playwright Chromium auto-install.
  - WebRTC & audio-oscillator anti-sleep keep-alive scripts.
  - Real-time colorized terminal logging.

---

## 🔒 Security & Git Hygiene Standards

1. **Environment Variables (`.env`):**
   - `.env` is strictly listed in `.gitignore` and must **never** be pushed to GitHub.
   - Any API keys, bot tokens, or credentials must only reside locally in `.env`.
   - Always update `.env.example` when introducing new configuration variables.
2. **Notebooks (`*.ipynb`):**
   - The interactive testing notebook `Colab_Bypass_Suite.ipynb` contains developer settings and must remain local.

---

## 🧪 Testing & Verification Guide

### Test Linkvertise API Resolver:
```bash
python -c "
import asyncio
from resolvers.linkvertise_api import LinkvertiseAPIResolver

async def test():
    resolver = LinkvertiseAPIResolver()
    result = await resolver.resolve('https://link-target.net/8645526/TSOQSm5fl6Bc', 'link-target.net')
    print('Result:', result.status, result.target_url)

asyncio.run(test())
"
```

### Test Browser & Userscript Engine:
```bash
python -c "
import asyncio
from playwright.async_api import async_playwright
from resolvers.browser_resolver import BrowserResolver

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        resolver = BrowserResolver(browser)
        result = await resolver.resolve('http://speedy-links.com/s?tQeV3k8s', 'speedy-links.com')
        print('Result:', result.status, result.target_url)
        await browser.close()

asyncio.run(test())
"
```

---

## 🧭 Instructions for Next Steps & Future Work

If you are asked to add support for a new shortener:
1. **Analyze Link Behavior First:**
   - Inspect network traffic for JSON endpoints (e.g., `links/go`, GraphQL, or base64 query params).
   - If an API exists, implement a dedicated resolver in `resolvers/` or extend `resolvers/linkvertise_api.py`.
2. **Catalog New Domains:**
   - Add new domain patterns into `data/supported_domains.json` with strategy (`api` or `browser`).
3. **If Relying on Headless Browser:**
   - Check if an existing userscript in `data/` handles it.
   - If intermediate blog steps exist, ensure `_looks_like_target()` rejects the intermediate URL and `_click_continue_buttons()` handles the button/form.
4. **Always Test Headless:**
   - Ensure the resolver operates without requiring a graphical display (Linux/Colab compatibility).
