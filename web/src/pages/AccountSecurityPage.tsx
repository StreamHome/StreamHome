import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  cancelSetup2FA,
  disable2FA,
  getAuthSessions,
  getReauthenticationStatus,
  getSecurityEvents,
  getSecuritySummary,
  regenerateRecoveryCodes,
  revokeAuthSession,
  revokeOtherSessions,
  setup2FA,
  updateAccountEmail,
  updateAccountPassword,
  updateSessionPolicy,
  verifySetup2FA,
} from "../api/auth";
import { ApiError } from "../api/client";
import { ReauthenticationForm } from "./admin/ReauthenticationForm";
import { AccountSectionNavigation } from "./admin/AccountSectionNavigation";
import { AuthenticatorCodeInput } from "../components/ui/AuthenticatorCodeInput";
import { MOTION_EASE, MOTION_TIMINGS, useAppMotion } from "../motion/motionSystem";
import { useAuthStore } from "../stores/authStore";
import type {
  AuthSessionInfo,
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

function CredentialPassword({ label, purpose, ...props }: React.InputHTMLAttributes<HTMLInputElement> & { label: string; purpose: string }) {
  const id = useId();
  const [visible, setVisible] = useState(false);
  return <div className="security-password-field"><label htmlFor={id}>{label}</label><div>
    <input {...props} id={id} type={visible ? "text" : "password"} />
    <button type="button" disabled={props.disabled} aria-controls={id} aria-pressed={visible} aria-label={`${visible ? "Hide" : "Show"} ${label.toLowerCase()} ${purpose}`} onClick={() => setVisible((value) => !value)}>{visible ? "Hide" : "Show"}</button>
  </div></div>;
}

export function AccountSecurityPage() {
  const logout = useAuthStore((state) => state.logout);
  const setToken = useAuthStore((state) => state.setToken);
  const [gate, setGate] = useState<GateState>("checking");
  const [summary, setSummary] = useState<SecuritySummary | null>(null);
  const [sessions, setSessions] = useState<AuthSessionInfo[]>([]);
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

  const handleError = useCallback((requestError: unknown, fallback: string) => {
    if (requestError instanceof ApiError && requestError.code === "reauthentication_required") {
      setGate("locked");
      setEmailPassword(""); setCurrentPassword(""); setNewPassword(""); setConfirmPassword("");
      setError("Your identity confirmation expired. Confirm your password, then retry your change.");
    } else setError(requestError instanceof Error ? requestError.message : fallback);
  }, []);

  const load = useCallback(async () => {
    setBusy(true); setError("");
    try {
      const [nextSummary, nextSessions, nextEvents] = await Promise.all([
        getSecuritySummary(),
        getAuthSessions(),
        getSecurityEvents(),
      ]);
      setSummary(nextSummary); setSessions(nextSessions); setEvents(nextEvents.events); setNextCursor(nextEvents.nextCursor);
      setEmail(nextSummary.email); setSessionLifetimeDays(nextSummary.sessionLifetimeDays);
    } catch (requestError) { handleError(requestError, "Security information could not be loaded."); }
    finally { setBusy(false); }
  }, [handleError]);

  useEffect(() => {
    getReauthenticationStatus().then((status) => { if (status.reauthenticated) { setGate("ready"); void load(); } else setGate("locked"); }).catch(() => setGate("locked"));
  }, [load]);

  const saveEmail = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError(""); setMessage("");
    try {
      const result = await updateAccountEmail(email, emailPassword);
      setToken("", result.email); setEmailPassword(""); setMessage(`${result.message} Other signed-in devices were revoked.`); await load();
    } catch (requestError) { handleError(requestError, "The account email could not be updated."); }
    finally { setBusy(false); }
  };

  const savePassword = async (event: React.FormEvent) => {
    event.preventDefault(); setError(""); setMessage("");
    if (newPassword !== confirmPassword) { setError("The new password confirmation does not match."); return; }
    setBusy(true);
    try {
      const result = await updateAccountPassword(currentPassword, newPassword);
      setCurrentPassword(""); setNewPassword(""); setConfirmPassword(""); setMessage(`${result.message} Other signed-in devices were revoked.`); await load();
    } catch (requestError) { handleError(requestError, "The password could not be updated."); }
    finally { setBusy(false); }
  };

  const saveSessionPolicy = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError(""); setMessage("");
    try { const result = await updateSessionPolicy(sessionLifetimeDays); setSessionLifetimeDays(result.sessionLifetimeDays); setMessage(result.message); await load(); }
    catch (requestError) { handleError(requestError, "The session lifetime could not be updated."); }
    finally { setBusy(false); }
  };

  const beginSetup = async () => { setBusy(true); setError(""); setSetupCopyStatus(""); try { setSetup(await setup2FA()); } catch (requestError) { handleError(requestError, "TOTP setup failed."); } finally { setBusy(false); } };
  const confirmSetup = async () => { if (!setup) return; setBusy(true); setError(""); try { const result = await verifySetup2FA(setup.enrollmentId, setupCode); setRecoveryCodes(result.recoveryCodes); setCodesSaved(false); setSetup(null); setSetupCode(""); setSetupCopyStatus(""); setMessage(result.message); await load(); } catch (requestError) { handleError(requestError, "The code was not accepted."); } finally { setBusy(false); } };
  const cancelSetup = async () => { if (!setup) return; setBusy(true); setError(""); try { await cancelSetup2FA(setup.enrollmentId); setSetup(null); setSetupCode(""); setSetupCopyStatus(""); } catch (requestError) { handleError(requestError, "TOTP enrollment could not be cancelled."); } finally { setBusy(false); } };
  const copySetupKey = async () => { if (!setup) return; try { await navigator.clipboard.writeText(setup.manualKey); setSetupCopyStatus("copied"); } catch { setSetupCopyStatus("failed"); } };
  const disable = async () => { setBusy(true); setError(""); try { const result = await disable2FA(disableCode); setDisableCode(""); setMessage(result.message); await load(); } catch (requestError) { handleError(requestError, "TOTP could not be disabled."); } finally { setBusy(false); } };
  const regenerate = async () => { setBusy(true); setError(""); try { const result = await regenerateRecoveryCodes(); setRecoveryCodes(result.recoveryCodes); setCodesSaved(false); setMessage("New recovery codes generated. Previous codes no longer work."); await load(); } catch (requestError) { handleError(requestError, "Recovery codes could not be generated."); } finally { setBusy(false); } };
  const copyCodes = async () => { await navigator.clipboard.writeText(recoveryCodes.join("\n")); setMessage("Recovery codes copied."); };
  const downloadCodes = () => { const blob = new Blob([`StreamHome recovery codes\nGenerated ${new Date().toISOString()}\n\n${recoveryCodes.join("\n")}\n`], { type: "text/plain" }); const href = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = href; anchor.download = "streamhome-recovery-codes.txt"; anchor.click(); URL.revokeObjectURL(href); };
  const revoke = async (session: AuthSessionInfo) => { setBusy(true); setError(""); try { const result = await revokeAuthSession(session.id); if (result.currentSession) { logout(); return; } await load(); } catch (requestError) { handleError(requestError, "Session could not be revoked."); } finally { setBusy(false); } };
  const revokeOthers = async () => { setBusy(true); setError(""); try { const result = await revokeOtherSessions(); setMessage(`${result.revokedCount} other session${result.revokedCount === 1 ? "" : "s"} signed out.`); await load(); } catch (requestError) { handleError(requestError, "Other sessions could not be revoked."); } finally { setBusy(false); } };
  const loadMore = async () => { if (!nextCursor) return; setBusy(true); try { const result = await getSecurityEvents(nextCursor); setEvents((current) => [...current, ...result.events]); setNextCursor(result.nextCursor); } finally { setBusy(false); } };

  const pageHeading = <header className="admin-panel__header"><p>IDENTITY / ACCESS</p><h1>Account and Security</h1><span>Manage sign-in, recovery and devices for your server account.</span></header>;
  return <section className="admin-panel admin-panel--account admin-security">
    {gate !== "ready" ? <>{pageHeading}<section className="security-gate">
      <p className="security-eyebrow">Sensitive controls</p><h2>{gate === "checking" ? "Checking access" : "Confirm your identity"}</h2><p>Verify access to your account’s sensitive controls.</p>
      {gate === "locked" && <>{error && <p className="security-error" role="alert">{error}</p>}<ReauthenticationForm onSuccess={async () => { setGate("ready"); await load(); }} /></>}
    </section></> : <div className="account-workspace"><AccountSectionNavigation /><div className="account-main">{pageHeading}<div className="security-content">
      <div className="security-heading"><div><p className="security-eyebrow">Server account controls</p><h2>Account overview</h2><span>Account changes apply across StreamHome and appear in security activity.</span></div><button onClick={() => void load()} disabled={busy}>Refresh</button></div>
      <AnimatePresence>{(error || message) && <motion.div className={error ? "security-notice security-notice--error" : "security-notice"} initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} role={error ? "alert" : "status"}>{error || message}</motion.div>}</AnimatePresence>
      <section className="security-overview"><article><a href="#account-credentials">Account</a><strong>{summary?.email ?? "Loading…"}</strong><small>TOTP {summary?.twoFactorEnabled ? "enabled" : "not enabled"}</small></article><article><span>Previous successful login</span><strong>{formatTime(summary?.previousLogin?.at)}</strong><small>{summary?.previousLogin ? `${summary.previousLogin.deviceLabel} · ${summary.previousLogin.ipAddress}` : "No earlier login recorded"}</small></article><article><a href="#account-sessions">Session policy</a><strong>{summary?.sessionLifetimeDays ?? 60} days</strong><small>Absolute lifetime for new sign-ins</small></article><article><a href="#account-authenticator">Recovery access</a><strong>{summary?.recoveryCodesRemaining ?? 0} codes remaining</strong><small>Every code works once</small></article></section>
      <section id="account-authenticator" tabIndex={-1} className="security-card"><header><div><p className="security-eyebrow">Local second factor</p><h2>Authenticator and recovery</h2></div>{!summary?.twoFactorEnabled && !setup && <button className="security-primary" onClick={() => void beginSetup()}>Set up TOTP</button>}</header>
        {setup && <div className="security-setup security-totp-enrollment"><div className="security-totp-qr"><img src={setup.qrImageUrl} alt={`Scan this QR code to add StreamHome for ${summary?.email ?? "this account"} to an authenticator app`} /><div><h3>Scan with your authenticator</h3><p>This QR code is generated by your StreamHome server and expires at {new Date(setup.expiresAt * 1000).toLocaleTimeString()}.</p></div></div><div className="security-totp-manual"><span>Can&apos;t scan? Enter this setup key manually.</span><code>{setup.manualKey}</code><button type="button" onClick={() => void copySetupKey()}>{setupCopyStatus === "copied" ? "Copied" : "Copy setup key"}</button>{setupCopyStatus === "failed" && <small role="status">Copy was blocked. Select the key and copy it manually.</small>}</div><AuthenticatorCodeInput label="TOTP setup code" value={setupCode} onChange={setSetupCode} disabled={busy} /><div className="security-inline-actions"><button type="button" onClick={() => void cancelSetup()} disabled={busy}>Cancel</button><button className="security-primary" disabled={setupCode.length !== 6 || busy} onClick={() => void confirmSetup()}>Enable TOTP</button></div></div>}
        {summary?.twoFactorEnabled && <div className="security-actions"><div><h3>Recovery codes</h3><p>Generating a new set immediately invalidates all previous codes. Save the new codes before leaving this page; they are shown only once.</p><button onClick={() => void regenerate()} disabled={busy}>Regenerate codes</button></div><div><h3>Disable TOTP</h3><p>This also invalidates recovery codes and signs out other devices.</p><AuthenticatorCodeInput label="TOTP code to disable" value={disableCode} onChange={setDisableCode} disabled={busy} /><button className="security-danger" disabled={disableCode.length !== 6 || busy} onClick={() => void disable()}>Disable TOTP</button></div></div>}
      </section>
      {recoveryCodes.length > 0 && <section className="security-card security-recovery"><header><div><p className="security-eyebrow">Show once</p><h2>Save your recovery codes now</h2></div></header><p>Each code can replace your authenticator once. They cannot be displayed again.</p><div className="security-code-grid">{recoveryCodes.map((item) => <code key={item}>{item}</code>)}</div><div className="security-inline-actions"><button onClick={() => void copyCodes()}>Copy all</button><button onClick={downloadCodes}>Download .txt</button></div><label><input type="checkbox" checked={codesSaved} onChange={(event) => setCodesSaved(event.target.checked)} /> I saved these codes somewhere secure</label><button className="security-primary" disabled={!codesSaved} onClick={() => setRecoveryCodes([])}>Finish</button></section>}
      <section id="account-credentials" tabIndex={-1} className="security-card security-credentials"><header><div><p className="security-eyebrow">Administrator identity</p><h2>Email and password</h2></div></header><div className="security-credential-grid">
        <form onSubmit={saveEmail}><div><h3>Account email</h3><p>This address is used to sign in. Updating it signs out every other device.</p></div><label>Email address<input type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} required /></label><CredentialPassword label="Current password" purpose="for email change" autoComplete="current-password" value={emailPassword} onChange={(event) => setEmailPassword(event.target.value)} required disabled={busy} /><button className="security-primary" disabled={busy || !summary || email.trim().toLowerCase() === summary.email.toLowerCase()}>Update email</button></form>
        <form onSubmit={savePassword}><div><h3>Change password</h3><p>Use at least six characters. Updating it invalidates pending challenges and signs out other devices.</p></div><CredentialPassword label="Current password" purpose="for password change" autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} required disabled={busy} /><CredentialPassword label="New password" purpose="for password change" autoComplete="new-password" minLength={6} maxLength={72} value={newPassword} onChange={(event) => setNewPassword(event.target.value)} required disabled={busy} /><CredentialPassword label="Confirm new password" purpose="for password change" autoComplete="new-password" minLength={6} maxLength={72} value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} required disabled={busy} /><button className="security-primary" disabled={busy || newPassword.length < 6 || confirmPassword.length < 6}>Update password</button></form>
      </div></section>
      <section id="account-sessions" tabIndex={-1} className="security-card"><header><div><p className="security-eyebrow">Session access policy</p><h2>Active sessions</h2></div><button onClick={() => void revokeOthers()} disabled={busy || sessions.length < 2}>Sign out all other devices</button></header><form className="security-session-policy" onSubmit={saveSessionPolicy}><label>New-session lifetime<div><input aria-label="Session lifetime in days" type="number" min={1} max={365} value={sessionLifetimeDays} onChange={(event) => setSessionLifetimeDays(Number(event.target.value))} /><span>days</span></div></label><p>Applies to future sign-ins. Existing sessions retain the expiration shown below.</p><button className="security-primary" disabled={busy || !summary || sessionLifetimeDays === summary.sessionLifetimeDays || sessionLifetimeDays < 1 || sessionLifetimeDays > 365}>Save lifetime</button></form><div className="security-session-list">{sessions.map((item) => <article key={item.id}><div><strong>{item.deviceLabel}{item.current && <b>Current</b>}</strong><span>{item.ipAddress} · Last active {formatTime(item.lastSeenAt)}</span><small>Expires {formatTime(item.expiresAt)}</small></div><button className={item.current ? "security-danger" : ""} onClick={() => void revoke(item)}>{item.current ? "Sign out" : "Revoke"}</button></article>)}</div></section>
      <section id="account-activity" tabIndex={-1} className="security-card"><header><div><p className="security-eyebrow">180-day history</p><h2>Security activity</h2></div></header><div className="security-event-list">{events.map((item) => <button type="button" className="security-event-entry" key={item.id} aria-haspopup="dialog" onClick={() => setSelectedEvent(item)}><i data-outcome={item.outcome} aria-hidden="true" /><span className="security-event-entry__copy"><strong>{eventLabel(item.type)}</strong><small>{item.deviceLabel} · {item.ipAddress}</small></span><span className="security-event-entry__details" aria-hidden="true">View details →</span><time dateTime={new Date(item.createdAt * 1000).toISOString()}>{formatTime(item.createdAt)}</time></button>)}{events.length === 0 && <p>No security activity has been recorded yet.</p>}</div>{nextCursor && <button className="security-load-more" disabled={busy} onClick={() => void loadMore()}>Load earlier activity</button>}</section>
    </div></div></div>}
    <SecurityEventDialog event={selectedEvent} onClose={closeEventDialog} />
  </section>;
}
