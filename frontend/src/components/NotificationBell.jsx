import { useEffect, useState, useRef } from "react";
import { Bell, BellRinging, Check } from "@phosphor-icons/react";
import { useNavigate } from "react-router-dom";
import api from "@/lib/api";

/**
 * In-app notification bell.
 *
 * Polls /api/notifications every 30 seconds for the unread count + the
 * latest 30 notifications. Click → opens a dropdown showing the activity
 * feed; clicking a notification marks it read and navigates to the linked
 * route (typically /?candidate=ID to open that candidate's drawer).
 *
 * NOTE: kept LIGHTWEIGHT — no react-query, no global state. The local poll
 * is fine until we have hundreds of recruiters. Upgrade to SSE/websockets
 * when scaling.
 */
export default function NotificationBell() {
    const [items, setItems] = useState([]);
    const [unread, setUnread] = useState(0);
    const [open, setOpen] = useState(false);
    const popRef = useRef(null);
    const navigate = useNavigate();

    const fetchData = async () => {
        try {
            const r = await api.get("/notifications", { params: { limit: 30 } });
            setItems(r.data.items || []);
            setUnread(r.data.unread || 0);
        } catch { /* silent — bell is best-effort */ }
    };

    useEffect(() => {
        fetchData();
        const id = setInterval(fetchData, 30_000);
        return () => clearInterval(id);
    }, []);

    // Close popover on outside click / ESC
    useEffect(() => {
        if (!open) return;
        const onDoc = (e) => { if (popRef.current && !popRef.current.contains(e.target)) setOpen(false); };
        const onEsc = (e) => { if (e.key === "Escape") setOpen(false); };
        document.addEventListener("mousedown", onDoc);
        document.addEventListener("keydown", onEsc);
        return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onEsc); };
    }, [open]);

    const onItemClick = async (n) => {
        // Optimistic mark-read
        if (!n.read) {
            setItems((prev) => prev.map((x) => x.id === n.id ? { ...x, read: true } : x));
            setUnread((u) => Math.max(0, u - 1));
            api.post(`/notifications/${n.id}/read`).catch(() => { });
        }
        if (n.link) navigate(n.link);
        setOpen(false);
    };

    const markAllRead = async () => {
        setItems((prev) => prev.map((x) => ({ ...x, read: true })));
        setUnread(0);
        try { await api.post("/notifications/read-all"); } catch { /* no-op */ }
    };

    const Icon = unread > 0 ? BellRinging : Bell;

    return (
        <div className="relative" ref={popRef}>
            <button
                onClick={() => setOpen((v) => !v)}
                data-testid="notif-bell-btn"
                className="relative w-9 h-9 rounded-md hover:bg-surface-hover transition-colors flex items-center justify-center"
                title={unread > 0 ? `${unread} new` : "Notifications"}
            >
                <Icon size={18} weight={unread > 0 ? "fill" : "regular"} className={unread > 0 ? "text-brand-primary" : "text-ink-muted"} />
                {unread > 0 && (
                    <span
                        data-testid="notif-bell-badge"
                        className="absolute -top-0.5 -right-0.5 min-w-[16px] h-4 px-1 rounded-full text-[9px] font-bold leading-none flex items-center justify-center bg-pink-500 text-white"
                    >
                        {unread > 99 ? "99+" : unread}
                    </span>
                )}
            </button>
            {open && (
                <div
                    data-testid="notif-bell-dropdown"
                    className="absolute right-0 top-full mt-2 w-[380px] max-h-[480px] surface shadow-2xl z-50 flex flex-col overflow-hidden"
                >
                    <div className="flex items-center justify-between border-b border-strokes px-4 py-3">
                        <div className="font-heading text-sm font-semibold">Notifications</div>
                        {unread > 0 && (
                            <button onClick={markAllRead} data-testid="notif-mark-all-read" className="text-[11px] text-ink-muted hover:text-ink flex items-center gap-1">
                                <Check size={11} weight="bold" /> Mark all read
                            </button>
                        )}
                    </div>
                    <div className="overflow-y-auto flex-1">
                        {items.length === 0 ? (
                            <div className="text-center py-12 text-xs text-ink-muted">You're all caught up. ✨</div>
                        ) : items.map((n) => <NotificationRow key={n.id} n={n} onClick={() => onItemClick(n)} />)}
                    </div>
                </div>
            )}
        </div>
    );
}

function NotificationRow({ n, onClick }) {
    return (
        <button
            onClick={onClick}
            data-testid={`notif-row-${n.id}`}
            className={`w-full text-left px-4 py-3 border-b border-strokes/50 hover:bg-surface-hover transition-colors ${n.read ? "" : "bg-[rgba(139,92,246,0.06)]"}`}
        >
            <div className="flex items-start gap-2.5">
                <div className={`w-1.5 h-1.5 rounded-full mt-1.5 flex-shrink-0 ${n.read ? "bg-strokes" : "bg-brand-primary"}`} />
                <div className="flex-1 min-w-0">
                    <div className="text-sm leading-snug">{n.title}</div>
                    {n.body && <div className="text-xs text-ink-muted mt-0.5 leading-relaxed line-clamp-2">{n.body}</div>}
                    <div className="text-[10px] text-ink-muted mt-1">{formatRelative(n.created_at)}</div>
                </div>
            </div>
        </button>
    );
}

function formatRelative(iso) {
    if (!iso) return "";
    const t = new Date(iso).getTime();
    const diff = (Date.now() - t) / 1000; // seconds
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
    return new Date(iso).toLocaleDateString();
}
