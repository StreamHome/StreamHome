import React, { useEffect, useState } from "react";
import { login, verify2FA } from "../../api/auth";
import { Button } from "../../components/ui/Button";
import { GlassPane } from "../../components/ui/GlassPane";
import { Input } from "../../components/ui/Input";
import { useAuthStore } from "../../stores/authStore";
import { useThemeStore } from "../../stores/themeStore";
import { getThemeDefinition } from "../../themes/application/themeRegistry";
import { AdminCenter } from "./AdminCenter";

const SESSION_KEY = "streamhome_admin_session";
const SESSION_TTL = 24 * 60 * 60 * 1000;

export function AdminGate() {
  const storedEmail = useAuthStore((state) => state.email);
  const theme = useThemeStore((state) => state.activeTheme);
  const definition = getThemeDefinition(theme);
  const Background = definition.Background;
  const [email, setEmail] = useState(storedEmail ?? "");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [needsCode, setNeedsCode] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return;
    try {
      const session = JSON.parse(raw) as { timestamp?: number };
      if (session.timestamp && Date.now() - session.timestamp < SESSION_TTL) setAuthenticated(true);
      else localStorage.removeItem(SESSION_KEY);
    } catch {
      localStorage.removeItem(SESSION_KEY);
    }
  }, []);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      if (needsCode) {
        if (code.length !== 6) throw new Error("Enter the six-digit TOTP code.");
        await verify2FA({ email, code });
      } else {
        const response = await login({ email, password });
        if ("requires2fa" in response) {
          setNeedsCode(true);
          setLoading(false);
          return;
        }
      }
      localStorage.setItem(SESSION_KEY, JSON.stringify({ timestamp: Date.now() }));
      setAuthenticated(true);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Reauthentication failed.");
    } finally {
      setLoading(false);
    }
  };

  if (authenticated) return <AdminCenter />;

  return (
    <main className={`theme-app admin-auth-screen ${definition.shellClass}`} data-theme={theme}>
      <Background />
      <GlassPane className="admin-auth-panel" spotlight={false}>
        <h1 className="text-2xl font-semibold">Admin reauthentication</h1>
        <p className="mt-2 text-sm text-[var(--text-muted)]">Confirm the server account before opening administrative controls.</p>
        <form className="mt-7 flex flex-col gap-4" onSubmit={submit}>
          <Input label="Account email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} required disabled={needsCode} />
          {!needsCode && <Input label="Password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} required />}
          {needsCode && <Input label="TOTP code" inputMode="numeric" maxLength={6} value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))} required />}
          {error && <p className="text-sm text-[var(--text-error)]">{error}</p>}
          <Button type="submit" disabled={loading}>{loading ? "Verifying…" : needsCode ? "Verify TOTP" : "Verify password"}</Button>
        </form>
      </GlassPane>
    </main>
  );
}
