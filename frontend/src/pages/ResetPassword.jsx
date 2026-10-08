import { useState } from "react";
import { useSearchParams, useNavigate, Link } from "react-router-dom";
import api from "@/lib/api";
import BRAND from "@/lib/brand";

export default function ResetPasswordPage() {
    const [searchParams] = useSearchParams();
    const token = searchParams.get("token") || "";
    const navigate = useNavigate();
    const [password, setPassword] = useState("");
    const [confirm, setConfirm] = useState("");
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");

    const submit = async (e) => {
        e.preventDefault();
        setError("");
        if (password !== confirm) {
            setError("Passwords don't match.");
            return;
        }
        if (password.length < 10) {
            setError("Password must be at least 10 characters.");
            return;
        }
        setBusy(true);
        try {
            await api.post("/auth/reset-password", { token, new_password: password });
            navigate("/login?reset=1");
        } catch (err) {
            setError(err?.response?.data?.detail || "Invalid or expired link. Please request a new one.");
        } finally {
            setBusy(false);
        }
    };

    if (!token) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-[#0C0C0E] text-ink p-8">
                <div className="text-center space-y-3">
                    <p className="text-ink-muted">Invalid reset link.</p>
                    <Link to="/login" className="text-sm text-brand-primary hover:underline">Back to sign in</Link>
                </div>
            </div>
        );
    }

    return (
        <div className="min-h-screen flex items-center justify-center bg-[#0C0C0E] text-ink p-8">
            <div className="w-full max-w-sm">
                <div className="flex items-center gap-3 mb-8">
                    <img src={BRAND.logo} alt={BRAND.name} className="w-8 h-8 object-contain" />
                    <span className="font-heading text-lg font-bold tracking-tight">{BRAND.name}</span>
                </div>
                <h2 className="font-heading text-2xl font-bold tracking-tight mb-2">Set new password</h2>
                <p className="text-sm text-ink-muted mb-8">Choose a new password for your account.</p>
                <form onSubmit={submit} className="space-y-4">
                    <div>
                        <label className="label-overline block mb-1.5">New Password</label>
                        <input
                            type="password"
                            className="input-dark"
                            value={password}
                            onChange={(e) => setPassword(e.target.value)}
                            required
                            minLength={6}
                            autoFocus
                        />
                    </div>
                    <div>
                        <label className="label-overline block mb-1.5">Confirm Password</label>
                        <input
                            type="password"
                            className="input-dark"
                            value={confirm}
                            onChange={(e) => setConfirm(e.target.value)}
                            required
                            minLength={6}
                        />
                    </div>
                    {error && <p className="text-sm text-red-400">{error}</p>}
                    <button type="submit" disabled={busy} className="btn-primary w-full !py-2.5">
                        {busy ? "Saving…" : "Update password"}
                    </button>
                </form>
            </div>
        </div>
    );
}
