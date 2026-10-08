import { useEffect, useMemo, useState } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { confirmDialog } from "@/components/ConfirmDialog";
import { Plus, Trash, Pencil, ArrowCounterClockwise, EnvelopeSimple, ChatCircleText } from "@phosphor-icons/react";
import { Switch } from "@/components/ui/switch";

const KEYS = [
    "warmup",
    "warmup_chat_first",
    "warmup_offhours",
    // Single template fires for ANY screening failure — consolidated in v34.5
    // from 4 separate per-failure-mode templates that were never actually wired.
    "screening_retry",
    // Stage moves
    "pencil_in", "rejection", "approval",
    "form", "form_decline", "close_success",
    // Appointment lifecycle
    "appointment_reminder_1h", "appointment_reminder_10m", "appointment_confirm_chaser",
    "appointment_no_show",
    "appointment_rescheduled",
    "slot_picker",
    "appointment_cancelled_rebook",
    "revival_followup",
];

const KEY_LABELS = {
    warmup: "Warmup",
    warmup_chat_first: "First Text · Chat-First",
    warmup_offhours: "Warmup · Off-Hours",
    screening_retry: "Screening Retry Link",
    pencil_in: "Pencil-in",
    rejection: "Rejection",
    approval: "Approval / Booked",
    form: "Form Request",
    form_decline: "Form Declined",
    close_success: "Hire Confirmation",
    appointment_reminder_1h: "Reminder · 1 hour before",
    appointment_reminder_10m: "Reminder · 10 minutes before",
    appointment_confirm_chaser: "Confirm Chaser · unconfirmed only",
    appointment_no_show: "No-Show Reschedule",
    appointment_rescheduled: "Appointment Rescheduled",
    slot_picker: "Slot Picker Link",
    appointment_cancelled_rebook: "Cancelled — Pick a New Time",
    revival_followup: "Revival Follow-Up",
};

// Plain-English description of WHEN each comm fires. Shown above the editor so
// recruiters know what they're toggling on/off and never wonder "wait, when
// does this actually go out?"
const KEY_DESCRIPTIONS = {
    warmup: "Sent automatically the moment a new applicant lands in the pipeline (manual add, application portal, or email-intake). Sets expectations and shares the next step before the AI screening call.",
    warmup_chat_first: "The very first thing a candidate receives when this office is set to Chat First — sent within seconds of them applying. The SMS is the one that matters: it opens with the first screening question so they can answer on the spot rather than deciding to click something. Keep it free of em dashes, curly quotes and emoji — one such character doubles the cost of every send. The email offers the web-chat link for anyone who would rather click than type.",
    warmup_offhours: "Same trigger as Warmup, but sent instead when a candidate arrives outside call hours (the call window in Auto-Dialer settings). Lets them know Olivia will reach out in the morning rather than implying an immediate call.",
    screening_retry: "Sent when an AI screening call fails for ANY reason — no answer, didn't connect, incomplete info, no availability. Includes the chat-or-web-call retry link so the candidate can finish online in ~3 minutes.",
    pencil_in: "Sent when you 'pencil in' a candidate (placeholder appointment). Confirms a tentative slot before the official approval.",
    rejection: "Sent when you Deny a candidate from the dashboard card or move them to Close. A polite 'thanks but no' note.",
    approval: "Sent the moment an interview slot is booked — Olivia during a successful screening call, the recruiter via Approve, the candidate via the status page, or the 2-hour delayed auto-promote. Includes the locked-in date/time and meeting link.",
    form: "Sent automatically when you mark a candidate Attended + Form OR Attended, No Form. Includes a personal link to the custom intake form.",
    form_decline: "Sent if a candidate declines the form or fails one of its disqualifying questions.",
    close_success: "Sent when you Hire a candidate (CLOSE → Training). Shares onboarding details and any next-day instructions.",
    appointment_reminder_1h: "SMS + email fired automatically before each booked appointment. Lead time defaults to 60 minutes — change below.",
    appointment_reminder_10m: "Last-minute SMS + email fired right before the appointment. Lead time defaults to 10 minutes — change below.",
    appointment_confirm_chaser: "Sent ONLY to candidates who haven't replied Y to confirm — anyone already confirmed is skipped automatically. Fires 3 hours before by default; for morning appointments it shifts to 7 PM the evening before (never sends between 9 PM and 8 AM).",
    appointment_no_show: "Sent when you mark a candidate No-Show after their appointment. Includes a one-click reschedule link valid for 4 days.",
    appointment_rescheduled: "Sent when EITHER you reschedule a candidate from their drawer (Reschedule button) OR a candidate reschedules themselves via the public status page. Confirms the new date/time + meeting link.",
    slot_picker: "Sent when an AI screening call completes but no slot was agreed on the call. Gives the candidate a self-serve link to pick their own interview time.",
    appointment_cancelled_rebook: "Sent when a candidate told us by text or email that they couldn't attend their interview and then never picked a new time — fires 48 hours after the slot they missed. Deliberately separate from Slot Picker (which congratulates them on passing screening) and No-Show Reschedule (which implies they simply didn't turn up).",
    revival_followup: "Sent once per candidate when their FIRST no-show revival call ends in voicemail or no answer. Includes the rebooking link — booking through it restores them to the board, same as booking on the call. Email only; toggle it in Settings → No-Show Revival.",
};

