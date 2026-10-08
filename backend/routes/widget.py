"""Embeddable apply widget — a tiny JS snippet candidates can drop into ANY
careers page that opens your apply form in a modal without
navigating away, as an alternative to linking to the standalone /apply/{slug}
portal (no page jump).

Usage on the customer's careers site:
    <script src="https://recruit.example.com/api/widget.js"
            data-pipeline-slug="downtown"
            async></script>
    <button data-cgr-apply>Apply Now</button>

The widget injects a button (or hooks into existing `[data-cgr-apply]`
elements) that opens an iframe pointing at /apply/{slug}?embed=1. The
embed=1 query param tells the React app to render compact mode (no top
nav, no footer)."""
from fastapi import APIRouter, Response

router = APIRouter()


WIDGET_JS = """// CGRecruit — Embed Widget v1.1
// Drop-in button that opens the apply portal in an overlay modal.
(function () {
    if (window.__cgrWidgetLoaded) return;
    window.__cgrWidgetLoaded = true;

    var script = document.currentScript || (function () {
        var scripts = document.getElementsByTagName('script');
        return scripts[scripts.length - 1];
    })();
    var SLUG = (script && script.getAttribute('data-pipeline-slug')) || '';
    var ORIGIN = new URL(script.src).origin;
    var BUTTON_LABEL = (script && script.getAttribute('data-button-label')) || 'Apply Now';
    var THEME = (script && script.getAttribute('data-theme')) || 'auto'; // auto|dark|light
    var AUTOINJECT = (script && script.getAttribute('data-autoinject')) !== 'false';

    if (!SLUG) {
        console.error('[CGRecruit Widget] data-pipeline-slug attribute is required.');
        return;
    }

    // ---- Inject CSS ------------------------------------------------------
    var css = document.createElement('style');
    css.innerHTML = '\\
.cgr-apply-overlay { position: fixed; inset: 0; background: rgba(10,6,16,0.72); backdrop-filter: blur(6px); display:flex; align-items:center; justify-content:center; z-index: 2147483647; opacity: 0; pointer-events: none; transition: opacity 0.2s; }\\
.cgr-apply-overlay.open { opacity: 1; pointer-events: auto; }\\
.cgr-apply-modal { position: relative; width: min(560px, 95vw); height: min(720px, 95vh); background: #0a0610; border: 1.5px solid #3a2258; border-radius: 20px; box-shadow: 0 30px 80px rgba(10,6,16,0.7); overflow: hidden; transform: scale(0.96); transition: transform 0.2s; }\\
.cgr-apply-overlay.open .cgr-apply-modal { transform: scale(1); }\\
.cgr-apply-modal iframe { width:100%; height:100%; border:0; }\\
.cgr-apply-close { position:absolute; top:12px; right:12px; width:30px; height:30px; border-radius:50%; background:rgba(255,255,255,0.08); color:#f7eefb; border:1px solid #3a2258; cursor:pointer; font-size:18px; display:flex; align-items:center; justify-content:center; line-height:1; transition:background 0.15s; z-index:1; }\\
.cgr-apply-close:hover { background:rgba(255,255,255,0.18); }\\
.cgr-apply-btn { display:inline-flex; align-items:center; gap:8px; background: linear-gradient(135deg,#ec008c,#c026a9 52%,#7a2a9e); color:#fff; border:0; padding:13px 26px; border-radius:999px; font-weight:700; font-family:inherit; font-size:15px; cursor:pointer; box-shadow:0 12px 30px -8px rgba(236,0,140,0.55); transition:transform 0.15s, box-shadow 0.15s, filter 0.15s; }\\
.cgr-apply-btn:hover { transform: translateY(-1px); filter: brightness(1.08); box-shadow:0 16px 36px -8px rgba(236,0,140,0.65); }\\
';
    document.head.appendChild(css);

    // ---- Modal management ------------------------------------------------
    var overlay = null;
    function ensureOverlay() {
        if (overlay) return overlay;
        overlay = document.createElement('div');
        overlay.className = 'cgr-apply-overlay';
        overlay.setAttribute('data-cgr-apply-overlay', 'true');
        overlay.innerHTML = '\\
<div class="cgr-apply-modal" role="dialog" aria-modal="true">\\
  <button class="cgr-apply-close" aria-label="Close">&times;</button>\\
  <iframe src="" title="Apply"></iframe>\\
</div>';
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay || e.target.classList.contains('cgr-apply-close')) {
                close();
            }
        });
        document.body.appendChild(overlay);
        // ESC key closes
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && overlay.classList.contains('open')) close();
        });
        return overlay;
    }

    function open() {
        var ov = ensureOverlay();
        var iframe = ov.querySelector('iframe');
        var u = ORIGIN + '/apply/' + encodeURIComponent(SLUG) + '?embed=1';
        if (THEME && THEME !== 'auto') u += '&theme=' + THEME;
        iframe.src = u;
        ov.classList.add('open');
        document.body.style.overflow = 'hidden';
    }

    function close() {
        if (!overlay) return;
        overlay.classList.remove('open');
        document.body.style.overflow = '';
        // Drop the iframe content so re-opens get a fresh form
        setTimeout(function () { overlay.querySelector('iframe').src = ''; }, 200);
    }

    // ---- Button injection -----------------------------------------------
    function attachToTriggers() {
        // Hook ALL existing [data-cgr-apply] elements
        var triggers = document.querySelectorAll('[data-cgr-apply]');
        triggers.forEach(function (el) {
            if (el.__cgrBound) return;
            el.__cgrBound = true;
            el.addEventListener('click', function (e) {
                e.preventDefault();
                open();
            });
        });
    }

    // ---- Auto-inject a default button if no triggers exist + autoinject is on
    function maybeAutoInject() {
        if (!AUTOINJECT) return;
        if (document.querySelector('[data-cgr-apply]')) return; // user has their own button
        // Look for a placeholder div, e.g. <div data-cgr-apply-button></div>
        var slot = document.querySelector('[data-cgr-apply-button]');
        if (!slot) return;
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'cgr-apply-btn';
        btn.setAttribute('data-cgr-apply', '');
        btn.textContent = BUTTON_LABEL;
        slot.appendChild(btn);
    }

    function init() {
        maybeAutoInject();
        attachToTriggers();
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Public API
    window.CgrApply = { open: open, close: close, version: '1.1' };
})();
"""


@router.get("/widget.js")
async def widget_js():
    """Serve the embed widget script. Cached by browsers, so any update to
    the JS string above will need a version bump in the data-attribute
    docs (or hash-based cache busting on the customer's side)."""
    return Response(
        content=WIDGET_JS,
        media_type="application/javascript",
        headers={
            # 1-day cache — long enough to be cheap, short enough that bug
            # fixes propagate within 24 hours.
            "Cache-Control": "public, max-age=86400",
            # Allow embedding from any origin (this is meant to be cross-site).
            "Access-Control-Allow-Origin": "*",
        },
    )
