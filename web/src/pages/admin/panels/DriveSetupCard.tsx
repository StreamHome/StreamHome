import React, { useCallback, useEffect, useRef, useState } from "react";
import * as drive from "../../../api/adminDrive";
import { ApiError } from "../../../api/client";
import { testGoogleDrive } from "../../../api/system";
import type { SystemSettings } from "../../../types/api";
import type { DriveFolderList } from "../../../api/setup";
import { AdminSelect } from "../../../components/ui/AdminSelect";
import { Button } from "../../../components/ui/Button";
import { GlassPane } from "../../../components/ui/GlassPane";
import { Input } from "../../../components/ui/Input";
import { SudoModal } from "../SudoModal";
import "./DriveSetupCard.css";

type Attempt = { kind: "start" | "cancel" | "create" | "select" | "test" | "save"; id?: string; previousId?: string; path?: string };
const terminal = new Set(["saved", "failed", "cancelled", "expired"]);
const messageFor = (error: unknown) => error instanceof Error ? error.message : "Drive is unavailable. Check status before continuing.";

export function DriveSetupCard({ settings, onSettingsRefresh }: { settings: SystemSettings | null; onSettingsRefresh: () => void }) {
  const [setup, setSetup] = useState<drive.AdminDriveSetup | null>(null);
  const [job, setJob] = useState<drive.AdminDriveJob | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [locked, setLocked] = useState(false);
  const [reauthNeeded, setReauthNeeded] = useState(false);
  const [sudo, setSudo] = useState(false);
  const [unknown, setUnknown] = useState<Attempt | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [formVisible, setFormVisible] = useState(false);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [audience, setAudience] = useState<"external" | "internal">("external");
  const [publishing, setPublishing] = useState<"testing" | "production">("testing");
  const [testConsent, setTestConsent] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [folderPath, setFolderPath] = useState("");
  const [folders, setFolders] = useState<DriveFolderList | null>(null);
  const [folderLoading, setFolderLoading] = useState(false);
  const [folderError, setFolderError] = useState("");
  const [newFolder, setNewFolder] = useState("");
  const live = useRef(false);
  const generation = useRef(0);
  const readers = useRef(0);
  const folderReaders = useRef(0);
  const folderReadCount = useRef(0);
  const busy = useRef(false);
  const jobRef = useRef<drive.AdminDriveJob | null>(null);
  const unknownRef = useRef<Attempt | null>(null);
  const popup = useRef<Window | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const clientIdInput = useRef<HTMLInputElement>(null);
  useEffect(() => { if (formVisible && !jobRef.current) clientIdInput.current?.focus(); }, [formVisible]);
  const publishJob = useCallback((value: drive.AdminDriveJob | null) => { jobRef.current = value; setJob(value); }, []);
  const publishUnknown = (value: Attempt | null) => { unknownRef.current = value; setUnknown(value); };
  const reauth = useCallback((requestError: unknown) => {
    if (requestError instanceof ApiError && requestError.code === "reauthentication_required") {
      readers.current += 1; folderReaders.current += 1;
      setLocked(true); setReauthNeeded(true); setLoading(false); setFolderLoading(false); setSudo(true); setError("Confirm your identity, then check the current Drive status.");
      return true;
    }
    return false;
  }, []);
  const refresh = useCallback(async () => {
    if (busy.current) return;
    const owner = generation.current;
    const reader = ++readers.current;
    setLoading(true); setError("");
    try {
      const value = await drive.getAdminDriveSetup();
      if (!live.current || owner !== generation.current || reader !== readers.current || busy.current) return;
      setSetup(value); publishJob(value.job); setLocked(false); setReauthNeeded(false);
      if (value.job) setFormVisible(true);
      // A missing job never proves that a lost start or cancellation failed.
      const attempt = unknownRef.current;
      let confirmed = !attempt;
      if (attempt?.kind === "start") confirmed = value.job !== null && (attempt.id ? value.job.id === attempt.id : value.job.id !== attempt.previousId);
      else if (attempt && value.job && value.job.id === attempt.id) {
        confirmed = attempt.kind === "cancel" ? value.job.status === "cancelled"
          : attempt.kind === "save" ? value.configured && value.job.status === "saved"
          : attempt.kind === "select" ? value.job.selectedPath === attempt.path
          : attempt.kind === "test" ? value.job.status === "ready" : false;
        if (attempt.kind === "create" && attempt.path && value.job.pendingOperation === null) {
          const parent = attempt.path.split("/").slice(0, -1).join("/");
          const listing = await drive.listAdminDriveFolders(attempt.id!, parent);
          if (!live.current || owner !== generation.current || reader !== readers.current || busy.current) return;
          confirmed = listing.folders.some(folder => folder.path === attempt.path);
        }
      }
      if (confirmed && attempt) {
        publishUnknown(null); setNotice("The server confirmed the current state. Continue from the step below.");
        if (value.configured) onSettingsRefresh();
      } else if (attempt) setError("The previous change is still unconfirmed. Check status again; do not repeat it.");
    } catch (requestError) {
      if (live.current && owner === generation.current && reader === readers.current) { if (!reauth(requestError)) setError(messageFor(requestError)); }
    } finally { if (live.current && owner === generation.current && reader === readers.current) setLoading(false); }
  }, [onSettingsRefresh, publishJob, reauth]);
  useEffect(() => {
    live.current = true; void refresh();
    return () => { live.current = false; generation.current += 1; readers.current += 1; folderReaders.current += 1; popup.current?.close(); };
  }, [refresh]);

  const readFolders = useCallback(async (id: string, path: string) => {
    const owner = generation.current;
    const reader = ++folderReaders.current;
    folderReadCount.current += 1;
    setFolders(null); setFolderLoading(true); setFolderError("");
    try {
      const listing = await drive.listAdminDriveFolders(id, path);
      if (live.current && owner === generation.current && reader === folderReaders.current && jobRef.current?.id === id && !busy.current) setFolders(listing);
    } catch (requestError) {
      if (live.current && owner === generation.current && reader === folderReaders.current) { if (!reauth(requestError)) setFolderError(messageFor(requestError)); }
    } finally { folderReadCount.current -= 1; if (live.current && owner === generation.current && reader === folderReaders.current) setFolderLoading(false); }
  }, [reauth]);
  const serverPending = Boolean(job?.pendingOperation) || job?.status === "saving";
  useEffect(() => { setTestConsent(false); setAcknowledged(false); }, [job?.id, job?.selectedPath]);
  const folderEligible = job && !serverPending && ["selecting_folder", "ready"].includes(job.status) && !pending && !unknown && !locked;
  useEffect(() => {
    if (folderEligible && job) void readFolders(job.id, folderPath);
    return () => { folderReaders.current += 1; };
  }, [folderEligible, job?.id, folderPath, readFolders]);

  useEffect(() => {
    if (!job || terminal.has(job.status) || pending || unknown || locked) return;
    const id = job.id;
    let stopped = false;
    let inFlight = false;
    const timer = window.setInterval(async () => {
      if (busy.current || unknownRef.current || stopped || inFlight || folderReadCount.current > 0) return;
      inFlight = true;
      const owner = generation.current;
      const reader = ++readers.current;
      try {
        const value = await drive.getAdminDriveJob(id);
        if (stopped || !live.current || generation.current !== owner || readers.current !== reader || busy.current || jobRef.current?.id !== id) return;
        const previousStatus = jobRef.current?.status;
        publishJob(value);
        if (value.status === "selecting_folder" && ["authorizing", "exchanging_code"].includes(previousStatus || "")) { popup.current?.close(); heading.current?.focus(); }
        if (popup.current?.closed && value.status === "authorizing") setNotice("The authorization window was closed. Complete authorization or cancel this connection to start again.");
      } catch (requestError) {
        if (!stopped && live.current && owner === generation.current && reader === readers.current) { if (!reauth(requestError)) { setLocked(true); setError("Drive status could not be refreshed. Check status before continuing."); } }
      } finally { inFlight = false; }
    }, 1500);
    return () => { stopped = true; window.clearInterval(timer); readers.current += 1; };
  }, [job?.id, job?.status, pending, unknown, locked, publishJob, reauth]);

  const mutate = async (attempt: Attempt, action: () => Promise<void>) => {
    if (busy.current || (unknownRef.current && !(attempt.kind === "cancel" && jobRef.current?.pendingOperation === "mkdir_uncertain")) || locked) return;
    busy.current = true; readers.current += 1; folderReaders.current += 1;
    const owner = generation.current;
    setPending(true); setError(""); setNotice("");
    try { await action(); }
    catch (requestError) {
      if (!live.current || owner !== generation.current) return;
      if (!reauth(requestError)) {
        const definite = requestError instanceof ApiError && requestError.status >= 400 && requestError.status < 500;
        if (!definite) { if (!unknownRef.current || attempt.kind === "cancel") publishUnknown(attempt); popup.current?.close(); setError("The change was sent, but its result is unknown. Check status before continuing."); }
        else { setLocked(true); setError(messageFor(requestError)); }
      }
    } finally { if (live.current && owner === generation.current) { busy.current = false; setPending(false); heading.current?.focus(); } }
  };
  const configured = setup?.configured === true || settings?.driveConfigured === true;
  const disabled = pending || serverPending || Boolean(unknown) || locked || loading;
  const activeJob = job && !terminal.has(job.status);
  const accept = (value: drive.AdminDriveJob) => { if (live.current) publishJob(value); };
  const connect = () => {
    if (disabled || busy.current || activeJob) return;
    const opened = window.open("about:blank", "streamhome-admin-drive", "popup,width=600,height=760");
    if (!opened) { setError("Allow popups for StreamHome, then choose Connect Google Drive again. No connection was started."); return; }
    popup.current = opened;
    const attempt: Attempt = { kind: "start", previousId: job?.id };
    void mutate(attempt, async () => {
      try {
        setClientSecret("");
        const result = await drive.startAdminDrive({ client_id: clientId.trim(), client_secret: clientSecret, audience, publishing_status: publishing });
        if (!live.current) return;
        setClientSecret(""); opened.location.href = result.authorizationUrl;
        let current: drive.AdminDriveJob;
        try { current = await drive.getAdminDriveJob(result.jobId); } catch (requestError) { if (live.current) publishUnknown({ ...attempt, id: result.jobId }); throw requestError; }
        if (live.current) { accept(current); setFolderPath(""); setAcknowledged(false); }
      } finally { if (!live.current) opened.close(); }
    });
  };
  const testExisting = async () => {
    if (busy.current || disabled) return;
    busy.current = true; setPending(true); setError(""); setNotice("");
    const owner = generation.current;
    try { await testGoogleDrive(); if (live.current && owner === generation.current) { setNotice("Google Drive is reachable and authorized."); onSettingsRefresh(); } }
    catch (requestError) { if (live.current && owner === generation.current && !reauth(requestError)) setError(messageFor(requestError)); }
    finally { if (live.current && owner === generation.current) { busy.current = false; setPending(false); } }
  };

  return <GlassPane className="admin-settings-card admin-drive-setup" spotlight={false}>
    <header className="admin-drive-setup__header"><p>GOOGLE DRIVE</p><h2 ref={heading} tabIndex={-1}>Google Drive connection</h2><p>Connect a Drive folder. Your storage mode stays unchanged.</p></header>
    {error && <p className="admin-form-message admin-form-message--error" role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {loading && <p role="status">Checking Drive setup…</p>}
    {(error || unknown || locked || serverPending || activeJob) && <Button type="button" variant="ghost" disabled={pending || loading} onClick={() => void refresh()}>Check status</Button>}
    {reauthNeeded && <Button type="button" variant="secondary" onClick={() => setSudo(true)}>Confirm identity</Button>}
    {configured ? <section aria-label="Google Drive status" className="admin-drive-setup__connected">
      <strong>{settings?.driveReachable ? "Connected" : "Connection saved"}</strong>
      <span>{settings?.rcloneRemotePath || (job?.status === "saved" ? `${job.remoteName}:${job.selectedPath}` : "Target saved on the server")}</span>
      {settings?.driveReachable === false && <p>Test the saved connection to check its current availability.</p>}
      <Button type="button" variant="secondary" disabled={disabled} onClick={() => void testExisting()}>{pending ? "Testing…" : "Test connection"}</Button>
    </section> : <>
      {!activeJob && !loading && <p>Not configured</p>}
      <ol className="admin-drive-setup__steps" aria-label="Connection steps"><li aria-current={!activeJob || ["authorizing", "exchanging_code"].includes(job!.status) ? "step" : undefined}>1. Connect</li><li aria-current={job?.status === "selecting_folder" ? "step" : undefined}>2. Choose folder</li><li aria-current={job?.status === "ready" || job?.status === "testing" ? "step" : undefined}>3. Verify &amp; save</li></ol>
      {!formVisible && <Button type="button" className="admin-primary-action" disabled={disabled} onClick={() => setFormVisible(true)}>Connect Google Drive</Button>}
      {formVisible && <section className="admin-drive-setup__oauth" aria-labelledby="drive-oauth-heading">
        <h3 id="drive-oauth-heading">OAuth app settings</h3>
        <p>Create a Web application OAuth client in Google Cloud, enable the Google Drive API, and add the exact callback below as an authorized redirect URI.</p>
        <div className="admin-drive-setup__grid">
          <Input ref={clientIdInput} className="admin-field" label="Client ID" autoComplete="off" value={clientId} disabled={disabled || Boolean(activeJob)} onChange={event => setClientId(event.target.value)} />
          <Input className="admin-field" label="Client secret" type="password" autoComplete="off" value={clientSecret} disabled={disabled || Boolean(activeJob)} onChange={event => setClientSecret(event.target.value)} />
          <AdminSelect label="Audience" value={job && activeJob ? job.audience : audience} disabled={disabled || Boolean(activeJob)} onChange={value => setAudience(value as typeof audience)} options={[{ value: "external", label: "External" }, { value: "internal", label: "Internal (Google Workspace)" }]} />
          <AdminSelect label="Publishing status" value={job && activeJob ? job.publishingStatus : publishing} disabled={disabled || Boolean(activeJob)} onChange={value => setPublishing(value as typeof publishing)} options={[{ value: "testing", label: "Testing" }, { value: "production", label: "In production" }]} />
        </div>
        <p id="drive-publishing-help">These fields describe your Google Cloud app; changing them here does not change Google's Audience or Publishing status. External apps in Testing can have refresh tokens expire after 7 days. Add your Google account as a test user, or publish the app in Google Cloud for a durable connection.</p>
        <div className="admin-drive-setup__callback"><Input className="admin-field" label="Authorized redirect URI" readOnly value={setup?.callbackUrl || ""} /><Button type="button" variant="secondary" disabled={!setup?.callbackUrl} onClick={() => { void navigator.clipboard?.writeText(setup!.callbackUrl).then(() => { if (live.current) setNotice("Callback URL copied."); }).catch(() => { if (live.current) setError("Copy is unavailable. Select and copy the callback URL manually."); }); }}>Copy callback URL</Button></div>
        {setup && !setup.callbackUrl && <p role="alert">Set a valid public server URL before connecting Google Drive.</p>}
        {!activeJob && <Button type="button" className="admin-primary-action" disabled={disabled || !setup?.callbackUrl || !clientId.trim() || !clientSecret} onClick={connect}>Authorize Google Drive</Button>}
      </section>}
      <a href={setup?.guideUrl || "https://console.cloud.google.com/apis/credentials"} target="_blank" rel="noreferrer">Google Cloud setup guide ↗</a>
      {job && <section className="admin-drive-setup__job" aria-label="Drive setup progress">
        <p role="status">{job.progress || job.status}</p>
        {job.status === "expired" && <p>This authorization expired. Connect again to begin a new connection.</p>}
        {job.status === "failed" && <p>The connection could not be completed. Check your Google Cloud settings before connecting again.</p>}
        {folderEligible && <>
          <h3>Choose a folder</h3><p>Current folder: <strong>{folderPath || "My Drive"}</strong></p>
          <div className="admin-drive-setup__actions"><Button type="button" variant="secondary" disabled={disabled || folderLoading || !folderPath} onClick={() => setFolderPath(folderPath.split("/").slice(0, -1).join("/"))}>Parent folder</Button><Button type="button" variant="secondary" disabled={disabled || folderLoading} onClick={() => void readFolders(job.id, folderPath)}>Refresh folders</Button></div>
          {folderLoading && <p role="status">Loading folders…</p>}{folderError && <p role="alert">{folderError}</p>}
          {folders && <><AdminSelect label="Browse subfolders" value="" disabled={disabled || !folders.folders.length} onChange={setFolderPath} options={[{ value: "", label: folders.folders.length ? "Choose a subfolder" : "No subfolders" }, ...folders.folders.map(folder => ({ value: folder.path, label: folder.name }))]} />
          <div className="admin-drive-setup__callback"><Input className="admin-field" label="New folder name" value={newFolder} disabled={disabled} onChange={event => setNewFolder(event.target.value)} /><Button type="button" variant="secondary" disabled={disabled || !newFolder.trim() || /[\\/]/.test(newFolder)} onClick={() => {
            const path = [folderPath, newFolder.trim()].filter(Boolean).join("/");
            void mutate({ kind: "create", id: job.id, path }, async () => { await drive.createAdminDriveFolder(job.id, path); if (live.current) { setNewFolder(""); setNotice("Folder created. Refresh folders to see the current listing."); } });
          }}>Create folder</Button></div>
          <Button type="button" variant="secondary" disabled={disabled || !folderPath} onClick={() => void mutate({ kind: "select", id: job.id, path: folderPath }, async () => { const value = await drive.selectAdminDriveFolder(job.id, folderPath); accept(value); setAcknowledged(false); })}>Use this folder</Button></>}
        </>}
        {job.selectedPath && ["selecting_folder", "testing", "ready", "saving"].includes(job.status) && <section className="admin-drive-setup__verify"><h3>Verify &amp; save</h3><p>Selected target: <strong>{job.selectedPath}</strong></p>
          <label className="admin-drive-setup__checkbox"><input type="checkbox" checked={testConsent} disabled={disabled} onChange={event => setTestConsent(event.target.checked)} /><span aria-hidden="true">{testConsent ? "✓" : ""}</span><span>Allow StreamHome to create, read and delete a temporary test file in this folder.</span></label>
          <Button type="button" variant="secondary" disabled={disabled || !testConsent || !["selecting_folder", "ready"].includes(job.status)} onClick={() => void mutate({ kind: "test", id: job.id }, async () => { const value = await drive.testAdminDriveFolder(job.id); accept(value.job); })}>Verify folder</Button>
          {job.status === "ready" && <><p>{job.libraryState === "empty" ? "This folder is empty." : job.libraryState === "streamhome" ? "An existing StreamHome library was detected." : job.libraryState === "nonempty" ? "This folder contains files." : "Folder contents have not been classified."} Existing files remain in place.</p>
          <label className="admin-drive-setup__checkbox"><input type="checkbox" checked={acknowledged} disabled={disabled} onChange={event => setAcknowledged(event.target.checked)} /><span aria-hidden="true">{acknowledged ? "✓" : ""}</span><span>Use this folder for the Drive connection. Existing files stay in place.</span></label>
          <p>Storage mode stays unchanged.</p><Button type="button" className="admin-primary-action" disabled={disabled || !acknowledged} onClick={() => void mutate({ kind: "save", id: job.id }, async () => { const value = await drive.saveAdminDrive(job.id); if (live.current) { accept(value.job); setSetup(current => current ? { ...current, configured: true, job: value.job } : current); setNotice("Drive connection saved. Choose storage mode separately below when you are ready."); onSettingsRefresh(); } })}>Save connection</Button></>}
        </section>}
        {job.status !== "saved" && job.status !== "cancelled" && <Button type="button" variant="ghost" disabled={job.pendingOperation === "mkdir_uncertain" ? pending || loading || locked : disabled} onClick={() => void mutate({ kind: "cancel", id: job.id }, async () => { const value = await drive.cancelAdminDrive(job.id); accept(value); publishUnknown(null); popup.current?.close(); setClientSecret(""); })}>Cancel connection</Button>}
      </section>}
    </>}
    <SudoModal isOpen={sudo} actionLabel="Check Drive connection status" onCancel={() => setSudo(false)} onSuccess={async () => { if (live.current) { setSudo(false); await refresh(); } }} />
  </GlassPane>;
}
