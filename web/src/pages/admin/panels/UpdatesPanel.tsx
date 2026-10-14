import React, { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  cancelPendingUpdate,
  checkForUpdates,
  getUpdateStatus,
  installUpdate,
  updateUpdatePolicy,
} from "../../../api/updates";
import { ApiError } from "../../../api/client";
import { Button } from "../../../components/ui/Button";
import { GlassPane } from "../../../components/ui/GlassPane";
import { Modal } from "../../../components/ui/Modal";
import { MOTION_TIMINGS } from "../../../motion/motionSystem";
import type { UpdatePolicy, UpdateStatus } from "../../../types/api";
import { SudoModal } from "../SudoModal";
import { AdminHelp } from "../AdminHelp";
import { AdminSelect } from "../../../components/ui/AdminSelect";

type ProtectedAction = "save" | "install-now" | "install-idle" | "retry" | "cancel" | "read" | null;

const ACTIVE_PHASES = new Set(["checking", "queued", "preflight", "waiting_for_idle", "stopping", "installing", "starting", "rolling_back", "recovering"]);
const CANCELLABLE_PHASES = new Set(["queued", "preflight", "waiting_for_idle"]);

function shortCommit(commit: string): string {
  return commit ? commit.slice(0, 12) : "Unknown";
}

function formatTime(timestamp: number | null): string {
  return timestamp ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(timestamp * 1000)) : "Never";
}

