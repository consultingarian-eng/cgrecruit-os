import { useState, useEffect } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { UploadSimple, FileText, UserPlus, EnvelopeOpen, Copy, ShareNetwork } from "@phosphor-icons/react";
import api from "@/lib/api";
import { toast } from "sonner";

const UPLOAD_STATE = {
    pending:   { label: "Queued",    className: "text-ink-muted" },
    uploading: { label: "Parsing…",  className: "text-ink-muted" },
    added:     { label: "Added",     className: "text-brand-success" },
    duplicate: { label: "Duplicate", className: "text-brand-warning" },
    failed:    { label: "Failed",    className: "text-brand-danger" },
};

export default function AddApplicantModal({ open, onClose, pipelineId, pipelineName, jobs = [], onCreated, pipelineSlug }) {
    const [tab, setTab] = useState("upload");
    const [files, setFiles] = useState([]);
    const [rows, setRows] = useState([]); // submitted CVs: {file, state}
    const [jobId, setJobId] = useState(jobs[0]?.id || "");
    const [busy, setBusy] = useState(false);
    const [uploadReferredBy, setUploadReferredBy] = useState("");
    const [first, setFirst] = useState("");
    const [last, setLast] = useState("");
    const [email, setEmail] = useState("");
    const [phone, setPhone] = useState("");
    const [emailIn, setEmailIn] = useState(null);

    // The modal outlives a single open, and `jobs` only arrives after it is opened
    // — so the useState seed above always ran against an empty list and smart
    // scoring was silently skipped. Pick the first job up once it lands, and drop
    // any id that is not in the current list: the modal is never unmounted, so a
    // kept id would otherwise tag a candidate with another office's job.
    useEffect(() => {
        if (!open) return;
        setJobId((prev) => (jobs.some((j) => j.id === prev) ? prev : jobs[0]?.id || ""));
    }, [open, jobs]);

    // Load the per-pipeline email-in address whenever the Email-in tab is opened.
    useEffect(() => {
        if (!open || tab !== "email-in" || !pipelineId) return;
        let alive = true;
        api.get(`/email-intake/pipeline/${pipelineId}`).then((r) => {
            if (alive) setEmailIn(r.data);
        }).catch(() => alive && setEmailIn({ error: true }));
        return () => { alive = false; };
    }, [open, tab, pipelineId]);

    const copyEmail = () => {
        if (!emailIn?.inbound_address) return;
        navigator.clipboard.writeText(emailIn.inbound_address);
        toast.success("Copied to clipboard");
    };

    const setRowState = (i, state) =>
        setRows((prev) => prev.map((row, j) => (j === i ? { ...row, state } : row)));

    const sendOne = (file, i) => {
        const fd = new FormData();
        fd.append("file", file);
        fd.append("pipeline_id", pipelineId);
        if (jobId) fd.append("job_id", jobId);
        if (uploadReferredBy.trim()) fd.append("referred_by", uploadReferredBy.trim());

        setRowState(i, "uploading");
        api.post("/candidates/upload", fd, { headers: { "Content-Type": "multipart/form-data" } })
            .then((r) => {
                setRowState(i, r.data?.duplicate ? "duplicate" : "added");
                onCreated?.();
            })
            .catch(() => {
                // Toast as well as the row — the recruiter may have closed the modal.
                setRowState(i, "failed");
                toast.error(`Failed to parse ${file.name}`);
            });
    };

    // Stay open until every CV is terminal: closing first threw away the File
    // objects, so a transport failure could only be retried from the filesystem.
    const upload = () => {
        if (!files.length) return toast.warning("Choose at least one file");
        const batch = [...files];
        const base = rows.length;
        setRows((prev) => [...prev, ...batch.map((file) => ({ file, state: "pending" }))]);
        setFiles([]);
        batch.forEach((f, i) => sendOne(f, base + i));
    };

    const resetAndClose = () => {
        setRows([]);
        setFiles([]);
        setUploadReferredBy("");
        onClose();
    };

    const manual = async () => {
        if (!first.trim()) return toast.warning("First name required");
        // Nothing downstream normalises this — a non-E.164 number produces a
        // candidate the dialer and SMS sender can never reach.
        if (phone.trim() && !/^\+[1-9]\d{7,14}$/.test(phone.trim())) {
            return toast.warning("Phone must be E.164 — e.g. +15551234567");
        }
        setBusy(true);
        try {
            await api.post("/candidates", {
                pipeline_id: pipelineId,
                job_id: jobId || undefined,
                first_name: first.trim(),
                last_name: last.trim(),
                email: email.trim(),
                phone: phone.trim(),
            });
            toast.success("Candidate added");
            setFirst(""); setLast(""); setEmail(""); setPhone("");
            onCreated?.();
            resetAndClose();
        } catch (e) {
            const msg = e?.response?.data?.detail;
            if (e?.response?.status === 409 && msg) toast.warning(msg);
            else toast.error("Failed to add");
        }
        finally { setBusy(false); }
    };

    const inFlight = rows.some((r) => r.state === "pending" || r.state === "uploading");
    const counts = rows.reduce((acc, r) => ({ ...acc, [r.state]: (acc[r.state] || 0) + 1 }), {});

    return (
        <Dialog open={open} onOpenChange={(o) => !o && resetAndClose()}>
            <DialogContent className="bg-[#0E0E11] border-strokes max-w-xl" data-testid="add-applicant-modal">
                <DialogHeader>
                    <DialogTitle className="font-heading text-2xl tracking-tight">Add Applicants</DialogTitle>
                </DialogHeader>
                <div style={{ background: "rgba(124,58,237,0.15)", border: "1px solid rgba(124,58,237,0.35)", borderRadius: 8, padding: "8px 12px", marginTop: -4, display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ fontSize: 12, color: "#a78bfa", fontWeight: 700 }}>PIPELINE</span>
                    <span style={{ fontSize: 13, color: "#c4b5fd", fontWeight: 600 }}>{pipelineName || "…"}</span>
                </div>

                <div className="flex gap-1 border-b border-strokes -mx-6 px-6">
                    <TabBtn active={tab === "upload"} onClick={() => setTab("upload")} testid="tab-upload">
                        <UploadSimple size={14} className="mr-1.5" /> Upload CVs
                    </TabBtn>
                    <TabBtn active={tab === "manual"} onClick={() => setTab("manual")} testid="tab-manual">
                        <UserPlus size={14} className="mr-1.5" /> Manual Entry
                    </TabBtn>
                    <TabBtn active={tab === "email-in"} onClick={() => setTab("email-in")} testid="tab-email-in">
                        <EnvelopeOpen size={14} className="mr-1.5" /> Email-in
                    </TabBtn>
                    <TabBtn active={tab === "referral"} onClick={() => setTab("referral")} testid="tab-referral">
                        <ShareNetwork size={14} className="mr-1.5" /> Referral Link
                    </TabBtn>
                </div>

                {tab !== "email-in" && tab !== "referral" && (
                    <div className="mt-4">
                        <label className="label-overline block mb-1.5">Assign to Job (optional)</label>
                        <select data-testid="job-select" className="input-dark" value={jobId} onChange={(e) => setJobId(e.target.value)}>
                            <option value="">— No job (skip smart-scoring) —</option>
                            {jobs.map((j) => <option key={j.id} value={j.id}>{j.title}</option>)}
                        </select>
                    </div>
                )}

                {tab === "upload" && (
                    <div className="mt-4">
                        <div
                            className="block border-2 border-dashed border-strokes rounded-lg p-8 text-center cursor-pointer hover:border-brand-primary transition-colors"
                            data-testid="upload-dropzone"
                            onClick={() => document.getElementById('cv-upload').click()}
                            onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); }}
                            onDragEnter={(e) => { e.preventDefault(); e.stopPropagation(); }}
                            onDrop={(e) => {
                                e.preventDefault();
                                e.stopPropagation();
                                const dropped = Array.from(e.dataTransfer.files).filter(f =>
                                    /\.(pdf|doc|docx|txt)$/i.test(f.name)
                                );
                                if (dropped.length) setFiles(prev => [...prev, ...dropped]);
                            }}
                        >
                            <UploadSimple size={28} weight="duotone" className="mx-auto text-brand-primary mb-2" />
                            <div className="text-sm font-medium">Drop CVs here or click to browse</div>
                            <div className="text-xs text-ink-muted mt-1">PDF, DOCX, TXT — bulk supported</div>
                            <input
                                id="cv-upload"
                                data-testid="cv-file-input"
                                type="file"
                                multiple
                                accept=".pdf,.doc,.docx,.txt"
                                className="hidden"
                                onChange={(e) => setFiles(prev => [...prev, ...Array.from(e.target.files || [])])}
                            />
                        </div>
                        {(files.length > 0 || rows.length > 0) && (
                            <div className="mt-3 space-y-1 max-h-48 overflow-y-auto pr-1">
                                {rows.map((row, i) => {
                                    const st = UPLOAD_STATE[row.state];
                                    return (
                                        <div key={`row-${i}`} className="flex items-center gap-2 text-xs surface p-2">
                                            <FileText size={13} /> <span className="truncate flex-1">{row.file.name}</span>
                                            {row.state === "failed" && (
                                                <button onClick={() => sendOne(row.file, i)} className="text-brand-primary hover:underline">
                                                    Retry
                                                </button>
                                            )}
                                            <span className={st.className}>{st.label}</span>
                                        </div>
                                    );
                                })}
                                {files.map((f, i) => (
                                    <div key={i} className="flex items-center gap-2 text-xs surface p-2">
                                        <FileText size={13} /> <span className="truncate flex-1">{f.name}</span>
                                        <span className="text-ink-muted">{Math.round(f.size / 1024)} KB</span>
                                    </div>
                                ))}
                            </div>
                        )}
                        {rows.length > 0 && !inFlight && (
                            <div className="mt-2 text-xs text-ink-muted" data-testid="upload-summary">
                                {counts.added || 0} added · {counts.duplicate || 0} duplicate · {counts.failed || 0} failed
                            </div>
                        )}
                        <div className="mt-3">
                            <label className="label-overline block mb-1">Referred by (optional)</label>
                            <input
                                className="input-dark"
                                placeholder="Employee name who provided these CVs"
                                value={uploadReferredBy}
                                onChange={(e) => setUploadReferredBy(e.target.value)}
                            />
                        </div>
                        {rows.length > 0 && !inFlight && !files.length ? (
                            <button
                                onClick={resetAndClose}
                                data-testid="upload-done-btn"
                                className="btn-primary w-full mt-4 !py-2.5"
                            >
                                Done
                            </button>
                        ) : (
                            <button
                                onClick={upload}
                                disabled={busy || !files.length}
                                data-testid="upload-submit-btn"
                                className="btn-primary w-full mt-4 !py-2.5"
                            >
                                {inFlight && !files.length ? "Parsing with AI…" : `Add ${files.length || ""} applicant${files.length === 1 ? "" : "s"}`}
                            </button>
                        )}
                    </div>
                )}

                {tab === "manual" && (
                    <div className="mt-4 space-y-3">
                        <div className="grid grid-cols-2 gap-3">
                            <div>
                                <label className="label-overline block mb-1">First Name *</label>
                                <input data-testid="manual-first-name" className="input-dark" value={first} onChange={(e) => setFirst(e.target.value)} />
                            </div>
                            <div>
                                <label className="label-overline block mb-1">Last Name</label>
                                <input data-testid="manual-last-name" className="input-dark" value={last} onChange={(e) => setLast(e.target.value)} />
                            </div>
                        </div>
                        <div>
                            <label className="label-overline block mb-1">Email</label>
                            <input data-testid="manual-email" className="input-dark" value={email} onChange={(e) => setEmail(e.target.value)} />
                        </div>
                        <div>
                            <label className="label-overline block mb-1">Phone (E.164 format)</label>
                            <input data-testid="manual-phone" placeholder="+15551234567" className="input-dark" value={phone} onChange={(e) => setPhone(e.target.value)} />
                        </div>
                        <button onClick={manual} disabled={busy} data-testid="manual-submit-btn" className="btn-primary w-full !py-2.5">
                            {busy ? "Adding…" : "Add Applicant"}
                        </button>
                    </div>
                )}

                {tab === "referral" && (
                    <ReferralLinkTab pipelineSlug={pipelineSlug} />
                )}

                {tab === "email-in" && (
                    <div className="mt-4 space-y-4" data-testid="email-in-tab">
                        {!emailIn && (
                            <div className="text-sm text-ink-muted text-center py-8">Loading…</div>
                        )}
                        {emailIn?.error && (
                            <div className="text-sm text-brand-danger">Couldn't load email-in details for this pipeline.</div>
                        )}
                        {emailIn && !emailIn.error && (
                            <>
                                <div>
                                    <label className="label-overline block mb-1.5">Inbound address for {emailIn.pipeline_name}</label>
                                    <div className="flex gap-2">
                                        <input
                                            readOnly
                                            data-testid="email-in-address"
                                            className="input-dark font-mono text-xs"
                                            value={emailIn.inbound_address}
                                            onClick={(e) => e.target.select()}
                                        />
                                        <button onClick={copyEmail} data-testid="email-in-copy-btn" className="btn-secondary !py-1.5 !px-3 text-xs flex items-center gap-1.5 whitespace-nowrap">
                                            <Copy size={11} weight="bold" /> Copy
                                        </button>
                                    </div>
                                    {!emailIn.enabled && (
                                        <div className="text-[11px] text-brand-warning mt-1.5">
                                            Email-in is disabled — turn it on in Settings → Email Intake to start receiving.
                                        </div>
                                    )}
                                </div>

                                <div className="surface p-4 space-y-2.5 text-sm">
                                    <div className="font-semibold flex items-center gap-1.5">
                                        <EnvelopeOpen size={14} weight="duotone" className="text-brand-primary" /> How it works
                                    </div>
                                    <ol className="text-xs text-ink-muted space-y-1.5 list-decimal list-inside leading-relaxed">
                                        <li>Forward (or have applicants email) their CV to the address above.</li>
                                        <li>Olivia parses the resume, extracts contact info, and creates a candidate on this pipeline.</li>
                                        <li>If the candidate has a phone number and auto-dial is on, screening kicks off automatically.</li>
                                        <li>Failed parses land in <span className="text-ink">Settings → Email Intake → Failed resumes</span> for review.</li>
                                    </ol>
                                </div>

                                <details className="text-xs">
                                    <summary className="cursor-pointer text-ink-muted hover:text-ink">Other ways to receive CVs by email</summary>
                                    <div className="mt-2 space-y-1.5 text-ink-muted leading-relaxed">
                                        <div>
                                            <strong className="text-ink">Generic auto-detect:</strong>{" "}
                                            <span className="font-mono">{emailIn.fallback_address}</span> — Olivia picks the best matching pipeline based on the email subject &amp; body.
                                        </div>
                                        <div>
                                            <strong className="text-ink">Plus-addressing:</strong>{" "}
                                            <span className="font-mono">apply+{emailIn.pipeline_slug || "slug"}@…</span> — works alongside any other forwarding rules you have.
                                        </div>
                                    </div>
                                </details>
                            </>
                        )}
                    </div>
                )}
            </DialogContent>
        </Dialog>
    );
}

