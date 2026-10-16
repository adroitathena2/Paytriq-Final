import type { ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import { Loading } from "./components/ui";
import { useAuth } from "./lib/auth";
import EventsCreate from "./pages/EventsCreate";
import EventsDetail from "./pages/EventsDetail";
import EventsList from "./pages/EventsList";
import Login from "./pages/Login";
import RoiReportPage from "./pages/RoiReport";
import Settings from "./pages/Settings";
import Trace from "./pages/Trace";

function RequireAuth({ children }: { children: ReactNode }) {
  const { loading, signedIn } = useAuth();
  if (loading) return <Loading label="Checking your session" />;
  return signedIn ? <>{children}</> : <Navigate to="/login" replace />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route index element={<Navigate to="/events" replace />} />
        <Route path="/events" element={<EventsList />} />
        <Route path="/events/new" element={<EventsCreate />} />
        <Route path="/events/:eventId" element={<EventsDetail />} />
        <Route path="/runs/:runId/report" element={<RoiReportPage />} />
        <Route path="/trace" element={<Trace />} />
        <Route path="/trace/:runId" element={<Trace />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<p className="text-sm text-slate-500">Page not found.</p>} />
      </Route>
    </Routes>
  );
}
