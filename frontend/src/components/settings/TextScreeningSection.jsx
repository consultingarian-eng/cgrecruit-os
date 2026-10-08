import { useState, useEffect, useCallback } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { ChatTeardrop, Eye, Warning, CheckCircle, Clock } from "@phosphor-icons/react";

/**
 * Text Screening.
 *
 * The screening now runs over text, but every piece of it was scattered: the
 * questions live under "Screen Call Agent" (still named for the phone), the
 * assistant's instruction under "AI Stage Prompts", the booking shape under
 * "Booking & Slots", and the first message under "Applicant Comms". Nothing
 * showed what the assistant is actually told, so the only way to check the thing
 * that talks to every applicant was to read the source.
 *
 * This is that one place. It edits the fields that genuinely belong to text
 * screening and *reads* the rest, linking to wherever they really live rather
 * than duplicating them — two screens that both claim to own the question list
 * is how they end up disagreeing.
 */
export default function TextScreeningSection({ settings, onSaved, pipelineId, scopeName, readOnly }) {
    const [form, setForm] = useState({});
    const [preview, setPreview] = useState(null);
    const [loadingPreview, setLoadingPreview] = useState(false);
    const [showPrompt, setShowPrompt] = useState(false);
    const [busy, setBusy] = useState(false);
    const [reengage, setReengage] = useState(null); // null | {count, sample} | "sending" | {sent}
    const [includeArchived, setIncludeArchived] = useState(false);
    const [includeStranded, setIncludeStranded] = useState(false);

    const sca = settings?.screen_call_agent || {};
    const booking = settings?.booking_preferences || {};

    useEffect(() => {
        setForm({
            screening_mode: ["chat_first", "chat_only"].includes(sca.screening_mode)
                ? sca.screening_mode : "voice_first",
            chat_first_delay_minutes: sca.chat_first_delay_minutes ?? 120,
        });
    }, [settings]); // eslint-disable-line react-hooks/exhaustive-deps

    const loadPreview = useCallback(async () => {
        setLoadingPreview(true);
        try {
            const r = await api.get("/settings/screening-prompt-preview", {
                params: { channel: "sms", ...(pipelineId ? { pipeline_id: pipelineId } : {}) },
            });
            setPreview(r.data);
        } catch {
            toast.error("Couldn't load the preview");
        } finally {
            setLoadingPreview(false);
        }
    }, [pipelineId]);

    useEffect(() => { loadPreview(); }, [loadPreview]);

    const save = async () => {
        setBusy(true);
        try {
            // PUT /settings/screen-call-agent is a FULL REPLACE — it does
            // $set: {screen_call_agent: payload}. Sending only the two fields
            // this screen owns would reset everything else in that section to
            // model defaults: the question list, the persona, the voice, the
            // agent id. An office's ten custom questions would have become the six
            // shipped defaults, silently, on a save from this page. So send the
            // whole resolved object with our two fields laid over it.
            await api.put("/settings/screen-call-agent", { ...sca, ...form },
                { params: pipelineId ? { pipeline_id: pipelineId } : {} });
            await onSaved?.();
            await loadPreview();
            toast.success("Saved");
        } catch {
            toast.error("Failed to save");
        } finally { setBusy(false); }
    };

    const active = form.screening_mode !== "voice_first";
    const questions = preview?.questions || [];
    const gates = preview?.hard_gates ?? 0;

    return (
        <div className="space-y-6 max-w-4xl" data-testid="text-screening-section">
            <div>
                <h2 className="font-heading text-2xl font-bold tracking-tight">Text Screening</h2>
                <p className="text-sm text-ink-muted mt-1">
                    How candidates are screened by text and chat{scopeName ? ` — ${scopeName}` : ""}.
                    The questions themselves are shared with the phone agent, so editing them once
                    changes all three channels.
                </p>
            </div>

            {/* ── the switch ─────────────────────────────────────────── */}
            <div className="surface p-5 space-y-4">
                <div className="label-overline">First contact</div>
                {[
                    {
                        v: "chat_first",
                        t: "Chat first",
                        d: "Text and email the moment they apply, opening with the first screening question. The AI calls later only if they never reply.",
                    },
                    {
                        v: "voice_first",
                        t: "Call first",
                        d: "The AI calls shortly after they apply. Text is only offered if that call goes unanswered. This is the original behaviour.",
                    },
                    {
                        v: "chat_only",
                        t: "Chat only",
                        d: "No screening calls at all, unless the candidate asks to be called.",
                    },
                ].map((o) => (
                    <label
                        key={o.v}
                        className={`flex gap-3 items-start p-3 rounded-lg border cursor-pointer transition-colors ${
                            form.screening_mode === o.v
                                ? "border-brand-primary bg-brand-primary/10"
                                : "border-strokes hover:border-ink-dim"
                        }`}
                    >
                        <input
                            type="radio"
                            className="mt-1"
                            disabled={readOnly}
                            checked={form.screening_mode === o.v}
                            onChange={() => setForm((f) => ({ ...f, screening_mode: o.v }))}
                            data-testid={`mode-${o.v}`}
                        />
                        <span>
                            <span className="text-sm font-semibold text-ink">{o.t}</span>
                            <span className="block text-xs text-ink-muted mt-0.5">{o.d}</span>
                        </span>
                    </label>
                ))}

                {form.screening_mode === "chat_first" && (
                    <div className="pt-2 border-t border-strokes">
                        <label className="label-overline block mb-1.5">Wait before calling (minutes)</label>
                        <input
                            type="number"
                            min="15"
                            disabled={readOnly}
                            className="input-dark w-32"
                            value={form.chat_first_delay_minutes}
                            onChange={(e) => setForm((f) => ({ ...f, chat_first_delay_minutes: Number(e.target.value) }))}
                            data-testid="chat-first-delay"
                        />
                        <p className="text-[11px] text-ink-muted mt-1.5">
                            How long to give them to reply to the text before the AI calls. Two hours
                            is long enough to catch someone on a break, short enough that the call
                            still lands the same working day.
                        </p>
                    </div>
                )}

                {form.screening_mode !== "voice_first" && !readOnly && (
                    <div className="pt-3 border-t border-strokes" data-testid="reengage-block">
                        <div className="label-overline mb-1.5">Voice-era backlog</div>
                        <p className="text-xs text-ink-muted mb-2">
                            Candidates the phone never reached (no answer / didn&apos;t connect, call
                            attempts exhausted) can be re-approached once by text: an SMS that opens
                            with question 1 plus an email with the chat button. Their remaining calls
                            are cancelled — from then on they&apos;re text-first like everyone else.
                        </p>
                        <label className="flex items-center gap-2 text-xs text-ink-muted mb-2 cursor-pointer">
                            <input
                                type="checkbox"
                                checked={includeArchived}
                                data-testid="reengage-include-archived"
                                onChange={(e) => { setIncludeArchived(e.target.checked); setReengage(null); }}
                            />
                            Also include auto-archived candidates (call attempts exhausted, applied in the last 30 days)
                        </label>
                        <label className="flex items-center gap-2 text-xs text-ink-muted mb-2 cursor-pointer">
                            <input
                                type="checkbox"
                                checked={includeStranded}
                                data-testid="reengage-include-stranded"
                                onChange={(e) => { setIncludeStranded(e.target.checked); setReengage(null); }}
                            />
                            Also include stranded candidates (intake failed — never called or texted at all; they get the normal arrival message)
                        </label>
                        {reengage === null && (
                            <button
                                className="cube-pill px-4 py-2 text-sm"
                                data-testid="reengage-preview-btn"
                                onClick={async () => {
                                    try {
                                        const r = await api.post("/screening/reengage-by-text", { pipeline_id: pipelineId, dry_run: true, include_archived: includeArchived, include_stranded: includeStranded, max_age_days: 30 });
                                        setReengage(r.data);
                                    } catch (e) { toast.error(e?.response?.data?.detail || "Preview failed"); }
                                }}
                            >
                                Preview who&apos;d get it
                            </button>
                        )}
                        {reengage && reengage.dry_run && (
                            <div className="space-y-2">
                                <div className="text-sm text-ink">
                                    <span className="font-semibold">{reengage.count}</span> candidate{reengage.count === 1 ? "" : "s"} would receive it
                                    {(reengage.archived > 0 || reengage.stranded > 0) && (
                                        <span className="text-xs text-ink-muted"> ({reengage.active} on the board{reengage.stranded > 0 ? `, ${reengage.stranded} stranded` : ""}{reengage.archived > 0 ? `, ${reengage.archived} revived from the archive` : ""})</span>
                                    )}
                                    {reengage.sample?.length > 0 && (
                                        <span className="block text-xs text-ink-muted mt-1">{reengage.sample.join(", ")}{reengage.count > reengage.sample.length ? "…" : ""}</span>
                                    )}
                                </div>
                                {reengage.count > 0 && (
                                    <button
                                        className="btn-primary px-4 py-2 text-sm"
                                        data-testid="reengage-send-btn"
                                        onClick={async () => {
                                            setReengage("sending");
                                            try {
                                                const r = await api.post("/screening/reengage-by-text", { pipeline_id: pipelineId, dry_run: false, include_archived: includeArchived, include_stranded: includeStranded, max_age_days: 30 });
                                                setReengage({ sent: r.data.count });
                                                toast.success(`Re-engagement sent to ${r.data.count} candidate${r.data.count === 1 ? "" : "s"}`);
                                            } catch (e) {
                                                setReengage(null);
                                                toast.error(e?.response?.data?.detail || "Send failed");
                                            }
                                        }}
                                    >
                                        Send to {reengage.count} candidate{reengage.count === 1 ? "" : "s"}
                                    </button>
                                )}
                            </div>
                        )}
                        {reengage === "sending" && <div className="text-sm text-ink-muted">Sending…</div>}
                        {reengage && reengage.sent !== undefined && (
                            <div className="text-sm text-cube-cyan" data-testid="reengage-done">
                                Sent to {reengage.sent} — replies land in the normal screening flow.
                            </div>
                        )}
                    </div>
                )}

                {!readOnly && (
                    <button onClick={save} disabled={busy} className="btn-primary" data-testid="save-text-screening">
                        {busy ? "Saving…" : "Save"}
                    </button>
                )}
            </div>

            {/* ── what the candidate is actually asked ───────────────── */}
            <div className="surface p-5">
                <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
                    <div className="label-overline">The questions</div>
                    <span className="text-[11px] text-ink-dim">
                        {questions.length} question{questions.length === 1 ? "" : "s"} · {gates} hard gate{gates === 1 ? "" : "s"}
                        {" · "}edit under <span className="text-ink">Screen Call Agent</span>
                    </span>
                </div>
                {questions.length === 0 && (
                    <div className="text-sm text-ink-muted">No screening questions configured.</div>
                )}
                <ol className="space-y-1.5">
                    {questions.map((q) => (
                        <li key={q.n} className="flex gap-2 items-start text-sm" data-testid={`question-${q.n}`}>
                            <span className="text-ink-dim tabular-nums w-5 shrink-0">{q.n}.</span>
                            <span className="text-ink-muted">{q.question}</span>
                            {q.hard_gate && (
                                <span
                                    className="status-pill text-[9px] shrink-0"
                                    style={{ background: "rgba(245,158,11,0.14)", color: "#FBBF24" }}
                                    title="A disqualifying answer ends the screening"
                                >
                                    GATE
                                </span>
                            )}
                        </li>
                    ))}
                </ol>
                <p className="text-[11px] text-ink-muted mt-3 leading-relaxed">
                    A candidate who fails a gate is told politely and the screening ends. If they
                    message back correcting that answer, the assistant reads it back to them and can
                    resume — it will not reopen on a maybe.
                </p>
            </div>

            {/* ── the rest of the flow, read-only, pointing at the truth ─ */}
            <div className="surface p-5">
                <div className="label-overline mb-3">The rest of the flow</div>
                <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3 text-sm">
                    {[
                        ["First message", "Applicant Comms → First Text · Chat-First"],
                        // Named as a permission, not a path: PUT
                        // /settings/ai-stage-prompts is super-admin-only, so AI
                        // Stage Prompts is not a row a recruiter can navigate to.
                        ["Assistant's instruction", "Set by your administrator, under AI Stage Prompts"],
                        ["Slots offered", `${booking.slots_primary ?? 2} first, then ${booking.slots_fallback ?? 1} — Booking & Slots`],
                        ["Chase texts", "2h, 6h then 24h after they go quiet, never overnight"],
                    ].map(([k, v]) => (
                        <div key={k}>
                            <dt className="text-ink-dim text-[11px] uppercase tracking-wide">{k}</dt>
                            <dd className="text-ink-muted">{v}</dd>
                        </div>
                    ))}
                </dl>
            </div>

            {/* ── what the assistant is actually told ────────────────── */}
            <div className="surface p-5">
                <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
                    <div className="label-overline flex items-center gap-1.5">
                        <Eye size={13} /> The assembled prompt
                    </div>
                    <div className="flex items-center gap-2">
                        {preview && (
                            <span className="text-[11px] text-ink-dim">{preview.prompt_chars} chars</span>
                        )}
                        <button
                            onClick={() => setShowPrompt((v) => !v)}
                            className="btn-secondary !py-1 !px-2 text-xs"
                            data-testid="toggle-prompt"
                        >
                            {showPrompt ? "Hide" : "Show"}
                        </button>
                    </div>
                </div>

                {preview && !preview.active && (
                    <div
                        className="flex gap-2 items-start p-3 rounded-lg mb-3 text-sm"
                        style={{ background: "rgba(245,158,11,0.10)", color: "#FBBF24" }}
                        data-testid="inactive-warning"
                    >
                        <Warning size={16} className="mt-0.5 shrink-0" />
                        <span>
                            None of this is live for this office yet — first contact is still set to
                            <strong> Call first</strong>. The prompt below is what the assistant
                            <em> would</em> be told once you switch it on.
                        </span>
                    </div>
                )}
                {preview?.active && (
                    <div className="flex gap-2 items-center text-sm mb-3" style={{ color: "#34D399" }}>
                        <CheckCircle size={15} /> Live for this office.
                    </div>
                )}

                <p className="text-[11px] text-ink-muted mb-3 leading-relaxed">
                    Exactly what the assistant receives, assembled from the questions above, the
                    stage instruction, the booking rules and the misuse rules. Read it before
                    switching an office over — it is faster than finding out from a candidate.
                </p>

                {loadingPreview && <div className="text-sm text-ink-muted">Loading…</div>}
                {showPrompt && preview && (
                    <pre
                        className="text-[11px] leading-relaxed whitespace-pre-wrap break-words p-3 rounded-lg max-h-96 overflow-y-auto"
                        style={{ background: "var(--surface-2, #1B1B1B)", color: "var(--ink-muted, #A0A0A0)" }}
                        data-testid="prompt-preview"
                    >
                        {preview.prompt}
                    </pre>
                )}
            </div>

            {/* ── is it working ──────────────────────────────────────── */}
            <SpeedToContact pipelineId={pipelineId} />
        </div>
    );
}

