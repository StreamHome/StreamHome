import React, { useCallback, useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useLocation, useNavigate } from "react-router-dom";
import {
  beginReauthentication, disable2FA, getAuthSessions, getReauthenticationStatus, getSecurityEvents,
  getSecuritySummary, regenerateRecoveryCodes, revokeAuthSession, revokeOtherSessions, setup2FA,
  verifyReauthentication, verifySetup2FA,
} from "../api/auth";
import { BrandLogo } from "../components/brand/BrandLogo";
import { useAuthStore } from "../stores/authStore";
import type { AuthSessionInfo, SecurityEventInfo, SecuritySummary, TwoFASetupResponse } from "../types/api";
import "./security.css";

type GateState = "checking" | "locked" | "ready";

function safeReturn(value: unknown): string {
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//") && !value.startsWith("/account/security") ? value : "/profiles";
}

function formatTime(value?: number | null): string {
  return value ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value * 1000)) : "Not available";
}

function eventLabel(type: string): string {
  return type.split("_").map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join(" ");
}

export function AccountSecurityPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const logout = useAuthStore((state) => state.logout);
  const returnTo = safeReturn((location.state as { returnTo?: unknown } | null)?.returnTo);
  const [gate, setGate] = useState<GateState>("checking");
  const [password, setPassword] = useState("");
  const [challengeToken, setChallengeToken] = useState("");
  const [factorCode, setFactorCode] = useState("");
  const [factorMethod, setFactorMethod] = useState<"totp" | "recovery">("totp");
  const [summary, setSummary] = useState<SecuritySummary | null>(null);
  const [sessions, setSessions] = useState<AuthSessionInfo[]>([]);
  const [events, setEvents] = useState<SecurityEventInfo[]>([]);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [setup, setSetup] = useState<TwoFASetupResponse | null>(null);
  const [setupCode, setSetupCode] = useState("");
  const [disableCode, setDisableCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [codesSaved, setCodesSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    setBusy(true); setError("");
    try {
      const [nextSummary, nextSessions, nextEvents] = await Promise.all([getSecuritySummary(), getAuthSessions(), getSecurityEvents()]);
      setSummary(nextSummary); setSessions(nextSessions); setEvents(nextEvents.events); setNextCursor(nextEvents.nextCursor);
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : "Security information could not be loaded."); }
    finally { setBusy(false); }
  }, []);

  useEffect(() => {
    getReauthenticationStatus().then((status) => { if (status.reauthenticated) { setGate("ready"); void load(); } else setGate("locked"); }).catch(() => setGate("locked"));
  }, [load]);

  const authenticate = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError("");
    try {
      if (challengeToken) {
        await verifyReauthentication({ challengeToken, method: factorMethod, code: factorCode });
        setGate("ready"); setChallengeToken(""); setFactorCode(""); await load();
      } else {
        const response = await beginReauthentication(password);
        if ("requires2fa" in response) setChallengeToken(response.challengeToken);
        else { setGate("ready"); await load(); }
      }
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : "Reauthentication failed."); }
    finally { setBusy(false); }
  };

  const beginSetup = async () => { setBusy(true); setError(""); try { setSetup(await setup2FA()); } catch (e) { setError(e instanceof Error ? e.message : "TOTP setup failed."); } finally { setBusy(false); } };
  const confirmSetup = async () => { setBusy(true); setError(""); try { const result = await verifySetup2FA(setupCode); setRecoveryCodes(result.recoveryCodes); setCodesSaved(false); setSetup(null); setSetupCode(""); setMessage(result.message); await load(); } catch (e) { setError(e instanceof Error ? e.message : "The code was not accepted."); } finally { setBusy(false); } };
  const disable = async () => { setBusy(true); setError(""); try { const result = await disable2FA(disableCode); setDisableCode(""); setMessage(result.message); await load(); } catch (e) { setError(e instanceof Error ? e.message : "TOTP could not be disabled."); } finally { setBusy(false); } };
  const regenerate = async () => { setBusy(true); setError(""); try { const result = await regenerateRecoveryCodes(); setRecoveryCodes(result.recoveryCodes); setCodesSaved(false); setMessage("New recovery codes generated. Previous codes no longer work."); await load(); } catch (e) { setError(e instanceof Error ? e.message : "Recovery codes could not be generated."); } finally { setBusy(false); } };
  const copyCodes = async () => { await navigator.clipboard.writeText(recoveryCodes.join("\n")); setMessage("Recovery codes copied."); };
  const downloadCodes = () => { const blob = new Blob([`StreamHome recovery codes\nGenerated ${new Date().toISOString()}\n\n${recoveryCodes.join("\n")}\n`], { type: "text/plain" }); const href = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = href; anchor.download = "streamhome-recovery-codes.txt"; anchor.click(); URL.revokeObjectURL(href); };
  const revoke = async (session: AuthSessionInfo) => { setBusy(true); setError(""); try { const result = await revokeAuthSession(session.id); if (result.currentSession) { logout(); return; } await load(); } catch (e) { setError(e instanceof Error ? e.message : "Session could not be revoked."); } finally { setBusy(false); } };
  const revokeOthers = async () => { setBusy(true); setError(""); try { const result = await revokeOtherSessions(); setMessage(`${result.revokedCount} other session${result.revokedCount === 1 ? "" : "s"} signed out.`); await load(); } catch (e) { setError(e instanceof Error ? e.message : "Other sessions could not be revoked."); } finally { setBusy(false); } };
  const loadMore = async () => { if (!nextCursor) return; setBusy(true); try { const result = await getSecurityEvents(nextCursor); setEvents((current) => [...current, ...result.events]); setNextCursor(result.nextCursor); } finally { setBusy(false); } };

  return <main className="security-page" data-theme="ember">
    <div className="security-ambient" aria-hidden="true" />
    <header className="security-nav"><BrandLogo showWordmark={false} /><div><p>StreamHome account</p><h1>Account Security</h1></div><button onClick={() => navigate(returnTo)}>Back</button></header>
    {gate !== "ready" ? <section className="security-gate">
      <p className="security-eyebrow">Sensitive controls</p><h2>{gate === "checking" ? "Checking authorization" : "Confirm your identity"}</h2><p>Security controls require a recent password and local second factor.</p>
      {gate === "locked" && <form onSubmit={authenticate}>
        {!challengeToken ? <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required autoFocus /></label> : <><label>{factorMethod === "totp" ? "Authenticator code" : "Recovery code"}<input inputMode={factorMethod === "totp" ? "numeric" : "text"} autoComplete="one-time-code" value={factorCode} onChange={(event) => setFactorCode(factorMethod === "totp" ? event.target.value.replace(/\D/g, "").slice(0, 6) : event.target.value.toUpperCase())} required autoFocus /></label><button type="button" className="security-text-action" onClick={() => { setFactorMethod((value) => value === "totp" ? "recovery" : "totp"); setFactorCode(""); }}>{factorMethod === "totp" ? "Use recovery code" : "Use authenticator"}</button></>}
        {error && <p className="security-error" role="alert">{error}</p>}<button className="security-primary" disabled={busy}>{busy ? "Verifying…" : challengeToken ? "Verify factor" : "Continue"}</button>
      </form>}
    </section> : <div className="security-content">
      <div className="security-heading"><div><p className="security-eyebrow">Server account controls</p><h2>Protect every StreamHome session</h2><span>Review authentication, recovery access, active devices, and recent security activity.</span></div><button onClick={() => void load()} disabled={busy}>Refresh</button></div>
      <AnimatePresence>{(error || message) && <motion.div className={error ? "security-notice security-notice--error" : "security-notice"} initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} role={error ? "alert" : "status"}>{error || message}</motion.div>}</AnimatePresence>
      <section className="security-overview"><article><span>Account</span><strong>{summary?.email ?? "Loading…"}</strong><small>TOTP {summary?.twoFactorEnabled ? "enabled" : "not enabled"}</small></article><article><span>Previous successful login</span><strong>{formatTime(summary?.previousLogin?.at)}</strong><small>{summary?.previousLogin ? `${summary.previousLogin.deviceLabel} · ${summary.previousLogin.ipAddress}` : "No earlier login recorded"}</small></article><article><span>Recovery access</span><strong>{summary?.recoveryCodesRemaining ?? 0} codes remaining</strong><small>Every code works once</small></article></section>
      <section className="security-card"><header><div><p className="security-eyebrow">Local second factor</p><h3>Authenticator and recovery</h3></div>{!summary?.twoFactorEnabled && !setup && <button className="security-primary" onClick={() => void beginSetup()}>Set up TOTP</button>}</header>
        {setup && <div className="security-setup"><p>Add this secret or provisioning URI to your authenticator, then enter the current code.</p><code>{setup.secret}</code><small>{setup.provisioningUri}</small><div><input aria-label="TOTP setup code" inputMode="numeric" maxLength={6} value={setupCode} onChange={(event) => setSetupCode(event.target.value.replace(/\D/g, ""))} /><button className="security-primary" disabled={setupCode.length !== 6 || busy} onClick={() => void confirmSetup()}>Enable TOTP</button></div></div>}
        {summary?.twoFactorEnabled && <div className="security-actions"><div><h4>Recovery codes</h4><p>Generate a fresh set if the previous copy is unavailable.</p><button onClick={() => void regenerate()} disabled={busy}>Regenerate codes</button></div><div><h4>Disable TOTP</h4><p>This also invalidates recovery codes and signs out other devices.</p><input aria-label="TOTP code to disable" inputMode="numeric" maxLength={6} value={disableCode} onChange={(event) => setDisableCode(event.target.value.replace(/\D/g, ""))} placeholder="Six-digit code" /><button className="security-danger" disabled={disableCode.length !== 6 || busy} onClick={() => void disable()}>Disable TOTP</button></div></div>}
      </section>
      {recoveryCodes.length > 0 && <section className="security-card security-recovery"><header><div><p className="security-eyebrow">Show once</p><h3>Save your recovery codes now</h3></div></header><p>Each code can replace your authenticator once. They cannot be displayed again.</p><div className="security-code-grid">{recoveryCodes.map((item) => <code key={item}>{item}</code>)}</div><div className="security-inline-actions"><button onClick={() => void copyCodes()}>Copy all</button><button onClick={downloadCodes}>Download .txt</button></div><label><input type="checkbox" checked={codesSaved} onChange={(event) => setCodesSaved(event.target.checked)} /> I saved these codes somewhere secure</label><button className="security-primary" disabled={!codesSaved} onClick={() => setRecoveryCodes([])}>Finish</button></section>}
      <section className="security-card"><header><div><p className="security-eyebrow">60-day access</p><h3>Active sessions</h3></div><button onClick={() => void revokeOthers()} disabled={busy || sessions.length < 2}>Sign out all other devices</button></header><div className="security-session-list">{sessions.map((item) => <article key={item.id}><div><strong>{item.deviceLabel}{item.current && <b>Current</b>}</strong><span>{item.ipAddress} · Last active {formatTime(item.lastSeenAt)}</span><small>Expires {formatTime(item.expiresAt)}</small></div><button className={item.current ? "security-danger" : ""} onClick={() => void revoke(item)}>{item.current ? "Sign out" : "Revoke"}</button></article>)}</div></section>
      <section className="security-card"><header><div><p className="security-eyebrow">180-day history</p><h3>Security activity</h3></div></header><div className="security-event-list">{events.map((item) => <article key={item.id}><i data-outcome={item.outcome} /><div><strong>{eventLabel(item.type)}</strong><span>{item.deviceLabel} · {item.ipAddress}</span></div><time>{formatTime(item.createdAt)}</time></article>)}{events.length === 0 && <p>No security activity has been recorded yet.</p>}</div>{nextCursor && <button className="security-load-more" disabled={busy} onClick={() => void loadMore()}>Load earlier activity</button>}</section>
    </div>}
  </main>;
}
