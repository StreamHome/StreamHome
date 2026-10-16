import React from "react";
import { useAuthStore } from "../../stores/authStore";
import "./authentication-state.css";

export function AuthenticationState() {
  const status = useAuthStore((state) => state.hydrationStatus);
  const error = useAuthStore((state) => state.hydrationError);
  const hydrate = useAuthStore((state) => state.hydrate);
  if (status === "error") return (
    <main className="authentication-state">
      <section aria-labelledby="authentication-error-title">
        <h1 id="authentication-error-title">Unable to check your session</h1>
        <p role="alert">{error || "Your session could not be checked."}</p>
        <p>Check your connection, then try again.</p>
        <button type="button" onClick={() => { void hydrate({ force: true }); }}>Retry session check</button>
      </section>
    </main>
  );
  return <main className="authentication-state" aria-label="Loading authentication" aria-busy="true"><p role="status">Checking your session…</p></main>;
}
