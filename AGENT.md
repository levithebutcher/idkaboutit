# 🤖 AGENT.md — Link-Bypass Bot Architecture & Knowledge Base

Welcome to the **Link-Bypass Telegram Bot** codebase. This document serves as the single source of truth for the project's architecture, resolver orchestration, recent fixes, and operating procedures for any autonomous agent or developer working on this repository.

---

## 📌 Repository Purpose & Overview

This project is a high-performance, asynchronous link-bypass Telegram bot built using **Aiogram 3.x** and **Playwright**. Its purpose is to accept ad-heavy, timer-restricted, or task-gated shortlinks (e.g. Linkvertise, Speedy-links, Lootlinks, Safelink) and return clean, direct destination URLs to users without ads or tracking.

- **Primary Repository:** `https://github.com/levithebutcher/idkaboutit.git`
- **Main Branch:** `main`
- **Primary Runtime:** Python 3.10+ (asyncio, Playwright, Aiogram 3.x, HTTPX, Pydantic v2)
- **Deployment Environments:** Local machine or cloud containers (Google Colab / Linux VPS).

---

## 🏗️ Architectural Overview & Pipeline

The pipeline follows a clean, decoupled producer-consumer model:

```mermaid
flowchart TD
    User([Telegram User]) -->|Sends link| Bot[Aiogram Bot Dispatcher]
    Bot -->|Validate & Extract URL| URLParser[utils/url_parser.py]
    URLParser -->|Queue Job| QueueMgr[utils/queue_manager.py]
    QueueMgr -->|Dispatch to Worker| WorkerPool[Async Worker Pool (Max 3)]
    WorkerPool -->|Resolve URL| Orchestrator[resolvers/orchestrator.py]
    
    subgraph Resolvers Chain
        Orchestrator -->|1. Try Direct API| APIResolver[resolvers/linkvertise_api.py]
        APIResolver -->|If unresolvable / fallback| BrowserResolver[resolvers/browser_resolver.py]
        BrowserResolver -->|If unresolvable / fallback| FallbackResolver[resolvers/fallback_resolver.py]
    end
    
    BrowserResolver -->|Tampermonkey Polyfill & Scripts| UserscriptEngine[utils/userscript_engine.py]
    BrowserResolver -->|Anti-Detection & Fingerprint Evasion| StealthConfig[utils/stealth_config.py]
    
    Resolvers Chain -->|ResolutionResult| WorkerPool
    WorkerPool -->|Format & Send Reply| MsgSender[modules/message_sender.py]
    MsgSender -->|Telegram API| User
```

### Key Components:
1. **Dispatcher & Handlers (`bot.py`, `handlers/`):**
   - Handles `/start`, `/help`, and incoming text messages containing URLs.
   - Enforces user rate-limiting (`utils/rate_limiter.py`).
2. **Queue Manager (`utils/queue_manager.py`):**
   - In-memory async worker pool processing jobs concurrently without blocking Telegram polling.
3. **Resolver Orchestrator (`resolvers/orchestrator.py`):**
   - Determines the optimal resolution chain based on domain metadata in `data/supported_domains.json`.
   - Supports strategies: `linkvertise_api`, `browser`, and `fallback`.
   - Handles retries with exponential backoff on retryable network/timeout errors.
4. **The first resolver to return `ResolutionStatus.SUCCESS` terminates the chain and returns the destination.**

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

### 6. JS-Gateway & Userscript Bypass Enhancements (SpeedyLink / Best-Links / Unlockr)
- **Issue 1 (Userscript bypass URL & event capture):**
  - Added new `ResolutionMethod.USERSCRIPT` in `resolvers/base.py`.
  - Added `_wait_for_progress()` helper in `BrowserResolver`.
  - After `_click_continue_buttons()` and `_wait_for_progress()`, evaluates `window.__bypassed_url || null`. If valid URL and passes `_looks_like_target()`, returns success with `ResolutionMethod.USERSCRIPT`.
  - Added listener for the custom `bypassComplete` event dispatched by userscripts (`bypass.city.user.js` / `bypass.vip`).
- **Issue 2 (Target filtering & trusted host whitelist):**
  - Added `_TRUSTED_FILE_HOSTS` whitelist (`mega.nz`, `drive.google.com`, `mediafire.com`, `dropbox.com`, `pixeldrain.com`, `gofile.io`, `1fichier.com`, `yadi.sk`, `disk.yandex.com`) that are always accepted as destinations regardless of query parameters.
  - Relaxed `intermediate_params` rejection: URLs with `?url=`, `?link=`, `?go=` are now only rejected if they point to the shortener family (`_SHORTENER_FAMILY`) OR have a very short path (`<=2 chars`) with no file extension and no known file host.
- **Issue 3 (Robust userscript injection & @match isolation):**
  - `GM_POLYFILL_JS` is strictly injected prior to community `.user.js` files.
  - Wrapped each script injection in try/except with failure logging by script name.
  - Added INFO-level logging of total script count and loaded filenames.
  - Added origin check and `@match` pattern isolation in the script wrapper to ensure userscripts only execute on their intended hostnames (preventing cross-origin hijacking or redirect loops on internal error/about pages).
  - Prevented recursive self-redirects on bypass tool origins (`bypass.vip`, `bypass.city`, `adbypass.org`).
- **Issue 4 (Fallback resolver check & error propagation):**
  - In `resolvers/fallback_resolver.py`, logs once at INFO level on startup when `FALLBACK_API_URL` is not configured.
  - In `resolvers/orchestrator.py`, prevented `NoFallbackAPI` / `FallbackDisabled` from masking the real `browser_resolver` error; returns browser failure as primary, attaching fallback failure in `extra`.
- **Issue 5 (Automated failure debugging):**
  - In `resolvers/browser_resolver.py`, saves a full-page screenshot (`logs/fail_{domain}_{timestamp}.png`) and HTML dump (`logs/fail_{domain}_{timestamp}.html`) on failure and logs paths at WARNING level.
  - Refined `_has_captcha()` to inspect visible challenge frames/containers rather than matching raw text strings in invisible scripts.

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
        print('Result:', result.status, result.target_url, result.method)
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
