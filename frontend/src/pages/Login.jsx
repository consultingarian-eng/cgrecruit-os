import { useState, useEffect } from "react";
import { useNavigate, Link, useSearchParams } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import { toast } from "sonner";
import BRAND from "@/lib/brand";

export default function LoginPage() {
    const { login } = useAuth();
    const navigate = useNavigate();
    const [searchParams] = useSearchParams();
    const [email, setEmail] = useState("");
    const [password, setPassword] = useState("");
    const [busy, setBusy] = useState(false);

    useEffect(() => {
        if (searchParams.get("reset") === "1") {
            toast.success("Password updated — sign in with your new password.");
        }
    }, []);

    const submit = async (e) => {
        e.preventDefault();
        setBusy(true);
        try {
            const loggedInUser = await login(email, password);
            navigate(loggedInUser?.role === "analyst" ? "/intelligence" : "/");
        } catch (err) {
            let msg = err?.response?.data?.detail;
            if (!msg) {
                if (err?.code === "ERR_NETWORK" || !err?.response) {
                    msg = "Couldn't reach the server. Check your connection and try again.";
                } else if (err?.response?.status >= 500) {
                    msg = "Server error — please retry in a few seconds.";
                } else {
                    msg = "Authentication failed";
                }
            }
            toast.error(msg);
        } finally { setBusy(false); }
    };

    return (
        <div className="min-h-screen flex bg-[#0C0C0E] text-ink" data-testid="login-page">
            <div className="hidden md:flex flex-1 relative overflow-hidden border-r border-strokes">
                <div className="absolute inset-0 bg-[radial-gradient(circle_at_30%_20%,rgba(139,92,246,0.18),transparent_60%),radial-gradient(circle_at_80%_80%,rgba(236,72,153,0.12),transparent_60%)]" />
                <div className="relative z-10 p-14 flex flex-col justify-between w-full">
                    <div className="flex items-center gap-3">
                        <img src={BRAND.logo} alt={BRAND.name} className="w-9 h-9 object-contain" />
                        <span className="font-heading text-xl font-bold tracking-tight">{BRAND.name}</span>
                    </div>
                    <div>
                        <h1 className="font-heading text-4xl font-bold tracking-tight leading-[1.1]">
                            Recruitment,<br />
                            <span className="text-brand-primary">all in one place.</span>
                        </h1>
                        <p className="mt-5 text-ink-muted text-base max-w-md">
                            Pipeline, AI screening, scheduling, and reporting for your offices.
                        </p>
                    </div>
                    <div className="text-xs text-ink-dim">© {BRAND.name} {new Date().getFullYear()}</div>
                </div>
            </div>
            <div className="flex-1 flex items-center justify-center p-8">
                <div className="w-full max-w-sm">
                    <div className="md:hidden flex items-center gap-3 mb-8">
                        <img src={BRAND.logo} alt={BRAND.name} className="w-8 h-8 object-contain" />
                        <span className="font-heading text-lg font-bold tracking-tight">{BRAND.name}</span>
                    </div>
                    <h2 className="font-heading text-3xl font-bold tracking-tight mb-2">Welcome back</h2>
                    <p className="text-sm text-ink-muted mb-8">Sign in to continue.</p>

                    <form onSubmit={submit} className="space-y-4">
                        <div>
                            <label className="label-overline block mb-1.5">Email</label>
                            <input data-testid="login-email-input" type="email" className="input-dark" value={email} onChange={(e) => setEmail(e.target.value)} required />
                        </div>
                        <div>
                            <label className="label-overline block mb-1.5">Password</label>
                            <input data-testid="login-password-input" type="password" className="input-dark" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={6} />
                        </div>
                        <button data-testid="login-submit-button" type="submit" disabled={busy} className="btn-primary w-full !py-2.5 mt-2">
                            {busy ? "…" : "Sign in"}
                        </button>
                    </form>
                    <p className="text-center text-sm mt-5">
                        <Link to="/forgot-password" className="text-ink-muted hover:text-ink">
                            Forgot password?
                        </Link>
                    </p>
                </div>
            </div>
        </div>
    );
}