const LEAD_TIME_TEMPLATE_KEYS = new Set(["appointment_reminder_1h", "appointment_reminder_10m", "appointment_confirm_chaser"]);
const DEFAULT_LEAD_MINUTES = { appointment_reminder_1h: 60, appointment_reminder_10m: 10, appointment_confirm_chaser: 180 };

const newCustomReminderKey = () =>
    `custom_reminder_${Math.random().toString(36).slice(2, 8)}_${Date.now().toString(36).slice(-4)}`;

export default function ApplicantCommsSection({ settings, onSaved, pipelineId }) {
    const [defaults, setDefaults] = useState({});
    const [templates, setTemplates] = useState({});
    const [active, setActive] = useState("warmup");
    const [busy, setBusy] = useState(false);
    const [previewHtml, setPreviewHtml] = useState(null);
    const [previewLoading, setPreviewLoading] = useState(false);
    const [showAddReminder, setShowAddReminder] = useState(false);

    useEffect(() => {
        api.get("/settings/comms-defaults").then((r) => setDefaults(r.data));
        const cur = settings.applicant_comms?.templates || {};
        const init = {};
        // Built-in keys: keep their existing structure (or seed empties).
        for (const k of KEYS) {
            init[k] = cur[k] || { enabled: true, email_enabled: true, sms_enabled: true, use_custom: false, subject: "", body: "", sms_body: "", lead_minutes_before_appointment: null };
            if (init[k].enabled === undefined) init[k].enabled = true;
            // Migrate older docs: a missing per-channel flag inherits the legacy `enabled`.
            if (init[k].email_enabled === undefined) init[k].email_enabled = init[k].enabled !== false;
            if (init[k].sms_enabled === undefined) init[k].sms_enabled = init[k].enabled !== false;
        }
        // Carry over any user-defined custom reminders.
        for (const [k, v] of Object.entries(cur)) {
            if (v?.is_custom_reminder) init[k] = {
                enabled: true, email_enabled: true, sms_enabled: true, ...v,
            };
        }
        setTemplates(init);
    }, [settings]);

    // Sorted custom reminder keys (newest last) for the left list.
    const customKeys = useMemo(() =>
        Object.entries(templates)
            .filter(([, v]) => v?.is_custom_reminder)
            .sort((a, b) => (a[1].lead_minutes_before_appointment || 0) - (b[1].lead_minutes_before_appointment || 0))
            .map(([k]) => k),
    [templates]);

    const cur = templates[active] || {};
    const def = defaults[active] || {};
    const isCustomReminder = !!cur.is_custom_reminder;
    const description = isCustomReminder
        ? `Custom reminder fires ${cur.lead_minutes_before_appointment || 0} minutes before every confirmed appointment. Toggle off to pause without losing your wording.`
        : KEY_DESCRIPTIONS[active] || "";

    const updCur = (k, v) => {
        if (k === "lead_minutes_before_appointment" || k === "enabled" || k === "email_enabled" || k === "sms_enabled" || k === "label") {
            setTemplates((prev) => ({ ...prev, [active]: { ...(prev[active] || {}), [k]: v } }));
            return;
        }
        setTemplates((prev) => ({ ...prev, [active]: { ...(prev[active] || {}), use_custom: true, [k]: v } }));
    };

    const startEditing = () => {
        setTemplates((prev) => {
            const t = prev[active] || {};
            if (t.use_custom) return prev;
            return {
                ...prev,
                [active]: {
                    ...t,
                    use_custom: true,
                    subject: t.subject || def.subject || "",
                    body: t.body || def.body || "",
                    sms_body: t.sms_body || def.sms_body || "",
                },
            };
        });
    };

    const resetToDefault = () => {
        setTemplates((prev) => ({
            ...prev,
            [active]: { ...(prev[active] || {}), use_custom: false, subject: "", body: "", sms_body: "" },
        }));
        toast.success(`Reverted ${KEY_LABELS[active] || cur.label} to default`);
    };

    const addCustomReminder = ({ label, leadMinutes }) => {
        const key = newCustomReminderKey();
        const baseConfirm = templates.confirmation || {};
        setTemplates((prev) => ({
            ...prev,
            [key]: {
                enabled: true,
                use_custom: true,
                is_custom_reminder: true,
                label,
                lead_minutes_before_appointment: leadMinutes,
                subject: `Reminder · Your appointment is coming up`,
                body: baseConfirm.body || "Hi [First Name],\n\nQuick reminder that your appointment is coming up. We'll see you soon.\n\n[Company]",
                sms_body: `Hi [First Name], reminder of your appointment with [Company]. See you soon.`,
            },
        }));
        setActive(key);
        setShowAddReminder(false);
        toast.success(`Reminder "${label}" added — save to apply.`);
    };

    const deleteCustomReminder = async (key) => {
        if (!templates[key]?.is_custom_reminder) return;
        const ok = await confirmDialog({ title: `Delete the "${templates[key].label || "custom"}" reminder?`, description: "New reminders stop scheduling for upcoming appointments.", confirmLabel: "Delete", destructive: true });
        if (!ok) return;
        setTemplates((prev) => {
            const next = { ...prev };
            delete next[key];
            return next;
        });
        // Switch to a safe default so the editor doesn't render against a deleted key.
        if (active === key) setActive("warmup");
    };

    const save = async () => {
        setBusy(true);
        try {
            const params = pipelineId ? { pipeline_id: pipelineId } : {};
            // Reconcile the legacy `enabled` field with the per-channel flags so
            // that older clients reading the doc still behave correctly:
            // enabled = email_enabled OR sms_enabled.
            const out = {};
            for (const [k, v] of Object.entries(templates)) {
                const email_on = v?.email_enabled !== false;
                const sms_on = v?.sms_enabled !== false;
                out[k] = { ...v, enabled: email_on || sms_on };
            }
            await api.put("/settings/applicant-comms", { templates: out }, { params });
            await onSaved?.();
            toast.success("Communications saved");
        } catch { toast.error("Failed to save"); }
        finally { setBusy(false); }
    };

    const openPreview = async () => {
        setPreviewLoading(true);
        try {
            const r = await api.post("/templates/preview", {
                template_key: active,
                subject: cur.use_custom ? cur.subject : null,
                body: cur.use_custom ? cur.body : null,
            });
            setPreviewHtml(r.data.html);
        } catch {
            toast.error("Preview failed");
        } finally { setPreviewLoading(false); }
    };

    return (
        <div className="space-y-6 max-w-5xl" data-testid="applicant-comms-section">
            <div>
                <h2 className="font-heading text-2xl font-bold tracking-tight">Applicant Comms</h2>
                <p className="text-sm text-ink-muted mt-1">
                    Each row below is a trigger — when it fires, what it sends, and how to turn it off.
                    Stage transitions automatically dispatch the right message.
                </p>
            </div>

            <div className="grid grid-cols-12 gap-4">
                <div className="col-span-12 md:col-span-4">
                    <div className="surface p-2 space-y-0.5">
                        {KEYS.map((k) => (
                            <CommNavButton
                                key={k}
                                k={k}
                                label={KEY_LABELS[k]}
                                tpl={templates[k] || {}}
                                active={active === k}
                                onClick={() => setActive(k)}
                            />
                        ))}
                        {customKeys.length > 0 && (
                            <>
                                <div className="px-3 pt-3 pb-1 text-[9px] uppercase tracking-widest text-ink-muted font-bold">
                                    Custom reminders
                                </div>
                                {customKeys.map((k) => (
                                    <CommNavButton
                                        key={k}
                                        k={k}
                                        label={templates[k]?.label || "Custom reminder"}
                                        sub={`${templates[k]?.lead_minutes_before_appointment || 0} min before`}
                                        tpl={templates[k] || {}}
                                        active={active === k}
                                        onClick={() => setActive(k)}
                                        onDelete={() => deleteCustomReminder(k)}
                                    />
                                ))}
                            </>
                        )}
                        <button
                            onClick={() => setShowAddReminder(true)}
                            data-testid="add-custom-reminder-btn"
                            className="w-full mt-2 flex items-center justify-center gap-1.5 px-3 py-2 rounded text-xs text-brand-primary hover:bg-surface-hover border border-dashed border-brand-primary/40"
                        >
                            <Plus size={12} weight="bold" /> Schedule another reminder
                        </button>
                    </div>
                </div>

                <div className="col-span-12 md:col-span-8 surface p-5 space-y-4">
                    <div className="flex items-start justify-between gap-3">
                        <div className="flex-1 min-w-0">
                            <div className="label-overline">Trigger</div>
                            <div className="font-heading text-lg font-semibold flex items-center gap-2 flex-wrap">
                                {KEY_LABELS[active] || cur.label || "Custom reminder"}
                                {cur.use_custom && !isCustomReminder && (
                                    <span className="text-[9px] uppercase tracking-widest font-bold px-1.5 py-0.5 rounded bg-[rgba(245,158,11,0.15)] text-[#FBBF24]">
                                        CUSTOM
                                    </span>
                                )}
                                {cur.email_enabled === false && (
                                    <span className="text-[9px] uppercase tracking-widest font-bold px-1.5 py-0.5 rounded bg-[rgba(239,68,68,0.15)] text-[#F87171]" data-testid="email-off-pill">
                                        EMAIL OFF
                                    </span>
                                )}
                                {cur.sms_enabled === false && (
                                    <span className="text-[9px] uppercase tracking-widest font-bold px-1.5 py-0.5 rounded bg-[rgba(239,68,68,0.15)] text-[#F87171]" data-testid="sms-off-pill">
                                        SMS OFF
                                    </span>
                                )}
                            </div>
                            <p className="text-xs text-ink-muted leading-relaxed mt-2" data-testid="trigger-description">
                                {description}
                            </p>
                        </div>
                        <div className="flex flex-col items-stretch gap-2 min-w-[160px]">
                            <ChannelToggle
                                icon={<EnvelopeSimple size={12} weight="bold" />}
                                label="Email"
                                checked={cur.email_enabled !== false}
                                onChange={(v) => updCur("email_enabled", !!v)}
                                testid={`email-toggle-${active}`}
                            />
                            <ChannelToggle
                                icon={<ChatCircleText size={12} weight="bold" />}
                                label="SMS"
                                checked={cur.sms_enabled !== false}
                                onChange={(v) => updCur("sms_enabled", !!v)}
                                testid={`sms-toggle-${active}`}
                            />
                            {!isCustomReminder && (
                                cur.use_custom ? (
                                    <button
                                        onClick={resetToDefault}
                                        data-testid={`reset-template-${active}`}
                                        className="flex items-center justify-center gap-1 text-[11px] text-ink-muted hover:text-ink mt-1"
                                    >
                                        <ArrowCounterClockwise size={11} weight="bold" /> Reset to default
                                    </button>
                                ) : (
                                    <button
                                        onClick={startEditing}
                                        data-testid={`edit-template-${active}`}
                                        className="flex items-center justify-center gap-1 text-[11px] text-brand-primary hover:underline mt-1"
                                    >
                                        <Pencil size={11} weight="bold" /> Customize
                                    </button>
                                )
                            )}
                        </div>
                    </div>

                    {isCustomReminder && (
                        <div className="grid grid-cols-2 gap-3">
                            <div>
                                <label className="label-overline block mb-1.5">Display name</label>
                                <input
                                    data-testid={`comm-label-${active}`}
                                    className="input-dark"
                                    value={cur.label || ""}
                                    onChange={(e) => updCur("label", e.target.value)}
                                />
                            </div>
                            <div>
                                <label className="label-overline block mb-1.5">Lead time (minutes before)</label>
                                <input
                                    type="number"
                                    min={1}
                                    max={20160}
                                    data-testid={`comm-lead-minutes-${active}`}
                                    className="input-dark"
                                    value={cur.lead_minutes_before_appointment || 0}
                                    onChange={(e) => {
                                        const v = parseInt(e.target.value || "0", 10);
                                        updCur("lead_minutes_before_appointment", Number.isFinite(v) && v > 0 ? v : null);
                                    }}
                                />
                            </div>
                        </div>
                    )}

                    <fieldset disabled={cur.email_enabled === false} className={cur.email_enabled === false ? "opacity-50 pointer-events-none" : ""}>
                        <PhoneNumberMigrationBanner
                            templateKey={active}
                            currentBody={cur.body}
                            onFix={(fixedBody) => updCur("body", fixedBody)}
                            channel="email"
                        />
                        <div>
                            <label className="label-overline mb-1.5 flex items-center gap-1.5">
                                <EnvelopeSimple size={11} weight="bold" /> Email Subject
                            </label>
                            <input
                                data-testid={`comm-subject-${active}`}
                                className="input-dark"
                                placeholder={def.subject}
                                value={cur.use_custom || isCustomReminder ? (cur.subject ?? "") : (def.subject || "")}
                                onChange={(e) => updCur("subject", e.target.value)}
                                onFocus={!cur.use_custom && !isCustomReminder ? startEditing : undefined}
                            />
                        </div>
                        <div className="mt-3">
                            <label className="label-overline block mb-1.5">Email Body</label>
                            <textarea
                                data-testid={`comm-body-${active}`}
                                rows={9}
                                className="input-dark font-mono text-xs"
                                placeholder={def.body}
                                value={cur.use_custom || isCustomReminder ? (cur.body ?? "") : (def.body || "")}
                                onChange={(e) => updCur("body", e.target.value)}
                                onFocus={!cur.use_custom && !isCustomReminder ? startEditing : undefined}
                            />
                            <div className="text-[11px] text-ink-muted mt-1 leading-relaxed">
                                <strong className="text-ink">Placeholders:</strong>{" "}
                                <code>[First Name]</code>, <code>[Company]</code>, <code>[Role]</code>, <code>[Date]</code>, <code>[Time]</code>, <code>[Zoom Link]</code>, <code>[Agent Name]</code>, <code>[City]</code>
                                <div className="mt-1.5 space-y-0.5">
                                    <div><code>[Phone Number]</code> <span className="text-ink-muted/70">— candidate's phone (the person being messaged)</span></div>
                                    <div><code>[Caller ID]</code> <span className="text-ink-muted/70">— this pipeline's verified outbound caller-ID number</span></div>
                                    <div><code>[Instagram]</code> <span className="text-ink-muted/70">— your Instagram URL (set in Company &amp; Branding → Social Links)</span></div>
                                    <div><code>[Instagram Line]</code> <span className="text-ink-muted/70">— full sentence: "You can also find us on Instagram: &lt;url&gt;." (empty if not set)</span></div>
                                    <div><code>[Website]</code> <span className="text-ink-muted/70">— your website URL (set in Company &amp; Branding)</span></div>
                                </div>
                            </div>
                        </div>
                    </fieldset>

                    <fieldset disabled={cur.sms_enabled === false} className={`mt-3 ${cur.sms_enabled === false ? "opacity-50 pointer-events-none" : ""}`}>
                        <PhoneNumberMigrationBanner
                            templateKey={active}
                            currentBody={cur.sms_body}
                            onFix={(fixedBody) => updCur("sms_body", fixedBody)}
                            channel="sms"
                        />
                        <label className="label-overline mb-1.5 flex items-center gap-1.5">
                            <ChatCircleText size={11} weight="bold" /> SMS Body
                        </label>
                        <textarea
                            data-testid={`comm-sms-${active}`}
                            rows={3}
                            className="input-dark font-mono text-xs"
                            placeholder={def.sms_body}
                            value={cur.use_custom || isCustomReminder ? (cur.sms_body ?? "") : (def.sms_body || "")}
                            onChange={(e) => updCur("sms_body", e.target.value)}
                            onFocus={!cur.use_custom && !isCustomReminder ? startEditing : undefined}
                        />
                    </fieldset>

                    {LEAD_TIME_TEMPLATE_KEYS.has(active) && (
                            <div className="mt-3 border border-strokes rounded p-3 bg-[#0B0B0F]" data-testid={`lead-time-block-${active}`}>
                                <label className="label-overline block mb-1.5">Send this reminder…</label>
                                <div className="flex items-center gap-2 text-sm">
                                    <input
                                        type="number"
                                        min={1}
                                        max={1440}
                                        data-testid={`comm-lead-minutes-${active}`}
                                        className="input-dark !w-24 !py-1 text-center"
                                        value={
                                            cur.lead_minutes_before_appointment != null
                                                ? cur.lead_minutes_before_appointment
                                                : DEFAULT_LEAD_MINUTES[active] || 0
                                        }
                                        onChange={(e) => {
                                            const v = parseInt(e.target.value || "0", 10);
                                            updCur("lead_minutes_before_appointment", Number.isFinite(v) && v > 0 ? v : null);
                                        }}
                                    />
                                    <span className="text-ink-muted">minutes <strong className="text-ink">before</strong> the appointment.</span>
                                </div>
                                <div className="text-[11px] text-ink-muted mt-1.5">
                                    Default: {DEFAULT_LEAD_MINUTES[active]} minutes. Each channel can be turned off independently above.
                                </div>
                            </div>
                        )}
                </div>
            </div>

            <div className="flex items-center gap-2">
                <button onClick={save} disabled={busy} data-testid="save-comms-btn" className="btn-primary">
                    {busy ? "Saving…" : "Save All Templates"}
                </button>
                <button onClick={openPreview} disabled={previewLoading} data-testid="preview-template-btn" className="btn-secondary">
                    {previewLoading ? "Rendering…" : "Preview email →"}
                </button>
            </div>

            {showAddReminder && (
                <AddReminderModal
                    onAdd={addCustomReminder}
                    onClose={() => setShowAddReminder(false)}
                />
            )}

            {previewHtml && (
                <div
                    role="dialog"
                    onClick={() => setPreviewHtml(null)}
                    className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-start justify-center pt-8 pb-8 overflow-y-auto"
                >
                    <div onClick={(e) => e.stopPropagation()} className="bg-white rounded-xl shadow-2xl w-full max-w-2xl mx-4">
                        <div className="flex items-center justify-between px-4 py-2.5 border-b border-zinc-200 bg-zinc-50 rounded-t-xl">
                            <div className="text-xs uppercase tracking-widest font-semibold text-zinc-500">Email preview · {KEY_LABELS[active] || cur.label}</div>
                            <button onClick={() => setPreviewHtml(null)} data-testid="close-preview-btn" className="text-xs text-zinc-500 hover:text-zinc-900 px-2 py-1">
                                Close ✕
                            </button>
                        </div>
                        <iframe data-testid="preview-iframe" title="email-preview" sandbox="" srcDoc={previewHtml} className="w-full h-[700px] rounded-b-xl border-0" />
                    </div>
                </div>
            )}
        </div>
    );
}

