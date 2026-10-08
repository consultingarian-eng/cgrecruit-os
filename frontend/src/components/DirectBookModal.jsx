import { useState, useEffect, useMemo } from "react";
import {
    Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import { CalendarPlus, ArrowRight } from "@phosphor-icons/react";
import api from "@/lib/api";
import { toast } from "sonner";
import { fmtET, DEFAULT_TZ } from "@/lib/formatET";
import { usePipeline } from "@/lib/pipeline";

// Recruiter-facing: the account's timezone, not whatever the browser is set to.
const fmtDate = (iso, tz) => fmtET(iso, { timeZone: tz, weekday: "short", month: "short", day: "numeric" });
const fmtTime = (iso, tz) => fmtET(iso, { timeZone: tz, hour: "numeric", minute: "2-digit" });

export default function DirectBookModal({ open, onClose, pipelineId, jobs = [], onBooked }) {
    const { timezone } = usePipeline() || {};
    const tz = timezone || DEFAULT_TZ;
    const [firstName, setFirstName] = useState("");
    const [lastName, setLastName]   = useState("");
    const [email, setEmail]         = useState("");
    const [phone, setPhone]         = useState("");
    const [jobId, setJobId]         = useState("");
    const [slot, setSlot]           = useState("");
    const [sendConf, setSendConf]   = useState(true);
    const [slots, setSlots]         = useState([]);
    const [loadingSlots, setLoadingSlots] = useState(false);
    const [busy, setBusy]           = useState(false);

    useEffect(() => {
        if (!open || !pipelineId) return;
        setSlot("");
        setLoadingSlots(true);
        api.get(`/pipelines/${pipelineId}/slots?days=21`)
            .then((r) => setSlots(r.data?.slots || []))
            .catch(() => setSlots([]))
            .finally(() => setLoadingSlots(false));
    }, [open, pipelineId]);

    const grouped = useMemo(() => {
        // Group on the account's calendar day. Slicing the UTC ISO string put a
        // late-afternoon ET slot on the following day's heading.
        const byDay = new Map();
        for (const s of slots) {
            const day = fmtET(s.datetime, { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" });
            if (!byDay.has(day)) byDay.set(day, []);
            byDay.get(day).push(s);
        }
        return [...byDay.values()];
    }, [slots, tz]);

    const reset = () => {
        setFirstName(""); setLastName(""); setEmail(""); setPhone("");
        setJobId(""); setSlot(""); setSendConf(true);
    };

    const handleClose = () => { reset(); onClose(); };

    const submit = async () => {
        if (!firstName.trim()) return toast.warning("First name required");
        if (!slot) return toast.warning("Pick an interview slot");
        setBusy(true);
        try {
            await api.post("/candidates/direct-book", {
                pipeline_id: pipelineId,
                first_name: firstName.trim(),
                last_name: lastName.trim(),
                email: email.trim(),
                phone: phone.trim(),
                appointment_at: slot,
                job_id: jobId || undefined,
                send_confirmation: sendConf,
            });
            toast.success(`${firstName} booked for ${fmtDate(slot, tz)} at ${fmtTime(slot, tz)}`);
            reset();
            onBooked?.();
            onClose();
        } catch (e) {
            const msg = e?.response?.data?.detail;
            if (e?.response?.status === 409 && msg) toast.warning(msg);
            else toast.error("Failed to book — try again");
        } finally {
            setBusy(false);
        }
    };

    return (
        <Dialog open={open} onOpenChange={(o) => !o && handleClose()}>
            <DialogContent className="bg-[#0E0E11] border-strokes max-w-lg">
                <DialogHeader>
                    <DialogTitle className="font-heading text-xl tracking-tight flex items-center gap-2">
                        <CalendarPlus size={18} weight="bold" className="text-brand-primary" />
                        Book Interview Directly
                    </DialogTitle>
                </DialogHeader>

                <div className="space-y-3 py-1">
                    <div className="grid grid-cols-2 gap-2">
                        <div>
                            <label className="label-overline block mb-1">First name *</label>
                            <input className="input-dark" value={firstName} onChange={(e) => setFirstName(e.target.value)} placeholder="Jane" />
                        </div>
                        <div>
                            <label className="label-overline block mb-1">Last name</label>
                            <input className="input-dark" value={lastName} onChange={(e) => setLastName(e.target.value)} placeholder="Smith" />
                        </div>
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                        <div>
                            <label className="label-overline block mb-1">Email</label>
                            <input className="input-dark" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="jane@example.com" />
                        </div>
                        <div>
                            <label className="label-overline block mb-1">Phone</label>
                            <input className="input-dark" type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+1 203 555 0100" />
                        </div>
                    </div>
                    {jobs.length > 0 && (
                        <div>
                            <label className="label-overline block mb-1">Job (optional)</label>
                            <select className="input-dark" value={jobId} onChange={(e) => setJobId(e.target.value)}>
                                <option value="">— no specific job —</option>
                                {jobs.map((j) => <option key={j.id} value={j.id}>{j.title}</option>)}
                            </select>
                        </div>
                    )}

                    <div>
                        <label className="label-overline block mb-2">Interview slot *</label>
                        {loadingSlots ? (
                            <p className="text-xs text-ink-muted">Loading slots…</p>
                        ) : grouped.length === 0 ? (
                            <p className="text-xs text-ink-muted">No slots available — check your calendar settings.</p>
                        ) : (
                            <div className="space-y-2 max-h-52 overflow-y-auto pr-1">
                                {grouped.map((daySlots) => (
                                    <div key={daySlots[0].datetime}>
                                        <div className="text-[10px] uppercase tracking-widest text-ink-muted mb-1">{fmtDate(daySlots[0].datetime, tz)}</div>
                                        <div className="flex flex-wrap gap-1.5">
                                            {daySlots.map((s) => (
                                                <button
                                                    key={s.datetime}
                                                    onClick={() => setSlot(s.datetime)}
                                                    className={`text-xs px-2.5 py-1 rounded border transition-colors ${
                                                        slot === s.datetime
                                                            ? "bg-brand-primary border-brand-primary text-white"
                                                            : "border-strokes text-ink-muted hover:border-strokes-focus hover:text-ink"
                                                    }`}
                                                >
                                                    {fmtTime(s.datetime, tz)}
                                                </button>
                                            ))}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>

                    <label className="flex items-center gap-2 cursor-pointer select-none pt-1">
                        <input type="checkbox" checked={sendConf} onChange={(e) => setSendConf(e.target.checked)} className="accent-brand-primary" />
                        <span className="text-xs text-ink-muted">Send interview confirmation email/SMS</span>
                    </label>
                </div>

                <DialogFooter>
                    <button onClick={handleClose} className="btn-secondary text-xs">Cancel</button>
                    <button onClick={submit} disabled={busy} className="btn-primary text-xs flex items-center gap-1.5 disabled:opacity-40">
                        {busy ? "Booking…" : "Book Interview"} <ArrowRight size={11} weight="bold" />
                    </button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}
