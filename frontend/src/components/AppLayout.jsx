import { Outlet, NavLink, useNavigate, useLocation } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import { usePipeline } from "@/lib/pipeline";
import {
    Briefcase, MagnifyingGlass, Funnel, GearSix, Plus, ChartLineUp,
    Calendar, Kanban, SignOut, CaretDown, UserCircle, Copy, CardsThree,
    List, X, ChartBar, Brain, ChatCircleDots, Phone,
} from "@phosphor-icons/react";
import { useState, useEffect, useRef } from "react";
import {
    DropdownMenu, DropdownMenuContent, DropdownMenuItem,
    DropdownMenuTrigger, DropdownMenuSeparator, DropdownMenuLabel,
} from "@/components/ui/dropdown-menu";
import api from "@/lib/api";
import { toast } from "sonner";
import NotificationBell from "@/components/NotificationBell";
import AddApplicantModal from "@/components/AddApplicantModal";
import { DialogHost, confirmDialog, promptDialog } from "@/components/ConfirmDialog";
import BRAND from "@/lib/brand";

// One list for both navs — desktop and mobile were hand-maintained in parallel and
// had already drifted apart.
const NAV_ITEMS = [
    { to: "/", Icon: Kanban, label: "Pipeline", testid: "nav-pipeline", hideForAnalyst: true },
    { to: "/inbox", Icon: ChatCircleDots, label: "Inbox", testid: "nav-inbox", hideForAnalyst: true },
    { to: "/queue", Icon: Phone, label: "Call Queue", testid: "nav-queue", hideForAnalyst: true },
    { to: "/intelligence", Icon: ChartLineUp, label: "Intelligence", testid: "nav-intelligence" },
    { to: "/report", Icon: ChartBar, label: "Weekly Report", testid: "nav-report" },
    { to: "/calendar", Icon: Calendar, label: "Calendar", testid: "nav-calendar", hideForAnalyst: true },
];

