import axios from "axios";

// Empty (or unset) = same origin, which is how the deployed build runs.
const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || "";
export const API = `${BACKEND_URL}/api`;

// `withCredentials: true` makes the browser auto-attach the httpOnly
// `cgr_token` cookie set by /api/auth/login — the SPA never sees the token
// in JavaScript, so an XSS that exfiltrates `localStorage` no longer leaks
// auth credentials.
//
// Bearer fallback: if a session token is provided programmatically (e.g. from
// a login response in tests, or the Public Reschedule URL signed token), we
// still attach it via the Authorization header for the request. The backend
// accepts EITHER header OR cookie (header wins), so test harnesses that don't
// speak cookies keep working.
const api = axios.create({ baseURL: API, withCredentials: true });

let _bearerOverride = null;
export function setBearerOverride(token) {
    // Used in rare cases (Cypress, integration tests, embedded retry UIs) where
    // the cookie isn't available. Pass `null` to clear.
    _bearerOverride = token || null;
}

api.interceptors.request.use((config) => {
    if (_bearerOverride) config.headers.Authorization = `Bearer ${_bearerOverride}`;
    return config;
});

// Public routes — pages a candidate visits without a recruiter login (signed-token URLs).
// We must NOT auto-redirect to /login from these even if /api/auth/me returns 401, otherwise
// candidates clicking their reschedule/retry/applicant links bounce to the recruiter login.
const PUBLIC_ROUTES = ["/login", "/apply/", "/retry/", "/reschedule/", "/applicant/", "/refer/"];
function isOnPublicRoute() {
    const p = window.location.pathname;
    return PUBLIC_ROUTES.some((prefix) => p === prefix || p.startsWith(prefix));
}

api.interceptors.response.use(
    (r) => r,
    (err) => {
        if (err?.response?.status === 401) {
            // Don't redirect candidates who are on a public token-signed page —
            // they were never expected to have a session cookie. The page itself
            // handles its own "invalid token" UX.
            if (!isOnPublicRoute()) {
                window.location.href = "/login";
            }
        }
        return Promise.reject(err);
    }
);

export default api;
