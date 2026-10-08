import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Warning, CaretDown, CaretRight, X } from "@phosphor-icons/react";
import { toast } from "sonner";
import api from "@/lib/api";

/**
 * "Needs attention" strip at the top of the dashboard.
 *
 * Intake runs unattended, so a partial ingest is otherwise silent — a candidate
 * with no phone number looks perfectly healthy on the board while the dialer
 * never calls them, and an application that only linked to its resume produces
 * no card at all.
 *
 * State-based, not event-based: an item disappears when the underlying problem is
 * fixed, not when it is read. Collapsing is remembered for the session only, so a
 * real problem comes back on the next load rather than staying dismissed forever.
 *
 * Renders nothing at all when there is nothing wrong.
 */
export default function NeedsAttentionPanel() {
    const [items, setItems] = useState([]);
    const [count, setCount] = useState(0);
    const [open, setOpen] = useState(false);
    const [hidden, setHidden] = useState(() => sessionStorage.getItem("cg:na-hidden") === "1");
    const navigate = useNavigate();

    const load = useCallback(async () => {
        try {
            const r = await api.get("/needs-attention");
            setItems(r.data.items || []);
            setCount(r.data.count || 0);
        } catch { /* silent — this panel is advisory, never blocks the board */ }
    }, []);

    useEffect(() => {
        load();
        const id = setInterval(load, 60_000);
        return () => clearInterval(id);
    }, [load]);

    const dismiss = () => {
        sessionStorage.setItem("cg:na-hidden", "1");
        setHidden(true);
    };

    if (hidden || count === 0) return null;

    return (
        <div className="border-b border-strokes bg-amber-500/[0.07]" data-testid="needs-attention">
            <div className="px-5 py-2 flex items-center gap-2">
                <Warning size={14} weight="fill" className="text-amber-400 flex-shrink-0" />
                <button
                    onClick={() => setOpen((v) => !v)}
                    data-testid="needs-attention-toggle"
                    className="flex items-center gap-1.5 text-xs font-semibold text-amber-200 hover:text-amber-100 transition-colors"
                >
                    {open ? <CaretDown size={11} weight="bold" /> : <CaretRight size={11} weight="bold" />}
                    {count} {count === 1 ? "applicant needs" : "applicants need"} attention
                </button>
                <span className="text-[11px] text-ink-muted hidden sm:inline">
                    added recently with something missing
                </span>
                <button
                    onClick={dismiss}
                    data-testid="needs-attention-dismiss"
                    title="Hide until next load"
                    className="ml-auto text-ink-muted hover:text-ink transition-colors"
                >
                    <X size={12} weight="bold" />
                </button>
            </div>

            {open && (
                <div className="max-h-[240px] overflow-y-auto border-t border-amber-500/15">
                    {items.map((it) => (
                        <div
                            key={`${it.source}-${it.id}`}
                            data-testid={`needs-attention-row-${it.id}`}
                            className="px-5 py-2 flex items-start gap-3 border-b border-strokes/40 last:border-b-0 hover:bg-surface-hover/40 transition-colors"
                        >
                            <span
                                className={`mt-1.5 w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                                    it.severity >= 3 ? "bg-rose-400" : it.severity >= 2 ? "bg-amber-400" : "bg-ink-muted"
                                }`}
                            />
                            <div className="flex-1 min-w-0">
                                <div className="text-sm leading-snug truncate">
                                    {it.name}
                                    {it.pipeline_name && (
                                        <span className="text-ink-muted text-xs"> · {it.pipeline_name}</span>
                                    )}
                                </div>
                                <div className="text-[11px] text-amber-200/80 mt-0.5">{it.summary}</div>
                                {it.reason && (
                                    <div className="text-[10px] text-ink-muted mt-0.5 line-clamp-1">{it.reason}</div>
                                )}
                            </div>
                            {it.can_dismiss ? (
                                <div className="flex items-center gap-1.5 flex-shrink-0">
                                    <button
                                        onClick={async () => {
                                            try {
                                                await api.post(`/needs-attention/dismiss/${it.id}`);
                                                setItems((xs) => xs.filter((x) => x.id !== it.id));
                                                setCount((n) => Math.max(0, n - 1));
                                                toast.success("Cleared — marked as handled");
                                            } catch (e) { toast.error(e?.response?.data?.detail || "Couldn't dismiss"); }
                                        }}
                                        data-testid={`needs-attention-dismiss-${it.id}`}
                                        title="I handled this by hand — clear it from the list"
                                        className="btn-secondary !py-1 !px-2 text-[11px] flex-shrink-0"
                                    >
                                        Handled
                                    </button>
                                </div>
                            ) : (
                                <button
                                    onClick={() => navigate(it.link)}
                                    data-testid={`needs-attention-open-${it.id}`}
                                    className="btn-secondary !py-1 !px-2.5 text-[11px] flex-shrink-0"
                                >
                                    Fix
                                </button>
                            )}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
