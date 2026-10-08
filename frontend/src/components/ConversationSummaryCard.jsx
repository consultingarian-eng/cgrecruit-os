import { useState } from "react";
import { fmtET } from "@/lib/formatET";
import { usePipeline } from "@/lib/pipeline";
import api from "@/lib/api";
import { toast } from "sonner";
import {
    ArrowsClockwise,
    ChartBar,
    Clock,
    Robot,
} from "@phosphor-icons/react";
import { VerdictBadge } from "./KanbanBoard";
import CallAudioPlayer from "./CallAudioPlayer";

/**
 * Cube-style "Conversation with X" card — sits at the top of the drawer,
 * auto-loads audio, shows summary + transcript + phone metadata in tabs.
 *
 * Decoupled from CandidateDrawer so it stays focused on its single job:
 * presenting the latest conversation. Receives candidate + conversations as
 * props; emits onSync when the recruiter wants to refresh the latest call
 * from ElevenLabs.
 */
export default function ConversationSummaryCard({ candidate, conversations, onSync, syncing }) {
    const [activeTab, setActiveTab] = useState("overview");
    const [findingAudio, setFindingAudio] = useState(false);
    const latest = (conversations || [])[0];
    if (!latest) return null;

    const hasEleven = !!latest.elevenlabs_conversation_id;
    const isComplete = latest.status === "completed" && (latest.transcript?.length || 0) > 0;
    const isLive = ["in_progress", "initiated", "ringing", "queued"].includes(latest.status);
    const turns = latest.transcript || [];
    const dur = latest.duration_seconds;
    const minutes = dur ? Math.floor(dur / 60) : 0;
    const seconds = dur ? dur % 60 : 0;

    const tryFindAudio = async () => {
        setFindingAudio(true);
        try {
            const r = await api.post(`/candidates/${candidate.id}/find-audio`);
            if (r.data?.matched > 0) {
                toast.success(`Audio recovered — ${r.data.matched} match${r.data.matched > 1 ? "es" : ""}`);
                await onSync();
            } else if ((r.data?.details || []).some((d) => d.ambiguous)) {
                toast.error("Found nearby ElevenLabs calls but none matched closely enough");
            } else {
                toast.error("No matching ElevenLabs conversation found within ±10 min");
            }
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Lookup failed");
        } finally { setFindingAudio(false); }
    };

    return (
        <div className="border-b border-strokes bg-[#0E0E11]" data-testid="conversation-summary-card">
            <div className="px-6 py-4">
                <div className="flex items-start justify-between gap-3 mb-3">
                    <div>
                        <div className="flex items-center gap-2">
                            <h3 className="font-heading text-base font-semibold">
                                Conversation with {candidate.first_name}
                            </h3>
                            {isComplete && (
                                <span className="text-[9px] font-bold uppercase tracking-widest px-1.5 py-0.5 rounded bg-[rgba(16,185,129,0.15)] text-brand-success">
                                    DONE
                                </span>
                            )}
                            {latest.status === "initiated" && (
                                <span className="text-[9px] font-bold uppercase tracking-widest px-1.5 py-0.5 rounded bg-[rgba(245,158,11,0.15)] text-[#FBBF24] flex items-center gap-1">
                                    <span className="relative flex h-1 w-1">
                                        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#FBBF24] opacity-75"></span>
                                        <span className="relative inline-flex rounded-full h-1 w-1 bg-[#FBBF24]"></span>
                                    </span>
                                    LIVE
                                </span>
                            )}
                        </div>
                    </div>
                    <button
                        onClick={onSync}
                        disabled={syncing}
                        data-testid="conversation-sync-btn"
                        className="text-[11px] text-ink-muted hover:text-ink flex items-center gap-1 px-2 py-1 rounded hover:bg-surface-hover"
                    >
                        <ArrowsClockwise size={11} weight="bold" className={syncing ? "animate-spin" : ""} />
                        {syncing ? "Syncing…" : "Sync"}
                    </button>
                </div>

                {/* Audio player — only if ElevenLabs conversation ID was captured */}
                {isComplete && hasEleven && (
                    <CallAudioPlayer
                        conversationId={latest.elevenlabs_conversation_id}
                        testid={`top-audio-${latest.id}`}
                        autoLoad={true}
                    />
                )}
                {isComplete && !hasEleven && (
                    <div className="rounded-md border border-dashed border-strokes p-2.5 text-[11px] text-ink-muted text-center mb-3 flex items-center justify-center gap-2 flex-wrap">
                        <span>Audio playback unavailable for this call (ElevenLabs conversation ID was not captured during streaming).</span>
                        <button
                            onClick={tryFindAudio}
                            disabled={findingAudio}
                            data-testid="find-audio-btn"
                            className="text-brand-primary hover:underline inline-flex items-center gap-1"
                        >
                            <ArrowsClockwise size={11} weight="bold" className={findingAudio ? "animate-spin" : ""} />
                            {findingAudio ? "Searching ElevenLabs…" : "Try to recover audio"}
                        </button>
                    </div>
                )}
                {!isComplete && isLive && (
                    <div className="rounded-md border border-dashed border-strokes p-3 text-xs text-ink-muted text-center mb-3 flex items-center justify-center gap-2">
                        <span className="relative flex h-1.5 w-1.5">
                            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#FBBF24] opacity-75"></span>
                            <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-[#FBBF24]"></span>
                        </span>
                        Call in progress — transcript will appear automatically once it ends.
                    </div>
                )}
                {!isComplete && !isLive && (
                    <div className="rounded-md border border-dashed border-strokes p-3 text-xs text-ink-muted text-center mb-3">
                        {latest.status === "no_answer"
                            ? "Voicemail or no answer — no transcript recorded for this attempt."
                            : "Call ended. Press Sync if transcript hasn't appeared yet."}
                    </div>
                )}

                {/* Compact metrics row */}
                {isComplete && (
                    <div className="flex items-center gap-4 text-[11px] text-ink-muted mb-3 flex-wrap">
                        {dur > 0 && (
                            <span className="flex items-center gap-1">
                                <Clock size={11} /> {minutes}:{seconds.toString().padStart(2, "0")}
                            </span>
                        )}
                        <span className="flex items-center gap-1">
                            <Robot size={11} /> {turns.length} turns
                        </span>
                        {latest.suitability_score != null && (
                            <span className="flex items-center gap-1">
                                <ChartBar size={11} /> Suitability {latest.suitability_score}/100
                            </span>
                        )}
                        {candidate.verdict && (
                            <VerdictBadge verdict={candidate.verdict} />
                        )}
                    </div>
                )}

                {/* Tabs row — Overview / Transcription / Phone */}
                {isComplete && (
                    <div className="flex items-center gap-4 text-xs border-b border-strokes -mx-6 px-6">
                        <ConvTab id="overview" active={activeTab} onClick={setActiveTab}>Overview</ConvTab>
                        <ConvTab id="transcription" active={activeTab} onClick={setActiveTab}>Transcription</ConvTab>
                        <ConvTab id="phone" active={activeTab} onClick={setActiveTab}>Phone call</ConvTab>
                    </div>
                )}
            </div>

            {/* Tab body */}
            {isComplete && (
                <div className="px-6 pb-5 max-h-96 overflow-y-auto">
                    {activeTab === "overview" && (
                        <OverviewPane latest={latest} candidate={candidate} />
                    )}
                    {activeTab === "transcription" && (
                        <TranscriptionPane turns={turns} candidate={candidate} />
                    )}
                    {activeTab === "phone" && (
                        <PhonePane latest={latest} candidate={candidate} dur={dur} minutes={minutes} seconds={seconds} turns={turns} />
                    )}
                </div>
            )}
        </div>
    );
}

function ConvTab({ id, active, onClick, children }) {
    const isActive = active === id;
    return (
        <button
            onClick={() => onClick(id)}
            data-testid={`conv-tab-${id}`}
            className={`pb-2 -mb-px border-b-2 transition-colors ${
                isActive
                    ? "border-brand-primary text-ink"
                    : "border-transparent text-ink-muted hover:text-ink"
            }`}
        >
            {children}
        </button>
    );
}

function OverviewPane({ latest, candidate }) {
    return (
        <div data-testid="conv-pane-overview">
            <div className="label-overline mb-1.5 mt-1">Summary</div>
            <p className="text-sm text-ink leading-relaxed mb-4">
                {latest.summary || candidate.call_summary || "—"}
            </p>
            {candidate.smart_score_rationale && (
                <>
                    <div className="label-overline mb-1.5">Why this verdict</div>
                    <p className="text-xs text-ink-muted leading-relaxed">{candidate.smart_score_rationale}</p>
                </>
            )}
        </div>
    );
}

function TranscriptionPane({ turns, candidate }) {
    return (
        <div className="space-y-2" data-testid="conv-pane-transcription">
            {turns.map((m, i) => {
                const isAgent = m.role === "agent" || m.role === "assistant";
                // Composite key keeps each bubble stable even if `text` is duplicated.
                return (
                    <div key={`${m.role}-${i}`} className={`flex gap-2 ${isAgent ? "" : "flex-row-reverse"}`}>
                        <div className={`flex-shrink-0 w-6 h-6 rounded-full flex items-center justify-center text-[9px] font-bold ${
                            isAgent ? "bg-brand-primary/20 text-brand-primary" : "bg-surface-active text-ink"
                        }`}>
                            {isAgent ? <Robot size={11} weight="bold" /> : (candidate.first_name?.[0] || "U")}
                        </div>
                        <div className={`flex-1 max-w-[85%] text-xs leading-relaxed rounded-lg px-2.5 py-1.5 ${
                            isAgent ? "bg-surface-hover border border-strokes" : "bg-brand-primary/10 border border-brand-primary/30"
                        }`}>
                            {m.text}
                        </div>
                    </div>
                );
            })}
        </div>
    );
}

function PhonePane({ latest, candidate, dur, minutes, seconds, turns }) {
    const { timezone } = usePipeline() || {};
    return (
        <div className="text-xs text-ink-muted space-y-1.5" data-testid="conv-pane-phone">
            <Row label="Status" value={latest.status} />
            <Row label="Duration" value={dur ? `${minutes}:${seconds.toString().padStart(2, "0")}` : "—"} />
            <Row label="Started" value={fmtET(latest.created_at, { timeZone: timezone })} />
            <Row label="Transcript turns" value={turns.length} />
            <Row label="Smart score" value={candidate.smart_score ?? "—"} />
        </div>
    );
}

function Row({ label, value }) {
    return (
        <div className="flex justify-between">
            <span>{label}</span>
            <span className="text-ink">{value}</span>
        </div>
    );
}
