import { useEffect, useState } from "react";
import api from "@/lib/api";
import { formatAmPm, isBeforeBusinessHours, pmSuggestion } from "@/lib/timeFormat";
import { toast } from "sonner";
import { Trash, Calendar as CalendarIcon, Globe, Plus, AirplaneTilt, UsersThree, ArrowClockwise, WarningCircle, VideoCamera, User } from "@phosphor-icons/react";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const _newBlackoutId = () =>
    (typeof crypto !== "undefined" && crypto.randomUUID)
        ? crypto.randomUUID()
        : `bo-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

const _formatBlackoutRange = (b) => {
    try {
        const s = new Date(b.start);
        const e = new Date(b.end);
        const sameDay =
            s.getUTCFullYear() === e.getUTCFullYear() &&
            s.getUTCMonth() === e.getUTCMonth() &&
            s.getUTCDate() === e.getUTCDate();
        const fmt = (d) =>
            d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
        return sameDay ? fmt(s) : `${fmt(s)} → ${fmt(e)}`;
    } catch {
        return `${b.start || ""} → ${b.end || ""}`;
    }
};

/**
 * Manage interview slots for one or many pipelines from a single panel.
 * Slot times are interpreted in the recruiter's account timezone (Settings →
 * Region & Language). Each rule yields one or more slots whose `capacity` lets
 * up to N candidates book the same instant (default 50).
 */
export default function AvailabilityManager({ pipelines, onClose, onSaved }) {
    const [activeId, setActiveId] = useState(pipelines[0]?.id);
    const active = pipelines.find((p) => p.id === activeId) || pipelines[0];
    const [draft, setDraft] = useState({
        availability_rules: active?.availability_rules || [],
        availability_blackouts: active?.availability_blackouts || [],
        appointment_duration_minutes: active?.appointment_duration_minutes || 30,
        appointment_link: active?.appointment_link || "",
        appointment_recruiter: active?.appointment_recruiter || "",
    });
    const [blackoutDraft, setBlackoutDraft] = useState({ start: "", end: "", reason: "" });
    const [busy, setBusy] = useState(false);
    const [accountTz, setAccountTz] = useState("UTC");
    const [bookingWindow, setBookingWindow] = useState({ days: 7, defaultCapacity: 50 });
    const [slotOffers, setSlotOffers] = useState({ primary: 2, fallback: 1 });
    const [fullBookingPrefs, setFullBookingPrefs] = useState({});
    const [slotPreview, setSlotPreview] = useState(null);
    const [previewing, setPreviewing] = useState(false);

    const loadSettings = (pipelineId) => {
        const params = pipelineId ? { pipeline_id: pipelineId } : {};
        api.get("/settings", { params }).then((r) => {
            const region = r.data?.region_language || {};
            const appt = r.data?.appointments || {};
            const bp = r.data?.booking_preferences || {};
            setAccountTz(region.timezone || "UTC");
            setBookingWindow({
                defaultCapacity: Number(appt.applicant_limit ?? 50),
            });
            setSlotOffers({
                primary: Number(bp.slots_primary ?? 2),
                fallback: Number(bp.slots_fallback ?? 1),
            });
            setFullBookingPrefs(bp);
        }).catch(() => {});
    };

    useEffect(() => { loadSettings(activeId); }, []);  // eslint-disable-line react-hooks/exhaustive-deps

    const switchPipeline = (id) => {
        const p = pipelines.find((x) => x.id === id);
        if (!p) return;
        setActiveId(id);
        setDraft({
            availability_rules: p.availability_rules || [],
            availability_blackouts: p.availability_blackouts || [],
            appointment_duration_minutes: p.appointment_duration_minutes || 30,
            appointment_link: p.appointment_link || "",
            appointment_recruiter: p.appointment_recruiter || "",
        });
        setBlackoutDraft({ start: "", end: "", reason: "" });
        setSlotPreview(null);
        loadSettings(id);
    };

    const computeEnd = (startHHMM, durationMinutes) => {
        const [h, m] = startHHMM.split(":").map(Number);
        const total = h * 60 + m + durationMinutes;
        return `${String(Math.floor(total / 60) % 24).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
    };

    const save = async () => {
        if (!active) return;
        setBusy(true);
        try {
            const normalizedRules = draft.availability_rules.map((r) => ({
                ...r,
                end: computeEnd(r.start, draft.appointment_duration_minutes),
                slot_minutes: draft.appointment_duration_minutes,
            }));
            await Promise.all([
                api.put(`/pipelines/${active.id}`, {
                    availability_rules: normalizedRules,
                    availability_blackouts: draft.availability_blackouts,
                    appointment_duration_minutes: draft.appointment_duration_minutes,
                    appointment_link: draft.appointment_link,
                    appointment_recruiter: draft.appointment_recruiter,
                }),
                api.put("/settings/booking-preferences", {
                    ...fullBookingPrefs,
                    slots_primary: slotOffers.primary,
                    slots_fallback: slotOffers.fallback,
                }, { params: { pipeline_id: active.id } }),
            ]);
            toast.success(`Updated availability for ${active.name}`);
            onSaved && (await onSaved());
        } catch {
            toast.error("Save failed");
        } finally { setBusy(false); }
    };

    const runPreview = async () => {
        if (!active) return;
        setPreviewing(true);
        setSlotPreview(null);
        try {
            const r = await api.get("/settings/preview-slots", { params: { pipeline_id: active.id } });
            setSlotPreview(r.data);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Preview failed");
        } finally { setPreviewing(false); }
    };

    const addRule = (wd) => {
        const start = "09:00";
        setDraft((d) => ({
            ...d,
            availability_rules: [
                ...d.availability_rules,
                {
                    weekday: wd,
                    start,
                    end: computeEnd(start, d.appointment_duration_minutes),
                    slot_minutes: d.appointment_duration_minutes,
                    capacity: bookingWindow.defaultCapacity || 50,
                    appointment_link_override: "",
                    appointment_recruiter_override: "",
                },
            ],
        }));
    };
    const updateRule = (idx, patch) => {
        setDraft((d) => {
            const next = [...d.availability_rules];
            const updated = { ...next[idx], ...patch };
            if ("start" in patch) {
                updated.end = computeEnd(updated.start, d.appointment_duration_minutes);
                updated.slot_minutes = d.appointment_duration_minutes;
            }
            next[idx] = updated;
            return { ...d, availability_rules: next };
        });
    };
    const removeRule = (idx) => {
        setDraft((d) => {
            const next = [...d.availability_rules];
            next.splice(idx, 1);
            return { ...d, availability_rules: next };
        });
    };

    const addBlackout = () => {
        const { start, end, reason } = blackoutDraft;
        if (!start) {
            toast.error("Pick a start date");
            return;
        }
        // If only `start` is set, treat it as a single-day blackout (00:00 → 23:59:59 UTC).
        const startISO = `${start}T00:00:00Z`;
        const endDate = end || start;
        const endISO = `${endDate}T23:59:59Z`;
        if (new Date(endISO) < new Date(startISO)) {
            toast.error("End date must be on or after start date");
            return;
        }
        setDraft((d) => ({
            ...d,
            availability_blackouts: [
                ...(d.availability_blackouts || []),
                { id: _newBlackoutId(), start: startISO, end: endISO, reason: reason.trim() },
            ],
        }));
        setBlackoutDraft({ start: "", end: "", reason: "" });
    };
    const removeBlackout = (id) => {
        setDraft((d) => ({
            ...d,
            availability_blackouts: (d.availability_blackouts || []).filter((b) => b.id !== id),
        }));
    };

    const grouped = DAYS.map((_, wd) => ({
        wd,
        rules: (draft.availability_rules || []).map((r, i) => ({ ...r, _idx: i })).filter((r) => r.weekday === wd),
    }));

    if (!active) {
        return (
            <div className="surface p-12 text-center" data-testid="no-pipelines-availability">
                <CalendarIcon size={28} weight="duotone" className="mx-auto mb-2 text-ink-muted" />
                <div className="font-heading text-base mb-1">No pipelines yet</div>
                <div className="text-sm text-ink-muted">Create a pipeline first to set availability.</div>
            </div>
        );
    }

    return (
        <div className="surface p-5 max-w-3xl" data-testid="availability-manager">
            <div className="flex items-center justify-between mb-4">
                <div>
                    <h2 className="font-heading text-xl font-semibold flex items-center gap-2">
                        <CalendarIcon size={18} weight="duotone" className="text-brand-primary" />
                        Manage interview slots
                    </h2>
                    <p className="text-xs text-ink-muted mt-0.5">
                        Olivia reads this in real time during screening calls and offers the next available slots.
                    </p>
                </div>
                {onClose && (
                    <button onClick={onClose} data-testid="close-availability-btn" className="text-xs text-ink-muted hover:text-ink px-2 py-1">
                        Close
                    </button>
                )}
            </div>

            {/* Timezone banner — booking window + capacity are configured in Settings → Booking & Slots */}
            <div className="mb-4 px-3 py-2.5 rounded-md border border-strokes bg-[#0B0B0F] flex items-center gap-2 text-xs">
                <Globe size={13} className="text-brand-primary" />
                <span className="text-ink-muted">All times shown in</span>
                <strong className="text-ink" data-testid="account-tz">{accountTz}</strong>
                <span className="text-ink-dim">(change in Settings → Region &amp; Language)</span>
            </div>

            {/* Pipeline switcher tabs */}
            {pipelines.length > 1 && (
                <div className="flex items-center gap-1 mb-4 border-b border-strokes -mx-5 px-5">
                    {pipelines.map((p) => (
                        <button
                            key={p.id}
                            onClick={() => switchPipeline(p.id)}
                            data-testid={`avail-pipeline-tab-${p.id}`}
                            className={`px-3 py-2 text-xs -mb-px border-b-2 transition-colors ${
                                p.id === activeId
                                    ? "border-brand-primary text-ink"
                                    : "border-transparent text-ink-muted hover:text-ink"
                            }`}
                        >
                            {p.name}
                        </button>
                    ))}
                </div>
            )}

            <div className="flex items-center gap-3 mb-4">
                <label className="label-overline">Slot duration</label>
                <select
                    className="input-dark w-32"
                    data-testid="appt-duration-select"
                    value={draft.appointment_duration_minutes}
                    onChange={(e) => setDraft((d) => ({ ...d, appointment_duration_minutes: Number(e.target.value) }))}
                >
                    {[15, 30, 45, 60, 90].map((m) => <option key={m} value={m}>{m} min</option>)}
                </select>
                <span className="text-xs text-ink-muted ml-auto">Pipeline: <strong className="text-ink">{active.name}</strong></span>
            </div>

            {/* Meeting link + recruiter — sent in the booking confirmation email */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-5 pb-5 border-b border-strokes">
                <div>
                    <label className="label-overline mb-1.5 block">Default meeting link</label>
                    <input
                        type="url"
                        className="input-dark"
                        placeholder="https://zoom.us/j/123456789  •  https://meet.google.com/...  •  https://teams.microsoft.com/..."
                        data-testid="appt-link-input"
                        value={draft.appointment_link || ""}
                        onChange={(e) => setDraft((d) => ({ ...d, appointment_link: e.target.value }))}
                    />
                    <div className="text-[11px] text-ink-muted mt-1">
                        Auto-attached to every booking confirmation email + chat log message for this pipeline.
                        Individual slots below can override this with their own link — overridden slots show a purple <VideoCamera size={10} weight="fill" className="inline text-brand-primary" /> icon.
                    </div>
                </div>
                <div>
                    <label className="label-overline mb-1.5 block">Recruiter name</label>
                    <input
                        type="text"
                        className="input-dark"
                        placeholder="e.g. Sam Kestrel"
                        data-testid="appt-recruiter-input"
                        value={draft.appointment_recruiter || ""}
                        onChange={(e) => setDraft((d) => ({ ...d, appointment_recruiter: e.target.value }))}
                    />
                    <div className="text-[11px] text-ink-muted mt-1">
                        Shown on the candidate's portal as "Interview with [name]".
                    </div>
                </div>
            </div>

            {/* Slot offer controls — how many slots Olivia offers on a call */}
            <div className="mb-5 pb-5 border-b border-strokes">
                <div className="label-overline mb-2">Slots Olivia offers on each call</div>
                <div className="flex items-center gap-4 text-xs flex-wrap">
                    <div className="flex items-center gap-2">
                        <span className="text-ink-muted">Primary slots</span>
                        <input
                            type="number" min={1} max={10}
                            className="input-dark !w-14 !py-1 !px-2 text-center"
                            value={slotOffers.primary}
                            onChange={(e) => setSlotOffers((s) => ({ ...s, primary: Math.max(1, Number(e.target.value)) }))}
                        />
                        <span className="text-ink-dim">(offered first)</span>
                    </div>
                    <div className="flex items-center gap-2">
                        <span className="text-ink-muted">Fallback slots</span>
                        <input
                            type="number" min={1} max={10}
                            className="input-dark !w-14 !py-1 !px-2 text-center"
                            value={slotOffers.fallback}
                            onChange={(e) => setSlotOffers((s) => ({ ...s, fallback: Math.max(1, Number(e.target.value)) }))}
                        />
                        <span className="text-ink-dim">(if candidate rejects all primary)</span>
                    </div>
                    <button
                        onClick={runPreview}
                        disabled={previewing}
                        className="btn-secondary !py-1 !px-2 text-[11px] flex items-center gap-1 ml-auto"
                    >
                        <CalendarIcon size={12} weight="duotone" />
                        {previewing ? "Loading…" : "Preview what Olivia sees"}
                    </button>
                </div>

                {slotPreview && (
                    <div className="mt-3 text-xs space-y-3 border border-strokes rounded-md p-3 bg-[#0B0B0F]">
                        <div className="flex items-center gap-2 text-ink-muted">
                            <span>{slotPreview.pipeline} · {slotPreview.timezone} · next {slotPreview.days_ahead}d · {slotPreview.total_available} open slot{slotPreview.total_available !== 1 ? "s" : ""}</span>
                            <button onClick={runPreview} className="ml-auto"><ArrowClockwise size={11} /></button>
                        </div>
                        {slotPreview.total_available === 0 ? (
                            <div className="flex items-center gap-2 text-amber-400 bg-amber-400/10 border border-amber-400/20 rounded px-2 py-1.5">
                                <WarningCircle size={13} weight="fill" />
                                No slots found — check your schedule below and booking window in Booking & Slots.
                            </div>
                        ) : (
                            <>
                                <div>
                                    <div className="text-[10px] uppercase tracking-widest font-semibold text-brand-primary mb-1">Primary (offered first)</div>
                                    {slotPreview.primary_slots.length === 0
                                        ? <span className="text-ink-muted italic">None</span>
                                        : slotPreview.primary_slots.map((s, i) => (
                                            <div key={s.datetime} className="flex items-center gap-1.5 py-0.5">
                                                <span className="w-4 h-4 rounded-full bg-brand-primary/20 text-brand-primary flex items-center justify-center font-bold text-[9px]">{i+1}</span>
                                                <span className="text-ink">{s.label}</span>
                                                {s.appointment_link_override && (
                                                    <span className="flex items-center gap-0.5 text-brand-primary" title={`Custom link: ${s.appointment_link_override}`}>
                                                        <VideoCamera size={10} weight="fill" /> custom link
                                                    </span>
                                                )}
                                                <span className="text-ink-dim ml-auto">{s.capacity_remaining} left</span>
                                            </div>
                                        ))
                                    }
                                </div>
                                <div>
                                    <div className="text-[10px] uppercase tracking-widest font-semibold text-ink-muted mb-1">Fallback (if all primary rejected)</div>
                                    {slotPreview.fallback_slots.length === 0
                                        ? <span className="text-ink-muted italic">None beyond primary</span>
                                        : slotPreview.fallback_slots.map((s, i) => (
                                            <div key={s.datetime} className="flex items-center gap-1.5 py-0.5">
                                                <span className="w-4 h-4 rounded-full bg-surface-active border border-strokes text-ink-muted flex items-center justify-center font-bold text-[9px]">{i+1}</span>
                                                <span className="text-ink">{s.label}</span>
                                                {s.appointment_link_override && (
                                                    <span className="flex items-center gap-0.5 text-brand-primary" title={`Custom link: ${s.appointment_link_override}`}>
                                                        <VideoCamera size={10} weight="fill" /> custom link
                                                    </span>
                                                )}
                                                <span className="text-ink-dim ml-auto">{s.capacity_remaining} left</span>
                                            </div>
                                        ))
                                    }
                                </div>
                            </>
                        )}
                    </div>
                )}
            </div>

            {/* Interview slot times — start time only; duration set above */}
            <div className="space-y-2">
                {grouped.map(({ wd, rules: dayRules }) => (
                    <div key={wd} className="flex items-start gap-2">
                        <div className="w-12 pt-2 text-xs font-semibold uppercase tracking-widest text-ink-muted">{DAYS[wd]}</div>
                        <div className="flex-1 space-y-1.5">
                            {dayRules.length === 0 && (
                                <div className="text-xs text-ink-dim italic">Closed</div>
                            )}
                            {dayRules.map((r) => (
                                <div key={r._idx}>
                                <div className="flex items-center gap-2 text-xs" data-testid={`avail-rule-${r._idx}`}>
                                    <input
                                        type="time"
                                        className={`input-dark !w-24 !py-1 ${isBeforeBusinessHours(r.start) ? "!border-brand-danger/70" : ""}`}
                                        value={r.start}
                                        onChange={(e) => updateRule(r._idx, { start: e.target.value })}
                                    />
                                    {/* Unambiguous AM/PM read-back: the native time input renders
                                        12h or 24h depending on the user's OS locale, and "14:00"
                                        misread as "2:00" once put five candidates at 2:00 AM. */}
                                    <span
                                        className={`w-[3.75rem] flex-shrink-0 text-[11px] font-semibold tabular-nums ${isBeforeBusinessHours(r.start) ? "text-brand-danger" : "text-ink-muted"}`}
                                        data-testid={`avail-ampm-${r._idx}`}
                                    >
                                        {formatAmPm(r.start)}
                                    </span>
                                    <span className="text-ink-dim text-[11px] flex-shrink-0">{draft.appointment_duration_minutes} min</span>
                                    <div className="flex-1 relative min-w-0">
                                        <VideoCamera
                                            size={11}
                                            weight={r.appointment_link_override ? "fill" : "regular"}
                                            className={`absolute left-2 top-1/2 -translate-y-1/2 pointer-events-none ${r.appointment_link_override ? "text-brand-primary" : "text-ink-dim"}`}
                                        />
                                        <input
                                            type="url"
                                            className={`input-dark w-full !py-1 !pl-6 text-[11px] ${r.appointment_link_override ? "!border-brand-primary/40" : ""}`}
                                            placeholder="Uses default meeting link — paste a Zoom URL to override for this slot"
                                            title="Custom meeting link for this slot only. Every booking/confirmation/reminder for this time uses it instead of the pipeline's default link."
                                            data-testid={`avail-link-override-${r._idx}`}
                                            value={r.appointment_link_override || ""}
                                            onChange={(e) => updateRule(r._idx, { appointment_link_override: e.target.value.trim() })}
                                        />
                                    </div>
                                    <div className="w-36 relative flex-shrink-0">
                                        <User
                                            size={11}
                                            weight={r.appointment_recruiter_override ? "fill" : "regular"}
                                            className={`absolute left-2 top-1/2 -translate-y-1/2 pointer-events-none ${r.appointment_recruiter_override ? "text-brand-primary" : "text-ink-dim"}`}
                                        />
                                        <input
                                            type="text"
                                            className={`input-dark w-full !py-1 !pl-6 text-[11px] ${r.appointment_recruiter_override ? "!border-brand-primary/40" : ""}`}
                                            placeholder="Interviewer"
                                            title="Who runs this slot. Shown on the candidate's portal, confirmations and calendar invite instead of the pipeline's default interviewer name."
                                            data-testid={`avail-recruiter-override-${r._idx}`}
                                            value={r.appointment_recruiter_override || ""}
                                            onChange={(e) => updateRule(r._idx, { appointment_recruiter_override: e.target.value })}
                                        />
                                    </div>
                                    <button onClick={() => removeRule(r._idx)} data-testid={`remove-avail-${r._idx}`} className="text-brand-danger p-1 hover:bg-surface-hover rounded ml-1 flex-shrink-0">
                                        <Trash size={11} />
                                    </button>
                                </div>
                                {isBeforeBusinessHours(r.start) && (
                                    <div className="flex items-center gap-1 mt-1 text-[11px] text-brand-danger" data-testid={`avail-early-warning-${r._idx}`}>
                                        <WarningCircle size={12} weight="fill" className="flex-shrink-0" />
                                        <span>
                                            This slot is at {formatAmPm(r.start)} — earlier than 8:30 AM. Candidates would be
                                            invited and reminded in the middle of the night.
                                            {pmSuggestion(r.start) ? ` Did you mean ${pmSuggestion(r.start)}?` : ""}
                                        </span>
                                    </div>
                                )}
                                </div>
                            ))}
                            <button
                                onClick={() => addRule(wd)}
                                data-testid={`add-avail-${wd}`}
                                className="text-[11px] text-brand-primary hover:underline"
                            >
                                + Add slot
                            </button>
                        </div>
                    </div>
                ))}
            </div>

            {/* Block dates: vacations / holidays / training days */}
            <div className="mt-6 pt-5 border-t border-strokes" data-testid="blackout-section">
                <div className="flex items-start justify-between mb-3 gap-3">
                    <div>
                        <h3 className="font-heading text-sm font-semibold flex items-center gap-2">
                            <AirplaneTilt size={14} weight="duotone" className="text-brand-primary" />
                            Block dates
                            <span className="text-[10px] font-normal uppercase tracking-widest text-ink-muted ml-1">
                                Vacations, holidays, training
                            </span>
                        </h3>
                        <p className="text-[11px] text-ink-muted mt-0.5">
                            Olivia will skip these days when offering interview slots — even if your weekly hours allow them.
                        </p>
                    </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-[1fr_1fr_2fr_auto] gap-2 mb-3 items-end">
                    <div>
                        <label className="label-overline mb-1.5 block">From</label>
                        <input
                            type="date"
                            className="input-dark !py-1.5 text-xs"
                            data-testid="blackout-start"
                            value={blackoutDraft.start}
                            onChange={(e) => setBlackoutDraft((b) => ({ ...b, start: e.target.value }))}
                        />
                    </div>
                    <div>
                        <label className="label-overline mb-1.5 block">To <span className="text-ink-dim normal-case font-normal">(optional)</span></label>
                        <input
                            type="date"
                            className="input-dark !py-1.5 text-xs"
                            data-testid="blackout-end"
                            value={blackoutDraft.end}
                            onChange={(e) => setBlackoutDraft((b) => ({ ...b, end: e.target.value }))}
                            min={blackoutDraft.start || undefined}
                        />
                    </div>
                    <div>
                        <label className="label-overline mb-1.5 block">Reason <span className="text-ink-dim normal-case font-normal">(internal note)</span></label>
                        <input
                            type="text"
                            className="input-dark !py-1.5 text-xs"
                            placeholder="e.g. Christmas break, recruiter PTO"
                            data-testid="blackout-reason"
                            value={blackoutDraft.reason}
                            onChange={(e) => setBlackoutDraft((b) => ({ ...b, reason: e.target.value }))}
                        />
                    </div>
                    <button
                        type="button"
                        onClick={addBlackout}
                        data-testid="add-blackout-btn"
                        className="btn-secondary !py-1.5 !px-3 text-xs flex items-center gap-1"
                    >
                        <Plus size={11} weight="bold" /> Block
                    </button>
                </div>

                {(draft.availability_blackouts || []).length === 0 ? (
                    <div className="text-[11px] text-ink-dim italic" data-testid="blackout-empty">
                        No blocked dates. Anyone can book within your weekly hours.
                    </div>
                ) : (
                    <ul className="space-y-1.5" data-testid="blackout-list">
                        {(draft.availability_blackouts || []).map((b) => (
                            <li
                                key={b.id}
                                className="flex items-center gap-3 px-3 py-2 rounded border border-strokes bg-[#0B0B0F] text-xs"
                                data-testid={`blackout-row-${b.id}`}
                            >
                                <CalendarIcon size={12} className="text-brand-primary flex-shrink-0" />
                                <span className="font-medium tabular-nums">{_formatBlackoutRange(b)}</span>
                                {b.reason && (
                                    <span className="text-ink-muted truncate">— {b.reason}</span>
                                )}
                                <button
                                    onClick={() => removeBlackout(b.id)}
                                    data-testid={`remove-blackout-${b.id}`}
                                    className="ml-auto text-brand-danger p-1 hover:bg-surface-hover rounded"
                                    aria-label="Remove blackout"
                                >
                                    <Trash size={11} />
                                </button>
                            </li>
                        ))}
                    </ul>
                )}
            </div>

            <div className="flex items-center gap-2 mt-5">
                <button onClick={save} disabled={busy} data-testid="save-availability-btn" className="btn-primary">
                    {busy ? "Saving…" : `Save ${active.name} schedule`}
                </button>
            </div>
        </div>
    );
}
