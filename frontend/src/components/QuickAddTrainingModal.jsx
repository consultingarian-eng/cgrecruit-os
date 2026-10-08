import { useState } from "react";
import {
    Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import { GraduationCap, ArrowRight, Bell, BellSlash } from "@phosphor-icons/react";
import api from "@/lib/api";
import { toast } from "sonner";

function nextMondayDate() {
    const d = new Date();
    const day = d.getDay();
    const daysUntilMonday = day === 1 ? 7 : (8 - day) % 7 || 7;
    d.setDate(d.getDate() + daysUntilMonday);
    return d.toISOString().slice(0, 10);
}

// Generate half-hour slots 7 AM – 7 PM in 12h format.
const TIME_OPTIONS = (() => {
    const opts = [];
    for (let h = 7; h <= 19; h++) {
        for (const m of [0, 30]) {
            const hh = String(h).padStart(2, "0");
            const mm = String(m).padStart(2, "0");
            const period = h < 12 ? "AM" : "PM";
            const display_h = h % 12 === 0 ? 12 : h % 12;
            opts.push({ value: `${hh}:${mm}`, label: `${display_h}:${mm} ${period}` });
        }
    }
    return opts;
})();

function formatDateUS(iso) {
    if (!iso) return "";
    const [y, mo, d] = iso.split("-").map(Number);
    return new Date(y, mo - 1, d).toLocaleDateString("en-US", {
        weekday: "short", month: "short", day: "numeric", year: "numeric",
    });
}

export default function QuickAddTrainingModal({ open, onClose, pipelineId, jobs = [], onAdded }) {
    const [firstName, setFirstName] = useState("");
    const [lastName, setLastName]   = useState("");
    const [email, setEmail]         = useState("");
    const [phone, setPhone]         = useState("");
    const [jobId, setJobId]         = useState("");
    const [startDate, setStartDate] = useState(nextMondayDate);
    const [startTime, setStartTime] = useState("13:00");
    const [sendComms, setSendComms] = useState(true);
    const [busy, setBusy]           = useState(false);

    const reset = () => {
        setFirstName(""); setLastName(""); setEmail(""); setPhone("");
        setJobId(""); setStartDate(nextMondayDate()); setStartTime("13:00"); setSendComms(true);
    };

    const handleClose = () => { reset(); onClose(); };

    const submit = async () => {
        if (!firstName.trim()) return toast.warning("First name is required");
        if (!startDate) return toast.warning("Start date is required");

        // Combine date + time into a naive local ISO string (no tz suffix)
        // so the backend stores it as local time without UTC conversion.
        const trainingStartAt = `${startDate}T${startTime}:00`;

        setBusy(true);
        try {
            // 1. Create the candidate (lands in SCREENING by default)
            const { data: created } = await api.post("/candidates", {
                pipeline_id: pipelineId,
                first_name: firstName.trim(),
                last_name: lastName.trim(),
                email: email.trim() || undefined,
                phone: phone.trim() || undefined,
                job_id: jobId || undefined,
                skip_warmup: true,
            });

            // 2. Move straight to TRAINING, optionally suppressing comms
            await api.post(`/candidates/${created.id}/move`, {
                stage: "TRAINING",
                training_start_at: trainingStartAt,
                skip_notifications: !sendComms,
            });

            const label = `${firstName.trim()} ${lastName.trim()}`.trim();
            const dateLabel = new Date(`${startDate}T12:00:00`).toLocaleDateString(undefined, {
                weekday: "short", month: "short", day: "numeric",
            });
            toast.success(
                sendComms
                    ? `${label} added to Training — starter email & SMS sent`
                    : `${label} added to Training — no comms sent`
            );
            reset();
            onAdded?.();
            onClose();
        } catch (e) {
            const msg = e?.response?.data?.detail;
            toast.error(msg || "Failed to add — try again");
        } finally {
            setBusy(false);
        }
    };

    return (
        <Dialog open={open} onOpenChange={(o) => !o && handleClose()}>
            <DialogContent className="bg-[#0E0E11] border-strokes max-w-md">
                <DialogHeader>
                    <DialogTitle className="font-heading text-xl tracking-tight flex items-center gap-2">
                        <GraduationCap size={18} weight="bold" className="text-brand-primary" />
                        Add Directly to Training
                    </DialogTitle>
                </DialogHeader>

                <div className="space-y-3 py-1">
                    {/* Name */}
                    <div className="grid grid-cols-2 gap-2">
                        <div>
                            <label className="label-overline block mb-1">First name *</label>
                            <input
                                className="input-dark"
                                value={firstName}
                                onChange={(e) => setFirstName(e.target.value)}
                                placeholder="Jane"
                                autoFocus
                            />
                        </div>
                        <div>
                            <label className="label-overline block mb-1">Last name</label>
                            <input
                                className="input-dark"
                                value={lastName}
                                onChange={(e) => setLastName(e.target.value)}
                                placeholder="Smith"
                            />
                        </div>
                    </div>

                    {/* Contact */}
                    <div className="grid grid-cols-2 gap-2">
                        <div>
                            <label className="label-overline block mb-1">Email</label>
                            <input
                                className="input-dark"
                                type="email"
                                value={email}
                                onChange={(e) => setEmail(e.target.value)}
                                placeholder="jane@example.com"
                            />
                        </div>
                        <div>
                            <label className="label-overline block mb-1">Phone</label>
                            <input
                                className="input-dark"
                                type="tel"
                                value={phone}
                                onChange={(e) => setPhone(e.target.value)}
                                placeholder="+1 203 555 0100"
                            />
                        </div>
                    </div>

                    {/* Job */}
                    {jobs.length > 0 && (
                        <div>
                            <label className="label-overline block mb-1">Job (optional)</label>
                            <select className="input-dark" value={jobId} onChange={(e) => setJobId(e.target.value)}>
                                <option value="">— no specific job —</option>
                                {jobs.map((j) => <option key={j.id} value={j.id}>{j.title}</option>)}
                            </select>
                        </div>
                    )}

                    {/* Start date + time */}
                    <div className="grid grid-cols-2 gap-2">
                        <div>
                            <label className="label-overline block mb-1">Start date *</label>
                            <input
                                className="input-dark"
                                type="date"
                                value={startDate}
                                onChange={(e) => setStartDate(e.target.value)}
                            />
                            {startDate && (
                                <span className="text-[10px] text-ink-muted mt-1 block">{formatDateUS(startDate)}</span>
                            )}
                        </div>
                        <div>
                            <label className="label-overline block mb-1">Start time *</label>
                            <select
                                className="input-dark"
                                value={startTime}
                                onChange={(e) => setStartTime(e.target.value)}
                            >
                                {TIME_OPTIONS.map((o) => (
                                    <option key={o.value} value={o.value}>{o.label}</option>
                                ))}
                            </select>
                        </div>
                    </div>

                    {/* Comms toggle */}
                    <button
                        type="button"
                        onClick={() => setSendComms((v) => !v)}
                        className={`w-full flex items-center gap-3 px-4 py-3 rounded-lg border transition-colors text-left ${
                            sendComms
                                ? "border-brand-primary bg-[rgba(139,92,246,0.08)] text-ink"
                                : "border-strokes bg-transparent text-ink-muted"
                        }`}
                    >
                        {sendComms
                            ? <Bell size={14} weight="duotone" className="text-brand-primary flex-shrink-0" />
                            : <BellSlash size={14} weight="duotone" className="flex-shrink-0" />
                        }
                        <div>
                            <div className="text-xs font-semibold">
                                {sendComms ? "Send starter email & confirmation SMS" : "Skip all comms"}
                            </div>
                            <div className="text-[10px] text-ink-muted mt-0.5">
                                {sendComms
                                    ? "Candidate receives the start date email and 3-hour-before SMS"
                                    : "Silent add — no email, no SMS sent to this person"
                                }
                            </div>
                        </div>
                    </button>
                </div>

                <DialogFooter>
                    <button onClick={handleClose} className="btn-secondary text-xs">Cancel</button>
                    <button
                        onClick={submit}
                        disabled={busy}
                        className="btn-primary text-xs flex items-center gap-1.5 disabled:opacity-40"
                    >
                        {busy ? "Adding…" : "Add to Training"}
                        <ArrowRight size={11} weight="bold" />
                    </button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}
