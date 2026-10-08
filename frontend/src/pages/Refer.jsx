import { useEffect, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import api from "@/lib/api";
import { phonePlaceholder } from "@/lib/phone";
import { toast } from "sonner";
import { CheckCircle, Star } from "@phosphor-icons/react";
import CubeBg from "@/components/CubeBg";
import BRAND from "@/lib/brand";

const LOGO = BRAND.logo;

export default function ReferPage() {
    const { slug } = useParams();
    const [searchParams] = useSearchParams();
    const referredBy = searchParams.get("ref") || "";

    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [selectedJobId, setSelectedJobId] = useState("");
    const [busy, setBusy] = useState(false);
    const [submitted, setSubmitted] = useState(false);
    const [submittedName, setSubmittedName] = useState("");
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
        if (!selectedJobId && data?.jobs?.length > 0) return toast.warning("Please select a role");
        setBusy(true);
        try {
            await api.post("/public/apply", {
                pipeline_id: data.pipeline.id,
                job_id: selectedJobId || undefined,
                first_name: form.first_name.trim(),
                last_name: form.last_name.trim(),
                email: form.email.trim(),
                phone: form.phone.trim(),
                referred_by: referredBy,
            });
            setSubmittedName(form.first_name.trim());
            setSubmitted(true);
        } catch (err) {
            toast.error(err?.response?.data?.detail || "Failed to submit");
        } finally { setBusy(false); }
    };

    if (loading) return <div className="cube-brand min-h-screen flex items-center justify-center text-sm" style={{ color: "var(--cube-lav)" }}>Loading…</div>;
    if (!data) return <div className="cube-brand min-h-screen flex items-center justify-center text-sm" style={{ color: "var(--cube-lav)" }}>Pipeline not found</div>;

    if (submitted) return (
        <div className="cube-brand min-h-screen flex items-center justify-center p-6">
            <CubeBg />
            <div className="cube-content text-center space-y-4 max-w-sm">
                <CheckCircle size={56} weight="fill" className="text-cube-magenta mx-auto" />
                <h1 className="font-heading text-2xl font-bold">
                    You're all set{submittedName ? `, ${submittedName}` : ""}!
                </h1>
                <p className="text-cube-lav text-base leading-relaxed">
                    You'll receive a screening call from Olivia shortly. Keep your phone nearby!
                </p>
            </div>
        </div>
    );

    return (
        <div className="cube-brand min-h-screen">
            <CubeBg />
            <div className="cube-content">
            <header className="sticky top-0 z-30 backdrop-blur-xl px-6 py-3" style={{ background: "rgba(10,6,16,.82)", borderBottom: "1.5px solid var(--cube-line)" }}>
                <div className="max-w-lg mx-auto flex items-center gap-3">
                    <img src={LOGO} alt="Logo" className="w-8 h-8 object-contain" />
                    <div>
                        <div className="font-heading text-base font-bold tracking-tight">{data.company.name}</div>
                        <div className="text-[10px] uppercase tracking-widest text-cube-lav">{data.pipeline.name}</div>
                    </div>
                </div>
            </header>

            <main className="max-w-lg mx-auto px-6 py-10">
                <div className="mb-6">
                    <div className="cube-kicker mb-3">
                        <Star size={11} weight="fill" /> Now Hiring
                    </div>
                    <h1 className="font-heading text-3xl font-bold tracking-tight leading-tight">
                        If you're great,<br /><span className="cube-grad">we want you on our team.</span>
                    </h1>
                    <p className="text-cube-lav mt-2 text-sm leading-relaxed">
                        Fill in your details below and Olivia will call you for a quick screening — usually within minutes.
                    </p>
                </div>

                <div className="surface p-6">
                    <form onSubmit={submit} className="space-y-4">
                        {data.jobs.length > 1 && (
                            <div>
                                <label className="label-overline block mb-1">Role *</label>
                                <select className="input-dark" value={selectedJobId} onChange={(e) => setSelectedJobId(e.target.value)}>
                                    {data.jobs.map((j) => <option key={j.id} value={j.id}>{j.title}</option>)}
                                </select>
                            </div>
                        )}

                        <div className="grid grid-cols-2 gap-2.5">
                            <div>
                                <label className="label-overline block mb-1">First name *</label>
                                <input required className="input-dark" value={form.first_name} onChange={(e) => setForm({ ...form, first_name: e.target.value })} />
                            </div>
                            <div>
                                <label className="label-overline block mb-1">Last name</label>
                                <input className="input-dark" value={form.last_name} onChange={(e) => setForm({ ...form, last_name: e.target.value })} />
                            </div>
                        </div>
                        <div>
                            <label className="label-overline block mb-1">Email *</label>
                            <input required type="email" className="input-dark" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
                        </div>
                        <div>
                            <label className="label-overline block mb-1">Mobile number *</label>
                            <input required type="tel" className="input-dark" placeholder={phonePlaceholder(data?.phone_country)} value={form.phone} onChange={(e) => setForm({ ...form, phone: e.target.value })} />
                            <div className="text-[11px] text-cube-dim mt-1">We'll call and text this number about the application.</div>
                        </div>

                        <button type="submit" disabled={busy} className="btn-primary w-full !py-2.5 mt-2">
                            {busy ? "Submitting…" : "Apply now →"}
                        </button>
                        <p className="text-[11px] text-cube-dim text-center">
                            By submitting, you confirm you consent to a brief AI screening call.
                        </p>
                    </form>
                </div>
            </main>

            <footer className="mt-12 py-6 px-6 text-xs text-cube-dim text-center" style={{ borderTop: "1.5px solid var(--cube-line)" }}>
                © {new Date().getFullYear()} {data.company.name}. Powered by Olivia — AI Recruiter.
            </footer>
            </div>
        </div>
    );
}
