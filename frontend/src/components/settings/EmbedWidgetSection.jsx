import { useEffect, useState } from "react";
import { Code, Copy, CheckCircle } from "@phosphor-icons/react";
import { toast } from "sonner";
import api from "@/lib/api";

/**
 * Settings → Embed Widget. Generates a copy-paste <script> snippet that
 * customers can drop into ANY careers page to open your apply
 * modal without navigating away.
 *
 * The actual JS is served by the backend at /api/widget.js — see
 * `backend/routes/widget.py` for the implementation.
 */
export default function EmbedWidgetSection() {
    const [pipelines, setPipelines] = useState([]);
    const [slug, setSlug] = useState("");
    const [copiedKey, setCopiedKey] = useState(null);

    useEffect(() => {
        api.get("/pipelines").then((r) => {
            setPipelines(r.data || []);
            if (r.data?.length) setSlug(r.data[0].public_slug || "");
        }).catch(() => { });
    }, []);

    const origin = process.env.REACT_APP_BACKEND_URL || window.location.origin;

    const scriptTag = `<script src="${origin}/api/widget.js"
        data-pipeline-slug="${slug || "your-pipeline-slug"}"
        data-button-label="Apply Now"
        async></script>`;

    const buttonHtml = `<!-- Place this where you want the "Apply Now" button to appear -->
<div data-cgr-apply-button></div>`;

    const customButton = `<!-- Or wire up your own button anywhere -->
<button data-cgr-apply class="your-existing-css">Join the team</button>`;

    const copy = (text, key) => {
        navigator.clipboard.writeText(text);
        setCopiedKey(key);
        toast.success("Copied!");
        setTimeout(() => setCopiedKey(null), 1500);
    };

    return (
        <div className="max-w-3xl mx-auto px-8 py-10 space-y-8" data-testid="embed-widget-section">
            <div>
                <div className="flex items-center gap-3 mb-1">
                    <Code size={22} weight="duotone" className="text-brand-primary" />
                    <h1 className="font-heading text-2xl font-bold">Embed Widget</h1>
                </div>
                <p className="text-sm text-ink-muted leading-relaxed max-w-2xl">
                    Drop this snippet on any careers page to open the apply form in a modal —
                    no page jump, higher conversion. Works on any platform: WordPress, Webflow,
                    Squarespace, Framer, plain HTML.
                </p>
            </div>

            <div className="surface p-5 space-y-4">
                <div>
                    <label className="label-overline block mb-2">Pipeline</label>
                    <select
                        data-testid="embed-pipeline-select"
                        className="input-dark"
                        value={slug}
                        onChange={(e) => setSlug(e.target.value)}
                    >
                        {pipelines.map((p) => (
                            <option key={p.id} value={p.public_slug}>{p.name} ({p.public_slug})</option>
                        ))}
                    </select>
                    <p className="text-[11px] text-ink-muted mt-1.5">
                        Each pipeline gets its own widget — pick the one this careers page should funnel to.
                    </p>
                </div>
            </div>

            <CodeBlock
                title="1. Add the script tag (just before <code>&lt;/body&gt;</code>)"
                description="Loads the widget once. Doesn't show anything yet — pairs with step 2."
                code={scriptTag}
                copied={copiedKey === "script"}
                onCopy={() => copy(scriptTag, "script")}
                testidPrefix="embed-script"
            />

            <CodeBlock
                title="2A. Auto-injected button (easiest)"
                description="Drop a placeholder div anywhere on the page; the widget injects a styled apply button into it."
                code={buttonHtml}
                copied={copiedKey === "auto"}
                onCopy={() => copy(buttonHtml, "auto")}
                testidPrefix="embed-auto-button"
            />

            <CodeBlock
                title="2B. Your own button (full control)"
                description="Already have a styled CTA on the page? Add data-cgr-apply to it and the widget will hijack the click."
                code={customButton}
                copied={copiedKey === "custom"}
                onCopy={() => copy(customButton, "custom")}
                testidPrefix="embed-custom-button"
            />

            <div className="surface p-5">
                <div className="font-heading text-base font-semibold mb-2">Live preview</div>
                <p className="text-xs text-ink-muted mb-3">
                    The button below uses YOUR pipeline slug — clicking it opens the same modal
                    candidates will see on customer sites.
                </p>
                <div data-cgr-apply-button></div>
                <PreviewLoader slug={slug} />
            </div>

            <div className="surface p-5 text-xs text-ink-muted leading-relaxed">
                <div className="font-heading text-sm text-ink mb-2">Optional script attributes</div>
                <ul className="space-y-1.5 list-disc list-inside">
                    <li><code className="text-brand-primary">data-pipeline-slug</code> — required. Which pipeline candidates land on.</li>
                    <li><code className="text-brand-primary">data-button-label</code> — text on the auto-injected button. Default: <em>Apply Now</em>.</li>
                    <li><code className="text-brand-primary">data-theme</code> — <em>auto</em> | <em>dark</em> | <em>light</em>. Default: <em>auto</em>.</li>
                    <li><code className="text-brand-primary">data-autoinject</code> — set to <em>false</em> to skip the auto-injected button (use 2B only).</li>
                </ul>
            </div>
        </div>
    );
}

function CodeBlock({ title, description, code, copied, onCopy, testidPrefix }) {
    return (
        <div className="surface p-5">
            <div className="font-heading text-sm font-semibold mb-1">{title}</div>
            {description && <p className="text-[11px] text-ink-muted mb-3">{description}</p>}
            <div className="relative">
                <pre
                    data-testid={`${testidPrefix}-code`}
                    className="bg-[#08080A] border border-strokes rounded-md p-3.5 text-[11px] font-mono text-ink overflow-x-auto whitespace-pre"
                >{code}</pre>
                <button
                    onClick={onCopy}
                    data-testid={`${testidPrefix}-copy-btn`}
                    className="absolute top-2 right-2 px-2 py-1 text-[10px] flex items-center gap-1 rounded bg-surface-hover text-ink-muted hover:text-ink"
                >
                    {copied ? <><CheckCircle size={11} weight="fill" className="text-emerald-400" /> Copied</> : <><Copy size={11} /> Copy</>}
                </button>
            </div>
        </div>
    );
}

// Small helper that loads the widget script for the live preview when the
// slug changes. We dynamically (re-)inject it so flipping pipelines updates
// the demo button on the fly.
function PreviewLoader({ slug }) {
    useEffect(() => {
        if (!slug) return;
        // Reset the loaded flag so the script can re-init for the new slug.
        delete window.__cgrWidgetLoaded;
        // Drop any existing widget overlays before re-mounting.
        document.querySelectorAll('[data-cgr-apply-overlay]').forEach((el) => el.remove());
        // Drop the auto-injected button so it gets re-rendered with new slug.
        document.querySelectorAll('[data-cgr-apply-button] button').forEach((b) => b.remove());

        const origin = process.env.REACT_APP_BACKEND_URL || window.location.origin;
        const old = document.querySelector('script[data-cgr-preview]');
        if (old) old.remove();
        const s = document.createElement("script");
        s.src = `${origin}/api/widget.js?_=${Date.now()}`; // cache-bust per preview reload
        s.async = true;
        s.setAttribute("data-pipeline-slug", slug);
        s.setAttribute("data-button-label", "Apply Now (preview)");
        s.setAttribute("data-cgr-preview", "true");
        document.body.appendChild(s);
    }, [slug]);
    return null;
}
