import { createContext, useContext, useEffect, useState } from "react";
import api from "./api";

const AuthContext = createContext(null);

/**
 * AuthProvider — bootstraps the user from the httpOnly cookie set by
 * /api/auth/login. We never read or write the token in JavaScript, so an XSS
 * that drains `localStorage` can't exfiltrate the session.
 *
 * On mount we just call /api/auth/me — if the cookie is valid, it returns the
 * user; if not, we render the login screen. The user PROFILE itself (name,
 * email, company) is non-sensitive and is kept in component state only.
 */
export function AuthProvider({ children }) {
    const [user, setUser] = useState(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        // Public routes don't need a session — skip the /auth/me probe so a
        // candidate visiting /retry/{token} doesn't see a "Loading..." flash
        // and doesn't pollute the network panel with a 401.
        const path = window.location.pathname;
        const isPublic = ["/apply/", "/retry/", "/reschedule/", "/applicant/", "/login", "/refer/"]
            .some((prefix) => path === prefix || path.startsWith(prefix));
        if (isPublic) {
            setLoading(false);
            return;
        }
        // Try the cookie. 401 = not logged in, anything else = bubble up.
        api.get("/auth/me")
            .then((r) => setUser(r.data))
            .catch(() => setUser(null))
            .finally(() => setLoading(false));
    }, []);

    const login = async (email, password) => {
        const r = await api.post("/auth/login", { email, password });
        // Backend already set the httpOnly cookie on this response — we DON'T
        // store the token in localStorage anymore. Just hold the user object
        // in memory for the rest of the session.
        setUser(r.data.user);
        return r.data.user;
    };

    const register = async (email, password, name, company) => {
        const r = await api.post("/auth/register", { email, password, name, company });
        setUser(r.data.user);
        return r.data.user;
    };

    const logout = async () => {
        // Backend clears the cookie via Set-Cookie; if the request fails
        // (offline), we still drop the local user state and bounce.
        try { await api.post("/auth/logout"); } catch { /* offline is fine */ }
        setUser(null);
        window.location.href = "/login";
    };

    const isSuperAdmin = user?.role === "super_admin";
    // Viewers are read-only: they only see candidates they added and cannot move/hire/delete.
    const isViewer = user?.role === "viewer";
    // Analysts are read-only: they only see the Intelligence/reporting section.
    const isAnalyst = user?.role === "analyst";
    // Recruiters + super-admins can mutate candidates.
    const canMutate = !!user && !isViewer && !isAnalyst;

    return (
        <AuthContext.Provider value={{ user, loading, login, register, logout, isSuperAdmin, isViewer, isAnalyst, canMutate }}>
            {children}
        </AuthContext.Provider>
    );
}

export const useAuth = () => useContext(AuthContext);