export default function AppLayout() {
    const { user, logout, isSuperAdmin, isViewer, isAnalyst, canMutate } = useAuth();
    const { pipelines, activePipelineId, setActive, refresh } = usePipeline();
    const navigate = useNavigate();
    const [creatingPipeline, setCreatingPipeline] = useState(false);
    const [newPipelineName, setNewPipelineName] = useState("");
    const [searchValue, setSearchValue] = useState("");
    const [mobileOpen, setMobileOpen] = useState(false);
    const [showAdd, setShowAdd] = useState(false);
    const [jobs, setJobs] = useState([]);
    const searchRef = useRef(null);
    const { pathname } = useLocation();

    useEffect(() => {
        if (!canMutate) return;
        const onAdd = () => setShowAdd(true);
        window.addEventListener("cgrecruit:open-add-applicant", onAdd);
        return () => window.removeEventListener("cgrecruit:open-add-applicant", onAdd);
    }, [canMutate]);

    // Cleared first, on every office change as well as every open: the modal is
    // never unmounted and seeds its job select from whatever list it is handed,
    // so a list left over from the previous office would tag the new office's
    // candidate with the old office's job — and job_id is only checked against
    // user_id server-side, so nothing downstream catches it. `alive` closes the
    // same hole from the other end: a slow response from the office we left.
    useEffect(() => {
        setJobs([]);
        if (!showAdd || !activePipelineId) return;
        let alive = true;
        api.get("/jobs", { params: { pipeline_id: activePipelineId } })
            .then((r) => { if (alive) setJobs(r.data); })
            .catch(() => {});
        return () => { alive = false; };
    }, [showAdd, activePipelineId]);
    const isAdmin = isSuperAdmin;

    // Listen for external resets (clear-filters chip on Dashboard) so the
    // input visually reflects the cleared search state.
    useEffect(() => {
        const onClear = () => setSearchValue("");
        window.addEventListener("cgrecruit:clear-search", onClear);
        return () => window.removeEventListener("cgrecruit:clear-search", onClear);
    }, []);

    // Dashboard is the only listener for cgrecruit:search, so the shortcut is offered
    // only where the box actually filters something.
    const searchShortcut = !isAnalyst && (pathname === "/" || pathname === "/dashboard");

    useEffect(() => {
        if (!searchShortcut) return;
        const onKey = (e) => {
            const el = e.target;
            if (el?.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el?.tagName)) return;
            // Radix traps focus inside open dialogs, sheets and menus; pulling focus to the
            // header behind the overlay fights that.
            if (document.querySelector("[role=dialog], [role=alertdialog]")) return;
            const slash = e.key === "/" && !e.metaKey && !e.ctrlKey && !e.altKey;
            const cmdK = (e.key === "k" || e.key === "K") && (e.metaKey || e.ctrlKey);
            if (!slash && !cmdK) return;
            const input = searchRef.current;
            // Hidden below md, where focus() would be a no-op anyway.
            if (!input || input.offsetParent === null) return;
            e.preventDefault();
            input.focus();
            input.select();
        };
        document.addEventListener("keydown", onKey);
        return () => document.removeEventListener("keydown", onKey);
    }, [searchShortcut]);

    const navItems = NAV_ITEMS.filter((i) => !(isAnalyst && i.hideForAnalyst));

    const activePipeline = pipelines.find((p) => p.id === activePipelineId);

    const createPipeline = async () => {
        if (!newPipelineName.trim()) return;
        try {
            const r = await api.post("/pipelines", { name: newPipelineName.trim() });
            setNewPipelineName("");
            setCreatingPipeline(false);
            await refresh();
            setActive(r.data.id);
            toast.success(`Pipeline "${r.data.name}" created`);
        } catch {
            toast.error("Failed to create pipeline");
        }
    };

    const duplicatePipeline = async (sourcePipeline) => {
        const newName = await promptDialog({
            title: `Duplicate "${sourcePipeline.name}"`,
            description: "Name the new office — jobs, availability, and a fresh AI agent are cloned over.",
            placeholder: "e.g. Riverside",
            confirmLabel: "Duplicate",
        });
        if (!newName) return;
        const t = toast.loading(`Cloning ${sourcePipeline.name} → ${newName.trim()}…`);
        try {
            const r = await api.post(`/pipelines/${sourcePipeline.id}/duplicate`, {
                name: newName.trim(),
            });
            await refresh();
            setActive(r.data.id);
            toast.dismiss(t);
            toast.success(`"${r.data.name}" created — fresh ElevenLabs agent provisioned + jobs/availability cloned`);
        } catch (e) {
            toast.dismiss(t);
            toast.error(e?.response?.data?.detail || "Duplicate failed");
        }
    };

    return (
        <div className="h-[100dvh] overflow-hidden flex flex-col bg-[#0C0C0E] text-ink">
            {/* Top Nav */}
            <header
                className="sticky top-0 z-40 bg-[#0C0C0E]/85 backdrop-blur-xl border-b border-strokes px-4 md:px-5 h-14 flex items-center justify-between"
                data-testid="top-nav"
            >
                <div className="flex items-center gap-3 md:gap-5 min-w-0">
                    <div className="flex items-center gap-2 cursor-pointer shrink-0" onClick={() => navigate("/")} title="Back to pipeline">
                        <img src={BRAND.logo} alt={BRAND.name} className="w-7 h-7 object-contain" />
                        <span className="font-heading text-base font-bold tracking-tight hidden sm:inline">{BRAND.name}</span>
                    </div>

                    {/* Pipeline switcher — hidden for analysts */}
                    {isAnalyst && (
                        <span className="hidden md:inline text-xs text-ink-muted px-2">Analytics View</span>
                    )}
                    <DropdownMenu className={isAnalyst ? "hidden" : ""}>
                        <DropdownMenuTrigger asChild>
                            <button
                                data-testid="pipeline-switcher"
                                className="flex items-center gap-1.5 md:gap-2 px-2 md:px-3 py-1.5 rounded-md hover:bg-surface-hover transition-colors text-sm min-w-0 max-w-[160px] md:max-w-none"
                            >
                                <span className="text-ink-muted text-xs hidden md:inline">Pipeline</span>
                                <span className="font-medium truncate">{activePipeline?.name || "—"}</span>
                                <CaretDown size={12} weight="bold" className="text-ink-muted shrink-0" />
                            </button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent className="w-72 bg-[#141519] border-strokes">
                            <DropdownMenuLabel className="text-[10px] uppercase tracking-widest text-ink-muted">Pipelines</DropdownMenuLabel>
                            {pipelines.map((p) => (
                                <DropdownMenuItem
                                    key={p.id}
                                    data-testid={`pipeline-option-${p.id}`}
                                    onClick={() => setActive(p.id)}
                                    className={`cursor-pointer flex-col items-start gap-1 ${p.id === activePipelineId ? "bg-surface-active" : ""}`}
                                >
                                    <div className="flex items-center justify-between w-full">
                                        <span className="font-medium">{p.name}</span>
                                        <div className="flex items-center gap-2">
                                            {p.id === activePipelineId && <span className="text-[9px] uppercase tracking-widest text-brand-primary">Active</span>}
                                            {isAdmin && (
                                                <button
                                                    onClick={(e) => {
                                                        e.stopPropagation();
                                                        duplicatePipeline(p);
                                                    }}
                                                    data-testid={`duplicate-pipeline-${p.id}`}
                                                    title="Duplicate this office"
                                                    className="text-ink-muted hover:text-brand-primary p-0.5 rounded"
                                                >
                                                    <Copy size={11} weight="bold" />
                                                </button>
                                            )}
                                        </div>
                                    </div>
                                    {p.twilio_phone_number && (
                                        <div className="text-[10px] font-mono text-ink-muted">SMS from {p.twilio_phone_number}</div>
                                    )}
                                </DropdownMenuItem>
                            ))}
                            <DropdownMenuSeparator />
                            {activePipeline?.public_slug && (
                                <DropdownMenuItem
                                    onClick={() => {
                                        const url = `${window.location.origin}/apply/${activePipeline.public_slug}`;
                                        navigator.clipboard.writeText(url);
                                        toast.success("Apply link copied!");
                                    }}
                                    data-testid="copy-apply-link"
                                    className="cursor-pointer text-brand-primary"
                                >
                                    Copy public Apply link
                                </DropdownMenuItem>
                            )}
                            {isAdmin && (creatingPipeline ? (
                                <div className="p-2 space-y-2">
                                    <input
                                        data-testid="new-pipeline-name-input"
                                        autoFocus
                                        value={newPipelineName}
                                        onChange={(e) => setNewPipelineName(e.target.value)}
                                        onKeyDown={(e) => e.key === "Enter" && createPipeline()}
                                        placeholder="Pipeline name"
                                        className="input-dark"
                                    />
                                    <div className="flex gap-2">
                                        <button data-testid="confirm-create-pipeline" onClick={createPipeline} className="btn-primary flex-1 !py-1.5 text-xs">Create</button>
                                        <button onClick={() => setCreatingPipeline(false)} className="btn-secondary flex-1 !py-1.5 text-xs">Cancel</button>
                                    </div>
                                </div>
                            ) : (
                                <DropdownMenuItem onClick={() => setCreatingPipeline(true)} data-testid="create-pipeline-trigger" className="cursor-pointer text-brand-primary">
                                    <Plus size={14} weight="bold" className="mr-2" /> New pipeline
                                </DropdownMenuItem>
                            ))}
                        </DropdownMenuContent>
                    </DropdownMenu>

                    {/* Tabs — desktop only */}
                    <nav className="hidden md:flex items-center gap-1">
                        {navItems.map(({ to, Icon, label, testid }) => (
                            <NavTab key={to} to={to} icon={<Icon size={15} weight="duotone" />} label={label} testid={testid} />
                        ))}
                    </nav>
                </div>

                <div className="flex items-center gap-1.5 md:gap-2">
                    {/* Search — desktop only, hidden for analysts */}
                    {!isAnalyst && (
                        <div className="hidden md:flex items-center gap-2 px-3 py-1.5 rounded-md bg-surface border border-strokes w-64">
                            <MagnifyingGlass size={14} className="text-ink-muted" />
                            <input
                                ref={searchRef}
                                data-testid="global-search-input"
                                placeholder={searchShortcut ? "Search candidates…  /" : "Search candidates…"}
                                className="bg-transparent text-sm flex-1 outline-none placeholder:text-ink-muted"
                                value={searchValue}
                                onChange={(e) => {
                                    setSearchValue(e.target.value);
                                    window.dispatchEvent(new CustomEvent("cgrecruit:search", { detail: e.target.value }));
                                }}
                                onKeyDown={(e) => {
                                    if (e.key !== "Escape") return;
                                    // cgrecruit:clear-search is Dashboard -> header only; the board
                                    // needs the search event to actually drop the filter.
                                    setSearchValue("");
                                    window.dispatchEvent(new CustomEvent("cgrecruit:search", { detail: "" }));
                                    e.currentTarget.blur();
                                }}
                            />
                        </div>
                    )}
                    <div className="hidden md:flex items-center gap-1.5">
                        {isViewer && (
                            <span className="text-[10px] uppercase tracking-widest px-2 py-0.5 rounded-full bg-amber-500/15 border border-amber-500/30 text-amber-400 font-semibold">
                                View Only
                            </span>
                        )}
                        {isAnalyst && (
                            <span className="text-[10px] uppercase tracking-widest px-2 py-0.5 rounded-full bg-brand-primary/15 border border-brand-primary/30 text-brand-primary font-semibold">
                                Analytics
                            </span>
                        )}
                        {!isAnalyst && <IconBtn testid="header-filter-btn" label="Filter candidates" icon={<Funnel size={15} weight="duotone" />} onClick={() => window.dispatchEvent(new CustomEvent("cgrecruit:open-filters"))} />}
                        {/* Notifications name candidates; analyst accounts get figures only. */}
                        {!isAnalyst && <NotificationBell />}
                        {canMutate && (
                            <button
                                data-testid="header-add-applicant-btn"
                                onClick={() => window.dispatchEvent(new CustomEvent("cgrecruit:open-add-applicant"))}
                                className="btn-primary flex items-center gap-1.5 !px-3 !py-1.5"
                            >
                                <Plus size={14} weight="bold" /> Add Applicant
                            </button>
                        )}
                        {!isViewer && !isAnalyst && <IconBtn testid="header-settings-btn" label="Settings" icon={<GearSix size={15} weight="duotone" />} onClick={() => navigate("/settings")} />}
                        <DropdownMenu>
                            <DropdownMenuTrigger asChild>
                                <button data-testid="user-menu-trigger" className="ml-1 p-1 rounded-md hover:bg-surface-hover">
                                    <UserCircle size={26} weight="duotone" className="text-ink-muted" />
                                </button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent className="w-56 bg-[#141519] border-strokes" align="end">
                                <DropdownMenuLabel className="text-xs">
                                    <div className="font-medium text-ink">{user?.name}</div>
                                    <div className="text-ink-muted">{user?.email}</div>
                                </DropdownMenuLabel>
                                <DropdownMenuSeparator />
                                {canMutate && (
                                    <DropdownMenuItem onClick={() => navigate("/duplicates")} className="cursor-pointer">
                                        <CardsThree size={14} className="mr-2" /> Duplicates
                                    </DropdownMenuItem>
                                )}
                                <DropdownMenuItem onClick={() => navigate("/insights")} className="cursor-pointer">
                                    <Brain size={14} className="mr-2" /> AI Insights
                                </DropdownMenuItem>
                                {/* Both the header gear and the mobile drawer gate Settings on
                                    !isViewer, so this menu is a viewer's only way in — and the
                                    route renders for them. Widened here would be new access. */}
                                {!isAnalyst && (
                                    <DropdownMenuItem onClick={() => navigate("/settings")} className="cursor-pointer">
                                        <GearSix size={14} className="mr-2" /> Settings
                                    </DropdownMenuItem>
                                )}
                                <DropdownMenuItem onClick={logout} data-testid="logout-button" className="cursor-pointer text-brand-danger">
                                    <SignOut size={14} className="mr-2" /> Sign out
                                </DropdownMenuItem>
                            </DropdownMenuContent>
                        </DropdownMenu>
                    </div>
                    {/* Mobile: notifications + hamburger */}
                    <div className="flex md:hidden items-center gap-1">
                        <NotificationBell />
                        <button
                            onClick={() => setMobileOpen((v) => !v)}
                            className="p-1.5 rounded-md hover:bg-surface-hover text-ink-muted hover:text-ink transition-colors"
                            aria-label="Menu"
                        >
                            {mobileOpen ? <X size={20} /> : <List size={20} />}
                        </button>
                    </div>
                </div>
            </header>

            {/* Mobile slide-down menu */}
            {mobileOpen && (
                <div className="md:hidden fixed inset-0 top-14 z-30 bg-[#0C0C0E] flex flex-col overflow-y-auto">
                    {/* Search */}
                    <div className="px-4 pt-4 pb-2">
                        <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-surface border border-strokes">
                            <MagnifyingGlass size={15} className="text-ink-muted shrink-0" />
                            <input
                                placeholder="Search candidates…"
                                className="bg-transparent text-sm flex-1 outline-none placeholder:text-ink-muted"
                                value={searchValue}
                                onChange={(e) => {
                                    setSearchValue(e.target.value);
                                    window.dispatchEvent(new CustomEvent("cgrecruit:search", { detail: e.target.value }));
                                }}
                            />
                        </div>
                    </div>
                    {/* Nav */}
                    <nav className="px-4 py-2 space-y-1">
                        {navItems.map(({ to, Icon, label }) => (
                            <NavLink
                                key={to}
                                to={to}
                                end
                                onClick={() => setMobileOpen(false)}
                                className={({ isActive }) =>
                                    `flex items-center gap-3 px-4 py-3 rounded-lg text-base transition-colors ${
                                        isActive ? "bg-surface-active text-ink" : "text-ink-muted hover:bg-surface-hover hover:text-ink"
                                    }`
                                }
                            >
                                <Icon size={18} weight="duotone" /> {label}
                            </NavLink>
                        ))}
                    </nav>
                    <div className="h-px bg-strokes mx-4 my-2" />
                    {/* Actions */}
                    <div className="px-4 py-2 space-y-1">
                        {canMutate && (
                            <button
                                onClick={() => {
                                    window.dispatchEvent(new CustomEvent("cgrecruit:open-add-applicant"));
                                    setMobileOpen(false);
                                }}
                                className="w-full flex items-center gap-3 px-4 py-3 rounded-lg text-base text-ink-muted hover:bg-surface-hover hover:text-ink transition-colors"
                            >
                                <Plus size={18} weight="bold" /> Add Applicant
                            </button>
                        )}
                        <button
                            onClick={() => {
                                window.dispatchEvent(new CustomEvent("cgrecruit:open-filters"));
                                setMobileOpen(false);
                            }}
                            className="w-full flex items-center gap-3 px-4 py-3 rounded-lg text-base text-ink-muted hover:bg-surface-hover hover:text-ink transition-colors"
                        >
                            <Funnel size={18} weight="duotone" /> Filter Candidates
                        </button>
                        {canMutate && (
                            <button
                                onClick={() => { navigate("/duplicates"); setMobileOpen(false); }}
                                className="w-full flex items-center gap-3 px-4 py-3 rounded-lg text-base text-ink-muted hover:bg-surface-hover hover:text-ink transition-colors"
                            >
                                <CardsThree size={18} weight="duotone" /> Duplicates
                            </button>
                        )}
                        <button
                            onClick={() => { navigate("/insights"); setMobileOpen(false); }}
                            className="w-full flex items-center gap-3 px-4 py-3 rounded-lg text-base text-ink-muted hover:bg-surface-hover hover:text-ink transition-colors"
                        >
                            <Brain size={18} weight="duotone" /> AI Insights
                        </button>
                        {!isViewer && !isAnalyst && (
                            <button
                                onClick={() => { navigate("/settings"); setMobileOpen(false); }}
                                className="w-full flex items-center gap-3 px-4 py-3 rounded-lg text-base text-ink-muted hover:bg-surface-hover hover:text-ink transition-colors"
                            >
                                <GearSix size={18} weight="duotone" /> Settings
                            </button>
                        )}
                    </div>
                    <div className="h-px bg-strokes mx-4 my-2" />
                    {/* User */}
                    <div className="px-4 py-2">
                        <div className="px-4 py-2 text-xs text-ink-muted">
                            <div className="font-medium text-ink">{user?.name}</div>
                            <div>{user?.email}</div>
                        </div>
                        <button
                            onClick={() => { logout(); setMobileOpen(false); }}
                            className="w-full flex items-center gap-3 px-4 py-3 rounded-lg text-base text-brand-danger hover:bg-surface-hover transition-colors"
                        >
                            <SignOut size={18} weight="duotone" /> Sign out
                        </button>
                    </div>
                </div>
            )}

            <main className="flex-1 overflow-y-auto">
                <Outlet />
            </main>

            <DialogHost />
            <AddApplicantModal
                open={showAdd}
                onClose={() => setShowAdd(false)}
                pipelineId={activePipelineId}
                pipelineName={activePipeline?.name}
                pipelineSlug={activePipeline?.public_slug}
                jobs={jobs}
                onCreated={() => window.dispatchEvent(new CustomEvent("cgrecruit:refresh"))}
            />
        </div>
    );
}

function NavTab({ to, icon, label, testid }) {
    return (
        <NavLink
            to={to}
            end
            data-testid={testid}
            className={({ isActive }) =>
                `flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm transition-colors ${
                    isActive ? "bg-surface-active text-ink" : "text-ink-muted hover:bg-surface-hover hover:text-ink"
                }`
            }
        >
            {icon}
            {label}
        </NavLink>
    );
}

function IconBtn({ icon, onClick, testid, label }) {
    return (
        <button
            data-testid={testid}
            onClick={onClick}
            title={label}
            aria-label={label}
            className="p-1.5 rounded-md hover:bg-surface-hover text-ink-muted hover:text-ink transition-colors"
        >
            {icon}
        </button>
    );
}
