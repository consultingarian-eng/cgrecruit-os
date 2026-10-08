import { useState, useEffect, useRef } from "react";
import api from "@/lib/api";
import { toast } from "sonner";

export default function RecruiterProfileSection({ settings, onSaved, pipelineId }) {
    const [form, setForm] = useState(settings.recruiter_profile || {});
    const [busy, setBusy] = useState(false);

    // Only re-hydrate local form state when the user EXPLICITLY switches
    // scope (pipelineId changed). Resetting on every settings prop change
    // was wiping unsaved edits during background refreshes — the form is
    // now the source of truth for the current edit session.
    const lastScopeRef = useRef(pipelineId);
    useEffect(() => {
        if (lastScopeRef.current !== pipelineId) {
            lastScopeRef.current = pipelineId;
            setForm(settings.recruiter_profile || {});
        }
    }, [pipelineId, settings]);

    const save = async () => {
        setBusy(true);
        try {
            const params = pipelineId ? { pipeline_id: pipelineId } : {};
            await api.put("/settings/recruiter-profile", form, { params });
            await onSaved?.();
            toast.success("Profile saved");
        } catch { toast.error("Failed to save"); }
        finally { setBusy(false); }
    };

    const upd = (k, v) => setForm({ ...form, [k]: v });

    return (
        <div className="max-w-2xl space-y-6" data-testid="recruiter-profile-section">
            <div>
                <h2 className="font-heading text-2xl font-bold tracking-tight">Company &amp; Branding</h2>
                <p className="text-sm text-ink-muted mt-1">Your company&apos;s details, not your own — used as default values for stage emails and AI agent prompts.</p>
            </div>
            <div className="space-y-4">
                <Field label="Company Name" value={form.company_name} onChange={(v) => upd("company_name", v)} testid="profile-company" />
                <Field label="City" value={form.city} onChange={(v) => upd("city", v)} testid="profile-city" />
                <Field label="Default Job Role" value={form.job_role} onChange={(v) => upd("job_role", v)} testid="profile-role" />
                <Field label="Recruiter Email" value={form.recruiter_email} onChange={(v) => upd("recruiter_email", v)} testid="profile-email" />
                <Field label="Phone" value={form.phone} onChange={(v) => upd("phone", v)} testid="profile-phone" />
                <Field label="Website" value={form.website} onChange={(v) => upd("website", v)} testid="profile-website" />
                <Field label="Logo URL" value={form.logo_url || ""} onChange={(v) => upd("logo_url", v)} testid="profile-logo-url" placeholder="https://www.example.com/logo.png" />
                <Field
                    label="Instagram URL"
                    value={(form.social_links || {}).instagram || ""}
                    onChange={(v) => setForm({ ...form, social_links: { ...(form.social_links || {}), instagram: v } })}
                    testid="profile-instagram"
                />
                <Field
                    label="LinkedIn URL"
                    value={(form.social_links || {}).linkedin || ""}
                    onChange={(v) => setForm({ ...form, social_links: { ...(form.social_links || {}), linkedin: v } })}
                    testid="profile-linkedin"
                />
                <div>
                    <label className="label-overline block mb-1.5">Interview Etiquette Tips</label>
                    <textarea
                        data-testid="profile-etiquette"
                        rows={4}
                        className="input-dark resize-y"
                        placeholder="Quick interview tips: Join 2 minutes early, pick a quiet well-lit spot, test your mic + camera in advance, have your résumé open. Smile, breathe, and remember: we're rooting for you. 💜"
                        value={form.interview_etiquette || ""}
                        onChange={(e) => upd("interview_etiquette", e.target.value)}
                    />
                    <div className="text-[11px] text-ink-muted mt-1">
                        Appears under the Zoom link on every interview confirmation email. Leave blank to use the default tips. Basic HTML allowed (e.g. <code>&lt;strong&gt;</code>).
                    </div>
                </div>
            </div>
            <button onClick={save} disabled={busy} data-testid="save-profile-btn" className="btn-primary">
                {busy ? "Saving…" : "Save Profile"}
            </button>
        </div>
    );
}

function Field({ label, value, onChange, testid }) {
    return (
        <div>
            <label className="label-overline block mb-1.5">{label}</label>
            <input data-testid={testid} className="input-dark" value={value || ""} onChange={(e) => onChange(e.target.value)} />
        </div>
    );
}
