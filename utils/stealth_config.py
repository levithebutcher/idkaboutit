"""
utils/stealth_config.py
-----------------------
Builds Playwright browser-context options with realistic fingerprints,
and applies JS-level overrides to hide automation traces.

Key functions:
- load_user_agents()      : loads data/user_agents.json once (cached).
- build_context_options() : returns dict for browser.new_context(**opts).
- apply_navigator_overrides(page) : injects init script to patch navigator.*
- apply_human_like_mouse(page, x, y) : WindMouse-style movement + click.
"""

import asyncio
import json
import math
import random
from functools import lru_cache
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

_DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "user_agents.json"


# ---------- Loading ----------

@lru_cache(maxsize=1)
def load_user_agents() -> dict[str, Any]:
    """Load and cache user_agents.json."""
    with _DATA_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


# ---------- Context options builder ----------

def build_context_options(device_type: str | None = None) -> dict[str, Any]:
    """
    Build a dict of keyword-arguments for `browser.new_context(...)`.

    Args:
        device_type: "desktop" or "mobile". If None, random choice.

    Returns:
        dict with: user_agent, viewport, locale, timezone_id,
        device_scale_factor, extra_http_headers, java_script_enabled,
        bypass_csp, ignore_https_errors, is_mobile, has_touch.
    """
    data = load_user_agents()

    if device_type not in ("desktop", "mobile"):
        device_type = random.choice(["desktop", "mobile"])

    ua = random.choice(data[device_type])
    viewport = random.choice(data["viewports"][device_type])
    locale_block = random.choice(data["locales"])
    scale = random.choice(data["device_scale_factors"][device_type])

    is_mobile = device_type == "mobile"

    options: dict[str, Any] = {
        "user_agent": ua,
        "viewport": viewport,
        "locale": locale_block["locale"],
        "timezone_id": locale_block["timezone"],
        "device_scale_factor": scale,
        "is_mobile": is_mobile,
        "has_touch": is_mobile,
        "java_script_enabled": True,
        "ignore_https_errors": True,
        "bypass_csp": True,
        "extra_http_headers": {
            "Accept-Language": f"{locale_block['locale']},en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Sec-CH-UA-Mobile": "?1" if is_mobile else "?0",
            "Sec-CH-UA-Platform": (
                '"Android"' if is_mobile else '"Windows"'
            ),
            "Upgrade-Insecure-Requests": "1",
        },
    }

    logger.debug(
        "Built context options (device=%s, viewport=%s, locale=%s)",
        device_type, viewport, locale_block["locale"],
    )
    return options


def build_browser_launch_args() -> list[str]:
    """
    Chromium launch flags that disable automation hints and improve realism.
    Pass as `browser_type.launch(args=...)`.
    """
    return [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-infobars",
        "--disable-dev-shm-usage",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=IsolateOrigins,site-per-process",
        "--start-maximized",
    ]


# ---------- Navigator overrides ----------

