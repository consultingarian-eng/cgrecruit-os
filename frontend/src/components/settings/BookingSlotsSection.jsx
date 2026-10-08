import { useState, useRef, useEffect } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { CalendarBlank, ArrowClockwise, WarningCircle } from "@phosphor-icons/react";

const FIELD = ({ label, desc, children }) => (
    <div className="flex items-start justify-between gap-6 py-3 border-b border-strokes last:border-0">
        <div className="flex-1 min-w-0">
            <div className="text-sm font-medium text-ink">{label}</div>
            {desc && <div className="text-xs text-ink-muted mt-0.5 leading-relaxed">{desc}</div>}
        </div>
        <div className="shrink-0">{children}</div>
    </div>
);

const NumInput = ({ value, onChange, min = 1, max = 99 }) => (
    <input
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(e) => onChange(Math.max(min, Math.min(max, Number(e.target.value))))}
        className="w-20 bg-surface-active border border-strokes rounded px-2 py-1 text-sm text-center text-ink focus:outline-none focus:border-brand-primary"
    />
);

export default function BookingSlotsSection({ settings, onSaved, pipelineId }) {
    const initial = () => settings.booking_preferences || {
        slots_primary: 2,
        slots_fallback: 1,
        reschedule_link_days_ahead: 4,
        reschedule_retry_days: 3,
        scheduling_link: "",
    };
    // 50, not 1: every backend reader resolves an unset limit as
    // `int(applicant_limit or 50)`. Showing 1 here made an untouched Save write
    // 1 back and silently collapse every slot's capacity from 50 to 1.
    const initialAppt = () => {
        const a = settings.appointments || {};
        return {
            applicant_limit: Number(a.applicant_limit || 50),
            booking_days_offered: Number(a.booking_days_offered ?? 7),
        };
    };

    const [form, setForm] = useState(initial);
    const [appt, setAppt] = useState(initialAppt);
    const lastScopeRef = useRef(pipelineId);
    useEffect(() => {
        if (lastScopeRef.current !== pipelineId) {
            lastScopeRef.current = pipelineId;
            if (settings.booking_preferences) setForm(settings.booking_preferences);
            const a = settings.appointments || {};
            setAppt({
                applicant_limit: Number(a.applicant_limit || 50),
                booking_days_offered: Number(a.booking_days_offered ?? 7),
            });
        }
    }, [pipelineId, settings]);

    const [busy, setBusy] = useState(false);
    const [preview, setPreview] = useState(null);
    const [previewing, setPreviewing] = useState(false);
    const upd = (k, v) => setForm((f) => ({ ...f, [k]: v }));
    const updAppt = (k, v) => setAppt((a) => ({ ...a, [k]: v }));

    const runPreview = async () => {
        if (!pipelineId) { toast.error("Select a pipeline first"); return; }
        setPreviewing(true);
        setPreview(null);
        try {
            const r = await api.get("/settings/preview-slots", { params: { pipeline_id: pipelineId } });
            setPreview(r.data);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Preview failed");
        } finally { setPreviewing(false); }
    };

    const save = async () => {
        setBusy(true);
        try {
            const params = pipelineId ? { pipeline_id: pipelineId } : {};
            await api.put("/settings/booking-preferences", form, { params });
            // Send ONLY the fields this page owns. The endpoint merges partially,
            // so fields we don't send (reminder offsets, appointment_type, and
            // crucially booking_days_offered when untouched) keep their saved
            // values instead of snapping back to model defaults.
            await api.put("/settings/appointments", {
                applicant_limit: appt.applicant_limit,
                booking_days_offered: appt.booking_days_offered,
            }, { params });
            await onSaved?.();
            toast.success("Booking preferences saved");
        } catch {
            toast.error("Failed to save");
        } finally { setBusy(false); }
    };

    return (
        <div className="space-y-6">
            <div>
                <h2 className="label-overline mb-0.5">Slot Capacity</h2>
                <p className="text-xs text-ink-muted">
                    How many candidates can share the same interview slot.
                </p>
            </div>

            <div className="surface p-4 divide-y divide-strokes">
                <FIELD
                    label="Default slot capacity"
                    desc="How many candidates can book the same interview slot before it's hidden. Availability rules that were given their own capacity when they were created keep it — change those on the Calendar."
                >
                    <NumInput value={appt.applicant_limit} onChange={(v) => updAppt("applicant_limit", v)} min={1} max={500} />
                </FIELD>
                <FIELD
                    label="Booking window (days)"
                    desc="How far ahead candidates can be offered interview slots — applies to the AI call, the screening chat, and every booking link. Same/next-day slots show up best, so keep this tight."
                >
                    <NumInput value={appt.booking_days_offered} onChange={(v) => updAppt("booking_days_offered", v)} min={1} max={60} />
                </FIELD>
            </div>

            <div>
                <h2 className="label-overline mb-0.5">No-Agreement Fallback</h2>
                <p className="text-xs text-ink-muted">
                    When no slot is agreed, Olivia tells the candidate a booking link will be texted to them — and the system automatically sends it. Leave the field below blank to use CGRecruit's built-in retry link, or paste an external link (e.g. Calendly) to override it.
                </p>
            </div>

            <div className="surface p-4">
                <FIELD
                    label="External scheduling link (optional)"
                    desc="Leave blank to use CGRecruit's own retry link — usually the right choice. Only set this if you want to send candidates to an external booking page instead."
                >
                    <input
                        type="url"
                        placeholder="Leave blank for default CGRecruit link"
                        value={form.scheduling_link || ""}
                        onChange={(e) => upd("scheduling_link", e.target.value)}
                        className="w-64 bg-surface-active border border-strokes rounded px-2 py-1 text-sm text-ink focus:outline-none focus:border-brand-primary"
                    />
                </FIELD>
            </div>

            <div>
                <h2 className="label-overline mb-0.5">Reschedule Portal</h2>
                <p className="text-xs text-ink-muted">
                    Controls the public reschedule link sent after a no-show.
                </p>
            </div>

            <div className="surface p-4 divide-y divide-strokes">
                <FIELD
                    label="Days shown on reschedule portal"
                    desc="Slots on the reschedule link span this many calendar days from when the email is sent."
                >
                    <NumInput value={form.reschedule_link_days_ahead} onChange={(v) => upd("reschedule_link_days_ahead", v)} min={1} max={30} />
                </FIELD>

                <FIELD
                    label="Re-notify delay (days)"
                    desc={'When a candidate clicks "Can\'t do these?" we email them again after this many days with fresh slots.'}
                >
                    <NumInput value={form.reschedule_retry_days} onChange={(v) => upd("reschedule_retry_days", v)} min={1} max={30} />
                </FIELD>
            </div>

            <div>
                <h2 className="label-overline mb-0.5">Slot Preview</h2>
                <p className="text-xs text-ink-muted">
                    See exactly which slots Olivia would offer on a live call right now — pulled from the same endpoint ElevenLabs calls.
                </p>
            </div>

            <div className="surface p-4 space-y-3">
                <button
                    onClick={runPreview}
                    disabled={previewing || !pipelineId}
                    className="btn-secondary inline-flex items-center gap-1.5 text-xs !py-1.5 !px-3"
                >
                    <CalendarBlank size={13} weight="duotone" />
                    {previewing ? "Loading…" : "Preview slots agent will offer"}
                </button>

                {preview && (
                    <div className="mt-3 space-y-4 text-xs">
                        <div className="flex items-center gap-2 text-ink-muted">
                            <span>{preview.pipeline} · {preview.timezone} · next {preview.days_ahead} days · {preview.total_available} total open slot{preview.total_available !== 1 ? "s" : ""}</span>
                            <button onClick={runPreview} className="ml-auto text-ink-muted hover:text-ink">
                                <ArrowClockwise size={12} />
                            </button>
                        </div>

                        {preview.total_available === 0 ? (
                            <div className="flex items-center gap-2 text-amber-400 bg-amber-400/10 border border-amber-400/20 rounded-md px-3 py-2">
                                <WarningCircle size={14} weight="fill" />
                                <span>No slots found — check your availability calendar and booking window settings.</span>
                            </div>
                        ) : (
                            <>
                                <div>
                                    <div className="text-[10px] uppercase tracking-widest font-semibold text-brand-primary mb-1.5">
                                        Primary — Olivia offers these first ({preview.slots_primary} slot{preview.slots_primary !== 1 ? "s" : ""})
                                    </div>
                                    {preview.primary_slots.length === 0 ? (
                                        <div className="text-ink-muted italic">None — fewer slots available than slots_primary setting</div>
                                    ) : (
                                        <ul className="space-y-1">
                                            {preview.primary_slots.map((s, i) => (
                                                <li key={s.datetime} className="flex items-center gap-2">
                                                    <span className="w-4 h-4 rounded-full bg-brand-primary/20 text-brand-primary flex items-center justify-center font-bold text-[9px] shrink-0">{i + 1}</span>
                                                    <span className="text-ink font-medium">{s.label}</span>
                                                    <span className="text-ink-muted ml-auto">{s.capacity_remaining} spot{s.capacity_remaining !== 1 ? "s" : ""} left</span>
                                                </li>
                                            ))}
                                        </ul>
                                    )}
                                </div>

                                <div>
                                    <div className="text-[10px] uppercase tracking-widest font-semibold text-ink-muted mb-1.5">
                                        Fallback — Olivia offers these only if candidate rejects all primary ({preview.slots_fallback} slot{preview.slots_fallback !== 1 ? "s" : ""})
                                    </div>
                                    {preview.fallback_slots.length === 0 ? (
                                        <div className="text-ink-muted italic">None — no slots remain after primary</div>
                                    ) : (
                                        <ul className="space-y-1">
                                            {preview.fallback_slots.map((s, i) => (
                                                <li key={s.datetime} className="flex items-center gap-2">
                                                    <span className="w-4 h-4 rounded-full bg-surface-active border border-strokes text-ink-muted flex items-center justify-center font-bold text-[9px] shrink-0">{i + 1}</span>
                                                    <span className="text-ink">{s.label}</span>
                                                    <span className="text-ink-muted ml-auto">{s.capacity_remaining} spot{s.capacity_remaining !== 1 ? "s" : ""} left</span>
                                                </li>
                                            ))}
                                        </ul>
                                    )}
                                </div>

                                {preview.all_slots.length > preview.slots_primary + preview.slots_fallback && (
                                    <div className="text-ink-muted border-t border-strokes pt-2">
                                        +{preview.all_slots.length - preview.slots_primary - preview.slots_fallback} more slot{preview.all_slots.length - preview.slots_primary - preview.slots_fallback !== 1 ? "s" : ""} exist beyond what Olivia would offer
                                    </div>
                                )}
                            </>
                        )}
                    </div>
                )}
            </div>

            <div className="flex justify-end">
                <button onClick={save} disabled={busy} className="btn-primary">
                    {busy ? "Saving…" : "Save booking preferences"}
                </button>
            </div>
        </div>
    );
}
