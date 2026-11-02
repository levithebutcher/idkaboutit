"""
utils/userscript_engine.py
---------------------------
Injects Greasemonkey/Tampermonkey polyfills and battle-tested bypass userscripts
into Playwright pages before document load.
"""

from __future__ import annotations

from pathlib import Path
from playwright.async_api import Page
from utils.logger import get_logger

logger = get_logger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_USERSCRIPT_FILE = _DATA_DIR / "bypass_all_shortlinks.user.js"

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

    // Modern GM.* object
    window.GM = {
        xmlHttpRequest: window.GM_xmlhttpRequest,
        setValue: async (k, v) => window.GM_setValue(k, v),
        getValue: async (k, def) => window.GM_getValue(k, def),
        deleteValue: async (k) => window.GM_deleteValue(k),
        listValues: async () => window.GM_listValues(),
        addStyle: (css) => window.GM_addStyle(css),
        openInTab: (url) => window.GM_openInTab(url),
    };

    window.GM_info = {
        script: { name: 'Playwright Bypass Userscript Engine', version: '2.0' },
        scriptHandler: 'Tampermonkey',
        version: '5.0'
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
    window.BpNote = function(msg, level) {
        // console.debug('[Bypass Userscript ' + (level || 'info') + ']', msg);
    };

    // 7. Anti-Adblock & Bot Neutralization
    window.isAdBlockActive = false;
    window.canRunAds = true;
    window.adBlockDetected = false;

    // 8. Universal Timer Accelerator
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

    // 9. SpeedyLink & Linkvertise Early Hooks
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


async def apply_userscripts(page: Page) -> None:
    """
    Injects GM polyfill followed by community bypass userscripts into the page.
    """
    # 1. Inject Greasemonkey/Tampermonkey environment & hooks
    await page.add_init_script(GM_POLYFILL_JS)

    # 2. Inject Bypass All Shortlinks script if present
    if _USERSCRIPT_FILE.exists():
        try:
            script_code = _USERSCRIPT_FILE.read_text(encoding="utf-8")
            await page.add_init_script(script_code)
            logger.debug("Userscript injected: %s", _USERSCRIPT_FILE.name)
        except Exception as exc:
            logger.warning("Failed to inject userscript %s: %s", _USERSCRIPT_FILE.name, exc)
