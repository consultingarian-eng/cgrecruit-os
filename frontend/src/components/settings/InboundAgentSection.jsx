import { useState } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { PhoneIncoming, Robot, ArrowClockwise, Eye, X, CheckCircle, WarningCircle } from "@phosphor-icons/react";

/**
 * Inbound Call Agent — the per-office ElevenLabs receptionist that ANSWERS calls
 * to the office line. It handles booked / about-to-start candidates who ring in
 * to ask for the address or to reschedule. Address + Google Maps link (when set)
 * are baked per office; the caller is identified by their number at call start.
 *
 * "Create & sync" builds/updates the agent for the selected pipeline and assigns
 * it to that office's phone number for inbound calls.
 */
export default function InboundAgentSection({ pipelineId }) {
    const [syncing, setSyncing] = useState(false);
    const [rows, setRows] = useState(null);
    const [prompt, setPrompt] = useState(null);
    const [loadingPrompt, setLoadingPrompt] = useState(false);

    const sync = async () => {
        setSyncing(true);
        setRows(null);
        try {
            const body = pipelineId ? { pipeline_id: pipelineId } : {};
            const r = await api.post("/inbound/agent/sync", body);
            const results = r.data?.results || [];
            setRows(results);
            const failed = results.filter((x) => x.status !== "synced");
            if (failed.length) toast.error(`Sync: ${failed.length} failed — ${failed[0]?.error || "see below"}`);
            else toast.success(`Inbound agent${results.length > 1 ? "s" : ""} ready${results.some((x) => x.created) ? " (created new)" : ""}`);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Inbound sync failed");
        } finally { setSyncing(false); }
    };

    const loadPrompt = async () => {
        setLoadingPrompt(true);
        try {
            const params = pipelineId ? { pipeline_id: pipelineId } : {};
            const r = await api.get("/inbound/agent/preview-prompt", { params });
            setPrompt(r.data);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to load prompt");
        } finally { setLoadingPrompt(false); }
    };

    return (
        <div className="space-y-5">
            <div className="flex items-start gap-3">
                <div className="w-9 h-9 rounded-lg bg-brand-primary/15 flex items-center justify-center shrink-0">
                    <PhoneIncoming size={18} weight="duotone" className="text-brand-primary" />
                </div>
                <div>
                    <h2 className="text-base font-heading font-semibold">Inbound Call Agent</h2>
                    <p className="text-sm text-ink-muted mt-1 leading-relaxed max-w-2xl">
                        Answers calls to this office's line. When a booked or starting candidate rings in,
                        it recognises their number, gives the office address (with the office's Google Maps
                        link, when one is set in the company profile), and reschedules their interview — using the same availability as your
                        booking flow. Anything it can't answer is escalated to the bell as a follow-up.
                    </p>
                </div>
            </div>

            {!pipelineId && (
                <div className="flex items-center gap-2 text-xs text-amber-400 bg-amber-900/20 border border-amber-800/40 rounded-lg px-3 py-2">
                    <WarningCircle size={14} /> Select a specific pipeline above — inbound agents are created per office.
                </div>
            )}

            <div className="flex gap-2">
                <button onClick={sync} disabled={syncing} className="btn-primary flex items-center gap-2 text-sm disabled:opacity-50">
                    {syncing ? <ArrowClockwise size={14} className="animate-spin" /> : <Robot size={14} />}
                    {syncing ? "Syncing…" : "Create & sync inbound agent"}
                </button>
                <button onClick={loadPrompt} disabled={loadingPrompt} className="btn-secondary flex items-center gap-2 text-sm disabled:opacity-50">
                    <Eye size={14} /> Preview prompt
                </button>
            </div>

            {rows && (
                <div className="space-y-2">
                    {rows.map((r) => (
                        <div key={r.pipeline_id} className="text-xs bg-surface border border-strokes rounded-lg px-3 py-2">
                            <div className="flex items-center gap-2 font-medium">
                                {r.status === "synced" ? <CheckCircle size={14} className="text-emerald-500" /> : <WarningCircle size={14} className="text-red-500" />}
                                {r.pipeline} — {r.status}{r.created ? " (created)" : ""}
                            </div>
                            <div className="text-ink-muted mt-1 space-y-0.5">
                                {r.agent_id && <div>Agent: <span className="font-mono">{r.agent_id}</span></div>}
                                <div>Office address set: {r.office_address_set ? "yes" : "no"} · Maps link: {r.maps_link ? "yes" : "—"}</div>
                                <div>Phone-number assign: {r.assign?.status || "—"}{r.assign?.error ? ` (${r.assign.error})` : ""}</div>
                                {r.error && <div className="text-red-400">{r.error}</div>}
                            </div>
                        </div>
                    ))}
                </div>
            )}

            {prompt && (
                <div className="relative bg-surface border border-strokes rounded-lg p-4">
                    <button onClick={() => setPrompt(null)} className="absolute top-3 right-3 text-ink-muted hover:text-ink"><X size={14} /></button>
                    <div className="text-xs text-ink-muted mb-2">
                        Office: <b>{prompt.office_key || "—"}</b> · Address: {prompt.office_address || "(not set — edit in Starter Email template)"} · Maps: {prompt.maps_link || "—"} · Phone-number ID: {prompt.phone_number_id || "(none)"}
                    </div>
                    <div className="text-[11px] font-semibold text-ink-muted mb-1">First message</div>
                    <pre className="text-xs whitespace-pre-wrap text-ink mb-3">{prompt.first_message}</pre>
                    <div className="text-[11px] font-semibold text-ink-muted mb-1">System prompt</div>
                    <pre className="text-xs whitespace-pre-wrap text-ink max-h-[360px] overflow-y-auto">{prompt.system_prompt}</pre>
                </div>
            )}
        </div>
    );
}
