import { useEffect, useState, useMemo } from "react";
import { useParams } from "react-router-dom";
import api from "@/lib/api";
import { displayPhone, formatPhoneInput, looksLikeFullNumber, phoneCountry, phoneHint, phonePlaceholder } from "@/lib/phone";
import { toast } from "sonner";
import {
    CheckCircle, Circle, Phone, Calendar, ClipboardText, GraduationCap,
    UserPlus, VideoCamera, Sparkle, ChatCircle, CalendarPlus, Clock,
    Users, ChatText, Lightning, DeviceMobile, PresentationChart, ArrowSquareOut,
} from "@phosphor-icons/react";
import CubeBg from "@/components/CubeBg";
import CubeLoader from "@/components/CubeLoader";
import BRAND from "@/lib/brand";

const LOGO = BRAND.logo;
// Optional: the recruitment deck shown during the interview — surfaced on the
// portal from FORM stage onward so a candidate can revisit it while answering
// the questionnaire and while waiting on the decision. Set
// REACT_APP_PRESENTATION_URL at build time; blank hides the section.
const PRESENTATION_URL = process.env.REACT_APP_PRESENTATION_URL || "";
const STAGE_FLOW = ["APPLICANT", "SCREENING", "APPOINTMENT", "FORM", "CLOSE", "TRAINING"];
const STAGE_LABELS = { APPLICANT: "Applied", SCREENING: "Screening", APPOINTMENT: "Interview", FORM: "Questionnaire", CLOSE: "Decision", TRAINING: "Onboarding" };
const STAGE_ICONS = {
    APPLICANT: UserPlus, SCREENING: Phone, APPOINTMENT: Calendar,
    FORM: ClipboardText, CLOSE: CheckCircle, TRAINING: GraduationCap,
};

