import { useEffect, useState, type ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { getSetupStatus, type SetupStatus } from "../../api/setup";

export function SetupStateGate({ children }: { children: ReactNode }) {
  const location = useLocation();
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    getSetupStatus().then((value) => { if (active) { setStatus(value); setError(""); } }).catch((reason) => {
      if (active) setError(reason instanceof Error ? reason.message : "Setup status is unavailable.");
    });
    return () => { active = false; };
  }, [location.pathname]);

  if (error) return <main className="setup-gate-state"><h1>StreamHome is unavailable</h1><p>{error}</p><button onClick={() => window.location.reload()}>Retry</button></main>;
  if (!status) return <main className="setup-gate-state" aria-busy="true"><span className="setup-spinner" /><p>Checking server state…</p></main>;
  if (status.required && location.pathname !== "/setup") return <Navigate to="/setup" replace />;
  if (!status.required && location.pathname === "/setup") return <Navigate to="/login" replace />;
  return <>{children}</>;
}
