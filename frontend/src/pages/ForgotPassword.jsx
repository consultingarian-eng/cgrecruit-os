import { useState } from "react";
import { Link } from "react-router-dom";
import api from "@/lib/api";
import BRAND from "@/lib/brand";

export default function ForgotPasswordPage() {
    const [email, setEmail] = useState("");
    const [busy, setBusy] = useState(false);
    const [sent, setSent] = useState(false);
    const [error, setError] = useState("");

    const submit = async (e) => {
        e.preventDefault();
        setBusy(true);
        setError("");
        try {
            await api.post("/auth/forgot-password", { email });
            setSent(true);
        } catch {
            setError("Something went wrong. Please try again.");
        } finally {
            setBusy(false);
        }
    };

    return (
        <div className="min-h-screen flex items-center justify-center bg-[#0C0C0E] text-ink p-8">
            <div className="w-full max-w-sm">
                <div className="flex items-center gap-3 mb-8">
                    <img src={BRAND.logo} alt={BRAND.name} className="w-8 h-8 object-contain" />
                    <span className="font-heading text-lg font-bold tracking-tight">{BRAND.name}</span>
                </div>

                {sent ? (
                    <div className="space-y-4">
                        <h2 className="font-heading text-2xl font-bold tracking-tight">Check your email</h2>
                        <p className="text-sm text-ink-muted">
                            If <span className="text-ink">{email}</span> is registered, you'll receive a password reset link shortly. Check your spam folder if it doesn't arrive within a few minutes.
                        </p>
                        <Link to="/login" className="block text-sm text-brand-primary hover:underline mt-4">
                            Back to sign in
                        </Link>
                    </div>
                ) : (
                    <>
                        <h2 className="font-heading text-2xl font-bold tracking-tight mb-2">Forgot password?</h2>
                        <p className="text-sm text-ink-muted mb-8">
                            Enter your email and we'll send you a reset link — only if it's already registered.
                        </p>
                        <form onSubmit={submit} className="space-y-4">
                            <div>
                                <label className="label-overline block mb-1.5">Email</label>
                                <input
                                    type="email"
                                    className="input-dark"
                                    value={email}
                                    onChange={(e) => setEmail(e.target.value)}
                                    required
                                    autoFocus
                                />
                            </div>
                            {error && <p className="text-sm text-red-400">{error}</p>}
                            <button type="submit" disabled={busy} className="btn-primary w-full !py-2.5">
                                {busy ? "Sending…" : "Send reset link"}
                            </button>
                        </form>
                        <Link to="/login" className="block text-center text-sm text-ink-muted hover:text-ink mt-6">
                            Back to sign in
                        </Link>
                    </>
                )}
            </div>
        </div>
    );
}