export function UpdatesPanel() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [policy, setPolicy] = useState<UpdatePolicy | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [protectedAction, setProtectedAction] = useState<ProtectedAction>(null);
  const [confirmImmediate, setConfirmImmediate] = useState(false);
  const confirmationHeading = useRef<HTMLHeadingElement>(null);
  const [needsAuthorization, setNeedsAuthorization] = useState(false);

  useEffect(() => {
    if (!confirmImmediate) return;
    const heading = confirmationHeading.current;
    const dialog = heading?.closest<HTMLElement>('[role="dialog"]');
    // A footer button would scroll a short dialog past its consequences.
    heading?.focus({ preventScroll: true });
    if (dialog) dialog.scrollTop = 0;
  }, [confirmImmediate]);

  const applyStatus = useCallback((next: UpdateStatus, preserveDraft = false) => {
    setStatus(next);
    setPolicy((draft) => preserveDraft && draft ? draft : next.policy);
  }, []);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      applyStatus(await getUpdateStatus(), quiet);
      setNeedsAuthorization(false);
      setError("");
      return true;
    } catch (requestError) {
      setNeedsAuthorization(requestError instanceof ApiError && requestError.code === "reauthentication_required");
      setError(requestError instanceof Error ? requestError.message : "Update status could not be loaded.");
      return false;
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [applyStatus]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!status || needsAuthorization) return;
    const interval = window.setInterval(
      () => { void load(true); },
      ACTIVE_PHASES.has(status.phase) || status.updateInProgress ? 5_000 : 30_000,
    );
    return () => window.clearInterval(interval);
  }, [load, status, needsAuthorization]);

  const run = async (action: () => Promise<UpdateStatus>, successMessage: string, preserveDraft = true) => {
    setWorking(true);
    setError("");
    setMessage("");
    try {
      applyStatus(await action(), preserveDraft);
      setMessage(successMessage);
      setProtectedAction(null);
    } catch (requestError) {
      setNeedsAuthorization(requestError instanceof ApiError && requestError.code === "reauthentication_required");
      setError(requestError instanceof Error ? requestError.message : "The update action failed.");
      throw requestError;
    } finally {
      setWorking(false);
    }
  };

  const check = () => run(checkForUpdates, "Update check completed.").catch(() => undefined);
  const performProtectedAction = async () => {
    if (protectedAction === "read") { if (await load(true)) setProtectedAction(null); else throw new Error("Update status could not be loaded. Close this dialog and try again."); return; }
    if (!protectedAction || !policy) return;
    if (protectedAction === "save") await run(() => updateUpdatePolicy(policy), "Automatic update settings saved.", false);
    if (protectedAction === "install-now") await run(() => installUpdate("now"), "Official update.sh launch requested. StreamHome will restart after protected operations finish.");
    if (protectedAction === "install-idle") await run(() => installUpdate("when_idle"), "Update queued. Installation begins after all activity becomes idle.");
    if (protectedAction === "retry") await run(() => installUpdate("when_idle", true), "The failed target was explicitly queued for one retry.");
    if (protectedAction === "cancel") await run(cancelPendingUpdate, "Update cancellation requested.");
  };

  const header = (
    <header className="admin-panel__header">
      <p>SERVER / RELEASE LIFECYCLE</p>
      <h1>Updates and maintenance</h1>
      <span>Check releases and choose when to restart. If an update cannot recover safely, the server operator must resolve it.</span>
    </header>
  );

  const authorizationModal = <SudoModal isOpen={protectedAction !== null}
    actionLabel={protectedAction === "read" ? "view update controls" : protectedAction === "save" ? "save the update policy" : protectedAction === "cancel" ? "cancel the pending update" : protectedAction === "retry" ? "retry the failed update" : protectedAction === "install-now" ? "install the update now" : "queue the update for idle time"}
    onCancel={() => setProtectedAction(null)} onSuccess={performProtectedAction} />;

  const authorizationNotice = <div className="admin-authorization-notice" role="status"><p>Administrator access expired. Confirm your identity to continue.</p><Button onClick={() => setProtectedAction("read")}>Confirm your identity</Button></div>;

  if (!status || !policy) {
    return (
      <section className="admin-panel admin-panel--updates">
        {header}
        <GlassPane className="admin-state-card" spotlight={false}>
          <p>{loading ? "CONTACTING UPDATE SERVICE" : needsAuthorization ? "IDENTITY CONFIRMATION" : "UPDATE STATUS UNAVAILABLE"}</p>
          <h2>{loading ? "Loading update state…" : needsAuthorization ? "Confirm access to Updates" : "Update controls could not be loaded."}</h2>
          {needsAuthorization ? authorizationNotice : <>{error && <span role="alert">{error}</span>}{!loading && <Button variant="secondary" onClick={() => void load()}>Try again</Button>}</>}
        </GlassPane>
        {authorizationModal}
      </section>
    );
  }

  const recoveryRequired = status.phase === "recovery_required";
  const busy = recoveryRequired || needsAuthorization || working || status.updateInProgress || ACTIVE_PHASES.has(status.phase);
  const failedTargetSuppressed = Boolean(status.failedTarget && status.failedTarget === status.targetCommit);
  const policyDirty = JSON.stringify(policy) !== JSON.stringify(status.policy);

  return (
    <section className="admin-panel admin-panel--updates">
      {header}
      {needsAuthorization && authorizationNotice}
      <div className="update-overview-grid">
        <GlassPane className="admin-card update-release-card" spotlight={false}>
          <div className="update-status-line">
            <span className="update-phase" data-phase={status.phase}>{status.phase.replace(/_/g, " ")}</span>
            <span>{recoveryRequired ? "Installation paused pending recovery" : status.installMode === "now" && ACTIVE_PHASES.has(status.phase) ? "Immediate mode ignores the window" : status.maintenanceWindowOpen ? "Maintenance window open" : "Outside maintenance window"}</span>
          </div>
          <h2>{status.message}</h2>
          <dl className="update-release-summary">
            <div><dt>Installed release</dt><dd><code>{shortCommit(status.currentCommit)}</code></dd></div>
            <div><dt>Available release</dt><dd><code>{shortCommit(status.targetCommit)}</code></dd></div>
            <div><dt>Last checked · your local time</dt><dd>{formatTime(status.lastCheckedAt)}</dd></div>
          </dl>
          {recoveryRequired && <div className="update-operator-guidance" role="status"><h3>Ask the server operator to recover this update</h3><p>Automatic installation is paused. On the server, the operator must inspect the recovery diagnostics and select the recovery action supported by that state. Keep recovery files until the operation is resolved.</p><p>Share the diagnostic reference and transaction below with the operator, then refresh this status after recovery. This screen cannot perform operator recovery.</p></div>}
          {!recoveryRequired && status.blockers.length > 0 && (
            <div className="update-blockers">
              <strong>{status.installMode === "now" && ACTIVE_PHASES.has(status.phase) ? "Protected activity must finish" : status.phase === "waiting_for_idle" ? "Waiting for idle" : "Idle installation blockers"}</strong>
              <ul>{status.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul>
            </div>
          )}
          <div className="update-actions">
            {recoveryRequired ? <Button type="button" variant="secondary" disabled={loading || working || needsAuthorization} onClick={() => void load(true)}>Refresh status</Button> : <>
            <Button type="button" variant="secondary" disabled={busy} onClick={() => void check()}>{working ? "Working…" : "Check now"}</Button>
            {status.updateAvailable && status.phase !== "queued" && <Button type="button" disabled={busy} onClick={() => setConfirmImmediate(true)}>Update now</Button>}
            {status.updateAvailable && status.phase !== "queued" && <Button type="button" variant="secondary" disabled={busy} onClick={() => setProtectedAction("install-idle")}>Install when idle</Button>}
            {failedTargetSuppressed && <Button type="button" disabled={busy} onClick={() => setProtectedAction("retry")}>Retry failed target</Button>}
            {CANCELLABLE_PHASES.has(status.phase) && <Button type="button" variant="ghost" disabled={working} onClick={() => setProtectedAction("cancel")}>Cancel update</Button>}
            </>}
          </div>
          {recoveryRequired && <div className="update-recovery-reference"><p>Diagnostic reference: <code>{status.diagnosticId || "Not reported"}</code></p><p>Transaction: <code>{status.transactionId || "Not reported"}</code></p></div>}
          <AdminHelp label="Release and recovery details" title="Release and recovery details" variant="details">
          <dl className="update-commit-grid">
            <div><dt>Installed</dt><dd>{shortCommit(status.currentCommit)}</dd></div>
            <div><dt>Available</dt><dd>{shortCommit(status.targetCommit)}</dd></div>
            <div><dt>Channel</dt><dd>{policy.branch}</dd></div>
            <div><dt>Last check</dt><dd>{formatTime(status.lastCheckedAt)}</dd></div>
            <div><dt>Transaction</dt><dd><code>{status.transactionId || "None"}</code></dd></div>
            <div><dt>Lifecycle update</dt><dd>{formatTime(status.updatedAt)}</dd></div>
          </dl>
          {status.recoveryRequestedAt && <p className="update-recovery-notice">Automatic recovery requested {formatTime(status.recoveryRequestedAt)}.</p>}
          {status.diagnosticId && <p className="update-recovery-notice">Diagnostic reference: <code>{status.diagnosticId}</code></p>}
          </AdminHelp>
        </GlassPane>

        <GlassPane className="admin-settings-card update-policy-card" spotlight={false}>
          <div className="update-card-heading"><p>AUTOMATIC POLICY</p><h2>Automatic updates</h2><span>Queue verified releases and install when server activity is idle.</span></div>
          {recoveryRequired && <p className="admin-draft-help">Policy changes are unavailable until operator recovery is resolved.</p>}
          <fieldset className="update-policy-fields" disabled={recoveryRequired || needsAuthorization || working}>
          <label className="update-toggle">
            <input type="checkbox" checked={policy.automaticUpdates} onChange={(event) => setPolicy({ ...policy, automaticUpdates: event.target.checked })} />
            <span><strong>Automatic updates</strong><small>Check the official {policy.branch} channel and queue validated commits.</small></span>
          </label>
          <div className="update-policy-layout">
          <section className="update-policy-group" aria-labelledby="update-checking-title"><h3 id="update-checking-title">Checks and idle time</h3><div className="admin-settings-grid">
            <div className="admin-control">
              <AdminSelect label="Idle grace period" value={String(policy.idleMinutes)} disabled={recoveryRequired || needsAuthorization || working} onChange={(value) => setPolicy({ ...policy, idleMinutes: Number(value) })} options={[5, 10, 15, 30, 60, 120].map((minutes) => ({ value: String(minutes), label: `${minutes} minutes` }))} aria-describedby="update-idle-help" />
              <small id="update-idle-help">Browser, playback, transfers and API activity must stay quiet for this long.</small>
            </div>
            <div className="admin-control">
              <AdminSelect label="Check interval" value={String(policy.checkIntervalHours)} disabled={recoveryRequired || needsAuthorization || working} onChange={(value) => setPolicy({ ...policy, checkIntervalHours: Number(value) })} options={[1, 3, 6, 12, 24].map((hours) => ({ value: String(hours), label: `Every ${hours} hour${hours === 1 ? "" : "s"}` }))} aria-describedby="update-check-help" />
              <small id="update-check-help">A failed release is not retried automatically.</small>
            </div>
          </div></section>
          <section className="update-policy-group" aria-labelledby="update-window-title"><h3 id="update-window-title">Maintenance window</h3><div className="admin-settings-grid">
            <label className="admin-control admin-control--input">
              <span>Maintenance window start</span>
              <input type="time" value={policy.maintenanceStart ?? ""} onChange={(event) => setPolicy({ ...policy, maintenanceStart: event.target.value || null })} />
            </label>
            <label className="admin-control admin-control--input">
              <span>Maintenance window end</span>
              <input type="time" value={policy.maintenanceEnd ?? ""} onChange={(event) => setPolicy({ ...policy, maintenanceEnd: event.target.value || null })} />
            </label>
          </div><p>Leave both times empty for any idle time. Overnight windows are supported.</p>
          {status.serverClock ? <div className="update-server-clock" aria-label="Server clock">
            <p><strong>Server time zone:</strong> {status.serverClock.timeZone} ({status.serverClock.utcOffset})</p>
            <p><strong>Server local time at last refresh:</strong> <time>{status.serverClock.localTime}</time></p>
            <p>Both window times use this server clock. The UTC offset can change with daylight saving time.</p>
          </div> : <p role="status">Server time zone unavailable. Refresh status to request it. If it remains unavailable, ask the server operator to confirm the host time zone before relying on a timed window.</p>}
          </section>
          </div>
          <footer className="admin-settings-actions">
            <span>{policy.requireSignedCommits ? "Signed commits required" : "Only verified official updates are permitted"}</span>
            <Button type="button" disabled={!policyDirty || busy} onClick={() => setProtectedAction("save")}>Save update policy</Button>
          </footer>
          </fieldset>
        </GlassPane>
      </div>

      <AnimatePresence mode="wait">
        {error ? <motion.p key="error" className="admin-form-message admin-form-message--error update-panel-message" role="alert" initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: MOTION_TIMINGS.notice }}>{error}</motion.p> : message ? <motion.p key="message" className="admin-form-message admin-form-message--success update-panel-message" role="status" initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: MOTION_TIMINGS.notice }}>{message}</motion.p> : null}
      </AnimatePresence>

      <section className="update-diagnostics" aria-label="Update diagnostics"><div><h2>Lifecycle output</h2><p>{status.logTail.length ? "Inspect the latest output when investigating an update." : "No update lifecycle has run yet."}</p></div><AdminHelp label="View lifecycle output" title="Latest lifecycle output" variant="details"><pre className="update-lifecycle-output">{status.logTail.length ? status.logTail.join("\n") : "No update lifecycle has run yet."}</pre></AdminHelp></section>

      <Modal ariaLabel="Update StreamHome now?" isOpen={confirmImmediate} onClose={() => setConfirmImmediate(false)} className="update-now-confirmation">
        <section aria-labelledby="update-now-title">
          <header><p>IMMEDIATE MAINTENANCE</p><h2 ref={confirmationHeading} id="update-now-title" tabIndex={-1} onKeyDown={(event) => {
            if (event.key === "Tab" && event.shiftKey) {
              event.preventDefault();
              const buttons = event.currentTarget.closest('[role="dialog"]')?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)');
              buttons?.[buttons.length - 1]?.focus();
            }
          }}>Update StreamHome now?</h2><span>Update checks start immediately in a separate environment. If they pass, StreamHome disconnects viewers and restarts both services.</span></header>
          <ul><li>Browser presence, playback, the maintenance window, and the idle grace period will not delay cutover.</li><li>Active ingestion, downloads, media processing, backups, and restores must finish or be cancelled first.</li><li>Early verification failures can roll back. Once the new runtime is committed, a failed update stays in protected recovery and may require the server operator.</li></ul>
          <div className="update-now-confirmation__actions"><Button type="button" variant="ghost" onClick={() => setConfirmImmediate(false)}>Cancel</Button><Button type="button" onClick={() => { setConfirmImmediate(false); setProtectedAction("install-now"); }}>Continue to authorization</Button></div>
        </section>
      </Modal>

      {authorizationModal}
    </section>
  );
}