export default function ApplicantStatusPage() {
    const { token } = useParams();
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [responses, setResponses] = useState({});
    const [bookingTime, setBookingTime] = useState("");
    const [busy, setBusy] = useState(false);
    const [formSubmitted, setFormSubmitted] = useState(false);
    const [realSlots, setRealSlots] = useState([]);
    const [slotTz, setSlotTz] = useState("");
    const [showReschedule, setShowReschedule] = useState(false);

    const refresh = async () => {
        try {
            const r = await api.get(`/public/applicant/${token}`);
            setData(r.data);
            // Load real availability if the candidate is in APPOINTMENT stage and not yet booked
            if ((r.data.candidate.stage === "APPOINTMENT" || showReschedule) && r.data.pipeline.slug) {
                try {
                    const av = await api.get(`/public/availability/${r.data.pipeline.slug}?days=10`);
                    // `slots` is the full grid. primary/fallback are the short
                    // lists the voice agent reads out — concat them as a floor
                    // so an older backend still offers something bookable.
                    setRealSlots(
                        av.data.slots
                        || [...(av.data.primary_slots || []), ...(av.data.fallback_slots || [])],
                    );
                    setSlotTz(av.data.timezone || "");
                } catch (_) { /* fall back to client-generated slots */ }
            }
        } catch (err) {
            toast.error("Application not found");
        }
    };

    useEffect(() => { refresh().finally(() => setLoading(false)); }, [token]);

    // Live polling so the applicant sees stage changes without manually refreshing
    useEffect(() => {
        const id = setInterval(refresh, 15000);
        return () => clearInterval(id);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [token, showReschedule]);

    // Only ever offer REAL availability. No client-generated fallback — a
    // candidate booking a fabricated slot that nobody is holding is worse
    // than asking them to retry.
    const slots = useMemo(
        () => (realSlots || []).map((s) => new Date(s.datetime)),
        [realSlots],
    );

    // Slots render in the candidate's own timezone, which is right for them but
    // disagrees with the confirmation email (sent in the office's zone). Say so,
    // and name the office zone by its abbreviation rather than "America/New_York".
    const tzNote = useMemo(() => {
        if (!slotTz) return "";
        try {
            const abbr = new Intl.DateTimeFormat("en-US", { timeZone: slotTz, timeZoneName: "short" })
                .formatToParts(new Date())
                .find((p) => p.type === "timeZoneName")?.value;
            return abbr ? ` — our office runs on ${abbr}` : "";
        } catch { return ""; }
    }, [slotTz]);

    const book = async () => {
        if (!bookingTime) return;
        setBusy(true);
        try {
            const path = showReschedule ? `/public/applicant/${token}/reschedule` : `/public/applicant/${token}/book`;
            const r = await api.post(path, { appointment_at: bookingTime });
            setData((d) => ({ ...d, candidate: r.data }));
            toast.success(showReschedule ? "Appointment rescheduled — check your email." : "Appointment booked! Check your email.");
            setBookingTime("");
            setShowReschedule(false);
        } catch (e) {
            // The portal now validates slots server-side, so show the reason
            // ("that slot is full") rather than a dead-end "Failed to book".
            toast.error(e?.response?.data?.detail || "Failed to book");
            refresh();
        }
        finally { setBusy(false); }
    };

    const submitForm = async () => {
        const required = (data.custom_form || []).filter((q) => q.required);
        for (const q of required) {
            if (!responses[q.id]) { toast.warning(`"${q.question}" is required`); return; }
        }
        setBusy(true);
        try {
            const r = await api.post(`/public/applicant/${token}/form`, { responses });
            setData((d) => ({ ...d, candidate: r.data }));
            setFormSubmitted(true);
            // Send them on to the company's Instagram or website (Recruiter
            // Profile → social links / website), if one is set.
            const next = data?.company?.instagram || data?.company?.website;
            if (next) {
                setTimeout(() => {
                    window.location.href = next;
                }, 4000);
            }
        } catch { toast.error("Submit failed"); }
        finally { setBusy(false); }
    };

    if (formSubmitted) return (
        <div className="cube-brand min-h-screen flex items-center justify-center p-6">
            <CubeBg />
            <div className="cube-content text-center space-y-4 max-w-sm">
                <CheckCircle size={56} weight="fill" className="text-cube-magenta mx-auto" />
                <h1 className="font-heading text-2xl font-bold">Thanks for your submission!</h1>
                <p className="text-cube-lav text-base">We'll be in touch shortly with next steps.</p>
                {(data?.company?.instagram || data?.company?.website) && (
                    <p className="text-xs text-cube-dim mt-6">Redirecting you in a moment…</p>
                )}
            </div>
        </div>
    );

    if (loading) return <div className="cube-brand min-h-screen flex items-center justify-center"><CubeLoader size={72} label="Loading…" /></div>;
    if (!data) return <div className="cube-brand min-h-screen flex items-center justify-center text-sm" style={{ color: "var(--cube-lav)" }}>Application not found</div>;

    const c = data.candidate;
    const currentIdx = STAGE_FLOW.indexOf(c.stage);
    // What this candidate was actually promised depends on their pipeline's
    // screening mode — an office can be chat-first or voice-first. Telling a voice-first candidate to "reply to our text" (no
    // text exists) sent people digging through empty inboxes a minute after
    // the apply page said "we'll call you".
    const agentName = data.screening?.agent_name || "Olivia";
    const voiceFirst = !(data.screening?.mode === "chat_first" || data.screening?.mode === "chat_only");

    return (
        <div className="cube-brand min-h-screen" data-testid="applicant-status-page">
            <CubeBg />
            <div className="cube-content">
            <header className="sticky top-0 z-30 backdrop-blur-xl px-6 py-3" style={{ background: "rgba(10,6,16,.82)", borderBottom: "1.5px solid var(--cube-line)" }}>
                <div className="max-w-4xl mx-auto flex items-center justify-between">
                    <div className="flex items-center gap-3">
                        <img src={LOGO} alt={data.company?.name || BRAND.companyName} className="w-8 h-8 object-contain" />
                        <div>
                            <div className="font-heading text-base font-bold tracking-tight">{data.company.name}</div>
                            <div className="text-[10px] uppercase tracking-widest text-cube-lav">Applicant portal</div>
                        </div>
                    </div>
                    <div className="text-xs text-cube-lav">
                        Hi <span className="font-medium" style={{ color: "var(--cube-cream)" }}>{c.first_name}</span> 👋
                    </div>
                </div>
            </header>

            <main className="max-w-4xl mx-auto px-6 py-8 space-y-6">
                {/* Progress */}
                <div className="surface p-6 sm:p-8" data-testid="status-progress">
                    <div className="label-overline mb-5">Your application journey</div>
                    <div className="grid grid-cols-3 sm:grid-cols-6 gap-y-5 gap-x-2 sm:gap-1.5">
                        {STAGE_FLOW.map((s, i) => {
                            const Icon = STAGE_ICONS[s];
                            const done = i < currentIdx;
                            const active = i === currentIdx;
                            return (
                                <div key={s} className="flex flex-col items-center gap-2" data-testid={`stage-${s}`}>
                                    <div
                                        className={`w-10 h-10 rounded-full flex items-center justify-center transition-all ${
                                            done || active ? "text-white" : "text-cube-dim"
                                        } ${active ? "ring-4 ring-[rgba(236,0,140,0.22)]" : ""}`}
                                        style={
                                            done || active
                                                ? { background: "var(--cube-grad)", boxShadow: "0 10px 24px -10px rgba(236,0,140,.6)" }
                                                : { background: "rgba(255,255,255,.04)", border: "1.5px solid var(--cube-line)" }
                                        }
                                    >
                                        {done ? <CheckCircle weight="fill" size={18} /> : <Icon weight={active ? "fill" : "regular"} size={16} />}
                                    </div>
                                    <div className={`text-[10px] sm:text-[11px] uppercase tracking-wider font-semibold text-center leading-tight ${active ? "" : "text-cube-dim"}`} style={active ? { color: "var(--cube-cream)" } : undefined}>
                                        {STAGE_LABELS[s]}
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                </div>

                {/* Active stage card */}
                {c.stage === "APPLICANT" && (
                    <StageCard icon={<Sparkle weight="fill" />} title="Application received" tone="primary">
                        <p className="text-sm leading-relaxed">
                            {voiceFirst
                                ? `Thanks for applying, ${c.first_name}. ${agentName}, our AI assistant, will give you a quick call shortly — it takes about three minutes, and you'll pick your interview time at the end.`
                                : `Thanks for applying, ${c.first_name}. We've just sent you a text with a few quick questions — reply whenever suits you and we'll take it from there. It only takes about three minutes, and you'll pick your interview time at the end.`}
                        </p>
                        <p className="text-xs text-cube-lav mt-3">
                            {voiceFirst
                                ? "Keep your phone handy — or skip the wait and answer the questions online below."
                                : "Check your messages — no rush, we'll hold your place."}
                        </p>
                        {voiceFirst && (
                            <a
                                href={`/retry/${token}`}
                                data-testid="applicant-chat-link"
                                className="btn-primary inline-flex items-center gap-2 mt-4"
                            >
                                <Sparkle size={15} weight="bold" /> Answer online instead
                            </a>
                        )}
                    </StageCard>
                )}

                {c.stage === "SCREENING" && (
                    <StageCard icon={<ChatCircle weight="fill" />} title="Let's finish your application" tone="primary">
                        <p className="text-sm leading-relaxed">
                            {voiceFirst
                                ? `A few quick questions and then you'll choose your interview slot — about three minutes in total. ${agentName} will call you, or you can finish right here:`
                                : "A few quick questions and then you'll choose your interview slot — about three minutes in total. Reply to our text, or pick up right here:"}
                        </p>
                        <a
                            href={`/retry/${token}`}
                            data-testid="continue-screening-link"
                            className="btn-primary inline-flex items-center gap-2 mt-4"
                        >
                            <ChatCircle size={15} weight="bold" /> Continue here
                        </a>
                    </StageCard>
                )}

                {c.stage === "APPOINTMENT" && (!c.appointment_at || showReschedule) && (
                    <StageCard icon={<Calendar weight="fill" />} title={showReschedule ? "Pick a new slot" : "Pick a slot for your interview"} tone="primary">
                        <p className="text-sm text-cube-lav mb-4">
                            {showReschedule
                                ? "Choose a new time below — we'll update your appointment and email you a confirmation."
                                : "Select a time that works for you. Interviews are on Zoom and last 30–45 minutes."}
                        </p>
                        {slots.length === 0 && (
                            <div className="text-center py-6 space-y-3" data-testid="no-slots-message">
                                <p className="text-sm text-cube-lav">
                                    We couldn't load available times right now.
                                </p>
                                <button
                                    onClick={() => refresh()}
                                    data-testid="retry-slots-btn"
                                    className="cube-pill px-4 py-2 text-sm"
                                >
                                    Try again
                                </button>
                            </div>
                        )}
                        <p className="text-[11px] text-cube-dim mb-2" data-testid="slot-tz-note">
                            Times are shown in your own timezone{tzNote}.
                        </p>
                        <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 max-h-64 overflow-y-auto pr-1">
                            {slots.map((s) => (
                                <button
                                    key={s.toISOString()}
                                    onClick={() => setBookingTime(s.toISOString())}
                                    data-testid={`slot-${s.toISOString()}`}
                                    className={`cube-pill text-left px-3 py-2 text-sm ${bookingTime === s.toISOString() ? "selected" : ""}`}
                                >
                                    <div className={`text-[11px] uppercase tracking-wider ${bookingTime === s.toISOString() ? "text-white/80" : "text-cube-dim"}`}>
                                        {s.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}
                                    </div>
                                    <div className="font-mono">{s.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</div>
                                </button>
                            ))}
                        </div>
                        <div className="flex gap-2 mt-4">
                            <button onClick={book} disabled={busy || !bookingTime} data-testid="book-slot-btn" className="btn-primary flex-1">
                                {busy ? (showReschedule ? "Rescheduling…" : "Booking…") : bookingTime ? (showReschedule ? "Confirm new slot" : "Confirm slot") : "Select a slot above"}
                            </button>
                            {showReschedule && (
                                <button
                                    onClick={() => { setShowReschedule(false); setBookingTime(""); }}
                                    data-testid="cancel-reschedule-btn"
                                    className="cube-pill px-4 text-sm"
                                >
                                    Cancel
                                </button>
                            )}
                        </div>
                    </StageCard>
                )}

                {c.stage === "APPOINTMENT" && c.appointment_at && !showReschedule && (
                    <InterviewPrep c={c} company={data.company} onReschedule={() => setShowReschedule(true)} />
                )}

                {c.stage === "FORM" && (
                    <StageCard icon={<ClipboardText weight="fill" />} title="One last questionnaire" tone="primary">
                        <p className="text-sm text-cube-lav mb-4">A quick 3–5 minute questionnaire. Your answers help us match you to the best team.</p>
                        <div className="space-y-4">
                            {(data.custom_form || []).map((q) => (
                                <div key={q.id} data-testid={`form-q-${q.id}`}>
                                    <label className="label-overline block mb-1.5">{q.question}{q.required && <span className="text-cube-magenta ml-1">*</span>}</label>
                                    {q.answer_type === "text" && (
                                        <textarea rows={3} className="input-dark" value={responses[q.id] || ""} onChange={(e) => setResponses({ ...responses, [q.id]: e.target.value })} />
                                    )}
                                    {q.answer_type === "date" && (
                                        <input type="date" className="input-dark" value={responses[q.id] || ""} onChange={(e) => setResponses({ ...responses, [q.id]: e.target.value })} />
                                    )}
                                    {q.answer_type === "multiple_choice" && (
                                        <select className="input-dark" value={responses[q.id] || ""} onChange={(e) => setResponses({ ...responses, [q.id]: e.target.value })}>
                                            <option value="">Choose…</option>
                                            {(q.options || []).map((o) => <option key={o} value={o}>{o}</option>)}
                                        </select>
                                    )}
                                </div>
                            ))}
                            {(data.custom_form || []).length === 0 && (
                                <div className="text-sm text-cube-lav">No questionnaire configured yet — please wait for the team to send you one.</div>
                            )}
                        </div>
                        <PhoneConfirmSection
                            c={c}
                            token={token}
                            onUpdated={(cand) => setData((d) => ({ ...d, candidate: cand }))}
                        />
                        {(data.custom_form || []).length > 0 && (
                            <button onClick={submitForm} disabled={busy} data-testid="submit-form-btn" className="btn-primary w-full mt-5">
                                {busy ? "Submitting…" : "Submit"}
                            </button>
                        )}
                    </StageCard>
                )}

                {c.stage === "CLOSE" && (
                    <StageCard icon={<CheckCircle weight="fill" />} title="Thanks for submitting!" tone="success">
                        <p className="text-sm leading-relaxed">
                            We'll contact you over the next 24 hours for your 15-minute 1-on-1 final call. Please be on the lookout for a call from{" "}
                            <span className="font-semibold">{data.callback_number || data.company?.phone || "our team"}</span>.
                        </p>
                        <PhoneConfirmSection
                            c={c}
                            token={token}
                            onUpdated={(cand) => setData((d) => ({ ...d, candidate: cand }))}
                        />
                    </StageCard>
                )}

                {PRESENTATION_URL && (c.stage === "FORM" || c.stage === "CLOSE") && (
                    <StageCard icon={<PresentationChart weight="fill" />} title="The presentation, anytime">
                        <p className="text-sm text-cube-lav mb-4">
                            Want another look at what we covered in your interview? Scroll through
                            the recruitment presentation right here, or open it full screen.
                        </p>
                        <div className="rounded-xl overflow-hidden" style={{ border: "1.5px solid var(--cube-line)" }}>
                            <iframe
                                src={PRESENTATION_URL}
                                title="Recruitment presentation"
                                className="w-full"
                                style={{ height: "60vh", border: 0 }}
                                loading="lazy"
                                data-testid="presentation-embed"
                            />
                        </div>
                        <a
                            href={PRESENTATION_URL}
                            target="_blank"
                            rel="noreferrer"
                            className="cube-pill flex items-center justify-center gap-1.5 px-4 py-2.5 text-sm mt-3"
                            data-testid="presentation-open-btn"
                        >
                            Open full screen <ArrowSquareOut size={14} weight="bold" />
                        </a>
                    </StageCard>
                )}

                {c.stage === "TRAINING" && (
                    <StageCard icon={<GraduationCap weight="fill" />} title="Welcome to the team!" tone="success">
                        <p className="text-sm leading-relaxed">
                            Excited to have you on board, {c.first_name}. Your training schedule will be sent shortly.
                        </p>
                    </StageCard>
                )}
            </main>

            <footer className="mt-12 py-6 px-6 text-xs text-cube-dim text-center space-y-2" style={{ borderTop: "1.5px solid var(--cube-line)" }}>
                {(data.company.website || data.company.instagram) && (
                    <div className="flex items-center justify-center gap-4">
                        {data.company.website && (
                            <a href={data.company.website} target="_blank" rel="noreferrer" className="text-cube-lav underline" data-testid="footer-website">
                                {data.company.website.replace(/^https?:\/\//, "")}
                            </a>
                        )}
                        {data.company.instagram && (
                            <a href={data.company.instagram} target="_blank" rel="noreferrer" className="text-cube-lav underline" data-testid="footer-instagram">
                                Instagram
                            </a>
                        )}
                    </div>
                )}
                <div>© {new Date().getFullYear()} {data.company.name}. Powered by Olivia — AI Recruiter.</div>
            </footer>
            </div>
        </div>
    );
}

/**
 * The moment the candidate has committed — booked and waiting. This is the
 * "impressive prep journey" surface: a live countdown, the join button that
 * comes alive as it gets close, one-tap add-to-calendar, what the 30 minutes
 * actually looks like, and how to show up ready. It should read like a
 * well-run company sweated the details, not like a form receipt.
 */
function InterviewPrep({ c, company, onReschedule }) {
    const when = useMemo(() => new Date(c.appointment_at), [c.appointment_at]);
    const [now, setNow] = useState(() => Date.now());
    useEffect(() => {
        const id = setInterval(() => setNow(Date.now()), 30000);
        return () => clearInterval(id);
    }, []);

    const msTo = when.getTime() - now;
    // The join link only lights up inside a 15-minute pre-window — before that
    // it is present but calm, so nobody clicks into an empty room an hour early.
    const joinLive = msTo <= 15 * 60 * 1000 && msTo > -90 * 60 * 1000;
    const countdown = countdownLabel(msTo);

    const addToCalendar = () => {
        const end = new Date(when.getTime() + 45 * 60 * 1000);
        const stamp = (d) => d.toISOString().replace(/[-:]/g, "").split(".")[0] + "Z";
        const ics = [
            "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//CGRecruit//Interview//EN",
            "BEGIN:VEVENT",
            `UID:${c.public_token || when.getTime()}@cgrecruit`,
            `DTSTAMP:${stamp(new Date(now))}`,
            `DTSTART:${stamp(when)}`,
            `DTEND:${stamp(end)}`,
            `SUMMARY:Interview with ${company?.name || BRAND.companyName}`,
            `DESCRIPTION:Your interview. Join here: ${c.appointment_link || ""}`,
            c.appointment_link ? `LOCATION:${c.appointment_link}` : "",
            "END:VEVENT", "END:VCALENDAR",
        ].filter(Boolean).join("\r\n");
        const url = "data:text/calendar;charset=utf-8," + encodeURIComponent(ics);
        const a = document.createElement("a");
        a.href = url; a.download = "interview.ics"; a.click();
    };

    return (
        <div className="space-y-4" data-testid="interview-prep">
            {/* Hero — confirmed + countdown */}
            <div
                className="rounded-[22px] p-6 sm:p-8 relative overflow-hidden"
                style={{ background: "linear-gradient(135deg, rgba(236,0,140,.10), rgba(67,83,255,.08) 60%, rgba(34,211,238,.06))", border: "1.5px solid var(--cube-line)" }}
            >
                <div className="cube-kicker mb-3"><CheckCircle weight="fill" size={13} /> You're all set</div>
                <h2 className="font-heading text-2xl sm:text-3xl font-bold leading-tight">
                    Your interview is <span className="cube-grad">confirmed</span>.
                </h2>
                <p className="text-cube-lav text-sm mt-2 mb-5">
                    {when.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })}
                    {" · "}
                    {when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", timeZoneName: "short" })}
                </p>

                <div className="flex items-center gap-3 flex-wrap">
                    <div
                        className="rounded-2xl px-5 py-3"
                        style={{ background: "rgba(255,255,255,.04)", border: "1.5px solid var(--cube-line)" }}
                        data-testid="countdown"
                    >
                        <div className="text-[10px] uppercase tracking-widest text-cube-dim">Starts in</div>
                        <div className="font-heading text-xl font-bold" style={{ color: "var(--cube-cream)" }}>{countdown}</div>
                    </div>
                    <div>
                        <div className="label-overline">You'll meet</div>
                        <div className="font-medium text-sm">{c.appointment_recruiter || "our hiring team"}</div>
                    </div>
                </div>

                <div className="flex gap-2 mt-6 flex-wrap">
                    {c.appointment_link && (
                        <a
                            href={joinLive ? c.appointment_link : undefined}
                            onClick={(e) => { if (!joinLive) e.preventDefault(); }}
                            target="_blank" rel="noreferrer"
                            data-testid="join-zoom-link"
                            aria-disabled={!joinLive}
                            className={`inline-flex items-center gap-2 rounded-full px-5 py-2.5 text-sm font-semibold ${joinLive ? "btn-primary" : ""}`}
                            style={joinLive ? undefined : { background: "rgba(255,255,255,.05)", border: "1.5px solid var(--cube-line)", color: "var(--cube-dim)", cursor: "default" }}
                            title={joinLive ? "" : "The link opens 15 minutes before your interview"}
                        >
                            <VideoCamera size={15} weight="bold" />
                            {joinLive ? "Join now" : "Join link ready 15 min before"}
                        </a>
                    )}
                    <button onClick={addToCalendar} data-testid="add-calendar-btn" className="cube-pill inline-flex items-center gap-2 px-4 py-2.5 text-sm">
                        <CalendarPlus size={15} weight="bold" /> Add to calendar
                    </button>
                    <button onClick={onReschedule} data-testid="reschedule-btn" className="cube-pill inline-flex items-center gap-2 px-4 py-2.5 text-sm">
                        <Calendar size={15} weight="bold" /> Reschedule
                    </button>
                </div>
            </div>

            {/* What to expect */}
            <div className="surface p-6">
                <div className="label-overline mb-4">What to expect</div>
                <div className="grid sm:grid-cols-3 gap-4">
                    {[
                        { icon: <Users weight="fill" />, h: "A group session", p: "You'll meet the hiring manager alongside a few other candidates, on Zoom." },
                        { icon: <Clock weight="fill" />, h: "About 30 minutes", p: "A relaxed overview of the role and the team, with time for your questions." },
                        { icon: <ChatText weight="fill" />, h: "A real conversation", p: "No trick questions — it's a chance for both sides to see if it's a fit." },
                    ].map((x) => (
                        <div key={x.h} className="rounded-2xl p-4" style={{ background: "rgba(255,255,255,.03)", border: "1.5px solid var(--cube-line)" }}>
                            <span className="text-cube-magenta">{x.icon}</span>
                            <div className="font-heading font-semibold mt-2 text-sm" style={{ color: "var(--cube-cream)" }}>{x.h}</div>
                            <p className="text-xs text-cube-lav mt-1 leading-relaxed">{x.p}</p>
                        </div>
                    ))}
                </div>
            </div>

            {/* How to prepare */}
            <div className="surface p-6">
                <div className="label-overline mb-4">Turn up ready</div>
                <ul className="space-y-2.5">
                    {[
                        [<Lightning weight="fill" key="l" />, "Find a quiet spot with a steady connection — join from a laptop if you can."],
                        [<VideoCamera weight="fill" key="v" />, "Test your camera and mic beforehand. Being seen and heard clearly goes a long way."],
                        [<DeviceMobile weight="fill" key="d" />, "Keep your phone handy in case we need to reach you."],
                        [<ChatText weight="fill" key="c" />, "Bring a question or two — curiosity lands well."],
                    ].map(([icon, text], i) => (
                        <li key={i} className="flex items-start gap-3 text-sm text-cube-lav">
                            <span className="text-cube-cyan mt-0.5 shrink-0">{icon}</span>
                            <span>{text}</span>
                        </li>
                    ))}
                </ul>
            </div>

            {/* The road ahead. Candidates arrive here from the booking email and
                SMS, which is the one link they reliably have, and until now
                nothing anywhere told them what came after the interview — the
                questionnaire and the follow-up call both landed as surprises.
                Deliberately shallow: the shape of the process and nothing about
                how anyone is assessed. */}
            <div className="surface p-6">
                <div className="label-overline mb-2">After the interview</div>
                <p className="text-sm text-cube-lav mb-5 leading-relaxed">
                    If it goes well on both sides, here's the rest of the process.
                </p>
                <ol className="space-y-4">
                    {[
                        { icon: <ClipboardText weight="fill" />, h: "A short questionnaire", p: "A few questions about you and the role, sent over straight after." },
                        { icon: <ChatCircle weight="fill" />, h: "A one-to-one call", p: "Around 15 minutes with a manager to talk it through and answer your questions." },
                        { icon: <GraduationCap weight="fill" />, h: "Your start date", p: "We confirm your first day and send everything you need to bring along." },
                    ].map((x, i) => (
                        <li key={x.h} className="flex items-start gap-4">
                            <span
                                className="shrink-0 grid place-items-center rounded-full font-heading font-semibold text-xs"
                                style={{
                                    width: 26, height: 26, color: "var(--cube-cream)",
                                    background: "rgba(255,255,255,.04)",
                                    border: "1.5px solid var(--cube-line)",
                                }}
                            >
                                {i + 1}
                            </span>
                            <div className="min-w-0">
                                <div className="font-heading font-semibold text-sm flex items-center gap-2" style={{ color: "var(--cube-cream)" }}>
                                    <span className="text-cube-magenta">{x.icon}</span>
                                    {x.h}
                                </div>
                                <p className="text-xs text-cube-lav mt-1 leading-relaxed">{x.p}</p>
                            </div>
                        </li>
                    ))}
                </ol>
                <p className="text-xs text-cube-lav mt-5 leading-relaxed" style={{ opacity: .75 }}>
                    We'll be in touch by email and text at each step, so you'll always know where you are.
                </p>
            </div>
        </div>
    );
}

/**
 * "We'll call you for the 1-on-1 on this number — is it right?" Recruiters
 * were dialling numbers that never picked up; the candidate is the only one
 * who knows which number actually reaches them, so ask while we have their
 * attention on the questionnaire. Confirm is one tap; correcting updates the
 * candidate's card on the board immediately.
 */
function PhoneConfirmSection({ c, token, onUpdated }) {
    const [editing, setEditing] = useState(!c.phone);
    const [draft, setDraft] = useState("");
    const [busy, setBusy] = useState(false);
    const confirmed = !!c.phone_confirmed_at;

    const post = async (phone, okMsg) => {
        setBusy(true);
        try {
            const r = await api.post(`/public/applicant/${token}/phone`, { phone });
            onUpdated(r.data);
            setEditing(false);
            setDraft("");
            toast.success(okMsg);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Couldn't save that number — please try again.");
        } finally { setBusy(false); }
    };

    const country = phoneCountry(c.phone_country);
    const saveDraft = () => {
        if (!looksLikeFullNumber(draft, country)) {
            toast.warning(phoneHint(country));
            return;
        }
        post(draft, "Number updated — we'll call you there.");
    };

    return (
        <div
            className="rounded-2xl p-4 mt-5"
            style={{ background: "rgba(255,255,255,.04)", border: "1.5px solid var(--cube-line)" }}
            data-testid="phone-confirm-section"
        >
            <div className="flex items-center gap-2 mb-2">
                <DeviceMobile weight="fill" size={16} className="text-cube-magenta" />
                <span className="label-overline">Your 1-on-1 call</span>
            </div>
            {!editing && (
                <>
                    <p className="text-sm text-cube-lav leading-relaxed">
                        We'll contact you for the 1-on-1 portion of the recruitment process on{" "}
                        <span className="font-semibold" style={{ color: "var(--cube-cream)" }} data-testid="phone-on-file">
                            {displayPhone(c.phone, country)}
                        </span>.
                    </p>
                    {confirmed ? (
                        <p className="text-xs mt-2 flex items-center gap-1.5" style={{ color: "#22d3ee" }} data-testid="phone-confirmed-note">
                            <CheckCircle weight="fill" size={14} /> Number confirmed — we'll call you there.
                        </p>
                    ) : (
                        <p className="text-xs text-cube-dim mt-2">
                            Make sure this is a number you actually answer.
                        </p>
                    )}
                    <div className="flex gap-2 mt-3 flex-wrap">
                        {!confirmed && (
                            <button
                                onClick={() => post(c.phone, "Number confirmed — we'll call you there.")}
                                disabled={busy}
                                data-testid="confirm-phone-btn"
                                className="btn-primary px-4 py-2 text-sm"
                            >
                                {busy ? "Saving…" : "Yes, that's my number"}
                            </button>
                        )}
                        <button
                            onClick={() => { setEditing(true); setDraft(""); }}
                            disabled={busy}
                            data-testid="change-phone-btn"
                            className="cube-pill px-4 py-2 text-sm"
                        >
                            Use a different number
                        </button>
                    </div>
                </>
            )}
            {editing && (
                <>
                    <p className="text-sm text-cube-lav leading-relaxed mb-3">
                        {c.phone
                            ? "Enter the best number to reach you for the 1-on-1:"
                            : "We don't have a number on file — add the best one to reach you for the 1-on-1:"}
                    </p>
                    <input
                        type="tel"
                        inputMode="tel"
                        autoComplete="tel"
                        placeholder={phonePlaceholder(country)}
                        className="input-dark"
                        value={draft}
                        onChange={(e) => setDraft(formatPhoneInput(e.target.value, country))}
                        data-testid="phone-input"
                    />
                    <div className="flex gap-2 mt-3">
                        <button onClick={saveDraft} disabled={busy} data-testid="save-phone-btn" className="btn-primary px-4 py-2 text-sm">
                            {busy ? "Saving…" : "Save number"}
                        </button>
                        {c.phone && (
                            <button
                                onClick={() => { setEditing(false); setDraft(""); }}
                                disabled={busy}
                                data-testid="cancel-phone-btn"
                                className="cube-pill px-4 py-2 text-sm"
                            >
                                Cancel
                            </button>
                        )}
                    </div>
                </>
            )}
        </div>
    );
}

function countdownLabel(ms) {
    if (ms <= -90 * 60 * 1000) return "In progress";
    if (ms <= 0) return "Starting now";
    const mins = Math.floor(ms / 60000);
    if (mins < 60) return `${mins} min`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ${mins % 60}m`;
    const days = Math.floor(hrs / 24);
    return days === 1 ? "1 day" : `${days} days`;
}

function StageCard({ icon, title, tone, children }) {
    const colors = {
        primary: { bg: "rgba(236,0,140,0.07)", border: "rgba(236,0,140,0.35)", text: "#ff4da6" },
        success: { bg: "rgba(34,211,238,0.06)", border: "rgba(34,211,238,0.35)", text: "#22d3ee" },
        warning: { bg: "rgba(245,158,11,0.08)", border: "rgba(245,158,11,0.3)", text: "#FBBF24" },
    };
    const c = colors[tone] || colors.primary;
    return (
        <div className="rounded-[20px] p-6" style={{ background: c.bg, border: `1.5px solid ${c.border}` }} data-testid={`stage-card-${title.toLowerCase().replace(/\s+/g, "-")}`}>
            <div className="flex items-center gap-2.5 mb-3">
                <span style={{ color: c.text }}>{icon}</span>
                <h3 className="font-heading text-xl font-semibold" style={{ color: c.text }}>{title}</h3>
            </div>
            {children}
        </div>
    );
}
