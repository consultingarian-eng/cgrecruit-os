import { useCallback, useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import api from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { usePipeline } from "@/lib/pipeline";
import { toast } from "sonner";
import { confirmDialog } from "@/components/ConfirmDialog";
import {
    Buildings, Sparkle, Phone, ClipboardText, Megaphone, Microphone,
    EnvelopeSimple, Globe, ShieldCheck, Stack, ClockClockwise, EnvelopeOpen, UsersThree, Plug, ArrowCounterClockwise, Code, CalendarBlank,
    List, X, CaretDown, ChatTeardrop,
} from "@phosphor-icons/react";
import RecruiterProfileSection from "@/components/settings/RecruiterProfileSection";
import ScreenCallAgentSection from "@/components/settings/ScreenCallAgentSection";
import TextScreeningSection from "@/components/settings/TextScreeningSection";
import ApplicantCommsSection from "@/components/settings/ApplicantCommsSection";
import RegionLanguageSection from "@/components/settings/RegionLanguageSection";
import CustomFormSection from "@/components/settings/CustomFormSection";
import SmartScoringSection from "@/components/settings/SmartScoringSection";
import PipelinesSection from "@/components/settings/PipelinesSection";
import AutoDialerSection from "@/components/settings/AutoDialerSection";
import EmailIntakeSection from "@/components/settings/EmailIntakeSection";
import TeamSection from "@/components/settings/TeamSection";
import PipelinePromptsSection from "@/components/settings/PipelinePromptsSection";
import IntegrationsSection from "@/components/settings/IntegrationsSection";
import EmbedWidgetSection from "@/components/settings/EmbedWidgetSection";
import BookingSlotsSection from "@/components/settings/BookingSlotsSection";
import NoShowRevivalSection from "@/components/settings/NoShowRevivalSection";
import InboundAgentSection from "@/components/settings/InboundAgentSection";
import StarterEmailSection from "@/components/settings/StarterEmailSection";
import AiStagePromptsSection from "@/components/settings/AiStagePromptsSection";
import AnalystAccessSection from "@/components/settings/AnalystAccessSection";

// `adminOnly: true` = only super-admins see the tab. These expose infra keys
// (ElevenLabs / Twilio / SendGrid) and must be hidden from recruiter sub-accounts.
// `global: true` = the section is tenant-wide (never per-pipeline) — the override
// banner is hidden and the section is always loaded against the global settings doc.
// `group` = which rail block the row sits in (see GROUPS); array order is the
// order inside the block.
//
// Keys are URLs (/settings/<key>) and must not be renamed, so several labels no
// longer match their key — "smart-scoring" is the job-ad editor and
// "pipeline-prompts" is the per-office AI voice. That is deliberate.
const SECTIONS = [
    { key: "applicant-comms", label: "Applicant Comms", icon: <EnvelopeSimple size={14} weight="duotone" />, group: "office" },
    { key: "booking-slots", label: "Booking & Slots", icon: <CalendarBlank size={14} weight="duotone" />, group: "office" },
    { key: "no-show-revival", label: "No-Show Revival", icon: <ArrowCounterClockwise size={14} weight="duotone" />, group: "office" },
    { key: "custom-form", label: "Post-Interview Form", icon: <ClipboardText size={14} weight="duotone" />, group: "office" },
    // Visible to recruiters too — they manage their own pipeline's agent.
    // Backend gates the GLOBAL save (pipeline_id=null) to super-admin; recruiters
    // can edit only their assigned pipeline (`assert_pipeline_access`).
    { key: "text-screening", label: "Text Screening", icon: <ChatTeardrop size={14} weight="duotone" />, group: "office" },
    { key: "screen-call-agent", label: "Screen Call Agent", icon: <Phone size={14} weight="duotone" />, group: "office" },

    { key: "smart-scoring", label: "Job Ads", icon: <Megaphone size={14} weight="duotone" />, global: true, group: "sources" },
    { key: "integrations", label: "Integrations", icon: <Plug size={14} weight="duotone" />, global: true, group: "sources" },
    { key: "email-intake", label: "Email Intake", icon: <EnvelopeOpen size={14} weight="duotone" />, adminOnly: true, global: true, group: "sources" },

    { key: "recruiter-profile", label: "Company & Branding", icon: <Buildings size={14} weight="duotone" />, group: "setup" },
    { key: "pipelines", label: "Offices & Variants", icon: <Stack size={14} weight="duotone" />, adminOnly: true, global: true, group: "setup" },
    { key: "pipeline-prompts", label: "Office AI Voice & Prompts", icon: <Microphone size={14} weight="duotone" />, global: true, group: "setup" },
    { key: "inbound-agent", label: "Inbound Call Agent", icon: <Phone size={14} weight="duotone" />, group: "setup" },
    { key: "region-language", label: "Region & Language", icon: <Globe size={14} weight="duotone" />, adminOnly: true, group: "setup" },

    { key: "auto-dialer", label: "Auto-Dialer", icon: <ClockClockwise size={14} weight="duotone" />, adminOnly: true, group: "advanced" },
    { key: "starter-email", label: "Starter Email", icon: <EnvelopeOpen size={14} weight="duotone" />, adminOnly: true, group: "advanced" },
    { key: "ai-stage-prompts", label: "AI Stage Prompts", icon: <Sparkle size={14} weight="duotone" />, adminOnly: true, group: "advanced" },
    { key: "embed-widget", label: "Embed Widget", icon: <Code size={14} weight="duotone" />, global: true, group: "advanced" },
    { key: "team", label: "Team", icon: <UsersThree size={14} weight="duotone" />, adminOnly: true, global: true, group: "advanced" },
    { key: "analyst-access", label: "Analyst Access", icon: <ShieldCheck size={14} weight="duotone" />, adminOnly: true, global: true, group: "advanced" },
];

const GROUPS = [
    { key: "office", label: "This office" },
    { key: "sources", label: "Job ads & applicant sources" },
    { key: "setup", label: "Office setup" },
    { key: "advanced", label: "Advanced", collapsible: true },
];

// The one section a recruiter opens week to week, and visible to every role.
// Landing on anything else means the first thing you see is a form you almost
// never came to change.
const DEFAULT_SECTION = "applicant-comms";

export default function SettingsPage() {
    const { section } = useParams();
    const navigate = useNavigate();
    const { user } = useAuth();
    const { pipelines, activePipelineId } = usePipeline();
    const [settings, setSettings] = useState(null);
    const [loading, setLoading] = useState(true);
    const [overridePipelines, setOverridePipelines] = useState([]);
    const [resetting, setResetting] = useState(false);
    const [sidebarOpen, setSidebarOpen] = useState(false);
    const [advancedOpen, setAdvancedOpen] = useState(false);
    // Fail CLOSED: a /me payload that arrives without `role` must not unlock the
    // admin-only sections.
    const isAdmin = user?.role === "super_admin";
    const visibleSections = SECTIONS.filter((s) => isAdmin || !s.adminOnly);

    // Scope toggle: super-admins can flip between editing for a specific
    // pipeline (override) vs editing the global default that applies to ALL
    // offices. Recruiters can only ever edit their own pipeline's override.
    // Persisted to sessionStorage so flipping a tab doesn't lose the choice —
    // deliberately NOT localStorage: "editing the tenant-wide default" has to
    // expire with the browser session instead of silently outliving the one
    // change it was flipped for.
    const [editGlobal, setEditGlobal] = useState(() =>
        sessionStorage.getItem("cgrecruit_settings_scope") === "global"
    );
    useEffect(() => {
        sessionStorage.setItem("cgrecruit_settings_scope", editGlobal ? "global" : "pipeline");
    }, [editGlobal]);
    // Recruiters can never edit global — force pipeline scope for them.
    const effectiveEditGlobal = isAdmin && editGlobal;

    const activePipeline = pipelines.find((p) => p.id === activePipelineId);
    const active = section || DEFAULT_SECTION;
    const activeMeta = SECTIONS.find((s) => s.key === active);
    // Section is hard-global (Offices & Variants, Job Ads, Email Intake,
    // Integrations, Team) → always pipeline_id=null. Otherwise: scope to the
    // active pipeline UNLESS the user has flipped the scope toggle to "Global".
    const pipelineScope = activeMeta?.global ? null : (effectiveEditGlobal ? null : activePipelineId);
    const hasOverride = overridePipelines.includes(activePipelineId);

    // If a recruiter lands on an admin-only section via URL, redirect them.
    useEffect(() => {
        if (section && !visibleSections.find((s) => s.key === section)) {
            navigate(`/settings/${DEFAULT_SECTION}`, { replace: true });
        }
    }, [section, visibleSections, navigate]);

    const refresh = useCallback(async () => {
        const params = pipelineScope ? { pipeline_id: pipelineScope } : {};
        try {
            const r = await api.get("/settings", { params });
            setSettings(r.data);
        } catch (err) {
            // Settings load is critical — surface the failure rather than silently
            // showing a blank form (we used to swallow this, which made debugging
            // pipeline_id mismatches impossible).
            console.error("Settings refresh failed:", err);
            toast.error("Couldn't load settings. Try refreshing the page.");
        }
        try {
            const ov = await api.get("/settings/override-status");
            setOverridePipelines(ov.data?.overrides || []);
        } catch {
            // Recruiters legitimately get a 403 here (they can't see other pipelines'
            // override status) — silent skip is the correct behaviour for this one.
        }
    }, [pipelineScope]);

    useEffect(() => {
        setLoading(true);
        refresh().finally(() => setLoading(false));
    }, [refresh, active]);

    const onResetToGlobal = async () => {
        if (!activePipelineId) return;
        const ok = await confirmDialog({
            title: `Reset ${activePipeline?.name || "this pipeline"} to global defaults?`,
            description: "Pipeline-specific overrides for this office will be discarded.",
            confirmLabel: "Reset",
            destructive: true,
        });
        if (!ok) return;
        setResetting(true);
        try {
            await api.delete("/settings/override", { params: { pipeline_id: activePipelineId } });
            toast.success(`${activePipeline?.name || "Pipeline"} reset to global defaults`);
            await refresh();
        } catch {
            toast.error("Failed to reset overrides");
        } finally {
            setResetting(false);
        }
    };

    if (loading || !settings) {
        return <div className="p-8 text-ink-muted text-sm">Loading settings…</div>;
    }

    const activeSectionLabel = visibleSections.find((s) => s.key === active)?.label || "Settings";

    const SidebarNav = ({ onSelect }) => (
        <>
            <div className="px-4 py-5 border-b border-strokes">
                <div className="label-overline">Settings</div>
                <div className="font-heading text-lg font-semibold mt-1">Recruitment</div>
                {!isAdmin && (
                    <div className="text-[10px] text-ink-muted mt-1.5" data-testid="recruiter-badge">
                        Recruiter access
                    </div>
                )}
            </div>
            <nav className="p-2 space-y-3">
                {GROUPS.map((g) => {
                    // Built from visibleSections, so a block a recruiter has no
                    // rows in never renders as a bare header.
                    const rows = visibleSections.filter((s) => s.group === g.key);
                    if (!rows.length) return null;
                    // A collapsed block still opens itself when the section
                    // you're on lives inside it — otherwise the rail would hide
                    // the row you just navigated to.
                    const open = !g.collapsible || advancedOpen || rows.some((s) => s.key === active);
                    return (
                        <div key={g.key}>
                            {g.collapsible ? (
                                <button
                                    onClick={() => setAdvancedOpen((v) => !v)}
                                    data-testid={`settings-group-${g.key}`}
                                    className="w-full flex items-center gap-1.5 px-3 py-1.5 label-overline hover:text-ink transition-colors"
                                >
                                    {g.label}
                                    <CaretDown size={10} weight="bold" className={`transition-transform ${open ? "rotate-180" : ""}`} />
                                </button>
                            ) : (
                                <div className="px-3 py-1.5 label-overline" data-testid={`settings-group-${g.key}`}>{g.label}</div>
                            )}
                            {open && rows.map((s) => (
                                <button
                                    key={s.key}
                                    onClick={() => { navigate(`/settings/${s.key}`); onSelect?.(); }}
                                    data-testid={`settings-nav-${s.key}`}
                                    className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded text-sm transition-colors text-left ${
                                        active === s.key
                                            ? "bg-surface-active text-ink"
                                            : "text-ink-muted hover:bg-surface-hover hover:text-ink"
                                    }`}
                                >
                                    {s.icon} {s.label}
                                </button>
                            ))}
                        </div>
                    );
                })}
            </nav>
        </>
    );

    return (
        <div className="flex h-[calc(100vh-3.5rem)] overflow-hidden" data-testid="settings-page">
            {/* Mobile nav bar */}
            <div className="md:hidden fixed top-14 left-0 right-0 z-20 bg-[#0E0E11] border-b border-strokes px-4 h-11 flex items-center justify-between">
                <button
                    onClick={() => setSidebarOpen((v) => !v)}
                    className="flex items-center gap-2 text-sm text-ink-muted hover:text-ink transition-colors"
                >
                    <List size={16} />
                    <span className="font-medium text-ink">{activeSectionLabel}</span>
                    <CaretDown size={12} className={`transition-transform ${sidebarOpen ? "rotate-180" : ""}`} />
                </button>
            </div>

            {/* Mobile sidebar overlay */}
            {sidebarOpen && (
                <div className="md:hidden fixed inset-0 top-[calc(3.5rem+2.75rem)] z-10 bg-[#0E0E11] overflow-y-auto">
                    <SidebarNav onSelect={() => setSidebarOpen(false)} />
                </div>
            )}

            {/* Desktop sidebar */}
            <aside className="hidden md:flex w-64 border-r border-strokes bg-[#0E0E11] flex-shrink-0 overflow-y-auto flex-col" data-testid="settings-sidebar">
                <SidebarNav />
            </aside>

            {/* Content */}
            <main className="flex-1 overflow-y-auto mt-11 md:mt-0" data-testid="settings-content">
                {!activeMeta?.global && activePipeline && (
                    <PipelineScopeBanner
                        pipelineName={activePipeline.name}
                        sectionLabel={activeMeta?.label || ""}
                        hasOverride={hasOverride}
                        onReset={onResetToGlobal}
                        resetting={resetting}
                        editGlobal={effectiveEditGlobal}
                        onToggleScope={isAdmin ? () => setEditGlobal((v) => !v) : null}
                    />
                )}
                <div className="p-4 md:p-8">
                    {active === "recruiter-profile" && (
                        <RecruiterProfileSection settings={settings} onSaved={refresh} pipelineId={pipelineScope} />
                    )}
                    {active === "team" && isAdmin && <TeamSection />}
                    {active === "analyst-access" && isAdmin && <AnalystAccessSection />}
                    {active === "pipelines" && isAdmin && <PipelinesSection />}
                    {active === "pipeline-prompts" && <PipelinePromptsSection />}
                    {active === "smart-scoring" && <SmartScoringSection />}
                    {active === "text-screening" && (
                        <TextScreeningSection
                            settings={settings} onSaved={refresh}
                            pipelineId={pipelineScope}
                            scopeName={effectiveEditGlobal ? "Global default" : (activePipeline?.name || "")}
                            readOnly={!isAdmin && !pipelineScope}
                        />
                    )}
                    {active === "screen-call-agent" && (
                        <ScreenCallAgentSection
                            settings={settings} onSaved={refresh}
                            pipelineId={pipelineScope}
                            scopeName={effectiveEditGlobal ? "Global default" : (activePipeline?.name || "")}
                            isAdmin={isAdmin}
                        />
                    )}
                    {active === "auto-dialer" && (
                        <AutoDialerSection
                            settings={settings} onSaved={refresh}
                            pipelineId={pipelineScope}
                            scopeName={effectiveEditGlobal ? "Global default" : (activePipeline?.name || "")}
                        />
                    )}
                    {active === "booking-slots" && (
                        <BookingSlotsSection settings={settings} onSaved={refresh} pipelineId={pipelineScope} />
                    )}
                    {active === "no-show-revival" && (
                        <NoShowRevivalSection settings={settings} onSaved={refresh} pipelineId={pipelineScope} />
                    )}
                    {active === "inbound-agent" && (
                        <InboundAgentSection pipelineId={pipelineScope} />
                    )}
                    {active === "email-intake" && isAdmin && (
                        <EmailIntakeSection settings={settings} onSaved={refresh} />
                    )}
                    {active === "integrations" && (
                        <IntegrationsSection settings={settings} onSaved={refresh} readOnly={!isAdmin} />
                    )}
                    {active === "custom-form" && (
                        <CustomFormSection settings={settings} onSaved={refresh} pipelineId={pipelineScope} />
                    )}
                    {active === "applicant-comms" && (
                        <ApplicantCommsSection settings={settings} onSaved={refresh} pipelineId={pipelineScope} />
                    )}
                    {active === "region-language" && isAdmin && (
                        <RegionLanguageSection settings={settings} onSaved={refresh} pipelineId={pipelineScope} />
                    )}
                    {active === "embed-widget" && <EmbedWidgetSection />}
                    {active === "starter-email" && isAdmin && (
                        <StarterEmailSection pipelineId={pipelineScope} />
                    )}
                    {active === "ai-stage-prompts" && isAdmin && (
                        <AiStagePromptsSection settings={settings} pipelineId={pipelineScope} onSave={refresh} />
                    )}
                </div>
            </main>
        </div>
    );
}

function PipelineScopeBanner({ pipelineName, sectionLabel, hasOverride, onReset, resetting, editGlobal, onToggleScope }) {
    return (
        <div
            className={`sticky top-0 z-10 border-b px-4 md:px-8 py-3 flex flex-wrap items-center justify-between gap-2 ${
                editGlobal
                    ? "border-amber-400/40 bg-amber-400/15"
                    : "border-strokes bg-gradient-to-r from-[rgba(139,92,246,0.18)] to-[rgba(236,72,153,0.10)] backdrop-blur-sm"
            }`}
            data-testid="pipeline-scope-banner"
            data-scope={editGlobal ? "global" : "pipeline"}
        >
            <div className="text-xs leading-relaxed min-w-0">
                {editGlobal ? (
                    <>
                        <span className="font-bold uppercase tracking-widest text-amber-300" data-testid="pipeline-scope-banner-name">
                            ⚠️ Editing the global default for {sectionLabel}
                        </span>
                        <span className="ml-2 text-[11px] text-amber-200/80">
                            — this is NOT {pipelineName}. Saves apply to every office that doesn&apos;t have its own override.
                        </span>
                    </>
                ) : (
                    <>
                        <span className="text-ink-muted">Editing {sectionLabel} for </span>
                        <span className="font-semibold text-ink" data-testid="pipeline-scope-banner-name">{pipelineName}</span>
                        {hasOverride ? (
                            <span className="ml-2 inline-block text-[9px] uppercase tracking-widest font-bold px-1.5 py-0.5 rounded bg-[rgba(139,92,246,0.20)] text-brand-primary">
                                OVERRIDE
                            </span>
                        ) : (
                            <span className="ml-2 text-[10px] text-ink-muted">— using global defaults until you save</span>
                        )}
                    </>
                )}
            </div>
            <div className="flex items-center gap-2">
                {/* Scope toggle — admins only. Click to flip between "edit
                    just this pipeline" and "edit the global default that all
                    pipelines inherit". */}
                {onToggleScope && (
                    <button
                        onClick={onToggleScope}
                        data-testid="settings-scope-toggle"
                        className={`flex items-center gap-1.5 text-[11px] px-2.5 py-1 rounded border whitespace-nowrap transition-colors ${
                            editGlobal
                                ? "border-amber-400/60 bg-amber-400/20 text-amber-200 font-semibold hover:bg-amber-400/30"
                                : "border-strokes hover:border-brand-primary hover:bg-surface-hover"
                        }`}
                        title={editGlobal
                            ? "Switch to editing only this pipeline's override"
                            : "Switch to editing the global default that applies to all offices"}
                    >
                        <Globe size={11} weight="bold" />
                        {editGlobal ? `Back to ${pipelineName} only` : "Edit Global default"}
                    </button>
                )}
                {!editGlobal && hasOverride && (
                    <button
                        onClick={onReset}
                        disabled={resetting}
                        data-testid="reset-pipeline-override-btn"
                        className="flex items-center gap-1.5 text-[11px] text-ink-muted hover:text-ink px-2 py-1 rounded hover:bg-surface-hover whitespace-nowrap disabled:opacity-50"
                    >
                        <ArrowCounterClockwise size={12} weight="bold" />
                        {resetting ? "Resetting…" : "Reset to global default"}
                    </button>
                )}
            </div>
        </div>
    );
}