function ReferralLinkTab({ pipelineSlug }) {
    const base = window.location.origin;
    const link = pipelineSlug ? `${base}/refer/${pipelineSlug}` : null;

    const copy = () => {
        if (!link) return;
        navigator.clipboard.writeText(link);
        toast.success("Referral link copied");
    };

    return (
        <div className="mt-4 space-y-4">
            <div className="surface p-4 space-y-2.5 text-sm">
                <div className="font-semibold flex items-center gap-1.5">
                    <ShareNetwork size={14} weight="duotone" className="text-brand-primary" /> How it works
                </div>
                <ol className="text-xs text-ink-muted space-y-1.5 list-decimal list-inside leading-relaxed">
                    <li>Share this link with your team members.</li>
                    <li>They fill in their own name and the candidate's details.</li>
                    <li>The candidate lands in Screening with "Ref: [Employee]" shown on their card.</li>
                    <li>Olivia calls them automatically just like any other applicant.</li>
                </ol>
            </div>

            {link ? (
                <div>
                    <label className="label-overline block mb-1.5">Referral link for this pipeline</label>
                    <div className="flex gap-2">
                        <input
                            readOnly
                            className="input-dark font-mono text-xs"
                            value={link}
                            onClick={(e) => e.target.select()}
                        />
                        <button onClick={copy} className="btn-secondary !py-1.5 !px-3 text-xs flex items-center gap-1.5 whitespace-nowrap">
                            <Copy size={11} weight="bold" /> Copy
                        </button>
                    </div>
                </div>
            ) : (
                <div className="text-xs text-ink-muted">No pipeline selected.</div>
            )}
        </div>
    );
}

function TabBtn({ active, onClick, children, testid }) {
    return (
        <button
            onClick={onClick}
            data-testid={testid}
            className={`flex items-center px-3 py-2.5 text-sm border-b-2 transition-colors ${
                active ? "border-brand-primary text-ink" : "border-transparent text-ink-muted hover:text-ink"
            }`}
        >
            {children}
        </button>
    );
}
