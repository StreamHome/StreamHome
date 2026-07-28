import React, { useCallback, useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  cancelPendingUpdate,
  checkForUpdates,
  getUpdateStatus,
  installUpdate,
  updateUpdatePolicy,
} from "../../../api/updates";
import { Button } from "../../../components/ui/Button";
import { GlassPane } from "../../../components/ui/GlassPane";
import { Modal } from "../../../components/ui/Modal";
import { MOTION_TIMINGS } from "../../../motion/motionSystem";
import type { UpdatePolicy, UpdateStatus } from "../../../types/api";
import { SudoModal } from "../SudoModal";

type ProtectedAction = "save" | "install-now" | "install-idle" | "retry" | "cancel" | null;

const ACTIVE_PHASES = new Set(["queued", "preflight", "waiting_for_idle", "stopping", "installing", "starting", "rolling_back"]);

function shortCommit(commit: string): string {
  return commit ? commit.slice(0, 12) : "Unknown";
}

function formatTime(timestamp: number | null): string {
  return timestamp ? new Date(timestamp * 1000).toLocaleString() : "Never";
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

  const applyStatus = useCallback((next: UpdateStatus) => {
    setStatus(next);
    setPolicy(next.policy);
  }, []);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      applyStatus(await getUpdateStatus());
      setError("");
    } catch (requestError) {
      if (!quiet) setError(requestError instanceof Error ? requestError.message : "Update status could not be loaded.");
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [applyStatus]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!status || !ACTIVE_PHASES.has(status.phase)) return;
    const interval = window.setInterval(() => { void load(true); }, 5_000);
    return () => window.clearInterval(interval);
  }, [load, status]);

  const run = async (action: () => Promise<UpdateStatus>, successMessage: string) => {
    setWorking(true);
    setError("");
    setMessage("");
    try {
      applyStatus(await action());
      setMessage(successMessage);
      setProtectedAction(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "The update action failed.");
    } finally {
      setWorking(false);
    }
  };

  const check = () => run(checkForUpdates, "Update check completed.");
  const performProtectedAction = async () => {
    if (!protectedAction || !policy) return;
    if (protectedAction === "save") await run(() => updateUpdatePolicy(policy), "Automatic update settings saved.");
    if (protectedAction === "install-now") await run(() => installUpdate("now"), "Immediate preflight started. StreamHome will restart after protected operations finish.");
    if (protectedAction === "install-idle") await run(() => installUpdate("when_idle"), "Update queued. Installation begins after all activity becomes idle.");
    if (protectedAction === "retry") await run(() => installUpdate("when_idle", true), "The failed target was explicitly queued for one retry.");
    if (protectedAction === "cancel") await run(cancelPendingUpdate, "Pending update cancelled.");
  };

  const header = (
    <header className="admin-panel__header">
      <p>SERVER / RELEASE LIFECYCLE</p>
      <h1>Updates and maintenance</h1>
      <span>Validate releases before downtime, choose an immediate or idle installation, and automatically recover the previous healthy version if startup fails.</span>
    </header>
  );

  if (!status || !policy) {
    return (
      <section className="admin-panel admin-panel--updates">
        {header}
        <GlassPane className="admin-state-card" spotlight={false}>
          <p>{loading ? "CONTACTING UPDATE SERVICE" : "UPDATE STATUS UNAVAILABLE"}</p>
          <h2>{loading ? "Loading update state…" : "Update controls could not be loaded."}</h2>
          {error && <span role="alert">{error}</span>}
          {!loading && <Button variant="secondary" onClick={() => void load()}>Try again</Button>}
        </GlassPane>
      </section>
    );
  }

  const busy = working || ACTIVE_PHASES.has(status.phase);
  const failedTargetSuppressed = Boolean(status.failedTarget && status.failedTarget === status.targetCommit);
  const policyDirty = JSON.stringify(policy) !== JSON.stringify(status.policy);

  return (
    <section className="admin-panel admin-panel--updates">
      {header}
      <div className="update-overview-grid">
        <GlassPane className="admin-card update-release-card" spotlight={false}>
          <div className="update-status-line">
            <span className="update-phase" data-phase={status.phase}>{status.phase.replace(/_/g, " ")}</span>
            <span>{status.installMode === "now" && ACTIVE_PHASES.has(status.phase) ? "Immediate mode ignores the window" : status.maintenanceWindowOpen ? "Maintenance window open" : "Outside maintenance window"}</span>
          </div>
          <h2>{status.message}</h2>
          <dl className="update-commit-grid">
            <div><dt>Installed</dt><dd>{shortCommit(status.currentCommit)}</dd></div>
            <div><dt>Available</dt><dd>{shortCommit(status.targetCommit)}</dd></div>
            <div><dt>Channel</dt><dd>{policy.branch}</dd></div>
            <div><dt>Last check</dt><dd>{formatTime(status.lastCheckedAt)}</dd></div>
          </dl>
          {status.blockers.length > 0 && (
            <div className="update-blockers">
              <strong>{status.installMode === "now" && ACTIVE_PHASES.has(status.phase) ? "Protected activity must finish" : "Waiting for idle"}</strong>
              <ul>{status.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul>
            </div>
          )}
          <div className="update-actions">
            <Button type="button" variant="secondary" disabled={busy} onClick={() => void check()}>{working ? "Working…" : "Check now"}</Button>
            {status.updateAvailable && status.phase !== "queued" && <Button type="button" disabled={busy} onClick={() => setConfirmImmediate(true)}>Update now</Button>}
            {status.updateAvailable && status.phase !== "queued" && <Button type="button" variant="secondary" disabled={busy} onClick={() => setProtectedAction("install-idle")}>Install when idle</Button>}
            {failedTargetSuppressed && <Button type="button" disabled={busy} onClick={() => setProtectedAction("retry")}>Retry failed target</Button>}
            {status.phase === "queued" && <Button type="button" variant="ghost" disabled={working} onClick={() => setProtectedAction("cancel")}>Cancel pending update</Button>}
          </div>
        </GlassPane>

        <GlassPane className="admin-settings-card update-policy-card" spotlight={false}>
          <div className="update-card-heading"><p>AUTOMATIC POLICY</p><h2>Install only during verified idle time</h2></div>
          <label className="update-toggle">
            <input type="checkbox" checked={policy.automaticUpdates} onChange={(event) => setPolicy({ ...policy, automaticUpdates: event.target.checked })} />
            <span><strong>Automatic updates</strong><small>Check the official {policy.branch} channel and queue validated commits.</small></span>
          </label>
          <div className="admin-settings-grid">
            <label className="admin-control">
              <span>Idle grace period</span>
              <select value={policy.idleMinutes} onChange={(event) => setPolicy({ ...policy, idleMinutes: Number(event.target.value) })}>
                {[5, 10, 15, 30, 60, 120].map((minutes) => <option key={minutes} value={minutes}>{minutes} minutes</option>)}
              </select>
              <small>Restarts cannot begin until browser, playback, transfer, and API activity remain quiet for this long.</small>
            </label>
            <label className="admin-control">
              <span>Check interval</span>
              <select value={policy.checkIntervalHours} onChange={(event) => setPolicy({ ...policy, checkIntervalHours: Number(event.target.value) })}>
                {[1, 3, 6, 12, 24].map((hours) => <option key={hours} value={hours}>Every {hours} hour{hours === 1 ? "" : "s"}</option>)}
              </select>
              <small>Failed targets are suppressed and never enter an automatic retry loop.</small>
            </label>
            <label className="admin-control admin-control--input">
              <span>Maintenance window start</span>
              <input type="time" value={policy.maintenanceStart ?? ""} onChange={(event) => setPolicy({ ...policy, maintenanceStart: event.target.value || null })} />
              <small>Leave both times empty to allow automatic installation at any idle time.</small>
            </label>
            <label className="admin-control admin-control--input">
              <span>Maintenance window end</span>
              <input type="time" value={policy.maintenanceEnd ?? ""} onChange={(event) => setPolicy({ ...policy, maintenanceEnd: event.target.value || null })} />
              <small>The server’s local timezone is used; overnight windows are supported.</small>
            </label>
          </div>
          <footer className="admin-settings-actions">
            <span>{policy.requireSignedCommits ? "Signed commits required" : "Official origin + fast-forward verification"}</span>
            <Button type="button" disabled={!policyDirty || working} onClick={() => setProtectedAction("save")}>Save update policy</Button>
          </footer>
        </GlassPane>
      </div>

      <AnimatePresence mode="wait">
        {error ? <motion.p key="error" className="admin-form-message admin-form-message--error update-panel-message" role="alert" initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: MOTION_TIMINGS.notice }}>{error}</motion.p> : message ? <motion.p key="message" className="admin-form-message admin-form-message--success update-panel-message" role="status" initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: MOTION_TIMINGS.notice }}>{message}</motion.p> : null}
      </AnimatePresence>

      <GlassPane className="admin-card update-log-card" spotlight={false}>
        <div className="update-card-heading"><p>RECOVERY LOG</p><h2>Latest lifecycle output</h2></div>
        <pre>{status.logTail.length ? status.logTail.join("\n") : "No update lifecycle has run yet."}</pre>
      </GlassPane>

      <Modal isOpen={confirmImmediate} onClose={() => setConfirmImmediate(false)} className="update-now-confirmation">
        <section aria-labelledby="update-now-title">
          <header><p>IMMEDIATE MAINTENANCE</p><h2 id="update-now-title">Update StreamHome now?</h2><span>The isolated preflight starts immediately. When it passes, StreamHome will disconnect viewers and restart both services.</span></header>
          <ul><li>Browser presence, playback, the maintenance window, and the idle grace period will not delay cutover.</li><li>Active ingestion, downloads, media processing, backups, and restores must finish or be cancelled first.</li><li>Failed startup or health checks still trigger automatic rollback.</li></ul>
          <div className="update-now-confirmation__actions"><Button type="button" variant="ghost" onClick={() => setConfirmImmediate(false)}>Keep waiting</Button><Button type="button" onClick={() => { setConfirmImmediate(false); setProtectedAction("install-now"); }}>Continue to authorization</Button></div>
        </section>
      </Modal>

      <SudoModal
        isOpen={protectedAction !== null}
        actionLabel={
          protectedAction === "save" ? "save the update policy"
            : protectedAction === "cancel" ? "cancel the pending update"
              : protectedAction === "retry" ? "retry the failed update"
                : protectedAction === "install-now" ? "install the update now"
                  : "queue the update for idle time"
        }
        onCancel={() => setProtectedAction(null)}
        onSuccess={performProtectedAction}
      />
    </section>
  );
}