// Templates where [Phone Number] is almost certainly meant as our outbound
// caller ID, not the candidate's phone — i.e. "we'll call you from X" style
// copy. Used by PhoneNumberMigrationBanner to show a one-click fix CTA.
const CALLER_ID_CONTEXT_TEMPLATES = new Set([
    "warmup", "no_answer", "didnt_connect", "incomplete_info", "no_availability",
    "screening_retry", "appointment_reminder_1h", "appointment_reminder_10m",
    "appointment_confirm_chaser",
    "pencil_in", "approval", "confirmation", "rebooking", "appointment_no_show",
]);

const CALLER_PHRASE_RE = /(?:we['']ll|we will|i['']?ll|we|i)\s+(?:be\s+)?(?:call|calling|reach|reaching|contact|dial|dialing|ring(?:ing)?)\s+(?:you\s+(?:back\s+)?)?(?:on|from|at)?\s*\[Phone (?:Number|Mumber)\]/i;
const SHORT_CALLER_PHRASE_RE = /(?:from|at)\s+\[Phone (?:Number|Mumber)\]/i;
// Catches the common typo "[Phone Mumber]" (m instead of n) that lingers in
// older customized templates. We always rewrite it because it never resolves
// to anything meaningful — there's no "Phone Mumber" placeholder.
const TYPO_RE = /\[Phone Mumber\]/g;

