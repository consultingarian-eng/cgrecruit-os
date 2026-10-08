import { useEffect, useState } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import api from "@/lib/api";
import { phonePlaceholder } from "@/lib/phone";
import { toast } from "sonner";
import { Briefcase, MapPin, Clock, CheckCircle, Sparkle } from "@phosphor-icons/react";
import CubeBg from "@/components/CubeBg";
import BRAND from "@/lib/brand";

const LOGO = BRAND.logo;

export default function ApplyPage() {
    const { slug } = useParams();
    const navigate = useNavigate();
    // Embed mode (?embed=1) — page is rendering inside the cross-site widget
    // iframe (see /api/widget.js). We hide the marketing-style left column,
    // skip the header, and tighten padding so the form fits a 560×720 modal.
    const [searchParams] = useSearchParams();
    const isEmbed = searchParams.get("embed") === "1";
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [selectedJobId, setSelectedJobId] = useState("");
    const [busy, setBusy] = useState(false);
    const [form, setForm] = useState({ first_name: "", last_name: "", email: "", phone: "" });

    useEffect(() => {
        api.get(`/public/pipelines/${slug}`)
            .then((r) => {
                setData(r.data);
                if (r.data.jobs?.[0]) setSelectedJobId(r.data.jobs[0].id);
            })
            .catch(() => toast.error("Pipeline not found"))
            .finally(() => setLoading(false));
    }, [slug]);

    const submit = async (e) => {
        e.preventDefault();
        if (!selectedJobId) return toast.warning("Please select a role");
        setBusy(true);
        try {
            const r = await api.post("/public/apply", {
                pipeline_id: data.pipeline.id,
                job_id: selectedJobId,
                ...form,
            });
            // A repeat application gets no portal link back (the server never
            // hands out an existing applicant's token); they already have it.
            if (r.data.duplicate || !r.data.public_token) {
                toast.success(r.data.message || "You've already applied. Check your texts and email from us for your personal link.");
                return;
            }
            // In embed mode keep the navigation INSIDE the iframe so the host
            // page experience stays put. The /applicant/{token} page reads
            // ?embed=1 the same way.
            const target = `/applicant/${r.data.public_token}${isEmbed ? "?embed=1" : ""}`;
            navigate(target);
        } catch (err) {
            toast.error(err?.response?.data?.detail || "Failed to submit");
        } finally { setBusy(false); }
    };

    if (loading) return <div className="cube-brand min-h-screen flex items-center justify-center text-sm" style={{ color: "var(--cube-lav)" }}>Loading…</div>;
    if (!data) return <div className="cube-brand min-h-screen flex items-center justify-center text-sm" style={{ color: "var(--cube-lav)" }}>Pipeline not found</div>;

    const selectedJob = data.jobs.find((j) => j.id === selectedJobId);

    // What happens after Submit depends on the pipeline's screening mode —
    // an office can be chat-first or voice-first — and the copy must promise
    // the touch that actually comes next.
    const agentName = data.screening?.agent_name || "Olivia";
    const chatFirst = data.screening?.mode === "chat_first" || data.screening?.mode === "chat_only";
    const nextStepLine = chatFirst
        ? `Apply in under 2 minutes — we'll text you a few quick questions right after you submit.`
        : `Apply in under 2 minutes — our AI assistant ${agentName} will give you a quick screening call to learn more about you.`;
    const afterSubmitLine = chatFirst
        ? "Takes about 60 seconds. We'll text you next."
        : "Takes about 60 seconds. We'll call you next.";
    const consentLine = chatFirst
        ? `By applying, you consent to receive screening texts and a follow-up call from our AI assistant ${agentName}.`
        : `By applying, you consent to receive a brief screening call and texts from our AI assistant ${agentName}.`;

    // Embed mode: compact single-column layout that fits the widget modal.
    // Skips the marketing left column (job descriptions etc.) — the host
    // careers page already sold them on the role; we just need the form.
    if (isEmbed) {
        return (
            <div className="cube-brand min-h-screen p-5" data-testid="apply-page-embed">
                <div className="flex items-center gap-2.5 mb-5">
                    <img src={LOGO} alt="Logo" className="w-7 h-7 object-contain" />
                    <div>
                        <div className="font-heading text-base font-bold tracking-tight">{data.company.name}</div>
                        <div className="text-[10px] uppercase tracking-widest text-cube-lav">Apply · {data.pipeline.name}</div>
                    </div>
                </div>
                {data.jobs.length > 1 && (
                    <div className="space-y-2 mb-4">
                        <div className="label-overline">Select a role</div>
                        {data.jobs.map((j) => (
                            <button
                                key={j.id}
                                onClick={() => setSelectedJobId(j.id)}
                                data-testid={`apply-embed-job-${j.id}`}
                                className={`w-full text-left surface p-2.5 transition-all ${selectedJobId === j.id ? "!border-cube-magenta ring-1 ring-cube-magenta" : ""}`}
                            >
                                <div className="flex items-center justify-between">
                                    <div className="font-medium text-sm">{j.title}</div>
                                    {selectedJobId === j.id && <CheckCircle weight="fill" size={16} className="text-cube-magenta" />}
                                </div>
                            </button>
                        ))}
                    </div>
                )}
                <form onSubmit={submit} className="space-y-3">
                    <div className="grid grid-cols-2 gap-2.5">
                        <div>
                            <label className="label-overline block mb-1">First name *</label>
                            <input data-testid="apply-first-name" required className="input-dark" value={form.first_name} onChange={(e) => setForm({ ...form, first_name: e.target.value })} />
                        </div>
                        <div>
                            <label className="label-overline block mb-1">Last name</label>
                            <input data-testid="apply-last-name" className="input-dark" value={form.last_name} onChange={(e) => setForm({ ...form, last_name: e.target.value })} />
                        </div>
                    </div>
                    <div>
                        <label className="label-overline block mb-1">Email *</label>
                        <input data-testid="apply-email" required type="email" className="input-dark" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
                    </div>
                    <div>
                        <label className="label-overline block mb-1">Phone *</label>
                        <input data-testid="apply-phone" required type="tel" className="input-dark" value={form.phone} onChange={(e) => setForm({ ...form, phone: e.target.value })} />
                    </div>
                    <button data-testid="apply-submit-btn" type="submit" disabled={busy} className="btn-primary w-full !py-2.5">
                        {busy ? "Submitting…" : "Apply now"}
                    </button>
                    <p className="text-[11px] text-cube-dim text-center mt-1">
                        {consentLine}
                    </p>
                </form>
            </div>
        );
    }

    return (
        <div className="cube-brand min-h-screen" data-testid="apply-page">
            <CubeBg />
            <div className="cube-content">
            {/* Header */}
            <header className="sticky top-0 z-30 backdrop-blur-xl px-6 py-3" style={{ background: "rgba(10,6,16,.82)", borderBottom: "1.5px solid var(--cube-line)" }}>
                <div className="max-w-5xl mx-auto flex items-center justify-between">
                    <div className="flex items-center gap-3">
                        <img src={LOGO} alt="Logo" className="w-8 h-8 object-contain" />
                        <div>
                            <div className="font-heading text-base font-bold tracking-tight">{data.company.name}</div>
                            <div className="text-[10px] uppercase tracking-widest text-cube-lav">Now hiring · {data.pipeline.name}</div>
                        </div>
                    </div>
                </div>
            </header>

            <main className="max-w-5xl mx-auto px-6 py-10">
                <div className="grid grid-cols-1 lg:grid-cols-5 gap-8">
                    {/* Left: Job description */}
                    <div className="lg:col-span-3 space-y-6">
                        <div>
                            <span className="cube-kicker">
                                <Sparkle size={11} weight="fill" /> Now hiring
                            </span>
                            <h1 className="font-heading text-4xl lg:text-5xl font-bold tracking-tight mt-3 leading-[1.05]">
                                Join the <span className="cube-grad">{data.pipeline.name.split(",")[0]}</span> team.
                            </h1>
                            <p className="text-cube-lav mt-3 text-sm leading-relaxed max-w-2xl">
                                We're growing fast and looking for outgoing, motivated people. {nextStepLine}
                            </p>
                        </div>

                        <div className="space-y-3">
                            <div className="label-overline">Open roles</div>
                            {data.jobs.map((j) => (
                                <button
                                    key={j.id}
                                    onClick={() => setSelectedJobId(j.id)}
                                    data-testid={`apply-job-${j.id}`}
                                    className={`w-full text-left surface p-4 transition-all ${
                                        selectedJobId === j.id ? "!border-cube-magenta ring-1 ring-cube-magenta" : "hover:!border-cube-purple"
                                    }`}
                                >
                                    <div className="flex items-start justify-between gap-3">
                                        <div className="flex-1 min-w-0">
                                            <div className="font-heading text-lg font-semibold">{j.title}</div>
                                            <div className="flex flex-wrap items-center gap-3 text-xs text-cube-lav mt-1.5">
                                                <span className="flex items-center gap-1"><MapPin size={11} /> {j.city || data.pipeline.name}</span>
                                                <span className="flex items-center gap-1"><Clock size={11} /> Mon–Fri, optional Sat</span>
                                                <span className="flex items-center gap-1"><Briefcase size={11} /> {j.category || "Customer Services"}</span>
                                            </div>
                                        </div>
                                        {selectedJobId === j.id && <CheckCircle weight="fill" size={20} className="text-cube-magenta flex-shrink-0" />}
                                    </div>
                                </button>
                            ))}
                            {data.jobs.length === 0 && (
                                <div className="surface p-6 text-center text-sm text-cube-lav">No open roles right now.</div>
                            )}
                        </div>

                        {selectedJob?.description && (
                            <div className="surface p-5">
                                <div className="label-overline mb-2">About the role</div>
                                <p className="text-sm leading-relaxed whitespace-pre-line" style={{ color: "var(--cube-cream)" }}>{selectedJob.description}</p>
                            </div>
                        )}
                    </div>

                    {/* Right: Apply form */}
                    <div className="lg:col-span-2">
                        <div className="surface p-6 sticky top-24">
                            <div className="font-heading text-xl font-semibold mb-1">Apply now</div>
                            <p className="text-xs text-cube-lav mb-5">{afterSubmitLine}</p>
                            <form onSubmit={submit} className="space-y-3">
                                <div className="grid grid-cols-2 gap-2.5">
                                    <div>
                                        <label className="label-overline block mb-1">First name *</label>
                                        <input data-testid="apply-first-name" required className="input-dark" value={form.first_name} onChange={(e) => setForm({ ...form, first_name: e.target.value })} />
                                    </div>
                                    <div>
                                        <label className="label-overline block mb-1">Last name</label>
                                        <input data-testid="apply-last-name" className="input-dark" value={form.last_name} onChange={(e) => setForm({ ...form, last_name: e.target.value })} />
                                    </div>
                                </div>
                                <div>
                                    <label className="label-overline block mb-1">Email *</label>
                                    <input data-testid="apply-email" type="email" required className="input-dark" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
                                </div>
                                <div>
                                    <label className="label-overline block mb-1">Mobile number *</label>
                                    <input data-testid="apply-phone" required type="tel" placeholder={phonePlaceholder(data?.phone_country)} className="input-dark" value={form.phone} onChange={(e) => setForm({ ...form, phone: e.target.value })} />
                                    <div className="text-[11px] text-cube-dim mt-1">We'll call and text this number about your application.</div>
                                </div>
                                <button type="submit" disabled={busy} data-testid="apply-submit-btn" className="btn-primary w-full !py-2.5 mt-2">
                                    {busy ? "Submitting…" : "Submit application →"}
                                </button>
                                <p className="text-[11px] text-cube-dim text-center mt-3">
                                    {consentLine}
                                </p>
                            </form>
                        </div>
                    </div>
                </div>
            </main>

            <footer className="mt-12 py-6 px-6 text-xs text-cube-dim text-center" style={{ borderTop: "1.5px solid var(--cube-line)" }}>
                © {new Date().getFullYear()} {data.company.name}. Powered by {agentName} — AI Recruiter.
            </footer>
            </div>
        </div>
    );
}