_NAVIGATOR_PATCH_JS = """
(() => {
    // 1. Hide webdriver flag
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
        configurable: true,
    });

    // 2. Populate plugins (Chrome PDF + Native Client)
    if (!navigator.plugins || navigator.plugins.length === 0) {
        const pluginData = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
            { name: 'Native Client', filename: 'internal-nacl-plugin' },
        ];
        Object.defineProperty(navigator, 'plugins', {
            get: () => pluginData,
            configurable: true,
        });
    }

    // 3. Language list
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
        configurable: true,
    });

    // 4. Hardware concurrency / memory (realistic desktop values)
    Object.defineProperty(navigator, 'hardwareConcurrency', {
        get: () => 8,
        configurable: true,
    });
    Object.defineProperty(navigator, 'deviceMemory', {
        get: () => 8,
        configurable: true,
    });

    // 5. Permissions query (avoid 'denied' default for notifications)
    if (navigator.permissions && navigator.permissions.query) {
        const origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (params) => {
            if (params && params.name === 'notifications') {
                return Promise.resolve({ state: 'default', onchange: null });
            }
            return origQuery(params);
        };
    }

    // 6. WebGL vendor / renderer spoofing
    const patchWebGL = (proto) => {
        if (!proto) return;
        const origGetParameter = proto.getParameter;
        proto.getParameter = function (p) {
            // UNMASKED_VENDOR_WEBGL = 0x9245, UNMASKED_RENDERER_WEBGL = 0x9246
            if (p === 0x9245) return 'Google Inc. (Intel)';
            if (p === 0x9246) {
                return 'ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)';
            }
            return origGetParameter.call(this, p);
        };
    };
    if (typeof WebGLRenderingContext !== 'undefined') {
        patchWebGL(WebGLRenderingContext.prototype);
    }
    if (typeof WebGL2RenderingContext !== 'undefined') {
        patchWebGL(WebGL2RenderingContext.prototype);
    }

    // 7. Chrome runtime shim (many sites check window.chrome)
    if (!window.chrome) {
        window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {} };
    }

    // 8. Hide CDP-injected properties on the window object
    delete window.__playwright;
    delete window.__pw_manual;
    delete window.__PW_inspect;

    // 9. Prevent `Function.prototype.toString` detection of patched functions
    const origToString = Function.prototype.toString;
    Function.prototype.toString = function () {
        if (this === Function.prototype.toString) {
            return 'function toString() { [native code] }';
        }
        try {
            return origToString.call(this);
        } catch (e) {
            return 'function () { [native code] }';
        }
    };
})();
"""


async def apply_navigator_overrides(page: Page) -> None:
    """
    Inject stealth JS into every page in this context BEFORE any navigation.
    """
    await page.add_init_script(script=_NAVIGATOR_PATCH_JS)
    logger.debug("Navigator overrides applied to page")


# ---------- Human-like mouse ----------

async def apply_human_like_mouse(
    page: Page,
    target_x: float | None = None,
    target_y: float | None = None,
) -> None:
    """
    Move the mouse from current position to (target_x, target_y) with
    WindMouse-like randomised path, then click.

    If target_x / target_y are None, click the current mouse position
    after a short move (useful for hover-then-click workflows).
    """
    if target_x is None:
        vp = page.viewport_size or {"width": 1280, "height": 720}
        target_x = random.uniform(vp["width"] * 0.3, vp["width"] * 0.7)
    if target_y is None:
        vp = page.viewport_size or {"width": 1280, "height": 720}
        target_y = random.uniform(vp["height"] * 0.3, vp["height"] * 0.7)

    start_x = random.uniform(0, 200)
    start_y = random.uniform(0, 200)

    # 6–12 intermediate steps along a slightly curved path
    steps = random.randint(6, 12)
    for i in range(1, steps + 1):
        t = i / steps
        # ease-in-out curve
        ease = 0.5 - math.cos(math.pi * t) / 2
        jitter_x = random.uniform(-3, 3)
        jitter_y = random.uniform(-3, 3)
        x = start_x + (target_x - start_x) * ease + jitter_x
        y = start_y + (target_y - start_y) * ease + jitter_y
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.008, 0.025))

    # Settle, then click
    await asyncio.sleep(random.uniform(0.05, 0.15))
    await page.mouse.click(target_x, target_y)


# ---------- Sanity check ----------

def validate_data_files() -> None:
    """
    Called once at startup — ensures data files exist and are parseable.
    """
    if not _DATA_FILE.exists():
        raise FileNotFoundError(f"Missing user-agents file: {_DATA_FILE}")
    data = load_user_agents()
    required = {"desktop", "mobile", "viewports", "locales", "device_scale_factors"}
    missing = required - data.keys()
    if missing:
        raise ValueError(f"user_agents.json missing keys: {missing}")
    logger.info("stealth_config data files validated OK")