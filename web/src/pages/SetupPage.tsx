import { useEffect, useMemo, useState } from "react";
import {
  beginSetupTOTP, cancelSetupRcloneConfig, completeSetup, continueSetupRcloneConfig,
  getSetupReadiness, getSetupRcloneProviders, getSetupRcloneRemotes, getSetupStatus,
  startSetupRcloneConfig, testSetupRclone, unlockSetup, validateSetupTMDB, verifySetupTOTP,
  type ReadinessCheck, type RcloneConfigStep, type SetupCompleteResponse,
} from "../api/setup";
import { ApiError } from "../api/client";
import { BrandLogo } from "../components/brand/BrandLogo";
import "./setup.css";

const STEPS = ["Unlock", "System", "Account", "Security", "TMDB", "Server", "Storage", "Review"];

function errorMessage(error: unknown) {
  return error instanceof ApiError || error instanceof Error ? error.message : "The request could not be completed.";
}

export function SetupPage() {
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [bootstrapCode, setBootstrapCode] = useState("");
  const [checks, setChecks] = useState<ReadinessCheck[]>([]);
  const [paths, setPaths] = useState({ media: "server/media", database: "server/database.db" });
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
  const [hevc, setHevc] = useState<"auto" | "always" | "never">("auto");
  const [backups, setBackups] = useState(false);
  const [updates, setUpdates] = useState(false);
  const [storage, setStorage] = useState<"LOCAL" | "CLOUD">("LOCAL");
  const [remotes, setRemotes] = useState<string[]>([]);
  const [remotePath, setRemotePath] = useState("");
  const [remoteValid, setRemoteValid] = useState(false);
  const [providers, setProviders] = useState<Array<{ id: string; name: string }>>([]);
  const [provider, setProvider] = useState("drive");
  const [remoteName, setRemoteName] = useState("streamhome");
  const [rcloneStep, setRcloneStep] = useState<RcloneConfigStep | null>(null);
  const [rcloneAnswer, setRcloneAnswer] = useState("");
  const [result, setResult] = useState<SetupCompleteResponse | null>(null);
  const [secretsSaved, setSecretsSaved] = useState(false);

  useEffect(() => {
    getSetupStatus().then((status) => {
      setWebPort(status.webPort || 3000);
      setPaths({ media: status.mediaPath, database: status.databasePath });
      if (status.unlocked) setStep(1);
    }).catch(() => undefined);
  }, []);

  const run = async (action: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await action(); } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  };

  const unlock = () => run(async () => {
    await unlockSetup(bootstrapCode);
    const status = await getSetupStatus();
    setPaths({ media: status.mediaPath, database: status.databasePath });
    setStep(1);
  });
  const inspect = () => run(async () => {
    const readiness = await getSetupReadiness();
    setChecks(readiness.checks);
    if (readiness.ready) setStep(2); else setError("Resolve the required checks before continuing. Rclone may be configured later.");
  });
  const prepareSecurity = () => {
    if (!email.includes("@")) return setError("Enter a valid administrator email.");
    if (password.length < 6 || new TextEncoder().encode(password).length > 72) return setError("Use a password between 6 characters and 72 UTF-8 bytes.");
    if (password !== confirmPassword) return setError("The passwords do not match.");
    setError(""); setStep(3);
  };
  const toggleTotp = (enabled: boolean) => run(async () => {
    setTotpEnabled(enabled); setTotpVerified(false); setTotpCode("");
    if (!enabled) { setTotpSecret(""); setTotpUri(""); return; }
    const setup = await beginSetupTOTP(email);
    setTotpSecret(setup.secret); setTotpUri(setup.provisioningUri);
  });
  const verifyTotp = () => run(async () => { await verifySetupTOTP(totpSecret, totpCode); setTotpVerified(true); });
  const validateTmdb = () => run(async () => { await validateSetupTMDB(tmdbToken); setTmdbValid(true); });
  const loadStorage = () => run(async () => {
    const [response, providerResponse] = await Promise.all([getSetupRcloneRemotes(), getSetupRcloneProviders()]);
    setRemotes(response.remotes); setProviders(providerResponse.providers);
    setStep(6);
  });
  const acceptRcloneStep = (next: RcloneConfigStep) => {
    setRcloneStep(next);
    setRcloneAnswer(next.question?.name === "config_is_local" ? "false" : next.question?.defaultValue ?? "");
    if (next.complete && next.remote) {
      setRemotes((current) => current.includes(next.remote!) ? current : [...current, next.remote!]);
      setRemotePath(`${next.remote}media`); setRemoteValid(false);
    }
  };
  const startRcloneConfig = () => run(async () => acceptRcloneStep(await startSetupRcloneConfig(remoteName, provider)));
  const continueRcloneConfig = () => run(async () => {
    if (!rcloneStep) return;
    acceptRcloneStep(await continueSetupRcloneConfig(rcloneStep.flowToken, rcloneAnswer));
  });
  const cancelRcloneConfig = () => run(async () => {
    if (rcloneStep) await cancelSetupRcloneConfig(rcloneStep.flowToken);
    setRcloneStep(null); setRcloneAnswer("");
  });
  const testRemote = () => run(async () => { await testSetupRclone(remotePath); setRemoteValid(true); });
  const finish = () => run(async () => {
    const response = await completeSetup({
      email, password, tmdb_token: tmdbToken, web_port: webPort,
      totp_secret: totpEnabled ? totpSecret : undefined,
      totp_code: totpEnabled ? totpCode : undefined,
      backup_enabled: backups, auto_update_enabled: updates,
      hevc_compression_mode: hevc, storage_engine: storage,
      rclone_remote_path: storage === "CLOUD" ? remotePath : undefined,
    });
    setResult(response);
  });

  const secretsText = useMemo(() => result ? [
    result.ingestionToken,
    ...(result.recoveryCodes.length ? ["", "TOTP recovery codes:", ...result.recoveryCodes] : []),
  ].join("\n") : "", [result]);

  const downloadSecrets = () => {
    const url = URL.createObjectURL(new Blob([secretsText], { type: "text/plain" }));
    const link = document.createElement("a"); link.href = url; link.download = "streamhome-recovery.txt"; link.click(); URL.revokeObjectURL(url);
  };
  const openLogin = () => {
    const target = new URL(window.location.href);
    target.pathname = "/login"; target.search = ""; target.hash = "";
    if (["localhost", "127.0.0.1"].includes(target.hostname)) target.port = String(result?.webPort ?? webPort);
    window.location.assign(target.toString());
  };

  if (result) return <main className="setup-page setup-page--complete"><section className="setup-complete-panel">
    <span className="setup-success-mark" aria-hidden="true">✓</span><p className="setup-eyebrow">INSTALLATION COMPLETE</p>
    <h1>StreamHome is restarting</h1><p>Save these values now. They will not be shown again.</p>
    <pre>{secretsText}</pre>
    <div className="setup-actions"><button onClick={() => navigator.clipboard.writeText(secretsText)}>Copy</button><button onClick={downloadSecrets}>Download</button></div>
    <label className="setup-check"><input type="checkbox" checked={secretsSaved} onChange={(event) => setSecretsSaved(event.target.checked)} /> I saved the ingestion token{result.recoveryCodes.length ? " and recovery codes" : ""}.</label>
    <button className="setup-primary" disabled={!secretsSaved} onClick={openLogin}>Open StreamHome</button>
  </section></main>;

  return <main className="setup-page" data-step={step}>
    <aside className="setup-sidebar"><BrandLogo className="setup-logo" /><div><p className="setup-eyebrow">SELF-HOSTED CONTROL PLANE</p><h1>Configure StreamHome</h1><span>Secure first-run provisioning for your private media server.</span></div>
      <ol>{STEPS.map((label, index) => <li key={label} data-active={index === step} data-done={index < step}><i>{index < step ? "✓" : index + 1}</i><span>{label}</span></li>)}</ol>
      <footer><i /> Local setup session · secrets stay on this server</footer>
    </aside>
    <section className="setup-workspace">
      <header><p className="setup-eyebrow">STEP {step + 1} OF {STEPS.length}</p><span>{STEPS[step]}</span></header>
      <div className="setup-panel">
        {step === 0 && <><h2>Unlock this installation</h2><p>Enter the one-time bootstrap code printed by <code>./start.sh</code>.</p><label>Bootstrap code<input autoFocus type="password" autoComplete="off" value={bootstrapCode} onChange={(e) => setBootstrapCode(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void unlock()} /></label><button className="setup-primary" disabled={!bootstrapCode || busy} onClick={unlock}>Unlock setup</button></>}
        {step === 1 && <><h2>System readiness</h2><p>StreamHome checks its runtime without changing the media layout.</p>{checks.length > 0 && <div className="setup-check-grid">{checks.map((check) => <article key={check.id} data-ready={check.ready}><i>{check.ready ? "✓" : "!"}</i><div><strong>{check.id.replace(/_/g, " ")}</strong><span>{check.detail}</span></div></article>)}</div>}<button className="setup-primary" disabled={busy} onClick={inspect}>{checks.length ? "Check again" : "Run system checks"}</button></>}
        {step === 2 && <><h2>Create the administrator</h2><p>This account controls profiles, server settings, and security.</p><div className="setup-form-grid"><label>Email address<input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} /></label><label>Password<input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} /></label><label>Confirm password<input type="password" autoComplete="new-password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} /></label></div><button className="setup-primary" onClick={prepareSecurity}>Continue</button></>}
        {step === 3 && <><h2>Local account security</h2><p>TOTP is optional. When enabled, setup creates ten one-time recovery codes.</p><div className="setup-choice"><button data-selected={!totpEnabled} onClick={() => void toggleTotp(false)}><strong>Password only</strong><span>Enable TOTP later in Admin.</span></button><button data-selected={totpEnabled} onClick={() => void toggleTotp(true)}><strong>Password + TOTP</strong><span>Recommended for remotely accessible servers.</span></button></div>{totpEnabled && <div className="setup-secret"><label>Authenticator secret<input readOnly value={totpSecret} /></label><small>{totpUri}</small><label>Six-digit code<input inputMode="numeric" maxLength={6} value={totpCode} onChange={(e) => { setTotpCode(e.target.value.replace(/\D/g, "")); setTotpVerified(false); }} /></label><button onClick={verifyTotp} disabled={totpCode.length !== 6 || busy}>{totpVerified ? "Verified ✓" : "Verify code"}</button></div>}<div className="setup-footer-actions"><button onClick={() => setStep(2)}>Back</button><button className="setup-primary" disabled={totpEnabled && !totpVerified} onClick={() => setStep(4)}>Continue</button></div></>}
        {step === 4 && <><h2>Connect TMDB</h2><p>A valid TMDB v4 read-access token is required for catalog metadata and artwork.</p><label>Read-access token<textarea value={tmdbToken} onChange={(e) => { setTmdbToken(e.target.value.trim()); setTmdbValid(false); }} rows={5} /></label><button onClick={validateTmdb} disabled={!tmdbToken || busy}>{tmdbValid ? "TMDB connected ✓" : "Validate token"}</button><div className="setup-footer-actions"><button onClick={() => setStep(3)}>Back</button><button className="setup-primary" disabled={!tmdbValid} onClick={() => setStep(5)}>Continue</button></div></>}
        {step === 5 && <><h2>Server behavior</h2><p>Database and media locations remain standardized for reliable recovery.</p><div className="setup-paths"><span><b>Database</b>{paths.database}</span><span><b>Media</b>{paths.media}</span></div><div className="setup-form-grid"><label>Web port<input type="number" min={1} max={65535} value={webPort} onChange={(e) => setWebPort(Number(e.target.value))} /></label><label>HEVC compression<select value={hevc} onChange={(e) => setHevc(e.target.value as typeof hevc)}><option value="auto">Automatic</option><option value="always">Always</option><option value="never">Never</option></select></label></div><label className="setup-check"><input type="checkbox" checked={backups} onChange={(e) => setBackups(e.target.checked)} /> Enable automatic database backups</label><label className="setup-check"><input type="checkbox" checked={updates} onChange={(e) => setUpdates(e.target.checked)} /> Enable automatic updates</label><div className="setup-footer-actions"><button onClick={() => setStep(4)}>Back</button><button className="setup-primary" disabled={webPort < 1 || webPort > 65535} onClick={loadStorage}>Continue</button></div></>}
        {step === 6 && <><h2>Storage</h2><p>Local mode is ready now. Rclone is the final optional configuration.</p>
          <div className="setup-choice"><button data-selected={storage === "LOCAL"} onClick={() => setStorage("LOCAL")}><strong>Local storage</strong><span>Use the standard server/media catalog.</span></button><button data-selected={storage === "CLOUD"} onClick={() => setStorage("CLOUD")}><strong>Rclone cloud</strong><span>Connect an application-owned cloud remote.</span></button></div>
          {storage === "CLOUD" && <div className="setup-secret setup-rclone-config">
            <label>Configured remote<select value={remotePath} onChange={(e) => { setRemotePath(e.target.value); setRemoteValid(false); }}><option value="">Select a remote</option>{remotes.map((remote) => <option key={remote} value={`${remote}media`}>{remote}</option>)}</select></label>
            {remotePath && <button disabled={busy} onClick={testRemote}>{remoteValid ? "Remote connected ✓" : "Test remote"}</button>}
            <div className="setup-divider"><span>or add a provider</span></div>
            {!rcloneStep && <div className="setup-rclone-start"><label>Provider<select value={provider} onChange={(e) => setProvider(e.target.value)}>{providers.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>Remote name<input value={remoteName} onChange={(e) => setRemoteName(e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, "").slice(0, 32))} /></label><button disabled={remoteName.length < 2 || busy} onClick={startRcloneConfig}>Configure provider</button></div>}
            {rcloneStep?.question && <div className="setup-rclone-question"><strong>{rcloneStep.question.name.replace(/_/g, " ")}</strong><pre>{rcloneStep.question.help}</pre><label>Value{rcloneStep.question.examples.length ? <select value={rcloneAnswer} onChange={(e) => setRcloneAnswer(e.target.value)}><option value="">Choose a value</option>{rcloneStep.question.examples.map((example) => <option key={`${example.value}-${example.help}`} value={example.value}>{example.help || example.value}</option>)}</select> : rcloneStep.question.name === "config_token" ? <textarea rows={5} value={rcloneAnswer} onChange={(e) => setRcloneAnswer(e.target.value)} autoComplete="off" /> : <input type={rcloneStep.question.sensitive ? "password" : "text"} value={rcloneAnswer} onChange={(e) => setRcloneAnswer(e.target.value)} autoComplete="off" />}</label><div className="setup-actions"><button onClick={cancelRcloneConfig}>Cancel</button><button className="setup-primary" disabled={rcloneStep.question.required && !rcloneAnswer} onClick={continueRcloneConfig}>Continue</button></div></div>}
            {rcloneStep?.complete && <div className="setup-rclone-complete"><i>✓</i><span>Remote created. Select its media path above, then test the connection.</span><button onClick={() => setRcloneStep(null)}>Add another</button></div>}
          </div>}
          <div className="setup-footer-actions"><button onClick={() => setStep(5)}>Back</button><button className="setup-primary" disabled={storage === "CLOUD" && !remoteValid} onClick={() => setStep(7)}>Review setup</button></div></>}
        {step === 7 && <><h2>Ready to initialize</h2><p>Review the configuration. Finishing creates the account, writes server secrets, and restarts StreamHome.</p><dl className="setup-review"><div><dt>Administrator</dt><dd>{email}</dd></div><div><dt>Security</dt><dd>{totpEnabled ? "Password + TOTP" : "Password"}</dd></div><div><dt>TMDB</dt><dd>Validated</dd></div><div><dt>Web</dt><dd>Port {webPort}</dd></div><div><dt>Storage</dt><dd>{storage === "LOCAL" ? "Local server/media" : remotePath}</dd></div><div><dt>Automation</dt><dd>{backups ? "Backups on" : "Backups off"} · {updates ? "Updates on" : "Updates off"}</dd></div></dl><div className="setup-footer-actions"><button onClick={() => setStep(6)}>Back</button><button className="setup-primary" disabled={busy} onClick={finish}>Initialize StreamHome</button></div></>}
        {error && <div className="setup-error" role="alert"><i>!</i>{error}</div>}
        {busy && <div className="setup-busy" role="status"><span className="setup-spinner" />Working…</div>}
      </div>
    </section>
  </main>;
}
