import React, { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  beginReauthentication,
  cancelSetup2FA,
  createIntegrationCredential,
  disable2FA,
  getAuthSessions,
  getIntegrationCredentials,
  getIntegrationScopes,
  getReauthenticationStatus,
  getSecurityEvents,
  getSecuritySummary,
  regenerateRecoveryCodes,
  revokeAuthSession,
  revokeIntegrationCredential,
  revokeOtherSessions,
  setup2FA,
  updateAccountEmail,
  updateAccountPassword,
  updateIntegrationCredential,
  updateSessionPolicy,
  verifyReauthentication,
  verifySetup2FA,
} from "../api/auth";
import { MOTION_EASE, MOTION_TIMINGS, useAppMotion } from "../motion/motionSystem";
import { useAuthStore } from "../stores/authStore";
import type {
  AuthSessionInfo,
  IntegrationCredentialInfo,
  IntegrationScope,
  IntegrationScopeDefinition,
  SecurityEventInfo,
  SecuritySummary,
  TwoFASetupResponse,
} from "../types/api";

type GateState = "checking" | "locked" | "ready";

function formatTime(value?: number | null): string {
  return value ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value * 1000)) : "Not available";
}

function eventLabel(type: string): string {
  return type.split("_").map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join(" ");
}

function detailLabel(key: string): string {
  return key.replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/[_-]+/g, " ").replace(/^./, (character) => character.toUpperCase());
}

function detailValue(value: unknown): React.ReactNode {
  if (value === null || value === undefined) return "Not recorded";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "string" || typeof value === "number") return String(value);
  return <pre>{JSON.stringify(value, null, 2)}</pre>;
}