function PhoneNumberMigrationBanner({ templateKey, currentBody, onFix, channel }) {
    if (!currentBody) return null;
    const hasTypo = TYPO_RE.test(currentBody);
    TYPO_RE.lastIndex = 0;  // global regex stateful
    const isCallerContext = CALLER_ID_CONTEXT_TEMPLATES.has(templateKey);
    const hasPhoneNumberInCallerPhrase = isCallerContext && currentBody.includes("[Phone Number]") && (
        CALLER_PHRASE_RE.test(currentBody) || SHORT_CALLER_PHRASE_RE.test(currentBody)
    );
    if (!hasTypo && !hasPhoneNumberInCallerPhrase) return null;

    const fix = () => {
        let next = currentBody;
        if (hasPhoneNumberInCallerPhrase) {
            next = next.replace(CALLER_PHRASE_RE, (m) => m.replace(/\[Phone (?:Number|Mumber)\]/, "[Caller ID]"));
            next = next.replace(SHORT_CALLER_PHRASE_RE, (m) => m.replace(/\[Phone (?:Number|Mumber)\]/, "[Caller ID]"));
        }
        // Always clean up the bare typo even outside caller contexts.
        next = next.replace(TYPO_RE, "[Caller ID]");
        onFix(next);
        toast.success("Replaced with [Caller ID] — save to apply");
    };

    const headline = hasTypo
        ? "[Phone Mumber] — typo detected"
        : "[Phone Number] resolves to the candidate's phone";
    const body = hasTypo
        ? <>Looks like a typo — there's no <code className="text-ink">[Phone Mumber]</code> placeholder, so this prints literally. Click to fix.</>
        : <>Looks like you wrote "we'll call you from [Phone Number]" — that prints the candidate's own number, not your outbound caller ID. Click below to swap it for <code className="text-ink">[Caller ID]</code>.</>;

    return (
        <div
            data-testid={`phone-migration-banner-${channel}-${templateKey}`}
            className="mb-2 px-3 py-2 rounded border border-[rgba(245,158,11,0.30)] bg-[rgba(245,158,11,0.08)] flex items-start gap-2 text-xs"
        >
            <div className="text-[#FBBF24] text-base leading-none mt-0.5">⚠️</div>
            <div className="flex-1 leading-relaxed">
                <div className="text-[#FBBF24] font-semibold">{headline}</div>
                <div className="text-ink-muted">{body}</div>
            </div>
            <button
                onClick={fix}
                data-testid={`phone-migration-fix-${channel}-${templateKey}`}
                className="text-[11px] font-bold uppercase tracking-widest px-2 py-1 rounded bg-[#FBBF24] text-[#1F2937] hover:bg-[#F59E0B] flex-shrink-0"
            >
                Fix it
            </button>
        </div>
    );
}

