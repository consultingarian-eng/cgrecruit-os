import { Fragment, useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import axios from "axios";
import { toast } from "sonner";
import { ChatCircleText, Phone, CalendarBlank, PaperPlaneRight, CheckCircle, Sparkle } from "@phosphor-icons/react";
import CubeBg from "@/components/CubeBg";
import CubeLoader from "@/components/CubeLoader";

const API = `${process.env.REACT_APP_BACKEND_URL || ""}/api`;

/**
 * Public landing page for candidates whose phone screening fell short.
 * Two completion modes: text chat (Claude) or in-browser voice (ElevenLabs SDK).
 */
const VALID_TABS = ["chat", "callback", "schedule"];

export default function RetryPage() {
    const { token } = useParams();
    const [searchParams] = useSearchParams();
    const [session, setSession] = useState(null);
    const [error, setError] = useState("");
    // Preseed from ?tab= so the three warmup CTA links land the candidate
    // directly on the right mode (chat / instant callback / pick-a-time).
    const initialTab = searchParams.get("tab");
    const [mode, setMode] = useState(VALID_TABS.includes(initialTab) ? initialTab : "chat"); // "chat" | "callback" | "schedule"

    useEffect(() => {
        axios.get(`${API}/public/retry/${token}`)
            .then((r) => setSession(r.data))
            .catch((e) => setError(e?.response?.data?.detail || "Application not found"));
    }, [token]);

    if (error) {
        return (
            <div className="cube-brand min-h-screen flex items-center justify-center px-4">
                <CubeBg />
                <div className="cube-content surface p-8 max-w-md text-center">
                    <h1 className="font-heading text-xl mb-2">Hmm, that link looks wrong</h1>
                    <p className="text-sm text-cube-lav">{error}</p>
                </div>
            </div>
        );
    }
    if (!session) {
        return (
            <div className="cube-brand min-h-screen flex items-center justify-center">
                <CubeLoader size={72} label="Loading…" />
            </div>
        );
    }

    const c = session.candidate;
    const company = session.company;

    // Booked candidates are past screening — this page's job is done. No chat,
    // no text history: their interview lives on the status portal, and the SMS
    // thread keeps handling confirmations/reminders in its own context.
    if (c.appointment_at) {
        return (
            <div className="cube-brand min-h-screen flex items-center justify-center px-4" data-testid="retry-booked-redirect">
                <CubeBg />
                <div className="cube-content surface p-8 max-w-md text-center space-y-4">
                    <CheckCircle size={36} weight="fill" className="mx-auto text-cube-cyan" />
                    <h1 className="font-heading text-xl">You&apos;re booked in, {c.first_name}!</h1>
                    <p className="text-sm text-cube-lav">
                        Your screening is complete and your interview is scheduled. Everything
                        for the day — countdown, calendar, and what to expect — is on your status page.
                    </p>
                    <a href={`/applicant/${token}`} className="btn-primary inline-block px-6 py-3 text-sm" data-testid="retry-goto-portal">
                        View your interview details
                    </a>
                </div>
            </div>
        );
    }

    return (
        <div className="cube-brand min-h-screen flex flex-col" data-testid="retry-page">
            <CubeBg />
            <div className="cube-content flex-1 flex flex-col">
            <header className="px-6 py-4" style={{ borderBottom: "1.5px solid var(--cube-line)" }}>
                <div className="max-w-3xl mx-auto flex items-center justify-between">
                    <div>
                        <div className="cube-kicker">{company}</div>
                        <h1 className="font-heading text-xl font-semibold mt-0.5">Finish your <span className="cube-grad">screening.</span></h1>
                    </div>
                    <div className="text-[11px] text-cube-lav text-right">
                        Hi {c.first_name}!
                    </div>
                </div>
            </header>

            <main className="flex-1 max-w-3xl w-full mx-auto p-6">
                <p className="text-sm text-cube-lav leading-relaxed mb-5">
                    Let&apos;s finish your application — a few quick questions and then you&apos;ll pick your
                    interview slot. It takes about 3 minutes. Choose how you&apos;d like to do it below.
                </p>

                {/* Mode switcher */}
                <div className="grid grid-cols-3 gap-2 mb-6">
                    <button
                        onClick={() => setMode("chat")}
                        data-testid="retry-mode-chat"
                        className={`p-4 rounded-[16px] text-left transition-colors ${
                            mode === "chat" ? "bg-[rgba(236,0,140,0.08)]" : "hover:bg-[rgba(255,255,255,0.03)]"
                        }`}
                        style={{ border: `1.5px solid ${mode === "chat" ? "var(--cube-magenta)" : "var(--cube-line)"}` }}
                    >
                        <ChatCircleText size={18} weight="duotone" className="text-cube-magenta mb-1.5" />
                        <div className="font-medium text-sm">Text chat</div>
                        <div className="text-[11px] text-cube-lav mt-0.5">Type your answers — best in noisy spots</div>
                    </button>
                    <button
                        onClick={() => setMode("callback")}
                        data-testid="retry-mode-callback"
                        className={`p-4 rounded-[16px] text-left transition-colors ${
                            mode === "callback" ? "bg-[rgba(236,0,140,0.08)]" : "hover:bg-[rgba(255,255,255,0.03)]"
                        }`}
                        style={{ border: `1.5px solid ${mode === "callback" ? "var(--cube-magenta)" : "var(--cube-line)"}` }}
                    >
                        <Phone size={18} weight="duotone" className="text-cube-magenta mb-1.5" />
                        <div className="font-medium text-sm">Call me back</div>
                        <div className="text-[11px] text-cube-lav mt-0.5">We'll call your phone right now</div>
                    </button>
                    <button
                        onClick={() => setMode("schedule")}
                        data-testid="retry-mode-schedule"
                        className={`p-4 rounded-[16px] text-left transition-colors ${
                            mode === "schedule" ? "bg-[rgba(236,0,140,0.08)]" : "hover:bg-[rgba(255,255,255,0.03)]"
                        }`}
                        style={{ border: `1.5px solid ${mode === "schedule" ? "var(--cube-magenta)" : "var(--cube-line)"}` }}
                    >
                        <CalendarBlank size={18} weight="duotone" className="text-cube-magenta mb-1.5" />
                        <div className="font-medium text-sm">Pick a time</div>
                        <div className="text-[11px] text-cube-lav mt-0.5">Choose when we should call instead</div>
                    </button>
                </div>

                {mode === "chat" && <RetryChat token={token} session={session} />}
                {mode === "callback" && <RetryCallback token={token} session={session} />}
                {mode === "schedule" && <RetrySchedulePicker token={token} session={session} />}
            </main>
            </div>
        </div>
    );
}

// Office map card — shown under the commute gate question so "can you get
// here?" comes with a pin instead of a neighbourhood name. The backend sends it
// as session.office_map from the office's address / map link in
// backend/company_profile.json (null when the office has neither).

function RetryChat({ token, session }) {
    const [messages, setMessages] = useState(session.candidate.retry_chat || []);
    const [input, setInput] = useState("");
    const [busy, setBusy] = useState(false);
    const [done, setDone] = useState(false);
    const [bookedAt, setBookedAt] = useState(session.candidate.appointment_at || null);
    const scrollRef = useRef(null);
    // Belt-and-braces guard against the opening "Hi" firing twice. Without it,
    // React StrictMode in dev, fast-refresh, and the user toggling between
    // Text/Voice modes (which unmounts/remounts THIS component) would each
    // trigger a fresh kickoff — producing duplicate Olivia intros that ALSO get
    // persisted to the candidate's chat_log on the backend.
    const greetedRef = useRef(false);

    useEffect(() => {
        if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }, [messages, busy]);

    // If the chat is empty, kick it off with an opening from the AI — but ONLY ONCE.
    useEffect(() => {
        if (greetedRef.current) return;
        // Also skip if the candidate has prior retry messages (resumed session).
        if (messages.length > 0) {
            greetedRef.current = true;
            return;
        }
        if (!done && !busy) {
            greetedRef.current = true;
            send("Hi", true);
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const send = async (text, hidden = false) => {
        if (!text.trim() || busy || done) return;
        const candMsg = { role: "applicant", text, at: new Date().toISOString() };
        if (!hidden) setMessages((m) => [...m, candMsg]);
        setInput("");
        setBusy(true);
        const t0 = Date.now();
        try {
            const r = await axios.post(`${API}/public/retry/${token}/chat`, { text });
            // Pace the reveal like a person typing the reply (mirrors backend
            // reply_pacing.py): a long answer landing one second after send
            // reads as automation. The API round-trip already counts as
            // thinking time. Two turns stay snappy: the opening greeting (a
            // candidate arriving on the page shouldn't stare at a loader) and
            // yes/no taps — a one-word gate answer needs an acknowledgement,
            // not a thoughtful pause.
            const reply = r.data.reply || "";
            const isShortAnswer = text.trim().length <= 8;
            let cap = hidden ? 4000 : isShortAnswer ? 3000 : 15000;
            // A booking confirmation answers an action, not a question — the
            // candidate just picked a slot and wants the tick, not a pause.
            if (r.data.booked_at || r.data.ended) cap = Math.min(cap, 6000);
            const target = Math.min(
                cap,
                2000 + 30 * reply.length + Math.min(3000, 20 * text.length),
            );
            const wait = target - (Date.now() - t0);
            if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
            setMessages((m) => [...m, { role: "agent", text: r.data.reply, at: new Date().toISOString() }]);
            if (r.data.booked_at) {
                setBookedAt(r.data.booked_at);
                toast.success("Interview booked");
            }
            if (r.data.ended) setDone(true);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Couldn't reach the AI assistant");
        } finally { setBusy(false); }
    };

    return (
        <div className="surface flex flex-col h-[60vh]" data-testid="retry-chat">
            <div ref={scrollRef} className="flex-1 overflow-y-auto p-5 space-y-3">
                {messages.length === 0 && (
                    <div className="text-center text-cube-lav text-sm py-12 flex items-center justify-center gap-2">
                        <Sparkle size={14} className="text-cube-magenta" weight="duotone" />
                        Starting your chat…
                    </div>
                )}
                {messages.map((m, i) => {
                    const officeMap = m.role === "agent" && /commute/i.test(m.text || "")
                        ? session.office_map || null
                        : null;
                    return (
                        // Composite key: timestamp + role + index keeps each bubble stable
                        // even if the `text` is duplicated. Pure index would lose state on
                        // optimistic-update reorderings (we don't insert mid-list today
                        // but defensive for future "edit my last message" features).
                        <Fragment key={`${m.at}-${m.role}-${i}`}>
                            <div data-testid={`retry-message-${i}`} className={`flex ${m.role === "applicant" ? "justify-end" : "justify-start"}`}>
                                <div
                                    className={`max-w-[80%] px-3.5 py-2 rounded-2xl text-sm leading-relaxed whitespace-pre-wrap ${
                                        m.role === "applicant" ? "text-white" : ""
                                    }`}
                                    style={
                                        m.role === "applicant"
                                            ? { background: "var(--cube-grad)" }
                                            : { background: "rgba(255,255,255,.05)", border: "1.5px solid var(--cube-line)" }
                                    }
                                >
                                    {m.text}
                                    {m.channel === "sms" && (
                                        <span className="block text-[10px] opacity-50 mt-0.5">via text message</span>
                                    )}
                                </div>
                            </div>
                            {officeMap && (
                                <div className="flex justify-start" data-testid="office-map-card">
                                    <div className="max-w-[85%] w-full rounded-2xl overflow-hidden" style={{ border: "1.5px solid var(--cube-line)" }}>
                                        {officeMap.embed && (
                                            <iframe
                                                src={officeMap.embed}
                                                title={officeMap.label}
                                                className="w-full block"
                                                style={{ height: 220, border: 0 }}
                                                loading="lazy"
                                                referrerPolicy="no-referrer-when-downgrade"
                                            />
                                        )}
                                        <a
                                            href={officeMap.link}
                                            target="_blank"
                                            rel="noreferrer"
                                            className="block text-center text-sm py-2.5 text-cube-lav"
                                            style={{ borderTop: "1.5px solid var(--cube-line)", background: "rgba(255,255,255,.03)" }}
                                        >
                                            {officeMap.label} — open in Maps ↗
                                        </a>
                                    </div>
                                </div>
                            )}
                        </Fragment>
                    );
                })}
                {busy && (
                    <div className="flex justify-start">
                        <div className="px-3.5 py-2.5 rounded-2xl flex items-center" style={{ background: "rgba(255,255,255,.05)", border: "1.5px solid var(--cube-line)" }} data-testid="retry-typing">
                            <CubeLoader size={26} />
                        </div>
                    </div>
                )}
            </div>
            {bookedAt && (
                <div className="px-5 py-2.5 flex items-center gap-2 text-sm text-cube-cyan" style={{ borderTop: "1.5px solid var(--cube-line)", background: "rgba(34,211,238,0.06)" }} data-testid="retry-booked-banner">
                    <CheckCircle size={14} weight="fill" className="shrink-0" />
                    <span>
                        Interview booked — confirmation sent to{" "}
                        <span className="font-semibold">{session.candidate.email}</span>
                        {session.candidate.masked_phone ? (
                            <> and by text to <span className="font-semibold">{session.candidate.masked_phone}</span></>
                        ) : null}.
                    </span>
                </div>
            )}
            {/* Tap-to-answer chips — one tap beats typing on a phone, which is
                most of who lands here. Slot offers get a chip per offered time
                plus a "request other times" escape hatch; plain yes/no gate
                questions keep Yes/No. Hidden while typing so chips never get in
                the way of a real typed answer. */}
            {(() => {
                const last = messages[messages.length - 1];
                if (done || busy || bookedAt || input.trim() || !last || last.role !== "agent") return null;
                const text = last.text || "";
                if (!/\?\s*$/.test(text)) return null;
                // Slot offer: pull the offered times out of the message. Full
                // "Wednesday July 29th at 9:00 AM" phrases when present; bare
                // "10:30 AM" times when the agent lists a day's openings. Bare
                // times need an offer cue, and never fire on a range — the
                // schedule gate says "10:30 AM to 8:00 PM" and is a yes/no
                // question, not two bookable slots.
                const dayPhrases = [...text.matchAll(/(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day[^,.?!\n]*? at \d{1,2}:\d{2}\s?(?:AM|PM)/g)].map((m) => m[0]);
                const bareTimes = [...text.matchAll(/\b\d{1,2}:\d{2}\s?(?:AM|PM)\b/g)].map((m) => m[0]);
                const isRange = /\d{1,2}:\d{2}\s?(?:AM|PM)?\s*(?:to|until|–|-)\s*\d{1,2}:\d{2}/i.test(text);
                const offerCue = /\b(which|any of those|pick|choose|book|available|works? (?:best|better))\b/i.test(text);
                const slots = [...new Set(
                    dayPhrases.length ? dayPhrases
                        : (bareTimes.length >= 2 && offerCue && !isRange ? bareTimes : [])
                )].slice(0, 6);
                if (slots.length) {
                    return (
                        <div className="px-3 pt-3 flex flex-wrap gap-2" data-testid="slot-chips">
                            {slots.map((s) => (
                                <button
                                    key={s}
                                    onClick={() => send(s)}
                                    data-testid="slot-chip"
                                    className="cube-pill flex-1 basis-[45%] py-2.5 px-3 text-sm font-semibold"
                                >
                                    {s}
                                </button>
                            ))}
                            <button
                                onClick={() => send("Do you have any other times available?")}
                                data-testid="slot-chip-other"
                                className="cube-pill w-full py-2.5 px-3 text-sm"
                            >
                                Request other times
                            </button>
                        </div>
                    );
                }
                const asksYesNo = !/(what|which|when|where|how|why|tell me|describe|looking for)/i.test(text);
                if (!asksYesNo) return null;
                return (
                    <div className="px-3 pt-3 flex gap-2" data-testid="quick-replies">
                        {["Yes", "No"].map((q) => (
                            <button
                                key={q}
                                onClick={() => send(q)}
                                data-testid={`quick-reply-${q.toLowerCase()}`}
                                className="cube-pill flex-1 py-2.5 text-sm font-semibold"
                            >
                                {q}
                            </button>
                        ))}
                    </div>
                );
            })()}
            <div className="p-3 flex items-center gap-2" style={{ borderTop: "1.5px solid var(--cube-line)" }}>
                <input
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && send(input)}
                    placeholder={done ? "Conversation complete" : "Type your answer…"}
                    disabled={done || busy}
                    data-testid="retry-chat-input"
                    className="input-dark flex-1"
                />
                <button
                    onClick={() => send(input)}
                    disabled={!input.trim() || busy || done}
                    data-testid="retry-chat-send"
                    className="btn-primary !p-2"
                    aria-label="Send"
                >
                    <PaperPlaneRight size={14} weight="bold" />
                </button>
            </div>
        </div>
    );
}

function RetryCallback({ token, session }) {
    const [status, setStatus] = useState("idle"); // "idle" | "requesting" | "done" | "error"
    const maskedPhone = session.candidate.masked_phone || "your phone";

    const requestCallback = async () => {
        setStatus("requesting");
        try {
            await axios.post(`${API}/public/retry/${token}/callback`);
            setStatus("done");
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Couldn't place the call — please try again");
            setStatus("error");
        }
    };

    return (
        <div className="surface p-6 space-y-4" data-testid="retry-callback">
            <div className="flex items-start gap-3">
                <div className="w-12 h-12 rounded-full flex items-center justify-center flex-shrink-0" style={{ background: "rgba(236,0,140,0.12)", border: "1.5px solid rgba(236,0,140,0.4)" }}>
                    <Phone size={20} weight="duotone" className="text-cube-magenta" />
                </div>
                <div>
                    <div className="font-medium text-sm">Instant phone callback</div>
                    <div className="text-[11px] text-cube-lav mt-0.5 leading-relaxed">
                        {status === "done"
                            ? `Your phone should ring within 30 seconds at ${maskedPhone}. Pick up and our AI will continue your screening.`
                            : `We'll call you at ${maskedPhone} right now. Our AI will ask the remaining screening questions and book your interview at the end.`
                        }
                    </div>
                </div>
            </div>

            {status === "done" ? (
                <div className="flex items-center gap-2 text-sm text-cube-cyan">
                    <CheckCircle size={14} weight="fill" /> Call on its way — answer when your phone rings!
                </div>
            ) : (
                <button
                    onClick={requestCallback}
                    disabled={status === "requesting"}
                    data-testid="retry-callback-btn"
                    className="btn-primary inline-flex items-center gap-1.5 text-xs !py-2 !px-4"
                >
                    <Phone size={12} weight="bold" />
                    {status === "requesting" ? "Calling…" : "Call me now"}
                </button>
            )}
        </div>
    );
}

function RetrySchedulePicker({ token, session }) {
    const options = session.call_time_options || [];
    const [picked, setPicked] = useState(null); // {label, iso}
    const [status, setStatus] = useState("idle"); // "idle" | "booking" | "done"

    const confirm = async () => {
        if (!picked) return;
        setStatus("booking");
        try {
            await axios.post(`${API}/public/retry/${token}/schedule-call`, { run_at: picked.iso });
            setStatus("done");
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Couldn't schedule that time — try another");
            setStatus("idle");
        }
    };

    if (status === "done") {
        return (
            <div className="surface p-6" data-testid="retry-schedule-confirmed">
                <div className="flex items-center gap-2 text-sm text-cube-cyan">
                    <CheckCircle size={14} weight="fill" />
                    We'll call you {picked.label.toLowerCase()} — no need to do anything else.
                </div>
            </div>
        );
    }

    return (
        <div className="surface p-6 space-y-4" data-testid="retry-schedule">
            <div className="flex items-start gap-3">
                <div className="w-12 h-12 rounded-full flex items-center justify-center flex-shrink-0" style={{ background: "rgba(236,0,140,0.12)", border: "1.5px solid rgba(236,0,140,0.4)" }}>
                    <CalendarBlank size={20} weight="duotone" className="text-cube-magenta" />
                </div>
                <div>
                    <div className="font-medium text-sm">Pick a callback time</div>
                    <div className="text-[11px] text-cube-lav mt-0.5 leading-relaxed">
                        Choose a time that suits you better — we'll call then instead.
                    </div>
                </div>
            </div>

            {options.length === 0 ? (
                <div className="text-sm text-cube-lav">No times available right now — try the chat or callback options instead.</div>
            ) : (
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                    {options.map((opt) => (
                        <button
                            key={opt.iso}
                            onClick={() => setPicked(opt)}
                            data-testid={`retry-schedule-option-${opt.iso}`}
                            className={`cube-pill ${picked?.iso === opt.iso ? "selected" : ""}`}
                        >
                            {opt.label}
                        </button>
                    ))}
                </div>
            )}

            {picked && (
                <button
                    onClick={confirm}
                    disabled={status === "booking"}
                    data-testid="retry-schedule-confirm-btn"
                    className="btn-primary inline-flex items-center gap-1.5 text-xs !py-2 !px-4"
                >
                    <CalendarBlank size={12} weight="bold" />
                    {status === "booking" ? "Scheduling…" : `Confirm — ${picked.label}`}
                </button>
            )}
        </div>
    );
}
