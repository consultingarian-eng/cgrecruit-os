import { useState, useEffect, useRef } from "react";
import api from "@/lib/api";
import { toast } from "sonner";

export default function RegionLanguageSection({ settings, onSaved, pipelineId }) {
    const [form, setForm] = useState(settings.region_language || {});
    const [busy, setBusy] = useState(false);

    // Reset only when scope (pipeline) changes — see comment in
    // ScreenCallAgentSection / useScopedFormState for the rationale.
    const lastScopeRef = useRef(pipelineId);
    useEffect(() => {
        if (lastScopeRef.current !== pipelineId) {
            lastScopeRef.current = pipelineId;
            setForm(settings.region_language || {});
        }
    }, [pipelineId, settings]);

    const save = async () => {
        setBusy(true);
        try {
            const params = pipelineId ? { pipeline_id: pipelineId } : {};
            await api.put("/settings/region-language", form, { params });
            await onSaved?.();
            toast.success("Saved");
        } catch { toast.error("Failed to save"); }
        finally { setBusy(false); }
    };

    return (
        <div className="max-w-xl space-y-6" data-testid="region-language-section">
            <div>
                <h2 className="font-heading text-2xl font-bold tracking-tight">Region & Language</h2>
                <p className="text-sm text-ink-muted mt-1">
                    Configure your timezone, language, and date display preferences.
                </p>
            </div>
            <div>
                <label className="label-overline block mb-1.5">Timezone</label>
                <select className="input-dark" data-testid="timezone-select" value={form.timezone || ""} onChange={(e) => setForm({ ...form, timezone: e.target.value })}>
                    {[
                        "Europe/London", "Europe/Berlin", "Europe/Paris",
                        "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
                        "Asia/Dubai", "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney",
                    ].map((tz) => <option key={tz} value={tz}>{tz}</option>)}
                </select>
            </div>
            <div>
                <label className="label-overline block mb-1.5">Language</label>
                <select className="input-dark" data-testid="language-select" value={form.language || ""} onChange={(e) => setForm({ ...form, language: e.target.value })}>
                    {["English", "Spanish", "French", "German", "Portuguese", "Italian"].map((l) => <option key={l} value={l}>{l}</option>)}
                </select>
            </div>
            <div>
                <label className="label-overline block mb-1.5">Date Format</label>
                <div className="grid grid-cols-2 gap-2">
                    {["DD/MM/YYYY", "MM/DD/YYYY"].map((fmt) => (
                        <button
                            key={fmt}
                            data-testid={`date-format-${fmt}`}
                            onClick={() => setForm({ ...form, date_format: fmt })}
                            className={`p-3 rounded border text-sm transition-colors ${form.date_format === fmt ? "border-brand-primary bg-[rgba(59,130,246,0.08)]" : "border-strokes hover:border-strokes-focus"}`}
                        >
                            {fmt}
                        </button>
                    ))}
                </div>
            </div>
            <button onClick={save} disabled={busy} data-testid="save-region-btn" className="btn-primary">
                {busy ? "Saving…" : "Save"}
            </button>
        </div>
    );
}