function ChannelToggle({ icon, label, checked, onChange, testid }) {
    return (
        <div className="flex items-center justify-between gap-2 px-2 py-1.5 rounded border border-strokes bg-[#0B0B0F]">
            <div className="flex items-center gap-1.5 text-[11px] text-ink-muted font-semibold">
                {icon} {label}
            </div>
            <Switch checked={checked} onCheckedChange={onChange} data-testid={testid} />
        </div>
    );
}

function CommNavButton({ k, label, sub, tpl, active, onClick, onDelete }) {
    const t = tpl || {};
    const fullyOff = t.enabled === false;
    const emailOff = !fullyOff && t.email_enabled === false;
    const smsOff = !fullyOff && t.sms_enabled === false;
    return (
        <div className="group relative flex items-center">
            <button
                onClick={onClick}
                data-testid={`comm-tab-${k}`}
                className={`flex-1 flex items-center justify-between px-3 py-2 rounded text-sm text-left transition-colors ${
                    active ? "bg-surface-active text-ink" : "text-ink-muted hover:bg-surface-hover hover:text-ink"
                }`}
            >
                <span className="flex flex-col min-w-0">
                    <span className="truncate">{label}</span>
                    {sub && <span className="text-[10px] text-ink-muted">{sub}</span>}
                </span>
                <span className="flex items-center gap-1 flex-shrink-0">
                    {fullyOff && <span className="text-[9px] uppercase tracking-widest text-[#F87171] font-bold">OFF</span>}
                    {emailOff && <span className="text-[9px] uppercase tracking-widest text-[#F87171] font-bold">📧</span>}
                    {smsOff && <span className="text-[9px] uppercase tracking-widest text-[#F87171] font-bold">💬</span>}
                    {t.use_custom && !t.is_custom_reminder && <span className="text-[9px] uppercase tracking-widest text-brand-warning font-bold">CUSTOM</span>}
                </span>
            </button>
            {onDelete && (
                <button
                    onClick={onDelete}
                    data-testid={`delete-custom-reminder-${k}`}
                    className="absolute right-1 opacity-0 group-hover:opacity-100 p-1 rounded text-[#F87171] hover:bg-[rgba(239,68,68,0.15)]"
                    aria-label="Delete reminder"
                >
                    <Trash size={12} weight="bold" />
                </button>
            )}
        </div>
    );
}

