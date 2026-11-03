"""
utils/userscript_engine.py
---------------------------
Injects Greasemonkey/Tampermonkey polyfills and battle-tested bypass userscripts
(Bypass All Shortlinks, Bypass.city, Bypass.vip) into Playwright pages before document load.
"""

from __future__ import annotations

import json
from pathlib import Path
from playwright.async_api import Page
from utils.logger import get_logger

logger = get_logger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Comprehensive Tampermonkey / Greasemonkey API Shim & Timer Accelerator
GM_POLYFILL_JS = """
(() => {
    // 1. UnsafeWindow & Globals
    window.unsafeWindow = window;
    window.__bypassed_url = null;

    // 2. Storage Shim
    const _gm_storage = {};
    window.GM_setValue = (k, v) => {
        _gm_storage[k] = v;
        try { localStorage.setItem('GM_' + k, JSON.stringify(v)); } catch(e) {}
    };
    window.GM_getValue = (k, def) => {
        if (k in _gm_storage) return _gm_storage[k];
        try {
            const v = localStorage.getItem('GM_' + k);
            return v ? JSON.parse(v) : def;
        } catch(e) { return def; }
    };
    window.GM_deleteValue = (k) => {
        delete _gm_storage[k];
        try { localStorage.removeItem('GM_' + k); } catch(e) {}
    };
    window.GM_listValues = () => Object.keys(_gm_storage);

    // Modern GM.* object
    window.GM = {
        xmlHttpRequest: (details) => window.GM_xmlhttpRequest(details),
        setValue: async (k, v) => window.GM_setValue(k, v),
        getValue: async (k, def) => window.GM_getValue(k, def),
        deleteValue: async (k) => window.GM_deleteValue(k),
        listValues: async () => window.GM_listValues(),
        addStyle: (css) => window.GM_addStyle(css),
        openInTab: (url) => window.GM_openInTab(url),
    };

    window.GM_info = {
        script: { name: 'Playwright Bypass Userscript Suite', version: '14.0.2', downloadURL: '' },
        scriptHandler: 'Tampermonkey',
        version: '5.0'
    };

    // 3. DOM & Styling
    window.GM_addStyle = (css) => {
        const s = document.createElement('style');
        s.textContent = css;
        (document.head || document.documentElement).appendChild(s);
        return s;
    };
    window.GM_setClipboard = (data) => {};
    window.GM_registerMenuCommand = () => {};

    // 4. Navigation & Tab Capture
    window.GM_openInTab = (url) => {
        if (url && typeof url === 'string') {
            window.__bypassed_url = url;
            try { window.location.href = url; } catch(e) {}
        }
        return { close: () => {} };
    };

    // Override window.open to trap popup targets
    const _origOpen = window.open;
    window.open = function(url, ...args) {
        if (url && typeof url === 'string' && url.startsWith('http')) {
            window.__bypassed_url = url;
        }
        return null;
    };

    // 5. HTTP Request Shim (CORS fetch)
    window.GM_xmlhttpRequest = (details) => {
        if (!details || !details.url) return;
        fetch(details.url, {
            method: details.method || 'GET',
            headers: details.headers || {},
            body: details.data || undefined
        }).then(async (res) => {
            const text = await res.text();
            if (details.onload) {
                details.onload({
                    responseText: text,
                    status: res.status,
                    response: text,
                    finalUrl: res.url
                });
            }
        }).catch((err) => {
            if (details.onerror) details.onerror(err);
        });
    };

    // 6. MonkeyConfig & BpNote shims (used by Bypass All Shortlinks)
    window.MonkeyConfig = function(options) {
        this.get = function(key) {
            if (options && options.params && options.params[key]) {
                return options.params[key].default !== undefined ? options.params[key].default : false;
            }
            return false;
        };
        this.set = function() {};
        this.open = function() {};
    };
    window.BpNote = function(msg, level) {};

    // 7. Bypass.city CustomEvent Listener
    window.addEventListener("bypassComplete", (event) => {
        try {
            const data = event.detail;
            const u = (data && typeof data === 'object') ? (data.url || data.bypassData) : data;
            if (u && typeof u === 'string') {
                window.__bypassed_url = u;
            }
        } catch(e) {}
    });

    // 8. Anti-Adblock & Bot Neutralization
    window.isAdBlockActive = false;
    window.canRunAds = true;
    window.adBlockDetected = false;

    // 9. Universal Timer Accelerator
    const _origSetTimeout = window.setTimeout;
    const _origSetInterval = window.setInterval;
    window.setTimeout = function(fn, delay, ...args) {
        const d = (typeof delay === 'number' && delay > 80) ? Math.min(delay, 80) : delay;
        return _origSetTimeout(fn, d, ...args);
    };
    window.setInterval = function(fn, delay, ...args) {
        const d = (typeof delay === 'number' && delay > 120) ? Math.min(delay, 120) : delay;
        return _origSetInterval(fn, d, ...args);
    };

    // 10. SpeedyLink & Linkvertise Early Hooks
    const runEarlyHooks = () => {
        // Remove CMP overlays immediately
        try {
            document.querySelector('.qc-cmp2-container')?.remove();
            document.querySelector('#qc-cmp2-ui')?.remove();
            if (document.body) document.body.style.overflow = 'auto';
        } catch(e) {}

        // SpeedyLink verify trigger
        if (typeof document.verify === 'function') {
            try { document.verify(); } catch(e) {}
        }
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', runEarlyHooks, { once: true });
    } else {
        runEarlyHooks();
    }
})();
"""


