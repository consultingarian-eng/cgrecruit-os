import "@/App.css";
import { BrowserRouter, Routes, Route, Navigate, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "@/lib/auth";
import { PipelineProvider } from "@/lib/pipeline";
import { Toaster } from "@/components/ui/sonner";
import LoginPage from "@/pages/Login";
import ForgotPasswordPage from "@/pages/ForgotPassword";
import ResetPasswordPage from "@/pages/ResetPassword";
import DashboardPage from "@/pages/Dashboard";
import IntelligencePage from "@/pages/Intelligence";
import FunnelReportPage from "@/pages/FunnelReport";
import CalendarPage from "@/pages/Calendar";
import SettingsPage from "@/pages/Settings";
import AppLayout from "@/components/AppLayout";
import ApplyPage from "@/pages/Apply";
import ReferPage from "@/pages/Refer";
import ApplicantStatusPage from "@/pages/ApplicantStatus";
import RetryPage from "@/pages/Retry";
import ReschedulePage from "@/pages/Reschedule";
import CallQueuePage from "@/pages/CallQueue";
import DuplicatesPage from "@/pages/Duplicates";
import AiInsightsPage from "@/pages/AiInsights";
import InboxPage from "@/pages/Inbox";

const ANALYST_ALLOWED_PATHS = ["/intelligence", "/report", "/insights"];

function ProtectedRoute({ children, analyticsOnly = false }) {
    const { user, loading } = useAuth();
    const path = window.location.pathname;
    if (loading) {
        return (
            <div className="h-screen flex items-center justify-center bg-[#0C0C0E] text-ink-muted text-sm" data-testid="loading-screen">
                Loading…
            </div>
        );
    }
    if (!user) return <Navigate to="/login" replace />;
    // Analysts may only access intelligence and report pages.
    if (user.role === "analyst" && !ANALYST_ALLOWED_PATHS.some((p) => path === p || path.startsWith(p + "/"))) {
        return <Navigate to="/intelligence" replace />;
    }
    return children;
}

// /dashboard used to render the board itself, which left every nav tab unlit
// (NavTab passes `end`, and the Pipeline tab is "/"). Redirect instead, keeping
// the query string so a stale /dashboard?candidate=X still opens the drawer.
function DashboardRedirect() {
    const { search } = useLocation();
    return <Navigate to={{ pathname: "/", search }} replace />;
}

function App() {
    return (
        <div className="App">
            <BrowserRouter>
                <AuthProvider>
                    <Routes>
                        <Route path="/login" element={<LoginPage />} />
                        <Route path="/forgot-password" element={<ForgotPasswordPage />} />
                        <Route path="/reset-password" element={<ResetPasswordPage />} />
                        <Route path="/apply/:slug" element={<ApplyPage />} />
                        <Route path="/refer/:slug" element={<ReferPage />} />
                        <Route path="/applicant/:token" element={<ApplicantStatusPage />} />
                        <Route path="/retry/:token" element={<RetryPage />} />
                        <Route path="/reschedule/:token" element={<ReschedulePage />} />
                        <Route element={<ProtectedRoute><PipelineProvider><AppLayout /></PipelineProvider></ProtectedRoute>}>
                            <Route path="/" element={<DashboardPage />} />
                            <Route path="/dashboard" element={<DashboardRedirect />} />
                            <Route path="/queue" element={<CallQueuePage />} />
                            <Route path="/inbox" element={<InboxPage />} />
                            <Route path="/duplicates" element={<DuplicatesPage />} />
                            <Route path="/intelligence" element={<IntelligencePage />} />
                            <Route path="/report" element={<FunnelReportPage />} />
                            <Route path="/calendar" element={<CalendarPage />} />
                            <Route path="/settings" element={<SettingsPage />} />
                            <Route path="/settings/:section" element={<SettingsPage />} />
                            <Route path="/insights" element={<AiInsightsPage />} />
                        </Route>
                        <Route path="*" element={<Navigate to="/" replace />} />
                    </Routes>
                </AuthProvider>
            </BrowserRouter>
            <Toaster theme="dark" position="bottom-right" />
        </div>
    );
}

export default App;
