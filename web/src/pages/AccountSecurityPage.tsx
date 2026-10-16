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
import { useDialogLifecycle } from "../components/ui/useDialogLifecycle";
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
type ReadState = { status: "loading" | "refreshing" | "ready" | "error"; error: string };
const pendingRead: ReadState = { status: "loading", error: "" };
function ResourceNotice({ label, state, hasData, retry }: { label: string; state: ReadState; hasData: boolean; retry: () => void }) {
  if (state.status === "ready") return null;
  return <div className={state.status === "error" ? "security-error" : "security-resource-status"}>
    <p role={state.status === "error" ? "alert" : "status"}>{state.status === "error" ? `${label} unavailable. ${hasData ? "Showing previously loaded information. " : ""}${state.error}` : `${hasData ? "Refreshing" : "Loading"} ${label.toLowerCase()}…`}</p>
    {state.status === "error" && <button type="button" onClick={retry}>Retry {label.toLowerCase()}</button>}
  </div>;
}

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

function SecurityEventDialog({ event, onClose, returnFocus }: { event: SecurityEventInfo | null; onClose: () => void; returnFocus: () => HTMLElement | null }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const { reduced } = useAppMotion();

  useDialogLifecycle({ open: Boolean(event), dialogRef, onClose, returnFocus });

  const details = Object.entries(event?.details ?? {});
  return <AnimatePresence>{event && <motion.div className="security-event-dialog-layer" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.notice }}>
    <div className="security-event-dialog-backdrop" aria-hidden="true" onClick={onClose} />
    <motion.section ref={dialogRef} className="security-event-dialog" tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="security-event-dialog-title" initial={reduced ? { opacity: 0 } : { opacity: 0, y: 22, scale: .97 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={reduced ? { opacity: 0 } : { opacity: 0, y: 14, scale: .985 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialogEnter, ease: MOTION_EASE }}>
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
  const active = useRef(true);
  const authorization = useRef(0);
  const gateRef = useRef<GateState>("checking");
  const requests = useRef({ summary: 0, sessions: 0, events: 0 });
  const mutationPending = useRef(false);
  const gateHeading = useRef<HTMLHeadingElement>(null);
  const eventInvoker = useRef<HTMLElement | null>(null);
  const [summaryRead, setSummaryRead] = useState<ReadState>(pendingRead);
  const [sessionsRead, setSessionsRead] = useState<ReadState>(pendingRead);
  const [eventsRead, setEventsRead] = useState<ReadState>(pendingRead);
  const [sessionsLoaded, setSessionsLoaded] = useState(false);
  const [eventsLoaded, setEventsLoaded] = useState(false);
  const [paginationError, setPaginationError] = useState("");
  const [paginationBusy, setPaginationBusy] = useState(false);
  const paginationPending = useRef(false);
  const confirmed = useRef<SecuritySummary | null>(null);
  const emailDraft = useRef({ value: "", revision: 0, dirty: false });
  const lifetimeDraft = useRef({ value: "", revision: 0, dirty: false });
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
  const uncheckedMutation = useRef(false);
  const [uncertainMutation, setUncertainMutation] = useState<{ label: string; checked: boolean } | null>(null);
  const [checkingOutcome, setCheckingOutcome] = useState(false);
  const [selectedEvent, setSelectedEvent] = useState<SecurityEventInfo | null>(null);
  const [email, setEmail] = useState("");
  const [emailPassword, setEmailPassword] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [sessionLifetimeDays, setSessionLifetimeDays] = useState("");
  const closeEventDialog = useCallback(() => setSelectedEvent(null), []);

  const lockWorkspace = useCallback(() => {
    authorization.current += 1; gateRef.current = "locked";
    setGate("locked"); setCheckingOutcome(false); paginationPending.current = false; setPaginationBusy(false); setSelectedEvent(null); setSetup(null); setSetupCode(""); setDisableCode("");
    setEmailPassword(""); setCurrentPassword(""); setNewPassword(""); setConfirmPassword("");
    setError("Your identity confirmation expired. Confirm your password, then retry your change.");
  }, []);
  const handleError = useCallback((requestError: unknown, fallback: string) => {
    if (requestError instanceof ApiError && requestError.code === "reauthentication_required") lockWorkspace();
    else setError(requestError instanceof Error ? requestError.message : fallback);
  }, [lockWorkspace]);
  const readResource = useCallback(async (resource: "summary" | "sessions" | "events") => {
    const generation = authorization.current;
    const request = ++requests.current[resource];
    const owned = () => active.current && gateRef.current === "ready" && generation === authorization.current && request === requests.current[resource];
    const setRead = resource === "summary" ? setSummaryRead : resource === "sessions" ? setSessionsRead : setEventsRead;
    setRead((previous) => ({ status: previous.status === "ready" || previous.status === "refreshing" || (previous.status === "error" && resource === "summary" && confirmed.current !== null) ? "refreshing" : "loading", error: "" }));
    try {
      if (resource === "summary") {
        const result = await getSecuritySummary(); if (!owned()) return;
        confirmed.current = result; setSummary(result);
        if (!emailDraft.current.dirty) { emailDraft.current.value = result.email; setEmail(result.email); }
        else emailDraft.current.dirty = emailDraft.current.value.trim().toLowerCase() !== result.email.toLowerCase();
        if (!lifetimeDraft.current.dirty) { lifetimeDraft.current.value = String(result.sessionLifetimeDays); setSessionLifetimeDays(String(result.sessionLifetimeDays)); }
        else lifetimeDraft.current.dirty = Number(lifetimeDraft.current.value) !== result.sessionLifetimeDays;
      } else if (resource === "sessions") {
        const result = await getAuthSessions(); if (!owned()) return;
        setSessions(result); setSessionsLoaded(true);
      } else {
        paginationPending.current = false; setPaginationBusy(false); setPaginationError("");
        const result = await getSecurityEvents(); if (!owned()) return;
        setEvents(result.events); setNextCursor(result.nextCursor); setEventsLoaded(true);
      }
      setRead({ status: "ready", error: "" });
      return true;
    } catch (requestError) {
      if (!owned()) return;
      if (requestError instanceof ApiError && requestError.code === "reauthentication_required") { lockWorkspace(); return; }
      setRead({ status: "error", error: requestError instanceof Error ? requestError.message : "The read failed." });
      return false;
    }
  }, [lockWorkspace]);
  const load = useCallback(async () => { return Promise.all([readResource("summary"), readResource("sessions"), readResource("events")]); }, [readResource]);
  useEffect(() => {
    active.current = true;
    const generation = authorization.current;
    getReauthenticationStatus().then((status) => {
      if (!active.current || generation !== authorization.current) return;
      gateRef.current = status.reauthenticated ? "ready" : "locked"; setGate(gateRef.current);
      if (status.reauthenticated) void load();
    }).catch(() => { if (active.current && generation === authorization.current) { gateRef.current = "locked"; setGate("locked"); } });
    return () => { active.current = false; authorization.current += 1; };
  }, [load]);
  useEffect(() => { if (gate === "locked") gateHeading.current?.focus(); }, [gate]);

  const mutationNeedsCheck = uncertainMutation !== null && !uncertainMutation.checked;
  const runMutation = async (action: (owned: () => boolean) => Promise<void>, fallback: string, refresh = true, label = "Account change") => {
    if (mutationPending.current || uncheckedMutation.current || gateRef.current !== "ready") return;
    const generation = authorization.current;
    const owned = () => active.current && gateRef.current === "ready" && generation === authorization.current;
    mutationPending.current = true; setBusy(true); setError(""); setMessage(""); setUncertainMutation(null);
    try { await action(owned); if (owned() && refresh) await load(); }
    catch (requestError) {
      if (!owned()) return;
      if (requestError instanceof ApiError && requestError.status === 0) {
        uncheckedMutation.current = true; setUncertainMutation({ label, checked: false });
        setSummaryRead({ status: "error", error: "A change response was lost. Read current status before another change." });
        setSessionsRead({ status: "error", error: "Read current status before another change." });
      } else handleError(requestError, fallback);
    }
    finally { mutationPending.current = false; if (active.current) setBusy(false); }
  };
  const checkMutationOutcome = async () => {
    if (checkingOutcome || mutationPending.current || gateRef.current !== "ready") return;
    const generation = authorization.current; setCheckingOutcome(true);
    try {
      const results = await load();
      if (active.current && generation === authorization.current && results.every(Boolean)) { uncheckedMutation.current = false; setUncertainMutation((current) => current ? { ...current, checked: true } : null); }
    } finally { if (active.current && generation === authorization.current) setCheckingOutcome(false); }
  };
  const summaryFresh = summaryRead.status === "ready" && summary !== null && !mutationNeedsCheck;
  const sessionsFresh = sessionsRead.status === "ready" && sessionsLoaded && !mutationNeedsCheck;
  const saveEmail = async (event: React.FormEvent) => {
    event.preventDefault(); if (!summaryFresh) return;
    const submitted = { value: email, revision: emailDraft.current.revision, password: emailPassword };
    await runMutation(async (owned) => {
      const result = await updateAccountEmail(submitted.value, submitted.password); if (!owned()) return;
      setToken("", result.email); setEmailPassword(""); setMessage(`${result.message} Other signed-in devices were revoked.`);
      if (emailDraft.current.revision === submitted.revision) { emailDraft.current = { value: result.email, revision: submitted.revision, dirty: false }; setEmail(result.email); }
    }, "The account email could not be updated.");
  };
  const savePassword = async (event: React.FormEvent) => {
    event.preventDefault();
    if (newPassword !== confirmPassword) { setError("The new password confirmation does not match."); return; }
    await runMutation(async (owned) => {
      const result = await updateAccountPassword(currentPassword, newPassword); if (!owned()) return;
      setCurrentPassword(""); setNewPassword(""); setConfirmPassword(""); setMessage(`${result.message} Other signed-in devices were revoked.`);
    }, "The password could not be updated.");
  };
  const saveSessionPolicy = async (event: React.FormEvent) => {
    event.preventDefault(); if (!summaryFresh) return;
    const submitted = { value: Number(sessionLifetimeDays), revision: lifetimeDraft.current.revision };
    await runMutation(async (owned) => {
      const result = await updateSessionPolicy(submitted.value); if (!owned()) return;
      setMessage(result.message);
      if (lifetimeDraft.current.revision === submitted.revision) { lifetimeDraft.current = { value: String(result.sessionLifetimeDays), revision: submitted.revision, dirty: false }; setSessionLifetimeDays(String(result.sessionLifetimeDays)); }
    }, "The session lifetime could not be updated.");
  };
  const beginSetup = async () => { if (!summaryFresh) return; await runMutation(async (owned) => { setSetupCopyStatus(""); const result = await setup2FA(); if (owned()) setSetup(result); }, "TOTP setup failed.", false, "TOTP enrollment"); };
  const confirmSetup = async () => { if (!setup || !summaryFresh) return; await runMutation(async (owned) => { const result = await verifySetup2FA(setup.enrollmentId, setupCode); if (!owned()) return; setRecoveryCodes(result.recoveryCodes); setCodesSaved(false); setSetup(null); setSetupCode(""); setSetupCopyStatus(""); setMessage(result.message); }, "The code was not accepted."); };
  const cancelSetup = async () => { if (!setup) return; await runMutation(async (owned) => { await cancelSetup2FA(setup.enrollmentId); if (owned()) { setSetup(null); setSetupCode(""); setSetupCopyStatus(""); } }, "TOTP enrollment could not be cancelled.", false); };
  const copySetupKey = async () => { if (!setup) return; try { await navigator.clipboard.writeText(setup.manualKey); if (active.current) setSetupCopyStatus("copied"); } catch { if (active.current) setSetupCopyStatus("failed"); } };
  const disable = async () => { if (!summaryFresh) return; await runMutation(async (owned) => { const result = await disable2FA(disableCode); if (owned()) { setDisableCode(""); setMessage(result.message); } }, "TOTP could not be disabled."); };
  const regenerate = async () => { if (!summaryFresh) return; await runMutation(async (owned) => { const result = await regenerateRecoveryCodes(); if (owned()) { setRecoveryCodes(result.recoveryCodes); setCodesSaved(false); setMessage("New recovery codes generated. Previous codes no longer work."); } }, "Recovery codes could not be generated.", true, "Recovery-code generation"); };
  const copyCodes = async () => { await navigator.clipboard.writeText(recoveryCodes.join("\n")); if (active.current) setMessage("Recovery codes copied."); };
  const downloadCodes = () => { const blob = new Blob([`StreamHome recovery codes\nGenerated ${new Date().toISOString()}\n\n${recoveryCodes.join("\n")}\n`], { type: "text/plain" }); const href = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = href; anchor.download = "streamhome-recovery-codes.txt"; anchor.click(); URL.revokeObjectURL(href); };
  const revoke = async (session: AuthSessionInfo) => { if (!sessionsFresh) return; await runMutation(async (owned) => { const result = await revokeAuthSession(session.id); if (owned() && result.currentSession) logout(); }, "Session could not be revoked."); };
  const revokeOthers = async () => { if (!sessionsFresh) return; await runMutation(async (owned) => { const result = await revokeOtherSessions(); if (owned()) setMessage(`${result.revokedCount} other session${result.revokedCount === 1 ? "" : "s"} signed out.`); }, "Other sessions could not be revoked."); };
  const loadMore = async () => {
    if (!nextCursor || paginationPending.current || eventsRead.status !== "ready") return;
    const generation = authorization.current; const request = requests.current.events;
    const owned = () => active.current && gateRef.current === "ready" && generation === authorization.current && request === requests.current.events;
    paginationPending.current = true; setPaginationBusy(true); setPaginationError("");
    try { const result = await getSecurityEvents(nextCursor); if (owned()) { setEvents((current) => [...current, ...result.events.filter((item) => !current.some((existing) => existing.id === item.id))]); setNextCursor(result.nextCursor); } }
    catch (requestError) { if (owned()) { if (requestError instanceof ApiError && requestError.code === "reauthentication_required") lockWorkspace(); else setPaginationError(requestError instanceof Error ? requestError.message : "Earlier activity could not be loaded."); } }
    finally { if (owned()) { paginationPending.current = false; setPaginationBusy(false); } }
  };
  const summaryUnknown = summaryRead.status === "error" ? "Unavailable" : "Loading…";
  const pageHeading = <header className="admin-panel__header"><p>IDENTITY / ACCESS</p><h1>Account and Security</h1><span>Manage sign-in, recovery and devices for your server account.</span></header>;
  return <section className="admin-panel admin-panel--account admin-security">
    {gate !== "ready" ? <>{pageHeading}<section className="security-gate">
      <p className="security-eyebrow">Sensitive controls</p><h2 ref={gateHeading} tabIndex={-1}>{gate === "checking" ? "Checking access" : "Confirm your identity"}</h2><p>Verify access to your account’s sensitive controls.</p>
      {gate === "locked" && <>{error && <p className="security-error" role="alert">{error}</p>}<ReauthenticationForm onSuccess={async () => { authorization.current += 1; gateRef.current = "ready"; setGate("ready"); setError(""); await load(); }} /></>}
    </section></> : <div className="account-workspace"><AccountSectionNavigation /><div className="account-main">{pageHeading}<div className="security-content">
      <div className="security-heading"><div><p className="security-eyebrow">Server account controls</p><h2>Account overview</h2><span>Account changes apply across StreamHome and appear in security activity.</span></div><button onClick={() => void load()} disabled={busy || [summaryRead, sessionsRead, eventsRead].some((read) => read.status === "loading" || read.status === "refreshing")}>Refresh</button></div>
      <AnimatePresence>{(error || message) && <motion.div className={error ? "security-notice security-notice--error" : "security-notice"} initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} role={error ? "alert" : "status"}>{error || message}</motion.div>}</AnimatePresence>
      {uncertainMutation && <div className="security-notice security-notice--error"><p role="alert">{uncertainMutation.label} outcome is unconfirmed because its response was lost. {uncertainMutation.label === "Recovery-code generation" ? "A new set may exist and previous codes may no longer work. Unreturned codes cannot be recovered; an explicit new generation replaces that set. " : uncertainMutation.label === "TOTP enrollment" ? "An enrollment may exist on the server. No enrollment identifier was received, so it cannot be cancelled here. " : "The server may have completed the change. "}{uncertainMutation.checked ? "Current status was read successfully. Review it before explicitly repeating a change." : "Check current account status before another change. No change will be retried automatically."}</p><button type="button" disabled={checkingOutcome || busy} onClick={() => void checkMutationOutcome()}>{checkingOutcome ? "Checking current status…" : "Check current account status"}</button></div>}
      <ResourceNotice label="Account summary" state={summaryRead} hasData={summary !== null} retry={() => void readResource("summary")} /><section className="security-overview"><article><a href="#account-credentials">Account</a><strong>{summary?.email ?? summaryUnknown}</strong><small>{summary ? `TOTP ${summary.twoFactorEnabled ? "enabled" : "not enabled"}` : "TOTP status unavailable"}</small></article><article><span>Previous successful login</span><strong>{summary ? formatTime(summary.previousLogin?.at) : summaryUnknown}</strong><small>{summary?.previousLogin ? `${summary.previousLogin.deviceLabel} · ${summary.previousLogin.ipAddress}` : summary ? "No earlier login recorded" : "Login history unavailable"}</small></article><article><a href="#account-sessions">Session policy</a><strong>{summary ? `${summary.sessionLifetimeDays} days` : summaryUnknown}</strong><small>Absolute lifetime for new sign-ins</small></article><article><a href="#account-authenticator">Recovery access</a><strong>{summary ? `${summary.recoveryCodesRemaining} codes remaining` : summaryUnknown}</strong><small>Every code works once</small></article></section>
      <section id="account-authenticator" tabIndex={-1} className="security-card"><header><div><p className="security-eyebrow">Local second factor</p><h2>Authenticator and recovery</h2></div>{summaryFresh && !summary?.twoFactorEnabled && !setup && <button className="security-primary" disabled={busy} onClick={() => void beginSetup()}>Set up TOTP</button>}</header>
        {setup && <div className="security-setup security-totp-enrollment"><div className="security-totp-qr"><img src={setup.qrImageUrl} alt={`Scan this QR code to add StreamHome for ${summary?.email ?? "this account"} to an authenticator app`} /><div><h3>Scan with your authenticator</h3><p>This QR code is generated by your StreamHome server and expires at {new Date(setup.expiresAt * 1000).toLocaleTimeString()}.</p></div></div><div className="security-totp-manual"><span>Can&apos;t scan? Enter this setup key manually.</span><code>{setup.manualKey}</code><button type="button" onClick={() => void copySetupKey()}>{setupCopyStatus === "copied" ? "Copied" : "Copy setup key"}</button>{setupCopyStatus === "failed" && <small role="status">Copy was blocked. Select the key and copy it manually.</small>}</div><AuthenticatorCodeInput label="TOTP setup code" value={setupCode} onChange={setSetupCode} disabled={busy} /><div className="security-inline-actions"><button type="button" onClick={() => void cancelSetup()} disabled={busy}>Cancel</button><button className="security-primary" disabled={setupCode.length !== 6 || busy || !summaryFresh} onClick={() => void confirmSetup()}>Enable TOTP</button></div></div>}
        {summary?.twoFactorEnabled && <div className="security-actions"><div><h3>Recovery codes</h3><p>Generating a new set immediately invalidates all previous codes. Save the new codes before leaving this page; they are shown only once.</p><button onClick={() => void regenerate()} disabled={busy || !summaryFresh}>Regenerate codes</button></div><div><h3>Disable TOTP</h3><p>This also invalidates recovery codes and signs out other devices.</p><AuthenticatorCodeInput label="TOTP code to disable" value={disableCode} onChange={setDisableCode} disabled={busy} /><button className="security-danger" disabled={disableCode.length !== 6 || busy || !summaryFresh} onClick={() => void disable()}>Disable TOTP</button></div></div>}
      </section>
      {recoveryCodes.length > 0 && <section className="security-card security-recovery"><header><div><p className="security-eyebrow">Show once</p><h2>Save your recovery codes now</h2></div></header><p>Each code can replace your authenticator once. They cannot be displayed again.</p><div className="security-code-grid">{recoveryCodes.map((item) => <code key={item}>{item}</code>)}</div><div className="security-inline-actions"><button onClick={() => void copyCodes()}>Copy all</button><button onClick={downloadCodes}>Download .txt</button></div><label><input type="checkbox" checked={codesSaved} onChange={(event) => setCodesSaved(event.target.checked)} /> I saved these codes somewhere secure</label><button className="security-primary" disabled={!codesSaved} onClick={() => setRecoveryCodes([])}>Finish</button></section>}
      <section id="account-credentials" tabIndex={-1} className="security-card security-credentials"><header><div><p className="security-eyebrow">Administrator identity</p><h2>Email and password</h2></div></header><div className="security-credential-grid">
        <form onSubmit={saveEmail}><div><h3>Account email</h3><p>This address is used to sign in. Updating it signs out every other device.</p></div><label>Email address<input type="email" autoComplete="email" value={email} onChange={(event) => { const value = event.target.value; emailDraft.current = { value, revision: emailDraft.current.revision + 1, dirty: value.trim().toLowerCase() !== confirmed.current?.email.toLowerCase() }; setEmail(value); }} required /></label><CredentialPassword label="Current password" purpose="for email change" autoComplete="current-password" value={emailPassword} onChange={(event) => setEmailPassword(event.target.value)} required disabled={busy} /><button className="security-primary" disabled={busy || !summaryFresh || !summary || email.trim().toLowerCase() === summary.email.toLowerCase()}>Update email</button></form>
        <form onSubmit={savePassword}><div><h3>Change password</h3><p>Use at least six characters. Updating it invalidates pending challenges and signs out other devices.</p></div><CredentialPassword label="Current password" purpose="for password change" autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} required disabled={busy} /><CredentialPassword label="New password" purpose="for password change" autoComplete="new-password" minLength={6} maxLength={72} value={newPassword} onChange={(event) => setNewPassword(event.target.value)} required disabled={busy} /><CredentialPassword label="Confirm new password" purpose="for password change" autoComplete="new-password" minLength={6} maxLength={72} value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} required disabled={busy} /><button className="security-primary" disabled={busy || mutationNeedsCheck || newPassword.length < 6 || confirmPassword.length < 6}>Update password</button></form>
      </div></section>
      <section id="account-sessions" tabIndex={-1} className="security-card"><header><div><p className="security-eyebrow">Session access policy</p><h2>Active sessions</h2></div><button onClick={() => void revokeOthers()} disabled={busy || !sessionsFresh || sessions.length < 2}>Sign out all other devices</button></header><form className="security-session-policy" onSubmit={saveSessionPolicy}><label>New-session lifetime<div><input aria-label="Session lifetime in days" type="number" min={1} max={365} value={sessionLifetimeDays} onChange={(event) => { const value = event.target.value; lifetimeDraft.current = { value, revision: lifetimeDraft.current.revision + 1, dirty: Number(value) !== confirmed.current?.sessionLifetimeDays }; setSessionLifetimeDays(value); }} /><span>days</span></div></label><p>Applies to future sign-ins. Existing sessions retain the expiration shown below.</p><button className="security-primary" disabled={busy || !summaryFresh || !summary || Number(sessionLifetimeDays) === summary.sessionLifetimeDays || Number(sessionLifetimeDays) < 1 || Number(sessionLifetimeDays) > 365}>Save lifetime</button></form><ResourceNotice label="Sessions" state={sessionsRead} hasData={sessionsLoaded} retry={() => void readResource("sessions")} /><div className="security-session-list">{sessions.map((item) => <article key={item.id}><div><strong>{item.deviceLabel}{item.current && <b>Current</b>}</strong><span>{item.ipAddress} · Last active {formatTime(item.lastSeenAt)}</span><small>Expires {formatTime(item.expiresAt)}</small></div><button className={item.current ? "security-danger" : ""} disabled={busy || !sessionsFresh} onClick={() => void revoke(item)}>{item.current ? "Sign out" : "Revoke"}</button></article>)}</div></section>
      <section id="account-activity" tabIndex={-1} className="security-card"><header><div><p className="security-eyebrow">180-day history</p><h2>Security activity</h2></div></header><ResourceNotice label="Security activity" state={eventsRead} hasData={eventsLoaded} retry={() => void readResource("events")} /><div className="security-event-list">{events.map((item) => <button type="button" className="security-event-entry" key={item.id} aria-haspopup="dialog" onClick={(event) => { eventInvoker.current = event.currentTarget; setSelectedEvent(item); }}><i data-outcome={item.outcome} aria-hidden="true" /><span className="security-event-entry__copy"><strong>{eventLabel(item.type)}</strong><small>{item.deviceLabel} · {item.ipAddress}</small></span><span className="security-event-entry__details" aria-hidden="true">View details →</span><time dateTime={new Date(item.createdAt * 1000).toISOString()}>{formatTime(item.createdAt)}</time></button>)}{eventsLoaded && events.length === 0 && <p>No security activity has been recorded yet.</p>}</div>{paginationError && <p className="security-error" role="alert">{paginationError}</p>}{nextCursor && <button className="security-load-more" disabled={busy || paginationBusy || eventsRead.status !== "ready"} onClick={() => void loadMore()}>{paginationBusy ? "Loading earlier activity…" : paginationError ? "Retry earlier activity" : "Load earlier activity"}</button>}</section>
    </div></div></div>}
    {gate === "ready" && <SecurityEventDialog event={selectedEvent} onClose={closeEventDialog} returnFocus={() => gateRef.current === "ready" ? eventInvoker.current : gateHeading.current} />}
  </section>;
}