def _extract_patterns(script_code: str) -> list[str]:
    """Extract @match patterns from userscript metadata."""
    patterns: list[str] = []
    for line in script_code.splitlines():
        line = line.strip()
        if line.startswith("// @match") or line.startswith("// @include"):
            parts = line.split(maxsplit=2)
            if len(parts) >= 3:
                patterns.append(parts[2].strip())
    return patterns


async def apply_userscripts(page: Page) -> None:
    """
    Injects GM polyfill followed by community bypass userscripts into the page.
    """
    # 1. Inject Greasemonkey/Tampermonkey environment & hooks (strictly BEFORE userscripts)
    try:
        await page.add_init_script(GM_POLYFILL_JS)
        logger.debug("GM_POLYFILL_JS injected successfully")
    except Exception as exc:
        logger.warning("Failed to inject GM_POLYFILL_JS: %s", exc)

    # 2. Inject all available userscripts from data/*.user.js
    loaded_scripts: list[str] = []
    for js_path in sorted(_DATA_DIR.glob("*.user.js")):
        try:
            script_code = js_path.read_text(encoding="utf-8")
            patterns = _extract_patterns(script_code)
            patterns_json = json.dumps(patterns)
            guarded_code = (
                "(() => {\n"
                "    try {\n"
                "        const proto = window.location.protocol;\n"
                "        if (proto !== 'http:' && proto !== 'https:') return;\n"
                "        const href = window.location.href;\n"
                f"        const patterns = {patterns_json};\n"
                "        if (patterns && patterns.length > 0) {\n"
                "            const matches = patterns.some(p => {\n"
                r"                const parts = p.split('*').map(s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));" + "\n"
                "                return new RegExp('^' + parts.join('.*') + '$').test(href);\n"
                "            });\n"
                "            if (!matches) return;\n"
                "        }\n"
                "    } catch(e) { return; }\n"
                + script_code + "\n})();"
            )
            await page.add_init_script(guarded_code)
            loaded_scripts.append(js_path.name)
            logger.debug("Userscript injected: %s", js_path.name)
        except Exception as exc:
            logger.warning("Failed to inject userscript %s: %s", js_path.name, exc)

    logger.info("Userscripts loaded (%d): %s", len(loaded_scripts), ", ".join(loaded_scripts))
