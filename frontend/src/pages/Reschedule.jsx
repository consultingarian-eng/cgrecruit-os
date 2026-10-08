import { useEffect, useState, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import axios from "axios";
import { CheckCircle, Calendar, Clock, ArrowRight } from "@phosphor-icons/react";
import { toast } from "sonner";
import CubeBg from "@/components/CubeBg";

const API = (process.env.REACT_APP_BACKEND_URL || "") + "/api";

function fmtFullDate(iso) {
    try {
        const d = new Date(iso);
        return d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
    } catch { return iso; }
}
function fmtTime(iso) {
    try {
        const d = new Date(iso);
        return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    } catch { return iso; }
}
function fmtFull(iso) {
    try {
        const d = new Date(iso);
        return d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    } catch { return iso; }
}

export default function ReschedulePage() {
    const { token } = useParams();
    const navigate = useNavigate();
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const [picking, setPicking] = useState(null);
    const [submitting, setSubmitting] = useState(false);
    const [confirmed, setConfirmed] = useState(null);
    const [watchlisting, setWatchlisting] = useState(false);
    const [onWatchlist, setOnWatchlist] = useState(false);

    const joinWatchlist = async () => {
        setWatchlisting(true);
        try {
            await axios.post(`${API}/public/reschedule/${token}/watchlist`);
            setOnWatchlist(true);
            toast.success("You're on the watchlist!");
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Couldn't add you to the watchlist.");
        } finally {
            setWatchlisting(false);
        }
    };

    useEffect(() => {
        let alive = true;
        axios.get(`${API}/public/reschedule/${token}`)
            .then((r) => { if (alive) { setData(r.data); setLoading(false); } })
            .catch((e) => {
                if (!alive) return;
                const msg = e?.response?.data?.detail || "We couldn't load your appointment.";
                setError(msg);
                setLoading(false);
            });
        return () => { alive = false; };
    }, [token]);

    // Group slots by calendar day for clean display.
    const slotsByDay = useMemo(() => {
        if (!data?.slots) return [];
        const map = new Map();
        for (const slot of data.slots) {
            const d = new Date(slot.datetime);
            if (Number.isNaN(d.getTime())) continue;
            const key = d.toDateString();
            if (!map.has(key)) map.set(key, { date: d, slots: [] });
            map.get(key).slots.push(slot);
        }
        return [...map.values()];
    }, [data]);

    const submit = async () => {
        if (!picking) return;
        setSubmitting(true);
        try {
            const r = await axios.post(`${API}/public/reschedule/${token}/book`, {
                appointment_at: picking.datetime,
            });
            setConfirmed({
                appointment_at: r.data.appointment_at,
                appointment_link: r.data.appointment_link,
            });
            toast.success("Appointment confirmed!");
        } catch (e) {
            const msg = e?.response?.data?.detail || "Couldn't book that slot. Please pick another.";
            toast.error(msg);
        } finally {
            setSubmitting(false);
        }
    };

    if (loading) {
        return <div className="cube-brand min-h-screen text-sm flex items-center justify-center" style={{ color: "var(--cube-lav)" }} data-testid="reschedule-loading">Loading your appointment…</div>;
    }
    if (error) {
        return (
            <div className="cube-brand min-h-screen flex items-center justify-center px-6" data-testid="reschedule-error">
                <CubeBg />
                <div className="cube-content max-w-md text-center">
                    <h1 className="font-heading text-2xl tracking-tight mb-3">Hmm, something's not right</h1>
                    <p className="text-sm text-cube-lav">{error}</p>
                    <button onClick={() => navigate("/")} className="btn-secondary mt-6">Go home</button>
                </div>
            </div>
        );
    }
    if (confirmed) {
        return (
            <div className="cube-brand min-h-screen flex items-center justify-center px-6" data-testid="reschedule-confirmed">
                <CubeBg />
                <div className="cube-content max-w-md w-full surface p-8 text-center">
                    <div className="w-14 h-14 rounded-full mx-auto mb-4 flex items-center justify-center" style={{ background: "rgba(34,211,238,0.12)" }}>
                        <CheckCircle size={28} weight="duotone" className="text-cube-cyan" />
                    </div>
                    <h1 className="font-heading text-2xl tracking-tight mb-2">You're all set, <span className="cube-grad">{data?.candidate?.first_name}</span>!</h1>
                    <p className="text-sm text-cube-lav mb-5">
                        Your interview with <span style={{ color: "var(--cube-cream)" }}>{data?.company?.name}</span> is confirmed.
                    </p>
                    <div className="rounded-[14px] p-4 text-left space-y-2 mb-5" style={{ border: "1.5px solid var(--cube-line)", background: "rgba(255,255,255,.03)" }}>
                        <div className="flex items-center gap-2 text-sm">
                            <Calendar size={14} className="text-cube-magenta" />
                            <span className="font-semibold">{fmtFullDate(confirmed.appointment_at)}</span>
                        </div>
                        <div className="flex items-center gap-2 text-sm">
                            <Clock size={14} className="text-cube-magenta" />
                            <span>{fmtTime(confirmed.appointment_at)} ({data?.timezone})</span>
                        </div>
                    </div>
                    <p className="text-xs text-cube-lav leading-relaxed">
                        We've sent a confirmation email and a text. You'll get reminders 1 hour before and 10 minutes before — with the join link and a few quick prep tips.
                    </p>
                </div>
            </div>
        );
    }

    return (
        <div className="cube-brand min-h-screen py-12 px-4" data-testid="reschedule-page">
            <CubeBg />
            <div className="cube-content max-w-2xl mx-auto">
                {/* Header */}
                <div className="mb-8">
                    <h1 className="font-heading text-3xl tracking-tight font-bold" data-testid="reschedule-title">
                        {data?.candidate?.previous_appointment_at
                            ? <>Pick a new time, <span className="cube-grad">{data?.candidate?.first_name}</span></>
                            : <>Pick your interview time, <span className="cube-grad">{data?.candidate?.first_name}</span></>}
                    </h1>
                    <p className="text-sm text-cube-lav mt-2 max-w-xl leading-relaxed">
                        {(() => {
                            const prev = data?.candidate?.previous_appointment_at;
                            const isPast = prev && new Date(prev) < new Date();
                            if (!prev) {
                                return <>Choose a slot below and we'll send a confirmation straight away. If none of these times suit you, tap the link at the bottom and we'll reach out in 3 days with a fresh set.</>;
                            }
                            if (isPast) {
                                return <>We missed you at your earlier slot{prev && ` (${fmtFull(prev)})`} — no worries. Pick a fresh time over the next few days that works better for you, and we'll send a confirmation right away.</>;
                            }
                            return <>Need to change your slot{prev && ` (${fmtFull(prev)})`}? No problem — pick a new time below and we'll send an updated confirmation right away.</>;
                        })()}
                    </p>
                    {data?.timezone && (
                        <div className="text-[11px] uppercase tracking-widest text-cube-dim mt-3">
                            All times shown in your local timezone · slot timezone: {data.timezone}
                        </div>
                    )}
                </div>

                {/* Slot grid */}
                {slotsByDay.length === 0 ? (
                    <div className="surface p-8 text-center" data-testid="reschedule-no-slots">
                        <Calendar size={28} weight="duotone" className="text-cube-lav mx-auto mb-3" />
                        <h3 className="font-heading text-lg mb-2">No open slots in the next 4 days</h3>
                        <p className="text-sm text-cube-lav mb-5">
                            Get on the watchlist and we'll email you again with the latest slots in 3 days.
                        </p>
                        <button
                            onClick={joinWatchlist}
                            disabled={watchlisting || onWatchlist}
                            data-testid="reschedule-watchlist-empty-btn"
                            className="btn-primary disabled:opacity-50"
                        >
                            {onWatchlist ? "On the watchlist" : (watchlisting ? "Adding…" : "Notify me when slots open")}
                        </button>
                    </div>
                ) : (
                    <>
                        <div className="space-y-5" data-testid="reschedule-slots-by-day">
                            {slotsByDay.map(({ date, slots }) => (
                                <div key={date.toISOString()} className="surface p-4">
                                    <div className="text-[10px] uppercase tracking-widest text-cube-lav font-semibold mb-3">
                                        {date.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })}
                                    </div>
                                    <div className="grid grid-cols-3 sm:grid-cols-4 gap-2">
                                        {slots.map((s) => {
                                            const selected = picking?.datetime === s.datetime;
                                            return (
                                                <button
                                                    key={s.datetime}
                                                    onClick={() => setPicking(s)}
                                                    data-testid={`slot-${s.datetime}`}
                                                    className={`cube-pill text-xs px-2.5 py-2 ${selected ? "selected" : ""}`}
                                                >
                                                    {fmtTime(s.datetime)}
                                                </button>
                                            );
                                        })}
                                    </div>
                                </div>
                            ))}
                        </div>
                        <div className="mt-5 text-center">
                            <button
                                onClick={joinWatchlist}
                                disabled={watchlisting || onWatchlist}
                                data-testid="reschedule-watchlist-btn"
                                className="text-xs text-cube-lav hover:text-cube-magenta underline-offset-2 hover:underline transition-colors disabled:text-cube-dim"
                            >
                                {onWatchlist ? "✓ On the watchlist — we'll email you in 3 days" : "None of these work? Click here to be re-notified when new slots open"}
                            </button>
                        </div>
                    </>
                )}

                {/* Confirm bar */}
                {picking && (
                    <div
                        className="fixed bottom-0 left-0 right-0 px-4 py-3 flex items-center gap-3 justify-between backdrop-blur-xl"
                        style={{ zIndex: 99999, background: "rgba(10,6,16,.9)", borderTop: "1.5px solid var(--cube-line)" }}
                        data-testid="reschedule-confirm-bar"
                    >
                        <div className="text-sm">
                            <span className="text-cube-lav">Selected:</span>{" "}
                            <span className="font-semibold">{fmtFullDate(picking.datetime)}</span>{" "}
                            <span className="text-cube-lav">at</span>{" "}
                            <span className="font-semibold">{fmtTime(picking.datetime)}</span>
                        </div>
                        <button
                            onClick={submit}
                            disabled={submitting}
                            data-testid="reschedule-confirm-btn"
                            className="btn-primary flex items-center gap-1.5"
                        >
                            {submitting ? "Booking…" : "Confirm"} <ArrowRight size={12} weight="bold" />
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
}