function SecurityEventDialog({ event, onClose }: { event: SecurityEventInfo | null; onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const { reduced } = useAppMotion();

  useEffect(() => {
    if (!event) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    const handleKeyboard = (keyboardEvent: KeyboardEvent) => {
      if (keyboardEvent.key === "Escape") onClose();
      if (keyboardEvent.key !== "Tab") return;
      const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])") ?? []).filter((element) => !element.hasAttribute("disabled"));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (keyboardEvent.shiftKey && document.activeElement === first) { keyboardEvent.preventDefault(); last.focus(); }
      else if (!keyboardEvent.shiftKey && document.activeElement === last) { keyboardEvent.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", handleKeyboard);
    return () => {
      window.removeEventListener("keydown", handleKeyboard);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, [event, onClose]);

  const details = Object.entries(event?.details ?? {});
  return <AnimatePresence>{event && <motion.div className="security-event-dialog-layer" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.notice }}>
    <div className="security-event-dialog-backdrop" aria-hidden="true" onClick={onClose} />
    <motion.section ref={dialogRef} className="security-event-dialog" role="dialog" aria-modal="true" aria-labelledby="security-event-dialog-title" initial={reduced ? { opacity: 0 } : { opacity: 0, y: 22, scale: .97 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={reduced ? { opacity: 0 } : { opacity: 0, y: 14, scale: .985 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialogEnter, ease: MOTION_EASE }}>
      <header><div><p className="security-eyebrow">Security activity details</p><h2 id="security-event-dialog-title">{eventLabel(event.type)}</h2></div><button ref={closeRef} type="button" className="security-event-dialog__close" onClick={onClose} aria-label="Close activity details">Close</button></header>
      <div className="security-event-dialog__outcome" data-outcome={event.outcome}><i aria-hidden="true" /><span>{event.outcome}</span></div>
      <dl className="security-event-dialog__facts">
        <div><dt>Occurred</dt><dd>{formatTime(event.createdAt)}<small>{new Date(event.createdAt * 1000).toISOString()}</small></dd></div>
        <div><dt>Device</dt><dd>{event.deviceLabel}</dd></div>
        <div><dt>IP address</dt><dd><code>{event.ipAddress}</code></dd></div>
        <div><dt>Event type</dt><dd><code>{event.type}</code></dd></div>
        <div className="security-event-dialog__event-id"><dt>Event ID</dt><dd><code>{event.id}</code></dd></div>
      </dl>
      <section className="security-event-dialog__metadata" aria-labelledby="security-event-metadata-title">
        <header><p className="security-eyebrow">Recorded metadata</p><h3 id="security-event-metadata-title">Event-specific details</h3></header>
        {details.length ? <dl>{details.map(([key, value]) => <div key={key}><dt>{detailLabel(key)}</dt><dd>{detailValue(value)}</dd></div>)}</dl> : <p>No additional metadata was recorded for this activity.</p>}
      </section>
    </motion.section>
  </motion.div>}</AnimatePresence>;
}

export function AccountSecurityPage() {
  const logout = useAuthStore((state) => state.logout);
  const setToken = useAuthStore((state) => state.setToken);
  const [gate, setGate] = useState<GateState>("checking");
  const [password, setPassword] = useState("");
  const [challengeToken, setChallengeToken] = useState("");
  const [factorCode, setFactorCode] = useState("");
  const [factorMethod, setFactorMethod] = useState<"totp" | "recovery">("totp");
  const [summary, setSummary] = useState<SecuritySummary | null>(null);
  const [sessions, setSessions] = useState<AuthSessionInfo[]>([]);
  const [integrationCredentials, setIntegrationCredentials] = useState<IntegrationCredentialInfo[]>([]);
  const [integrationScopes, setIntegrationScopes] = useState<IntegrationScopeDefinition[]>([]);
  const [integrationName, setIntegrationName] = useState("");
  const [selectedIntegrationScopes, setSelectedIntegrationScopes] = useState<IntegrationScope[]>(["ingest"]);
  const [integrationExpiration, setIntegrationExpiration] = useState("never");
  const [generatedIntegrationToken, setGeneratedIntegrationToken] = useState("");
  const [generatedIntegrationName, setGeneratedIntegrationName] = useState("");
  const [integrationCopyStatus, setIntegrationCopyStatus] = useState<"" | "copied" | "failed">("");
  const [editingIntegrationId, setEditingIntegrationId] = useState<string | null>(null);
  const [editingIntegrationName, setEditingIntegrationName] = useState("");
  const [editingIntegrationScopes, setEditingIntegrationScopes] = useState<IntegrationScope[]>([]);
  const [confirmRevokeIntegrationId, setConfirmRevokeIntegrationId] = useState<string | null>(null);
  const [events, setEvents] = useState<SecurityEventInfo[]>([]);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [setup, setSetup] = useState<TwoFASetupResponse | null>(null);
  const [setupCode, setSetupCode] = useState("");
  const [setupCopyStatus, setSetupCopyStatus] = useState<"" | "copied" | "failed">("");
  const [disableCode, setDisableCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [codesSaved, setCodesSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [selectedEvent, setSelectedEvent] = useState<SecurityEventInfo | null>(null);
  const [email, setEmail] = useState("");
  const [emailPassword, setEmailPassword] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [sessionLifetimeDays, setSessionLifetimeDays] = useState(60);
  const closeEventDialog = useCallback(() => setSelectedEvent(null), []);

  const load = useCallback(async () => {
    setBusy(true); setError("");
    try {
      const [nextSummary, nextSessions, nextEvents, nextCredentials, nextScopes] = await Promise.all([
        getSecuritySummary(),
        getAuthSessions(),
        getSecurityEvents(),
        getIntegrationCredentials(),
        getIntegrationScopes(),
      ]);
      setSummary(nextSummary); setSessions(nextSessions); setEvents(nextEvents.events); setNextCursor(nextEvents.nextCursor);
      setIntegrationCredentials(nextCredentials); setIntegrationScopes(nextScopes);
      setEmail(nextSummary.email); setSessionLifetimeDays(nextSummary.sessionLifetimeDays);
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

  const saveEmail = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError(""); setMessage("");
    try {
      const result = await updateAccountEmail(email, emailPassword);
      setToken("", result.email); setEmailPassword(""); setMessage(`${result.message} Other signed-in devices were revoked.`); await load();
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : "The account email could not be updated."); }
    finally { setBusy(false); }
  };

  const savePassword = async (event: React.FormEvent) => {
    event.preventDefault(); setError(""); setMessage("");
    if (newPassword !== confirmPassword) { setError("The new password confirmation does not match."); return; }
    setBusy(true);
    try {
      const result = await updateAccountPassword(currentPassword, newPassword);
      setCurrentPassword(""); setNewPassword(""); setConfirmPassword(""); setMessage(`${result.message} Other signed-in devices were revoked.`); await load();
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : "The password could not be updated."); }
    finally { setBusy(false); }
  };

  const saveSessionPolicy = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError(""); setMessage("");
    try { const result = await updateSessionPolicy(sessionLifetimeDays); setSessionLifetimeDays(result.sessionLifetimeDays); setMessage(result.message); await load(); }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : "The session lifetime could not be updated."); }
    finally { setBusy(false); }
  };

  const beginSetup = async () => { setBusy(true); setError(""); setSetupCopyStatus(""); try { setSetup(await setup2FA()); } catch (requestError) { setError(requestError instanceof Error ? requestError.message : "TOTP setup failed."); } finally { setBusy(false); } };
  const confirmSetup = async () => { if (!setup) return; setBusy(true); setError(""); try { const result = await verifySetup2FA(setup.enrollmentId, setupCode); setRecoveryCodes(result.recoveryCodes); setCodesSaved(false); setSetup(null); setSetupCode(""); setSetupCopyStatus(""); setMessage(result.message); await load(); } catch (requestError) { setError(requestError instanceof Error ? requestError.message : "The code was not accepted."); } finally { setBusy(false); } };
  const cancelSetup = async () => { if (!setup) return; setBusy(true); setError(""); try { await cancelSetup2FA(setup.enrollmentId); setSetup(null); setSetupCode(""); setSetupCopyStatus(""); } catch (requestError) { setError(requestError instanceof Error ? requestError.message : "TOTP enrollment could not be cancelled."); } finally { setBusy(false); } };
  const copySetupKey = async () => { if (!setup) return; try { await navigator.clipboard.writeText(setup.manualKey); setSetupCopyStatus("copied"); } catch { setSetupCopyStatus("failed"); } };
  const disable = async () => { setBusy(true); setError(""); try { const result = await disable2FA(disableCode); setDisableCode(""); setMessage(result.message); await load(); } catch (requestError) { setError(requestError instanceof Error ? requestError.message : "TOTP could not be disabled."); } finally { setBusy(false); } };
  const regenerate = async () => { setBusy(true); setError(""); try { const result = await regenerateRecoveryCodes(); setRecoveryCodes(result.recoveryCodes); setCodesSaved(false); setMessage("New recovery codes generated. Previous codes no longer work."); await load(); } catch (requestError) { setError(requestError instanceof Error ? requestError.message : "Recovery codes could not be generated."); } finally { setBusy(false); } };
  const copyCodes = async () => { await navigator.clipboard.writeText(recoveryCodes.join("\n")); setMessage("Recovery codes copied."); };
  const downloadCodes = () => { const blob = new Blob([`StreamHome recovery codes\nGenerated ${new Date().toISOString()}\n\n${recoveryCodes.join("\n")}\n`], { type: "text/plain" }); const href = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = href; anchor.download = "streamhome-recovery-codes.txt"; anchor.click(); URL.revokeObjectURL(href); };
  const toggleIntegrationScope = (scope: IntegrationScope, editing = false) => {
    const selected = editing ? editingIntegrationScopes : selectedIntegrationScopes;
    const update = editing ? setEditingIntegrationScopes : setSelectedIntegrationScopes;
    update(selected.includes(scope) ? selected.filter((item) => item !== scope) : [...selected, scope]);
  };
  const createApiKey = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError(""); setMessage(""); setIntegrationCopyStatus("");
    try {
      const expiresInDays = integrationExpiration === "never" ? null : Number(integrationExpiration);
      const result = await createIntegrationCredential(integrationName, selectedIntegrationScopes, expiresInDays);
      setGeneratedIntegrationToken(result.token); setGeneratedIntegrationName(result.credential.name);
      setIntegrationName(""); setSelectedIntegrationScopes(["ingest"]); setIntegrationExpiration("never");
      setMessage("API key created. Save the secret now; StreamHome will not display it again.");
      await load();
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : "The API key could not be created."); }
    finally { setBusy(false); }
  };
  const copyIntegrationToken = async () => {
    try { await navigator.clipboard.writeText(generatedIntegrationToken); setIntegrationCopyStatus("copied"); }
    catch { setIntegrationCopyStatus("failed"); }
  };
  const downloadIntegrationToken = () => {
    const blob = new Blob([`StreamHome API key\nName: ${generatedIntegrationName}\nGenerated: ${new Date().toISOString()}\n\n${generatedIntegrationToken}\n`], { type: "text/plain" });
    const href = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = href;
    anchor.download = `streamhome-api-key-${generatedIntegrationName.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "credential"}.txt`;
    anchor.click(); URL.revokeObjectURL(href);
  };
  const beginIntegrationEdit = (credential: IntegrationCredentialInfo) => {
    setEditingIntegrationId(credential.id); setEditingIntegrationName(credential.name);
    setEditingIntegrationScopes([...credential.scopes]); setConfirmRevokeIntegrationId(null);
  };
  const saveIntegration = async (event: React.FormEvent) => {
    event.preventDefault(); if (!editingIntegrationId) return;
    setBusy(true); setError(""); setMessage("");
    try {
      await updateIntegrationCredential(editingIntegrationId, editingIntegrationName, editingIntegrationScopes);
      setEditingIntegrationId(null); setMessage("API key settings updated."); await load();
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : "The API key could not be updated."); }
    finally { setBusy(false); }
  };
  const revokeIntegration = async (credential: IntegrationCredentialInfo) => {
    if (confirmRevokeIntegrationId !== credential.id) { setConfirmRevokeIntegrationId(credential.id); setEditingIntegrationId(null); return; }
    setBusy(true); setError(""); setMessage("");
    try {
      await revokeIntegrationCredential(credential.id); setConfirmRevokeIntegrationId(null);
      setMessage(`API key “${credential.name}” revoked. Other API keys remain active.`); await load();
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : "The API key could not be revoked."); }
    finally { setBusy(false); }
  };
  const revoke = async (session: AuthSessionInfo) => { setBusy(true); setError(""); try { const result = await revokeAuthSession(session.id); if (result.currentSession) { logout(); return; } await load(); } catch (requestError) { setError(requestError instanceof Error ? requestError.message : "Session could not be revoked."); } finally { setBusy(false); } };
  const revokeOthers = async () => { setBusy(true); setError(""); try { const result = await revokeOtherSessions(); setMessage(`${result.revokedCount} other session${result.revokedCount === 1 ? "" : "s"} signed out.`); await load(); } catch (requestError) { setError(requestError instanceof Error ? requestError.message : "Other sessions could not be revoked."); } finally { setBusy(false); } };
  const loadMore = async () => { if (!nextCursor) return; setBusy(true); try { const result = await getSecurityEvents(nextCursor); setEvents((current) => [...current, ...result.events]); setNextCursor(result.nextCursor); } finally { setBusy(false); } };

  return <section className="admin-panel admin-panel--account admin-security">
    <header className="admin-panel__header"><p>IDENTITY / ACCESS</p><h1>Account and Security</h1><span>Manage credentials, authentication, recovery access, signed-in devices, and security history without leaving the control plane.</span></header>
    {gate !== "ready" ? <section className="security-gate">
      <p className="security-eyebrow">Sensitive controls</p><h2>{gate === "checking" ? "Checking authorization" : "Confirm your identity"}</h2><p>Security controls require a recent password and local second factor.</p>
      {gate === "locked" && <form onSubmit={authenticate}>
        {!challengeToken ? <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required autoFocus /></label> : <><label>{factorMethod === "totp" ? "Authenticator code" : "Recovery code"}<input inputMode={factorMethod === "totp" ? "numeric" : "text"} autoComplete="one-time-code" value={factorCode} onChange={(event) => setFactorCode(factorMethod === "totp" ? event.target.value.replace(/\D/g, "").slice(0, 6) : event.target.value.toUpperCase())} required autoFocus /></label><button type="button" className="security-text-action" onClick={() => { setFactorMethod((value) => value === "totp" ? "recovery" : "totp"); setFactorCode(""); }}>{factorMethod === "totp" ? "Use recovery code" : "Use authenticator"}</button></>}
        {error && <p className="security-error" role="alert">{error}</p>}<button className="security-primary" disabled={busy}>{busy ? "Verifying…" : challengeToken ? "Verify factor" : "Continue"}</button>
      </form>}
    </section> : <div className="security-content">
      <div className="security-heading"><div><p className="security-eyebrow">Server account controls</p><h2>Protect every StreamHome session</h2><span>Changes made here are enforced by the server and recorded in security activity.</span></div><button onClick={() => void load()} disabled={busy}>Refresh</button></div>
      <AnimatePresence>{(error || message) && <motion.div className={error ? "security-notice security-notice--error" : "security-notice"} initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} role={error ? "alert" : "status"}>{error || message}</motion.div>}</AnimatePresence>
      <section className="security-overview"><article><span>Account</span><strong>{summary?.email ?? "Loading…"}</strong><small>TOTP {summary?.twoFactorEnabled ? "enabled" : "not enabled"}</small></article><article><span>Previous successful login</span><strong>{formatTime(summary?.previousLogin?.at)}</strong><small>{summary?.previousLogin ? `${summary.previousLogin.deviceLabel} · ${summary.previousLogin.ipAddress}` : "No earlier login recorded"}</small></article><article><span>Session policy</span><strong>{summary?.sessionLifetimeDays ?? 60} days</strong><small>Absolute lifetime for new sign-ins</small></article><article><span>Recovery access</span><strong>{summary?.recoveryCodesRemaining ?? 0} codes remaining</strong><small>Every code works once</small></article></section>
      <section className="security-card security-credentials"><header><div><p className="security-eyebrow">Administrator identity</p><h3>Email and password</h3></div></header><div className="security-credential-grid">
        <form onSubmit={saveEmail}><div><h4>Account email</h4><p>This address is used to sign in. Updating it signs out every other device.</p></div><label>Email address<input type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} required /></label><label>Current password<input type="password" autoComplete="current-password" value={emailPassword} onChange={(event) => setEmailPassword(event.target.value)} required /></label><button className="security-primary" disabled={busy || !summary || email.trim().toLowerCase() === summary.email.toLowerCase()}>Update email</button></form>
        <form onSubmit={savePassword}><div><h4>Change password</h4><p>Use at least six characters. Updating it invalidates pending challenges and signs out other devices.</p></div><label>Current password<input type="password" autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} required /></label><label>New password<input type="password" autoComplete="new-password" minLength={6} maxLength={72} value={newPassword} onChange={(event) => setNewPassword(event.target.value)} required /></label><label>Confirm new password<input type="password" autoComplete="new-password" minLength={6} maxLength={72} value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} required /></label><button className="security-primary" disabled={busy || newPassword.length < 6 || confirmPassword.length < 6}>Update password</button></form>
      </div></section>
      <section className="security-card"><header><div><p className="security-eyebrow">Local second factor</p><h3>Authenticator and recovery</h3></div>{!summary?.twoFactorEnabled && !setup && <button className="security-primary" onClick={() => void beginSetup()}>Set up TOTP</button>}</header>
        {setup && <div className="security-setup security-totp-enrollment"><div className="security-totp-qr"><img src={setup.qrImageUrl} alt={`Scan this QR code to add StreamHome for ${summary?.email ?? "this account"} to an authenticator app`} /><div><h4>Scan with your authenticator</h4><p>This QR code is generated by your StreamHome server and expires at {new Date(setup.expiresAt * 1000).toLocaleTimeString()}.</p></div></div><div className="security-totp-manual"><span>Can&apos;t scan? Enter this setup key manually.</span><code>{setup.manualKey}</code><button type="button" onClick={() => void copySetupKey()}>{setupCopyStatus === "copied" ? "Copied" : "Copy setup key"}</button>{setupCopyStatus === "failed" && <small role="status">Copy was blocked. Select the key and copy it manually.</small>}</div><label>Six-digit authenticator code<input aria-label="TOTP setup code" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={setupCode} onChange={(event) => setSetupCode(event.target.value.replace(/\D/g, ""))} /></label><div className="security-inline-actions"><button type="button" onClick={() => void cancelSetup()} disabled={busy}>Cancel</button><button className="security-primary" disabled={setupCode.length !== 6 || busy} onClick={() => void confirmSetup()}>Enable TOTP</button></div></div>}
        {summary?.twoFactorEnabled && <div className="security-actions"><div><h4>Recovery codes</h4><p>Generate a fresh set if the previous copy is unavailable.</p><button onClick={() => void regenerate()} disabled={busy}>Regenerate codes</button></div><div><h4>Disable TOTP</h4><p>This also invalidates recovery codes and signs out other devices.</p><input aria-label="TOTP code to disable" inputMode="numeric" maxLength={6} value={disableCode} onChange={(event) => setDisableCode(event.target.value.replace(/\D/g, ""))} placeholder="Six-digit code" /><button className="security-danger" disabled={disableCode.length !== 6 || busy} onClick={() => void disable()}>Disable TOTP</button></div></div>}
      </section>
      {recoveryCodes.length > 0 && <section className="security-card security-recovery"><header><div><p className="security-eyebrow">Show once</p><h3>Save your recovery codes now</h3></div></header><p>Each code can replace your authenticator once. They cannot be displayed again.</p><div className="security-code-grid">{recoveryCodes.map((item) => <code key={item}>{item}</code>)}</div><div className="security-inline-actions"><button onClick={() => void copyCodes()}>Copy all</button><button onClick={downloadCodes}>Download .txt</button></div><label><input type="checkbox" checked={codesSaved} onChange={(event) => setCodesSaved(event.target.checked)} /> I saved these codes somewhere secure</label><button className="security-primary" disabled={!codesSaved} onClick={() => setRecoveryCodes([])}>Finish</button></section>}
      <section className="security-card security-integrations">
        <header><div><p className="security-eyebrow">Machine access</p><h3>API keys</h3></div><span>{integrationCredentials.filter((item) => !item.revokedAt && (!item.expiresAt || item.expiresAt > Date.now() / 1000)).length} active</span></header>
        <p>Create separate, named API keys for applications such as MediaSender. Each key receives only the permissions selected here.</p>
        <form className="security-integration-create" onSubmit={createApiKey}>
          <label>Key name<input aria-label="API key name" maxLength={80} value={integrationName} onChange={(event) => setIntegrationName(event.target.value)} placeholder="Living room MediaSender" required /></label>
          <label>Expiration<select aria-label="API key expiration" value={integrationExpiration} onChange={(event) => setIntegrationExpiration(event.target.value)}><option value="never">Never</option><option value="30">30 days</option><option value="90">90 days</option><option value="365">1 year</option></select></label>
          <fieldset><legend>Permissions</legend><div className="security-scope-grid">{integrationScopes.map((scope) => <label key={scope.id}><input aria-label={scope.label} type="checkbox" checked={selectedIntegrationScopes.includes(scope.id)} onChange={() => toggleIntegrationScope(scope.id)} /><span><strong>{scope.label}</strong><small>{scope.description}</small></span></label>)}</div></fieldset>
          <button className="security-primary" disabled={busy || Boolean(generatedIntegrationToken) || !integrationName.trim() || selectedIntegrationScopes.length === 0}>Create API key</button>
        </form>
        {generatedIntegrationToken && <section className="security-integration-secret" aria-labelledby="generated-api-key-title"><div><p className="security-eyebrow">Shown once</p><h4 id="generated-api-key-title">Save the API key for {generatedIntegrationName}</h4><p>StreamHome stores only a secure hash. This secret cannot be displayed again after you dismiss it.</p></div><code>{generatedIntegrationToken}</code><div className="security-inline-actions"><button type="button" onClick={() => void copyIntegrationToken()}>{integrationCopyStatus === "copied" ? "Copied" : "Copy key"}</button><button type="button" onClick={downloadIntegrationToken}>Download .txt</button><button type="button" className="security-primary" onClick={() => { setGeneratedIntegrationToken(""); setGeneratedIntegrationName(""); setIntegrationCopyStatus(""); }}>I saved the key</button></div>{integrationCopyStatus === "failed" && <small role="status">Clipboard access was blocked. Select the key above and copy it manually.</small>}</section>}
        <div className="security-integration-list">
          {integrationCredentials.map((credential) => {
            const expired = Boolean(credential.expiresAt && credential.expiresAt <= Date.now() / 1000);
            const state = credential.revokedAt ? "revoked" : expired ? "expired" : "active";
            return <article key={credential.id} data-state={state}>
              <div className="security-integration-summary"><div><strong>{credential.name}</strong><b>{state}</b></div><code>{credential.tokenHint || "Legacy key"}</code><span>Created {formatTime(credential.createdAt)} · Last used {formatTime(credential.lastUsedAt)}</span><span>{credential.expiresAt ? `Expires ${formatTime(credential.expiresAt)}` : "Does not expire"}</span><ul>{credential.scopes.map((scope) => <li key={scope}>{integrationScopes.find((item) => item.id === scope)?.label || scope}</li>)}</ul></div>
              {editingIntegrationId === credential.id ? <form className="security-integration-edit" onSubmit={saveIntegration}><label>Key name<input aria-label={`Edit name for ${credential.name}`} maxLength={80} value={editingIntegrationName} onChange={(event) => setEditingIntegrationName(event.target.value)} required /></label><fieldset><legend>Permissions</legend><div className="security-scope-grid">{integrationScopes.map((scope) => <label key={scope.id}><input aria-label={`${scope.label} for ${credential.name}`} type="checkbox" checked={editingIntegrationScopes.includes(scope.id)} onChange={() => toggleIntegrationScope(scope.id, true)} /><span><strong>{scope.label}</strong><small>{scope.description}</small></span></label>)}</div></fieldset><div className="security-inline-actions"><button type="button" onClick={() => setEditingIntegrationId(null)}>Cancel</button><button className="security-primary" disabled={busy || !editingIntegrationName.trim() || editingIntegrationScopes.length === 0}>Save changes</button></div></form> : <div className="security-integration-actions">{state === "active" && <button type="button" onClick={() => beginIntegrationEdit(credential)}>Edit</button>}{!credential.revokedAt && <button type="button" className="security-danger" onClick={() => void revokeIntegration(credential)}>{confirmRevokeIntegrationId === credential.id ? "Confirm revoke" : "Revoke"}</button>}{confirmRevokeIntegrationId === credential.id && <button type="button" onClick={() => setConfirmRevokeIntegrationId(null)}>Cancel</button>}</div>}
            </article>;
          })}
          {integrationCredentials.length === 0 && <p className="security-integration-empty">No API keys have been created.</p>}
        </div>
      </section>
      <section className="security-card"><header><div><p className="security-eyebrow">Session access policy</p><h3>Active sessions</h3></div><button onClick={() => void revokeOthers()} disabled={busy || sessions.length < 2}>Sign out all other devices</button></header><form className="security-session-policy" onSubmit={saveSessionPolicy}><label>New-session lifetime<div><input aria-label="Session lifetime in days" type="number" min={1} max={365} value={sessionLifetimeDays} onChange={(event) => setSessionLifetimeDays(Number(event.target.value))} /><span>days</span></div></label><p>Applies to future sign-ins. Existing sessions retain the expiration shown below.</p><button className="security-primary" disabled={busy || !summary || sessionLifetimeDays === summary.sessionLifetimeDays || sessionLifetimeDays < 1 || sessionLifetimeDays > 365}>Save lifetime</button></form><div className="security-session-list">{sessions.map((item) => <article key={item.id}><div><strong>{item.deviceLabel}{item.current && <b>Current</b>}</strong><span>{item.ipAddress} · Last active {formatTime(item.lastSeenAt)}</span><small>Expires {formatTime(item.expiresAt)}</small></div><button className={item.current ? "security-danger" : ""} onClick={() => void revoke(item)}>{item.current ? "Sign out" : "Revoke"}</button></article>)}</div></section>
      <section className="security-card"><header><div><p className="security-eyebrow">180-day history</p><h3>Security activity</h3></div></header><div className="security-event-list">{events.map((item) => <button type="button" className="security-event-entry" key={item.id} aria-haspopup="dialog" onClick={() => setSelectedEvent(item)}><i data-outcome={item.outcome} aria-hidden="true" /><span className="security-event-entry__copy"><strong>{eventLabel(item.type)}</strong><small>{item.deviceLabel} · {item.ipAddress}</small></span><time dateTime={new Date(item.createdAt * 1000).toISOString()}>{formatTime(item.createdAt)}</time></button>)}{events.length === 0 && <p>No security activity has been recorded yet.</p>}</div>{nextCursor && <button className="security-load-more" disabled={busy} onClick={() => void loadMore()}>Load earlier activity</button>}</section>
    </div>}
    <SecurityEventDialog event={selectedEvent} onClose={closeEventDialog} />
  </section>;
}
