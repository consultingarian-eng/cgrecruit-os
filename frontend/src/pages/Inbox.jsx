import { useEffect, useRef, useState, useCallback } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import api from "@/lib/api";
import { toast } from "sonner";
import { ChatCircleDots, PaperPlaneRight, ArrowsClockwise, User, ArrowLeft, ArrowSquareOut } from "@phosphor-icons/react";

/**
 * Two-way SMS Inbox.
 *
 * Left: every SMS conversation (matched candidates AND unmatched "orphan"
 * numbers), newest-active first, with a dot on threads awaiting a reply.
 * Right: the open thread + a free-text reply box. Replies to a matched
 * candidate route through their pipeline's warmed SMS number; replies to an
 * orphan number go back out the office line they texted.
 *
 * Deep-links: /inbox?candidate=ID or /inbox?phone=+1... (the notification bell
 * points here so a texted-back reply is one click from a reply box).
 */
export default function InboxPage() {
    const [threads, setThreads] = useState([]);
    const [loadingList, setLoadingList] = useState(true);
    const [active, setActive] = useState(null); // {candidate_id?, phone?}
    const [thread, setThread] = useState(null);
    const [loadingThread, setLoadingThread] = useState(false);
    const [draft, setDraft] = useState("");
    const [sending, setSending] = useState(false);
    const [params, setParams] = useSearchParams();
    const navigate = useNavigate();
    const scrollRef = useRef(null);
    const rootRef = useRef(null);

    // Keep the whole inbox sized to the *visible* viewport so the reply box never
    // hides behind the on-screen keyboard. `100dvh` handles the browser chrome,
    // but iOS Safari overlays the keyboard without shrinking dvh — so we track the
    // VisualViewport and set the height explicitly. Header is 56px (h-14) above us.
    useEffect(() => {
        const vv = window.visualViewport;
        const el = rootRef.current;
        if (!vv || !el) return;
        const HEADER = 56;
        const apply = () => {
            const h = Math.round(vv.height) - HEADER;
            el.style.height = h > 0 ? `${h}px` : "";
        };
        apply();
        vv.addEventListener("resize", apply);
        vv.addEventListener("scroll", apply);
        return () => {
            vv.removeEventListener("resize", apply);
            vv.removeEventListener("scroll", apply);
            if (rootRef.current) rootRef.current.style.height = "";
        };
    }, []);

    const loadList = useCallback(async ({ silent = false } = {}) => {
        try {
            const r = await api.get("/sms/inbox");
            setThreads(r.data.items || []);
        } catch (e) {
            if (!silent) toast.error(e?.response?.data?.detail || "Failed to load inbox");
        } finally { setLoadingList(false); }
    }, []);

    useEffect(() => { loadList(); }, [loadList]);

    // Open a thread from the ?candidate= / ?phone= deep-link on first load.
    useEffect(() => {
        const cid = params.get("candidate");
        const ph = params.get("phone");
        if (cid) setActive({ candidate_id: cid });
        else if (ph) setActive({ phone: ph });
    }, [params]);

    const loadThread = useCallback(async (sel, { silent = false } = {}) => {
        if (!sel) return;
        if (!silent) setLoadingThread(true);
        try {
            const r = await api.get("/sms/inbox/thread", { params: sel });
            setThread(r.data);
        } catch (e) {
            // A background poll that fails must leave the open conversation alone.
            if (!silent) {
                toast.error(e?.response?.data?.detail || "Failed to load thread");
                setThread(null);
            }
        } finally { if (!silent) setLoadingThread(false); }
    }, []);

    useEffect(() => { if (active) loadThread(active); }, [active, loadThread]);

    // Most threads are handled by the LLM, but a manual back-and-forth happens in
    // place — so refresh on the app's standard 30s cadence. The thread reload is
    // silent: the spinner would blank the conversation on every tick.
    useEffect(() => {
        const id = setInterval(() => {
            loadList({ silent: true });
            if (active && !sending) loadThread(active, { silent: true });
        }, 30000);
        return () => clearInterval(id);
    }, [loadList, loadThread, active, sending]);

    // Autoscroll to newest message when a thread loads/updates — keyed on the
    // message count so a poll that returns nothing new doesn't yank the scroll.
    useEffect(() => {
        if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }, [thread?.phone, thread?.messages?.length]);

    const openThread = (t) => {
        const sel = t.candidate_id ? { candidate_id: t.candidate_id } : { phone: t.phone_e164 || t.phone };
        setActive(sel);
        setParams(t.candidate_id ? { candidate: t.candidate_id } : { phone: t.phone_e164 || t.phone });
    };

    const send = async () => {
        const body = draft.trim();
        if (!body || !thread) return;
        setSending(true);
        try {
            const payload = thread.candidate_id
                ? { candidate_id: thread.candidate_id, body }
                : { phone: thread.phone, body, from_number: thread.office_number || undefined };
            await api.post("/sms/inbox/send", payload);
            setDraft("");
            await loadThread(active);
            await loadList();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to send");
        } finally { setSending(false); }
    };

    const onKey = (e) => {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send(); }
    };

    // Back to the list (mobile single-pane view).
    const closeThread = () => {
        setActive(null);
        setThread(null);
        setParams({});
    };

    return (
        <div ref={rootRef} className="flex h-full overflow-hidden" data-testid="inbox-page">
            {/* Thread list — full-width on mobile, fixed rail on desktop. Hidden on
                mobile while a thread is open so the conversation gets the full screen. */}
            <aside className={`w-full md:w-[340px] shrink-0 border-r border-strokes flex-col ${active ? "hidden md:flex" : "flex"}`}>
                <div className="px-4 py-3 border-b border-strokes flex items-center justify-between">
                    <div className="font-heading text-sm font-semibold flex items-center gap-2">
                        <ChatCircleDots size={16} weight="duotone" /> SMS Inbox
                    </div>
                    <button onClick={loadList} className="text-ink-muted hover:text-ink" title="Refresh">
                        <ArrowsClockwise size={14} className={loadingList ? "animate-spin" : ""} />
                    </button>
                </div>
                <div className="overflow-y-auto flex-1">
                    {loadingList ? (
                        <div className="text-center py-12 text-xs text-ink-muted">Loading…</div>
                    ) : threads.length === 0 ? (
                        <div className="text-center py-12 text-xs text-ink-muted">No conversations yet.</div>
                    ) : threads.map((t) => {
                        const isActive = active && (
                            (active.candidate_id && active.candidate_id === t.candidate_id) ||
                            (active.phone && (active.phone === t.phone_e164 || active.phone === t.phone))
                        );
                        return (
                            <button
                                key={t.phone}
                                onClick={() => openThread(t)}
                                data-testid={`inbox-thread-${t.phone}`}
                                className={`w-full text-left px-4 py-3 border-b border-strokes/50 hover:bg-surface-hover transition-colors ${isActive ? "bg-[rgba(139,92,246,0.08)]" : ""}`}
                            >
                                <div className="flex items-center gap-2">
                                    {t.needs_reply && <span className="w-2 h-2 rounded-full bg-brand-primary shrink-0" />}
                                    <div className="text-sm font-medium truncate flex-1">
                                        {t.name || t.phone_e164 || `…${t.phone.slice(-4)}`}
                                    </div>
                                    {t.stage && <span className="text-[9px] uppercase tracking-wide text-ink-muted">{t.stage}</span>}
                                </div>
                                <div className="text-xs text-ink-muted mt-0.5 truncate">
                                    {t.last_direction === "in" ? "" : "You: "}{t.last_body}
                                </div>
                                <div className="text-[10px] text-ink-dim mt-1">{fmtRel(t.last_at)}{t.opted_out ? " · ⊘ opted out" : ""}</div>
                            </button>
                        );
                    })}
                </div>
            </aside>

            {/* Conversation — hidden on mobile until a thread is picked (single-pane). */}
            <section className={`flex-1 flex-col min-w-0 ${active ? "flex" : "hidden md:flex"}`}>
                {!active ? (
                    <div className="flex-1 flex items-center justify-center text-sm text-ink-muted">
                        Select a conversation to view and reply.
                    </div>
                ) : (
                    <>
                        <div className="px-4 md:px-6 py-3 border-b border-strokes flex items-center gap-3">
                            <button
                                onClick={closeThread}
                                className="md:hidden -ml-1 mr-0.5 p-1 text-ink-muted hover:text-ink shrink-0"
                                aria-label="Back to inbox"
                                data-testid="inbox-back-btn"
                            >
                                <ArrowLeft size={20} />
                            </button>
                            <div className="w-8 h-8 rounded-full bg-surface border border-strokes flex items-center justify-center">
                                <User size={15} className="text-ink-muted" />
                            </div>
                            <div className="min-w-0">
                                {thread?.candidate_id ? (
                                    <button
                                        onClick={() => navigate(`/?candidate=${thread.candidate_id}`)}
                                        title="Open candidate record"
                                        data-testid="inbox-open-candidate"
                                        className="flex items-center gap-1 max-w-full text-sm font-semibold hover:text-brand-primary transition-colors"
                                    >
                                        <span className="truncate">{thread.name || thread.phone}</span>
                                        <ArrowSquareOut size={12} className="shrink-0" />
                                    </button>
                                ) : (
                                    <div className="text-sm font-semibold truncate">{thread?.name || thread?.phone || "…"}</div>
                                )}
                                <div className="text-[11px] text-ink-muted truncate">
                                    {thread?.phone}{thread?.stage ? ` · ${thread.stage}` : ""}{!thread?.candidate_id && thread ? " · unmatched number" : ""}
                                </div>
                            </div>
                        </div>

                        <div ref={scrollRef} className="flex-1 overflow-y-auto overflow-x-hidden px-4 md:px-6 py-4 space-y-2">
                            {loadingThread ? (
                                <div className="flex justify-center py-8"><ArrowsClockwise size={18} className="animate-spin text-ink-muted" /></div>
                            ) : !thread?.messages?.length ? (
                                <div className="text-center text-sm text-ink-muted py-8">No messages yet.</div>
                            ) : thread.messages.map((m, i) => {
                                const isOut = m.direction === "out";
                                return (
                                    <div key={i} className={`flex ${isOut ? "justify-end" : "justify-start"}`}>
                                        <div className={`max-w-[85%] md:max-w-[70%] min-w-0 rounded-2xl px-3 py-2 text-sm ${isOut ? "bg-brand-primary text-white rounded-tr-sm" : "bg-surface border border-strokes text-ink rounded-tl-sm"}`}>
                                            {m.was_llm_reply && <div className={`text-[10px] font-bold mb-0.5 ${isOut ? "text-blue-200" : "text-ink-muted"}`}>🤖 AI</div>}
                                            <div className="leading-snug whitespace-pre-wrap break-words [overflow-wrap:anywhere]">{m.body}</div>
                                            <div className={`text-[10px] mt-1 ${isOut ? "text-blue-200" : "text-ink-dim"}`}>{fmtRel(m.timestamp)}</div>
                                        </div>
                                    </div>
                                );
                            })}
                        </div>

                        <div className="px-4 md:px-6 pb-5 pt-3 border-t border-strokes">
                            {thread?.opted_out && (
                                <div className="mb-2 text-[11px] text-red-400">This number replied STOP — they won't receive messages.</div>
                            )}
                            <div className="flex gap-2 items-end">
                                <textarea
                                    value={draft}
                                    onChange={(e) => setDraft(e.target.value)}
                                    onKeyDown={onKey}
                                    placeholder="Type a reply…  (⌘/Ctrl + Enter to send)"
                                    rows={2}
                                    disabled={thread?.opted_out}
                                    data-testid="inbox-reply-input"
                                    className="flex-1 resize-none rounded-lg bg-surface border border-strokes px-3 py-2 text-sm text-ink placeholder:text-ink-dim focus:outline-none focus:border-brand-primary disabled:opacity-50"
                                />
                                <button
                                    onClick={send}
                                    disabled={sending || !draft.trim() || thread?.opted_out}
                                    data-testid="inbox-send-btn"
                                    className="btn-primary h-10 px-4 flex items-center gap-2 text-sm disabled:opacity-50"
                                >
                                    {sending ? <ArrowsClockwise size={14} className="animate-spin" /> : <PaperPlaneRight size={14} weight="fill" />}
                                    Send
                                </button>
                            </div>
                        </div>
                    </>
                )}
            </section>
        </div>
    );
}

function fmtRel(iso) {
    if (!iso) return "";
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return "";
    const diff = (Date.now() - t) / 1000;
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
    return new Date(iso).toLocaleDateString();
}
