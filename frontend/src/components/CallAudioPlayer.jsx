import { useEffect, useState } from "react";
import api from "@/lib/api";

/**
 * Lazy-loaded `<audio>` player for an ElevenLabs conversation recording.
 * Pulls the blob from `/conversations/{id}/audio` (which streams from
 * ElevenLabs). When `autoLoad=true` (Conversation Summary card), it
 * fetches on mount; otherwise renders a button so the recruiter can
 * decide whether to consume bandwidth.
 */
export default function CallAudioPlayer({ conversationId, testid, autoLoad = false }) {
    const [audioUrl, setAudioUrl] = useState(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState(null);

    const load = async () => {
        setBusy(true);
        setError(null);
        try {
            const r = await api.get(`/conversations/${conversationId}/audio`, { responseType: "blob" });
            const url = URL.createObjectURL(r.data);
            setAudioUrl(url);
        } catch (e) {
            setError(e?.response?.data?.detail || "Audio not available yet");
        } finally { setBusy(false); }
    };

    useEffect(() => {
        if (autoLoad && !audioUrl && !busy && !error) {
            load();
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [autoLoad, conversationId]);

    if (audioUrl) {
        return (
            <audio
                data-testid={testid}
                controls
                src={audioUrl}
                className="w-full mb-3"
                style={{ filter: "invert(0.85) hue-rotate(180deg)" }}
            />
        );
    }
    return (
        <div className="mb-3">
            <button
                onClick={load}
                disabled={busy}
                data-testid={`load-${testid}`}
                className="btn-secondary !py-1.5 !px-3 text-xs flex items-center gap-1.5"
            >
                {busy ? "Loading…" : "🎧 Load call recording"}
            </button>
            {error && <div className="text-xs text-brand-warning mt-1">{error}</div>}
        </div>
    );
}
