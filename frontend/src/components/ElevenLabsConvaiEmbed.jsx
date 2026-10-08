import { useEffect, useRef, useState } from "react";
import api from "@/lib/api";

/**
 * Mounts the ElevenLabs Convai web component (`<elevenlabs-convai>`) safely.
 *
 * Agents are synced with ElevenLabs authentication switched on, so the widget
 * can't connect with the agent id alone. It asks the backend
 * (`POST /api/elevenlabs/agent/test-session`, owner only) for a short-lived
 * signed URL and hands that to the widget instead.
 *
 * Using `document.createElement` + `setAttribute` instead of
 * `dangerouslySetInnerHTML` so the values pass through the DOM's safe
 * attribute serialiser — they can never break out into raw HTML.
 *
 * The web component itself is loaded via the `<script>` tag in `public/index.html`.
 */
export default function ElevenLabsConvaiEmbed({ agentId, dynamicVariables }) {
    const containerRef = useRef(null);
    const [signedUrl, setSignedUrl] = useState("");
    const [error, setError] = useState("");
    // Compared as a string: callers pass a fresh object literal on every render.
    const dynJson = dynamicVariables ? JSON.stringify(dynamicVariables) : "";

    useEffect(() => {
        let cancelled = false;
        setSignedUrl("");
        setError("");
        if (!agentId) return undefined;
        api.post("/elevenlabs/agent/test-session", { agent_id: agentId })
            .then((r) => { if (!cancelled) setSignedUrl(r.data?.signed_url || ""); })
            .catch((e) => {
                if (!cancelled) setError(e?.response?.data?.detail || "Could not start a test session.");
            });
        return () => { cancelled = true; };
    }, [agentId]);

    useEffect(() => {
        const container = containerRef.current;
        if (!container || !signedUrl) return undefined;
        container.innerHTML = "";  // Clear any prior mount so re-renders refresh the widget.
        const el = document.createElement("elevenlabs-convai");
        el.setAttribute("signed-url", signedUrl);
        if (dynJson) {
            el.setAttribute("dynamic-variables", dynJson);
        }
        container.appendChild(el);
        return () => {
            // Detach on unmount so the widget cleans its own audio session.
            container.innerHTML = "";
        };
    }, [signedUrl, dynJson]);

    if (!agentId) return null;
    if (error) {
        return <div className="text-xs text-[#F87171]" data-testid="elevenlabs-convai-error">{error}</div>;
    }
    return <div ref={containerRef} data-testid="elevenlabs-convai-embed" />;
}