function AddReminderModal({ onAdd, onClose }) {
    const [label, setLabel] = useState("24 hours before");
    const [leadMinutes, setLeadMinutes] = useState(1440);
    const PRESETS = [
        { label: "2 days before", minutes: 2880 },
        { label: "24 hours before", minutes: 1440 },
        { label: "4 hours before", minutes: 240 },
        { label: "30 minutes before", minutes: 30 },
    ];
    const submit = () => {
        if (!label.trim() || !leadMinutes || leadMinutes <= 0) {
            toast.error("Pick a positive lead-time and a label.");
            return;
        }
        onAdd({ label: label.trim(), leadMinutes: Number(leadMinutes) });
    };
    return (
        <div role="dialog" data-testid="add-reminder-modal" className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center" onClick={onClose}>
            <div onClick={(e) => e.stopPropagation()} className="surface p-6 w-full max-w-md mx-4 space-y-4">
                <div>
                    <h3 className="font-heading text-lg font-semibold">Schedule another reminder</h3>
                    <p className="text-xs text-ink-muted mt-1">
                        Fires once for every confirmed appointment, this many minutes before the start time.
                    </p>
                </div>
                <div>
                    <label className="label-overline block mb-1.5">Display name</label>
                    <input
                        data-testid="reminder-label-input"
                        className="input-dark"
                        value={label}
                        onChange={(e) => setLabel(e.target.value)}
                        placeholder="e.g. 24 hours before"
                    />
                </div>
                <div>
                    <label className="label-overline block mb-1.5">Lead time (minutes before)</label>
                    <input
                        type="number"
                        min={1}
                        max={20160}
                        data-testid="reminder-lead-input"
                        className="input-dark"
                        value={leadMinutes}
                        onChange={(e) => setLeadMinutes(parseInt(e.target.value || "0", 10))}
                    />
                    <div className="flex flex-wrap gap-1.5 mt-2">
                        {PRESETS.map((p) => (
                            <button
                                key={p.minutes}
                                onClick={() => { setLabel(p.label); setLeadMinutes(p.minutes); }}
                                className="text-[11px] px-2 py-1 rounded border border-strokes text-ink-muted hover:border-brand-primary/60 hover:text-brand-primary"
                            >
                                {p.label}
                            </button>
                        ))}
                    </div>
                </div>
                <div className="flex items-center justify-end gap-2">
                    <button onClick={onClose} className="text-xs text-ink-muted hover:text-ink px-3 py-1.5">Cancel</button>
                    <button onClick={submit} data-testid="reminder-submit-btn" className="btn-primary !py-1.5 !px-3 text-xs">
                        Add reminder
                    </button>
                </div>
            </div>
        </div>
    );
}