/**
 * The number this whole change is judged on. Candidates are created the instant
 * they apply and messaged within seconds, so this is effectively
 * apply-to-engagement. The no-reply count sits next to the median on purpose: a
 * fast median across a handful of repliers is not a good result, and a metric
 * that improves as fewer people answer would be worse than none.
 */
function SpeedToContact({ pipelineId }) {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        setLoading(true);
        api.get("/metrics/speed-to-contact", {
            params: { days: 30, ...(pipelineId ? { pipeline_id: pipelineId } : {}) },
        })
            .then((r) => setData(r.data))
            .catch(() => setData(null))
            .finally(() => setLoading(false));
    }, [pipelineId]);

    const fmt = (secs) => {
        if (secs == null) return "—";
        if (secs < 90) return `${Math.round(secs)}s`;
        if (secs < 5400) return `${Math.round(secs / 60)} min`;
        return `${(secs / 3600).toFixed(1)} h`;
    };

    const o = data?.overall;
    return (
        <div className="surface p-5" data-testid="speed-to-contact">
            <div className="label-overline flex items-center gap-1.5 mb-3">
                <Clock size={13} /> Speed to contact · last 30 days
            </div>
            {loading && <div className="text-sm text-ink-muted">Loading…</div>}
            {!loading && (!o || !o.contacted) && (
                <div className="text-sm text-ink-muted">
                    Nothing measured yet. This starts filling in once candidates are contacted under
                    the new flow.
                </div>
            )}
            {!loading && o?.contacted > 0 && (
                <>
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                        {[
                            ["Median reply", fmt(o.median_seconds)],
                            ["Replied", `${o.replies} of ${o.contacted}`],
                            ["Within 5 min", o.under_5_min],
                            ["Never replied", o.no_reply],
                        ].map(([k, v]) => (
                            <div key={k}>
                                <div className="text-[10px] uppercase tracking-wide text-ink-dim">{k}</div>
                                <div className="text-lg font-semibold text-ink tabular-nums">{v}</div>
                            </div>
                        ))}
                    </div>
                    {data.by_reply_channel && Object.keys(data.by_reply_channel).length > 0 && (
                        <div className="mt-4 pt-3 border-t border-strokes text-xs text-ink-muted">
                            Replied on:{" "}
                            {Object.entries(data.by_reply_channel)
                                .map(([ch, n]) => `${ch} ${n}`)
                                .join(" · ")}
                        </div>
                    )}
                </>
            )}
        </div>
    );
}
