import { useCallback, useEffect, useMemo, useState } from "react";
import {
  activateDrive,
  beginSetupTOTP,
  cancelDriveJob,
  completeSetup,
  createDriveFolder,
  getDriveJob,
  getSetupReadiness,
  getSetupStatus,
  listDriveFolders,
  selectDriveFolder,
  startDriveOAuth,
  testDriveFolder,
  unlockSetup,
  validateSetupTMDB,
  verifySetupTOTP,
  type DriveFolder,
  type DriveSetupJob,
  type ReadinessCheck,
  type SetupCompleteResponse,
} from "../api/setup";
import { ApiError } from "../api/client";
import { BrandLogo } from "../components/brand/BrandLogo";
import "./setup.css";

const STEPS = ["Unlock", "System", "Account", "Security", "TMDB", "Server", "Storage", "Review"] as const;
const TERMINAL_DRIVE_STATES = new Set(["ready", "failed", "cancelled", "expired"]);

function errorMessage(error: unknown) {
  return error instanceof ApiError || error instanceof Error ? error.message : "The request could not be completed.";
}

function formatBytes(value?: number) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "Unavailable";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = value;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
  return `${amount.toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

export function SetupPage() {
  const [step, setStep] = useState(0);
  const [openStep, setOpenStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [bootstrapCode, setBootstrapCode] = useState("");
  const [checks, setChecks] = useState<ReadinessCheck[]>([]);
  const [paths, setPaths] = useState({ media: "server/media", database: "server/database.db" });
  const [publicUrl, setPublicUrl] = useState(window.location.origin);
  const [driveCallbackUrl, setDriveCallbackUrl] = useState(`${window.location.origin}/api/setup/rclone/drive/callback`);
  const [driveGuideUrl, setDriveGuideUrl] = useState("https://github.com/WaqSea/StreamHome/blob/main/docs/google-drive.md");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [totpEnabled, setTotpEnabled] = useState(false);
  const [totpSecret, setTotpSecret] = useState("");
  const [totpUri, setTotpUri] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [totpVerified, setTotpVerified] = useState(false);
  const [tmdbToken, setTmdbToken] = useState("");
  const [tmdbValid, setTmdbValid] = useState(false);
  const [webPort, setWebPort] = useState(3000);
  const [hevc, setHevc] = useState<"auto" | "on" | "off">("auto");
  const [backups, setBackups] = useState(false);
  const [updates, setUpdates] = useState(false);
  const [storage, setStorage] = useState<"LOCAL" | "CLOUD">("LOCAL");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [remoteName, setRemoteName] = useState("streamhome-drive");
  const [audience, setAudience] = useState<"external" | "internal">("external");
  const [publishingStatus, setPublishingStatus] = useState<"testing" | "production">("production");
  const [driveJob, setDriveJob] = useState<DriveSetupJob | null>(null);
  const [drivePath, setDrivePath] = useState("");
  const [folderPath, setFolderPath] = useState("");
  const [folders, setFolders] = useState<DriveFolder[]>([]);
  const [newFolder, setNewFolder] = useState("StreamHome");
  const [healthConsent, setHealthConsent] = useState(false);
  const [driveActivated, setDriveActivated] = useState(false);
  const [remotePath, setRemotePath] = useState("");
  const [quota, setQuota] = useState<{ total?: number; used?: number; free?: number } | null>(null);
  const [result, setResult] = useState<SetupCompleteResponse | null>(null);
  const [secretsSaved, setSecretsSaved] = useState(false);

  const queryDriveJob = useMemo(() => new URLSearchParams(window.location.search).get("driveJob"), []);
  const oauthPopup = queryDriveJob && window.name === "streamhome-google-drive";

  useEffect(() => {
    getSetupStatus().then((status) => {
      setWebPort(status.webPort || 3000);
      setPaths({ media: status.mediaPath, database: status.databasePath });
      setPublicUrl(status.publicUrl || window.location.origin);
      setDriveCallbackUrl(status.driveCallbackUrl || `${window.location.origin}/api/setup/rclone/drive/callback`);
      setDriveGuideUrl(status.driveGuideUrl || "https://github.com/WaqSea/StreamHome/blob/main/docs/google-drive.md");
      if (status.unlocked) {
        const nextStep = queryDriveJob ? 6 : 1;
        setStep(nextStep);
        setOpenStep(nextStep);
      }
    }).catch(() => undefined);
  }, [queryDriveJob]);

  useEffect(() => {
    if (!queryDriveJob) return;
    getDriveJob(queryDriveJob).then((job) => {
      setDriveJob(job);
      setDrivePath(job.selectedPath);
    }).catch((reason) => setError(errorMessage(reason)));
  }, [queryDriveJob]);

  useEffect(() => {
    if (!driveJob || TERMINAL_DRIVE_STATES.has(driveJob.status)) return;
    const timer = window.setInterval(() => {
      getDriveJob(driveJob.id).then((job) => {
        setDriveJob(job);
        if (job.selectedPath) setDrivePath(job.selectedPath);
      }).catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [driveJob]);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try { await action(); } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  };

  const goToStep = (next: number) => {
    setError("");
    setStep(next);
    setOpenStep(next);
  };

  const unlock = () => run(async () => {
    await unlockSetup(bootstrapCode);
    const status = await getSetupStatus();
    setPaths({ media: status.mediaPath, database: status.databasePath });
    setPublicUrl(status.publicUrl || window.location.origin);
    setDriveCallbackUrl(status.driveCallbackUrl || `${window.location.origin}/api/setup/rclone/drive/callback`);
    goToStep(1);
  });

  const inspect = () => run(async () => {
    const readiness = await getSetupReadiness();
    setChecks(readiness.checks);
    if (readiness.ready) goToStep(2);
    else setError("Resolve the required checks before continuing. Rclone is optional when local storage is selected.");
  });

  const prepareSecurity = () => {
    if (!email.includes("@")) return setError("Enter a valid administrator email.");
    if (password.length < 6 || new TextEncoder().encode(password).length > 72) return setError("Use a password between 6 characters and 72 UTF-8 bytes.");
    if (password !== confirmPassword) return setError("The passwords do not match.");
    goToStep(3);
  };

  const toggleTotp = (enabled: boolean) => run(async () => {
    setTotpEnabled(enabled);
    setTotpVerified(false);
    setTotpCode("");
    if (!enabled) { setTotpSecret(""); setTotpUri(""); return; }
    const setup = await beginSetupTOTP(email);
    setTotpSecret(setup.secret);
    setTotpUri(setup.provisioningUri);
  });

  const verifyTotp = () => run(async () => {
    await verifySetupTOTP(totpSecret, totpCode);
    setTotpVerified(true);
  });

  const validateTmdb = () => run(async () => {
    await validateSetupTMDB(tmdbToken);
    setTmdbValid(true);
  });

  const loadFolders = useCallback((jobId: string, path = "") => run(async () => {
    const response = await listDriveFolders(jobId, path);
    setFolderPath(response.path);
    setFolders(response.folders);
  }), []);

  useEffect(() => {
    if (driveJob?.status === "selecting_folder" && folders.length === 0) void loadFolders(driveJob.id, folderPath);
  }, [driveJob?.id, driveJob?.status, folderPath, folders.length, loadFolders]);

  const startOAuth = async () => {
    const popup = window.open("about:blank", "streamhome-google-drive", "popup,width=620,height=760");
    setBusy(true);
    setError("");
    try {
      const started = await startDriveOAuth({ clientId, clientSecret, remoteName, audience, publishingStatus, publicUrl });
      const job = await getDriveJob(started.jobId);
      setDriveJob(job);
      setDriveActivated(false);
      setRemotePath("");
      if (popup) popup.location.assign(started.authorizationUrl);
      else window.location.assign(started.authorizationUrl);
    } catch (reason) {
      popup?.close();
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const cancelOAuth = () => run(async () => {
    if (driveJob) await cancelDriveJob(driveJob.id);
    setDriveJob(null);
    setDrivePath("");
    setFolders([]);
    setDriveActivated(false);
  });

  const createFolder = () => run(async () => {
    if (!driveJob) return;
    const path = [folderPath, newFolder.trim()].filter(Boolean).join("/");
    const created = await createDriveFolder(driveJob.id, path);
    setNewFolder("");
    await loadFolders(driveJob.id, folderPath);
    setDrivePath(created.path);
  });

  const chooseFolder = (path: string) => run(async () => {
    if (!driveJob) return;
    const job = await selectDriveFolder(driveJob.id, path);
    setDriveJob(job);
    setDrivePath(path);
    setHealthConsent(false);
    setDriveActivated(false);
  });

  const testDrive = () => run(async () => {
    if (!driveJob) return;
    const tested = await testDriveFolder(driveJob.id);
    setDriveJob(tested.job);
    setRemotePath(tested.remotePath);
    setQuota(tested.quota ?? null);
  });

  const activate = () => run(async () => {
    if (!driveJob) return;
    const activated = await activateDrive(driveJob.id);
    setDriveJob(activated.job);
    setRemotePath(activated.remotePath);
    setDriveActivated(true);
  });

  const finish = () => run(async () => {
    const response = await completeSetup({
      email,
      password,
      tmdb_token: tmdbToken,
      web_port: webPort,
      public_url: publicUrl,
      totp_secret: totpEnabled ? totpSecret : undefined,
      totp_code: totpEnabled ? totpCode : undefined,
      backup_enabled: backups,
      auto_update_enabled: updates,
      hevc_compression_mode: hevc,
      storage_engine: storage,
      rclone_remote_path: storage === "CLOUD" ? remotePath : undefined,
      drive_job_id: storage === "CLOUD" ? driveJob?.id : undefined,
    });
    setResult(response);
  });

  const summaries = [
    "Bootstrap access",
    checks.length ? `${checks.filter((item) => item.ready).length}/${checks.length} checks ready` : "Runtime checks",
    email || "Administrator account",
    totpEnabled ? "Password + TOTP" : "Password security",
    tmdbValid ? "Connected" : "Metadata access",
    `${publicUrl} · port ${webPort}`,
    storage === "LOCAL" ? "Local media" : driveActivated ? `Drive · ${drivePath}` : "Google Drive",
    "Confirm and initialize",
  ];

  const secretsText = useMemo(() => result ? [
    result.ingestionToken,
    ...(result.recoveryCodes.length ? ["", "TOTP recovery codes:", ...result.recoveryCodes] : []),
  ].join("\n") : "", [result]);

  const downloadSecrets = () => {
    const url = URL.createObjectURL(new Blob([secretsText], { type: "text/plain" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "streamhome-recovery.txt";
    link.click();
    URL.revokeObjectURL(url);
  };

  const openLogin = () => {
    const target = new URL(window.location.href);
    target.pathname = "/login";
    target.search = "";
    target.hash = "";
    if (["localhost", "127.0.0.1"].includes(target.hostname)) target.port = String(result?.webPort ?? webPort);
    window.location.assign(target.toString());
  };

  if (oauthPopup) return <main className="setup-page setup-page--complete"><section className="setup-complete-panel setup-oauth-return">
    <span className="setup-success-mark" aria-hidden="true">{driveJob?.status === "selecting_folder" || driveJob?.status === "ready" ? "✓" : "…"}</span>
    <p className="setup-eyebrow">GOOGLE DRIVE AUTHORIZATION</p>
    <h1>{driveJob?.status === "selecting_folder" || driveJob?.status === "ready" ? "Drive connected" : "Returning to setup"}</h1>
    <p>{driveJob?.progress || "StreamHome is verifying the Google authorization. Return to the original setup tab to choose a folder."}</p>
    <button className="setup-primary" onClick={() => window.close()}>Close this window</button>
  </section></main>;

  if (result) return <main className="setup-page setup-page--complete"><section className="setup-complete-panel">
    <span className="setup-success-mark" aria-hidden="true">✓</span>
    <p className="setup-eyebrow">INSTALLATION COMPLETE</p>
    <h1>StreamHome is restarting</h1>
    <p>Save these values now. They will not be shown again.</p>
    <pre>{secretsText}</pre>
    <div className="setup-actions"><button onClick={() => navigator.clipboard.writeText(secretsText)}>Copy</button><button onClick={downloadSecrets}>Download</button></div>
    <label className="setup-check"><input type="checkbox" checked={secretsSaved} onChange={(event) => setSecretsSaved(event.target.checked)} /> I saved the ingestion token{result.recoveryCodes.length ? " and recovery codes" : ""}.</label>
    <button className="setup-primary" disabled={!secretsSaved} onClick={openLogin}>Open StreamHome</button>
  </section></main>;

  return <main className="setup-page" data-step={step}>
    <aside className="setup-sidebar">
      <BrandLogo className="setup-logo" />
      <div><p className="setup-eyebrow">SELF-HOSTED CONTROL PLANE</p><h1>Configure StreamHome</h1><span>Secure first-run provisioning for your private media server.</span></div>
      <ol className="setup-step-accordion">{STEPS.map((label, index) => {
        const available = index <= step;
        const expanded = openStep === index;
        return <li key={label} data-active={index === step} data-done={index < step} data-locked={!available}>
          <button type="button" className="setup-step-trigger" aria-expanded={expanded} disabled={!available} onClick={() => {
            if (index < step) goToStep(index);
            else setOpenStep(expanded ? -1 : index);
          }}>
            <i>{index < step ? "✓" : index + 1}</i><span><b>{label}</b><small>{summaries[index]}</small></span><em>{available ? (expanded ? "−" : "+") : "•"}</em>
          </button>
          {expanded && <div className="setup-step-detail">{index === step ? "In progress" : index < step ? "Complete · Select to edit" : "Complete earlier steps first"}</div>}
        </li>;
      })}</ol>
      <footer><i /> Local setup session · secrets stay on this server</footer>
    </aside>

    <section className="setup-workspace">
      <header><p className="setup-eyebrow">STEP {step + 1} OF {STEPS.length}</p><span>{STEPS[step]}</span></header>
      <div className="setup-panel">
        {step === 0 && <><h2>Unlock this installation</h2><p>Enter the one-time bootstrap code printed by <code>./start.sh</code>.</p><label>Bootstrap code<input autoFocus type="password" autoComplete="off" value={bootstrapCode} onChange={(event) => setBootstrapCode(event.target.value)} onKeyDown={(event) => event.key === "Enter" && void unlock()} /></label><button className="setup-primary" disabled={!bootstrapCode || busy} onClick={unlock}>Unlock setup</button></>}

        {step === 1 && <><h2>System readiness</h2><p>StreamHome checks its runtime without changing the established media layout.</p>{checks.length > 0 && <div className="setup-check-grid">{checks.map((check) => <article key={check.id} data-ready={check.ready}><i>{check.ready ? "✓" : "!"}</i><div><strong>{check.id.replace(/_/g, " ")}</strong><span>{check.detail}</span></div></article>)}</div>}<button className="setup-primary" disabled={busy} onClick={inspect}>{checks.length ? "Check again" : "Run system checks"}</button></>}

        {step === 2 && <><h2>Create the administrator</h2><p>This account controls profiles, server settings, storage, and security.</p><div className="setup-form-grid"><label>Email address<input type="email" autoComplete="username" value={email} onChange={(event) => setEmail(event.target.value)} /></label><label>Password<input type="password" autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label><label>Confirm password<input type="password" autoComplete="new-password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} /></label></div><button className="setup-primary" onClick={prepareSecurity}>Continue</button></>}

        {step === 3 && <><h2>Local account security</h2><p>TOTP is optional. When enabled, setup creates ten one-time recovery codes.</p><div className="setup-choice"><button data-selected={!totpEnabled} onClick={() => void toggleTotp(false)}><strong>Password only</strong><span>Enable TOTP later in Admin.</span></button><button data-selected={totpEnabled} onClick={() => void toggleTotp(true)}><strong>Password + TOTP</strong><span>Recommended for remotely accessible servers.</span></button></div>{totpEnabled && <div className="setup-secret"><label>Authenticator secret<input readOnly value={totpSecret} /></label><small>{totpUri}</small><label>Six-digit code<input inputMode="numeric" maxLength={6} value={totpCode} onChange={(event) => { setTotpCode(event.target.value.replace(/\D/g, "")); setTotpVerified(false); }} /></label><button onClick={verifyTotp} disabled={totpCode.length !== 6 || busy}>{totpVerified ? "Verified ✓" : "Verify code"}</button></div>}<div className="setup-footer-actions"><button onClick={() => goToStep(2)}>Back</button><button className="setup-primary" disabled={totpEnabled && !totpVerified} onClick={() => goToStep(4)}>Continue</button></div></>}

        {step === 4 && <><h2>Connect TMDB</h2><p>A valid TMDB v4 read-access token is required for catalog metadata and artwork.</p><label>Read-access token<textarea value={tmdbToken} onChange={(event) => { setTmdbToken(event.target.value.trim()); setTmdbValid(false); }} rows={5} /></label><button onClick={validateTmdb} disabled={!tmdbToken || busy}>{tmdbValid ? "TMDB connected ✓" : "Validate token"}</button><div className="setup-footer-actions"><button onClick={() => goToStep(3)}>Back</button><button className="setup-primary" disabled={!tmdbValid} onClick={() => goToStep(5)}>Continue</button></div></>}

        {step === 5 && <><h2>Server behavior</h2><p>Database and media locations remain standardized for reliable recovery.</p><div className="setup-paths"><span><b>Database</b>{paths.database}</span><span><b>Media</b>{paths.media}</span></div><div className="setup-form-grid"><label>Public StreamHome URL<input type="url" value={publicUrl} onChange={(event) => setPublicUrl(event.target.value.trim())} placeholder="https://watch.example.com" /></label><label>Web port<input type="number" min={1} max={65535} value={webPort} onChange={(event) => setWebPort(Number(event.target.value))} /></label><label>HEVC compression<select value={hevc} onChange={(event) => setHevc(event.target.value as typeof hevc)}><option value="auto">Automatic</option><option value="on">Always</option><option value="off">Never</option></select></label></div><small className="setup-field-note">The public origin creates the exact Google OAuth callback and must not include a path.</small><label className="setup-check"><input type="checkbox" checked={backups} onChange={(event) => setBackups(event.target.checked)} /> Enable automatic database backups</label><label className="setup-check"><input type="checkbox" checked={updates} onChange={(event) => setUpdates(event.target.checked)} /> Enable automatic updates</label><div className="setup-footer-actions"><button onClick={() => goToStep(4)}>Back</button><button className="setup-primary" disabled={webPort < 1 || webPort > 65535 || !publicUrl} onClick={() => goToStep(6)}>Continue</button></div></>}

        {step === 6 && <><h2>Storage</h2><p>Keep media on this server, or connect Google Drive without installing Rclone on another computer.</p>
          <div className="setup-choice"><button data-selected={storage === "LOCAL"} onClick={() => setStorage("LOCAL")}><strong>Local storage</strong><span>Use the standard server/media catalog.</span></button><button data-selected={storage === "CLOUD"} onClick={() => setStorage("CLOUD")}><strong>Google Drive</strong><span>Authorize Drive directly in this browser.</span></button></div>
          {storage === "CLOUD" && <div className="setup-drive-config">
            <a className="setup-guide-link" href={driveGuideUrl} target="_blank" rel="noreferrer"><span>GOOGLE CLOUD GUIDE</span><strong>Learn how to create the required Client ID and OAuth application</strong><i>↗</i></a>
            <div className="setup-callback"><span>Authorized redirect URI</span><code>{publicUrl ? `${publicUrl.replace(/\/$/, "")}/api/setup/rclone/drive/callback` : driveCallbackUrl}</code><button type="button" onClick={() => navigator.clipboard.writeText(`${publicUrl.replace(/\/$/, "")}/api/setup/rclone/drive/callback`)}>Copy</button></div>
            {!driveJob && <div className="setup-secret setup-drive-credentials"><div className="setup-form-grid"><label>Google client ID<input value={clientId} onChange={(event) => setClientId(event.target.value.trim())} autoComplete="off" placeholder="….apps.googleusercontent.com" /></label><label>Google client secret<input type="password" value={clientSecret} onChange={(event) => setClientSecret(event.target.value.trim())} autoComplete="new-password" /></label><label>Rclone remote name<input value={remoteName} onChange={(event) => setRemoteName(event.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, "").slice(0, 32))} /></label></div><div className="setup-form-grid"><label>OAuth audience<select value={audience} onChange={(event) => setAudience(event.target.value as typeof audience)}><option value="external">External</option><option value="internal">Internal workspace</option></select></label><label>Publishing status<select value={publishingStatus} onChange={(event) => setPublishingStatus(event.target.value as typeof publishingStatus)}><option value="production">Production</option><option value="testing">Testing</option></select></label></div>{audience === "external" && publishingStatus === "testing" && <div className="setup-notice">Add this Google account as an OAuth test user. Testing-mode authorization may require reconnecting later.</div>}<button className="setup-primary setup-drive-connect" disabled={busy || !clientId || !clientSecret || remoteName.length < 2} onClick={startOAuth}>Continue with Google</button><small>Google consent opens in a separate window. The client secret is sent to this server and is never saved in browser storage.</small></div>}
            {driveJob && <div className="setup-drive-job" data-status={driveJob.status}><header><span className="setup-drive-status-dot" /><div><strong>{driveJob.status === "ready" ? "Google Drive verified" : "Google Drive connection"}</strong><small>{driveJob.progress}</small></div><button onClick={cancelOAuth}>Start over</button></header>
              {driveJob.errorCode && <div className="setup-error" role="alert"><i>!</i>{driveJob.errorCode.replace(/_/g, " ")}</div>}
              {driveJob.status === "selecting_folder" && <div className="setup-folder-picker"><div className="setup-folder-toolbar"><button disabled={!folderPath || busy} onClick={() => void loadFolders(driveJob.id, folderPath.split("/").slice(0, -1).join("/"))}>← Up</button><code>Drive / {folderPath || ""}</code><button onClick={() => void loadFolders(driveJob.id, folderPath)}>Refresh</button></div><div className="setup-folder-list">{folders.length ? folders.map((folder) => <article key={folder.path} data-selected={drivePath === folder.path}><button className="setup-folder-open" onClick={() => void loadFolders(driveJob.id, folder.path)}><i>▰</i><span>{folder.name}</span></button><button onClick={() => chooseFolder(folder.path)}>{drivePath === folder.path ? "Selected" : "Use folder"}</button></article>) : <p>No folders here. Create the StreamHome folder below.</p>}</div><div className="setup-folder-create"><input aria-label="New folder name" value={newFolder} onChange={(event) => setNewFolder(event.target.value.replace(/[\\/:]/g, ""))} placeholder="StreamHome" /><button disabled={!newFolder.trim()} onClick={createFolder}>Create folder</button></div>{drivePath && <div className="setup-folder-selection"><span>Selected folder</span><strong>Drive / {drivePath}</strong><button onClick={() => chooseFolder(drivePath)}>Confirm selection</button></div>}</div>}
              {(driveJob.status === "selecting_folder" && driveJob.selectedPath) && <div className="setup-drive-test"><label className="setup-check"><input type="checkbox" checked={healthConsent} onChange={(event) => setHealthConsent(event.target.checked)} /> Allow StreamHome to create, verify, and delete one small health-check file in this folder.</label><button className="setup-primary" disabled={!healthConsent || busy} onClick={testDrive}>Run read/write test</button></div>}
              {driveJob.status === "ready" && <div className="setup-drive-ready"><div><span>Folder</span><strong>Drive / {driveJob.selectedPath}</strong></div>{quota && <div><span>Drive usage</span><strong>{formatBytes(quota.used)} used · {formatBytes(quota.free)} free</strong></div>}<button className="setup-primary" disabled={driveActivated || busy} onClick={activate}>{driveActivated ? "Drive activated ✓" : "Activate Google Drive"}</button></div>}
            </div>}
          </div>}
          <div className="setup-footer-actions"><button onClick={() => goToStep(5)}>Back</button><button className="setup-primary" disabled={storage === "CLOUD" && !driveActivated} onClick={() => goToStep(7)}>Review setup</button></div></>}

        {step === 7 && <><h2>Ready to initialize</h2><p>Review the configuration. Finishing creates the account, writes server secrets, and restarts StreamHome.</p><dl className="setup-review"><div><dt>Administrator</dt><dd>{email}</dd></div><div><dt>Security</dt><dd>{totpEnabled ? "Password + TOTP" : "Password"}</dd></div><div><dt>TMDB</dt><dd>Validated</dd></div><div><dt>Public URL</dt><dd>{publicUrl}</dd></div><div><dt>Web</dt><dd>Port {webPort}</dd></div><div><dt>Storage</dt><dd>{storage === "LOCAL" ? "Local server/media" : `Google Drive / ${drivePath}`}</dd></div><div><dt>Automation</dt><dd>{backups ? "Backups on" : "Backups off"} · {updates ? "Updates on" : "Updates off"}</dd></div></dl><div className="setup-footer-actions"><button onClick={() => goToStep(6)}>Back</button><button className="setup-primary" disabled={busy} onClick={finish}>Initialize StreamHome</button></div></>}

        {error && <div className="setup-error" role="alert"><i>!</i>{error}</div>}
        {busy && <div className="setup-busy" role="status"><span className="setup-spinner" />Working…</div>}
      </div>
    </section>
  </main>;
}
