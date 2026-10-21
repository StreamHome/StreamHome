import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { formatStorageBytes } from "../utils/format";
import {
  activateDrive,
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
  type DriveFolder,
  type DriveSetupJob,
  type ReadinessCheck,
  type SetupCompleteResponse,
} from "../api/setup";
import { ApiError } from "../api/client";
import { BrandLogo } from "../components/brand/BrandLogo";
import {
  clearSetupCheckpoint,
  copySetupText,
  readSetupCheckpoint,
  writeSetupCheckpoint,
} from "./setupResume";
import { HEVC_LABELS, formatSetupSecrets, parseSetupPublicOrigin, revealSetupStep, validSetupPort } from "./setupValidation";
import { isRetiredSetupEnrollment, isUnknownSetupOutcome, useSetupEnrollment } from "./setupEnrollment";
import "./setup.css";

const STEPS = ["Unlock", "System", "Account", "Security", "TMDB", "Server", "Storage", "Review"] as const;
type DriveUnknownOperation = { operation: "start" | "create" | "select" | "test" | "activate" | "cancel"; jobId?: string; path?: string };
const TERMINAL_DRIVE_STATES = new Set(["ready", "failed", "cancelled", "expired"]);
const CREATED_FOLDER_REFRESH_FAILED_NOTICE = "Folder created. The list refresh failed; retry the folder listing before continuing.";

function errorMessage(error: unknown) {
  return error instanceof ApiError || error instanceof Error ? error.message : "The request could not be completed.";
}

function formatBytes(value?: number) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "Unavailable";
  return formatStorageBytes(value);
}

export function SetupPage() {
  const [initialCheckpoint] = useState(() => readSetupCheckpoint());
  const [step, setStep] = useState(0);
  const [openStep, setOpenStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [resumeNotice, setResumeNotice] = useState("");
  const [checkpointReady, setCheckpointReady] = useState(false);
  const [setupUnlocked, setSetupUnlocked] = useState(false);
  const [managedInstallation, setManagedInstallation] = useState(false);
  const [bootstrapCode, setBootstrapCode] = useState("");
  const [checks, setChecks] = useState<ReadinessCheck[]>([]);
  const [paths, setPaths] = useState({ media: "server/media", database: "server/database.db" });
  const [publicUrl, setPublicUrl] = useState(initialCheckpoint?.publicUrl || window.location.origin);
  const [driveGuideUrl, setDriveGuideUrl] = useState("https://github.com/StreamHome/StreamHome/blob/v0.1.0-alpha.1/docs/google-drive.md");
  const [email, setEmail] = useState(initialCheckpoint?.email ?? "");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [completionState, setCompletionState] = useState<"idle" | "pending" | "unknown" | "completed-unavailable">("idle");
  const completionRef = useRef(false);
  const enrollment = useSetupEnrollment(email, completionState !== "idle");
  const totpEnabled = enrollment.enabled;
  const totpEnrollmentId = enrollment.state.enrollment?.enrollmentId ?? "";
  const totpManualKey = enrollment.state.enrollment?.manualKey ?? "";
  const totpQrImageUrl = enrollment.state.enrollment?.qrImageUrl ?? "";
  const totpExpiresAt = enrollment.state.enrollment?.expiresAt ?? 0;
  const totpVerified = enrollment.state.phase === "verified";
  const [totpCode, setTotpCode] = useState("");
  const [totpCopyStatus, setTotpCopyStatus] = useState<"" | "copied" | "failed">("");
  const [tmdbToken, setTmdbToken] = useState("");
  const [tmdbValid, setTmdbValid] = useState(false);
  const [tmdbValidationToken, setTmdbValidationToken] = useState("");
  const [webPort, setWebPort] = useState(initialCheckpoint?.webPort ?? 3000);
  const [hevc, setHevc] = useState<"auto" | "on" | "off">(initialCheckpoint?.hevc ?? "auto");
  const [backups, setBackups] = useState(initialCheckpoint?.backups ?? false);
  const [updates, setUpdates] = useState(initialCheckpoint?.updates ?? false);
  const [storage, setStorage] = useState<"LOCAL" | "CLOUD">(initialCheckpoint?.storage ?? "LOCAL");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [remoteName, setRemoteName] = useState(initialCheckpoint?.remoteName ?? "streamhome-drive");
  const [audience, setAudience] = useState<"external" | "internal">(initialCheckpoint?.audience ?? "external");
  const [publishingStatus, setPublishingStatus] = useState<"testing" | "production">(initialCheckpoint?.publishingStatus ?? "production");
  const [driveJob, setDriveJob] = useState<DriveSetupJob | null>(null);
  const [driveUnknown, setDriveUnknown] = useState<DriveUnknownOperation | null>(null);
  const driveUnknownRef = useRef<DriveUnknownOperation | null>(null);
  const markDriveUnknown = (operation: DriveUnknownOperation | null) => { driveUnknownRef.current = operation; setDriveUnknown(operation); };
  const [driveJobRead, setDriveJobRead] = useState<"idle" | "loading" | "error" | "ready">("idle");
  const [driveJobReadError, setDriveJobReadError] = useState("");
  const [driveJobRetry, setDriveJobRetry] = useState(0);
  const [resumeDriveJobId, setResumeDriveJobId] = useState(initialCheckpoint?.driveJobId ?? "");
  const [drivePath, setDrivePath] = useState(initialCheckpoint?.drivePath ?? "");
  const [folderPath, setFolderPath] = useState("");
  const [folders, setFolders] = useState<DriveFolder[]>([]);
  const [folderRead, setFolderRead] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [folderError, setFolderError] = useState("");
  const [folderNotice, setFolderNotice] = useState("");
  const folderRequest = useRef<{ generation: number; controller?: AbortController; key: string }>({ generation: 0, key: "" });
  const folderPending = useRef(false);
  const driveEpoch = useRef(0);
  const driveMutationPending = useRef(false);
  const driveReadGeneration = useRef(0);
  const driveJobRef = useRef(driveJob);
  driveJobRef.current = driveJob;
  const [newFolder, setNewFolder] = useState("StreamHome");
  const [healthConsent, setHealthConsent] = useState(false);
  const [driveActivated, setDriveActivated] = useState(false);
  const [remotePath, setRemotePath] = useState("");
  const [quota, setQuota] = useState<{ total?: number; used?: number; free?: number } | null>(null);
  const [result, setResult] = useState<SetupCompleteResponse | null>(null);
  const [secretsSaved, setSecretsSaved] = useState(false);
  const [callbackCopyStatus, setCallbackCopyStatus] = useState<"" | "copied" | "failed">("");
  const [secretsCopyStatus, setSecretsCopyStatus] = useState<"" | "copied" | "failed">("");
  const [statusRead, setStatusRead] = useState<"loading" | "ready" | "error">("loading");
  const [statusError, setStatusError] = useState("");
  const [statusRetry, setStatusRetry] = useState(0);
  const [accountError, setAccountError] = useState<"email" | "password" | "confirm" | "">("");
  const emailInput = useRef<HTMLInputElement>(null);
  const passwordInput = useRef<HTMLInputElement>(null);
  const confirmInput = useRef<HTMLInputElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const completionHeadingRef = useRef<HTMLHeadingElement>(null);
  const stepsRef = useRef<HTMLOListElement>(null);
  const previousStep = useRef(0);
  const mounted = useRef(true);
  const busyRef = useRef(false);
  const oauthWindowRef = useRef<Window | null>(null);
  const oauthJobIdRef = useRef("");
  const callbackReadRef = useRef<{ generation: number; controller?: AbortController }>({ generation: 0 });
  const retireCallbackRead = useCallback(() => { callbackReadRef.current.generation += 1; callbackReadRef.current.controller?.abort(); }, []);
  const mutateDriveJob = async <T,>(action: () => Promise<T>): Promise<T> => {
    driveMutationPending.current = true;
    driveEpoch.current += 1;
    try { return await action(); }
    finally { driveEpoch.current += 1; driveMutationPending.current = false; }
  };

  const setupQuery = useMemo(() => new URLSearchParams(window.location.search), []);
  const queryDriveJob = setupQuery.get("driveJob");
  const queryDriveState = setupQuery.get("drive");
  const oauthPopup = Boolean(queryDriveJob && window.name === "streamhome-google-drive");
  const directCallbackVisit = queryDriveState === "callback";
  const [ignoredQueryJob, setIgnoredQueryJob] = useState(false);
  const driveJobIdToLoad = (!ignoredQueryJob && queryDriveJob) || resumeDriveJobId;
  const acceptedOrigin = parseSetupPublicOrigin(publicUrl);
  const callbackUrl = acceptedOrigin ? `${acceptedOrigin}/api/setup/rclone/drive/callback` : "";
  const totpPending = ["generating", "verifying"].includes(enrollment.state.phase);
  const completionLocked = completionState !== "idle";
  const completionView = result ? "success" : completionState === "unknown" || completionState === "completed-unavailable" ? completionState : null;
  useEffect(() => {
    if (!completionView) return;
    completionHeadingRef.current?.focus({ preventScroll: true });
    completionHeadingRef.current?.closest("main")?.scrollIntoView({ block: "start", behavior: "instant" });
  }, [completionView]);
  const driveMutationBlocked = completionLocked || Boolean(driveUnknown);

  useEffect(() => {
    if (["idle", "error", "expired", "unknown"].includes(enrollment.state.phase)) { setTotpCode(""); setTotpCopyStatus(""); }
  }, [enrollment.state.phase]);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; folderRequest.current.controller?.abort(); retireCallbackRead(); }; }, [retireCallbackRead]);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setStatusRead("loading");
    setStatusError("");
    getSetupStatus({ signal: controller.signal }).then((status) => {
      if (!active) return;
      setManagedInstallation(status.managedInstallation === true);
      if (status.managedInstallation || !initialCheckpoint) setWebPort(status.webPort || 3000);
      setPaths({ media: status.mediaPath, database: status.databasePath });
      if (status.managedInstallation || !initialCheckpoint?.publicUrl) setPublicUrl(status.publicUrl || window.location.origin);
      setDriveGuideUrl(status.driveGuideUrl || "https://github.com/StreamHome/StreamHome/blob/v0.1.0-alpha.1/docs/google-drive.md");
      if (status.unlocked) {
        setSetupUnlocked(true);
        let nextStep = 1;
        if (queryDriveJob || directCallbackVisit) nextStep = 6;
        else if (initialCheckpoint) {
          nextStep = initialCheckpoint.step > 2 ? 2 : Math.max(1, initialCheckpoint.step);
          if (initialCheckpoint.step > 2) setResumeNotice("Your saved setup choices and Google Drive connection were restored. Re-enter the password and service credentials, which are never stored in the browser.");
        }
        setStep(nextStep);
        setOpenStep(nextStep);
        if (queryDriveJob || directCallbackVisit) setStorage("CLOUD");
      }
      setCheckpointReady(true);
      setStatusRead("ready");
    }).catch((reason) => { if (active) { setStatusRead("error"); setStatusError(errorMessage(reason)); } });
    return () => { active = false; controller.abort(); };
  }, [directCallbackVisit, initialCheckpoint, queryDriveJob, statusRetry]);

  useEffect(() => {
    if (step !== previousStep.current) {
      headingRef.current?.focus({ preventScroll: true });
      headingRef.current?.scrollIntoView({ block: "start", behavior: "instant" });
      previousStep.current = step;
    }
    const reveal = () => {
      const container = stepsRef.current;
      const row = container?.querySelector<HTMLElement>(`li[data-active="true"]`);
      if (container && row) revealSetupStep(container, row);
    };
    reveal();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(reveal);
    if (stepsRef.current) observer?.observe(stepsRef.current);
    let active = true;
    void document.fonts?.ready.then(() => { if (active) reveal(); });
    window.addEventListener("resize", reveal);
    return () => { active = false; observer?.disconnect(); window.removeEventListener("resize", reveal); };
  }, [openStep, step, statusRead]);

  useEffect(() => {
    if (!setupUnlocked || !driveJobIdToLoad || oauthPopup || completionRef.current) return;
    const controller = new AbortController();
    let active = true;
    const epoch = driveEpoch.current;
    const readGeneration = ++driveReadGeneration.current;
    const jobId = driveJobIdToLoad;
    const owned = () => active && mounted.current && !completionRef.current && !driveMutationPending.current
      && epoch === driveEpoch.current && readGeneration === driveReadGeneration.current
      && (!driveJobRef.current || driveJobRef.current.id === jobId);
    setDriveJobRead("loading"); setDriveJobReadError("");
    getDriveJob(jobId, { signal: controller.signal }).then((job) => {
      if (!owned() || job.id !== jobId) return;
      setDriveJob(job);
      setDriveJobRead("ready");
      setResumeDriveJobId(job.id);
      setDrivePath(job.selectedPath);
      setRemoteName(job.remoteName);
      setAudience(job.audience);
      setPublishingStatus(job.publishingStatus);
      setStorage("CLOUD");
    }).catch((reason) => {
      if (!owned()) return;
      setDriveJobRead("error");
      setDriveJobReadError(errorMessage(reason));
    });
    return () => { active = false; controller.abort(); };
  }, [driveJobIdToLoad, driveJobRetry, oauthPopup, setupUnlocked]);

  useEffect(() => {
    if (!driveJob || TERMINAL_DRIVE_STATES.has(driveJob.status)) return;
    let active = true;
    let pending = false;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      if (pending || busyRef.current || completionRef.current) return;
      pending = true;
      const epoch = driveEpoch.current;
      const readGeneration = ++driveReadGeneration.current;
      getDriveJob(driveJob.id, { signal: controller.signal }).then((job) => {
        if (!active || busyRef.current || completionRef.current || epoch !== driveEpoch.current
            || readGeneration !== driveReadGeneration.current || driveJobRef.current?.id !== job.id) return;
        setDriveJob(job); setDriveJobRead("ready"); setDriveJobReadError("");
        if (job.selectedPath) setDrivePath(job.selectedPath);
      }).catch(() => undefined).finally(() => { pending = false; });
    }, 1500);
    return () => { active = false; controller.abort(); window.clearInterval(timer); };
  }, [driveJob?.id, driveJob?.status]);

  useEffect(() => {
    if (!checkpointReady || oauthPopup || directCallbackVisit || result) return;
    writeSetupCheckpoint({
      version: 1,
      step,
      email,
      publicUrl,
      webPort,
      hevc,
      backups,
      updates,
      storage,
      remoteName,
      audience,
      publishingStatus,
      driveJobId: driveJob?.id || resumeDriveJobId,
      drivePath,
    });
  }, [
    audience,
    backups,
    checkpointReady,
    directCallbackVisit,
    driveJob?.id,
    drivePath,
    email,
    hevc,
    oauthPopup,
    publicUrl,
    publishingStatus,
    remoteName,
    result,
    resumeDriveJobId,
    step,
    storage,
    updates,
    webPort,
  ]);

  useEffect(() => {
    const receiveOAuthResult = (event: MessageEvent) => {
      const data = event.data as { type?: unknown; jobId?: unknown } | null;
      if (
        completionRef.current
        || driveMutationPending.current
        || !data
        || data.type !== "streamhome:drive-oauth-complete"
        || typeof data.jobId !== "string"
        || data.jobId !== oauthJobIdRef.current
        || event.source !== oauthWindowRef.current
      ) return;
      const configuredOrigin = (() => {
        try { return new URL(publicUrl).origin; } catch { return ""; }
      })();
      if (event.origin !== window.location.origin && event.origin !== configuredOrigin) return;
      setStep(6);
      setOpenStep(6);
      setStorage("CLOUD");
      setResumeDriveJobId(data.jobId);
      retireCallbackRead();
      const controller = new AbortController();
      callbackReadRef.current.controller = controller;
      const generation = callbackReadRef.current.generation;
      const epoch = driveEpoch.current;
      const jobId = data.jobId;
      const readGeneration = ++driveReadGeneration.current;
      setDriveJobRead("loading"); setDriveJobReadError("");
      const ownsRead = () => mounted.current && !completionRef.current && !driveMutationPending.current && !controller.signal.aborted
        && readGeneration === driveReadGeneration.current
        && callbackReadRef.current.generation === generation && driveEpoch.current === epoch
        && oauthJobIdRef.current === jobId && event.source === oauthWindowRef.current;
      void getDriveJob(jobId, { signal: controller.signal }).then((job) => {
        if (!ownsRead() || job.id !== jobId) return;
        setDriveJob(job); setDriveJobRead("ready"); setDriveJobReadError("");
        if (job.selectedPath) setDrivePath(job.selectedPath);
      }).catch((reason) => { if (ownsRead()) { setDriveJobRead("error"); setDriveJobReadError(errorMessage(reason)); setError(errorMessage(reason)); } });
    };
    window.addEventListener("message", receiveOAuthResult);
    return () => { window.removeEventListener("message", receiveOAuthResult); retireCallbackRead(); };
  }, [publicUrl, retireCallbackRead]);

  useEffect(() => {
    if (!oauthPopup || !queryDriveJob || !queryDriveState) return;
    if (window.opener && !window.opener.closed) {
      window.opener.postMessage({
        type: "streamhome:drive-oauth-complete",
        jobId: queryDriveJob,
        result: queryDriveState,
      }, "*");
      const closeTimer = window.setTimeout(() => window.close(), 900);
      return () => window.clearTimeout(closeTimer);
    }
  }, [oauthPopup, queryDriveJob, queryDriveState]);

  useEffect(() => {
    if (!result) return;
    clearSetupCheckpoint();
  }, [result]);

  const run = async (action: () => Promise<void>) => {
    if (busyRef.current || completionRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    try { await action(); } catch (reason) { if (mounted.current) setError(errorMessage(reason)); }
    finally { busyRef.current = false; if (mounted.current) setBusy(false); }
  };

  const goToStep = (next: number) => {
    if (completionRef.current) return;
    setError("");
    setStep(next);
    setOpenStep(next);
  };

  const unlock = () => run(async () => {
    await unlockSetup(bootstrapCode, driveJobIdToLoad);
    const status = await getSetupStatus();
    setSetupUnlocked(true);
    setPaths({ media: status.mediaPath, database: status.databasePath });
    setPublicUrl(status.publicUrl || window.location.origin);
    if (directCallbackVisit) {
      setStorage("CLOUD");
      goToStep(6);
    } else {
      goToStep(1);
    }
  });

  const inspect = () => run(async () => {
    const readiness = await getSetupReadiness();
    setChecks(readiness.checks);
    if (readiness.ready) goToStep(2);
    else setError("Resolve the required checks before continuing. Rclone is optional when local storage is selected.");
  });

  const changeAdministratorEmail = (nextEmail: string) => {
    if (completionRef.current) return;
    setEmail(nextEmail);
    setAccountError("");
    setTotpCode("");
    setTotpCopyStatus("");
  };

  const prepareSecurity = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busyRef.current || completionRef.current) return;
    const trimmed = email.trim();
    if (emailInput.current) emailInput.current.value = trimmed;
    setEmail(trimmed);
    let invalid: "email" | "password" | "confirm" | "" = "";
    let message = "";
    if (!trimmed || !emailInput.current?.validity.valid) { invalid = "email"; message = "Enter a valid administrator email."; }
    else if (password.length < 6 || new TextEncoder().encode(password).length > 72) { invalid = "password"; message = "Use a password between 6 characters and 72 UTF-8 bytes."; }
    else if (password !== confirmPassword) { invalid = "confirm"; message = "The passwords do not match."; }
    setAccountError(invalid);
    if (invalid) { setError(message); ({ email: emailInput, password: passwordInput, confirm: confirmInput }[invalid]).current?.focus(); return; }
    setResumeNotice("");
    goToStep(3);
  };

  const toggleTotp = (enabled: boolean) => { setTotpCode(""); setTotpCopyStatus(""); enrollment.choose(enabled); };
  const verifyTotp = () => enrollment.verify(totpCode);

  const copyTotpKey = async () => {
    const copied = await copySetupText(totpManualKey);
    setTotpCopyStatus(copied ? "copied" : "failed");
  };

  const validateTmdb = () => run(async () => {
    const validation = await validateSetupTMDB(tmdbToken);
    setTmdbValidationToken(validation.validationToken);
    setTmdbValid(true);
  });

  const loadFolders = useCallback(async (jobId: string, path = "", allowOwnedRefresh = false) => {
    if ((busyRef.current && !allowOwnedRefresh) || folderPending.current || completionRef.current || driveJobRef.current?.id !== jobId) return false;
    const generation = ++folderRequest.current.generation;
    folderRequest.current.controller?.abort();
    const controller = new AbortController();
    folderRequest.current.controller = controller;
    folderRequest.current.key = `${jobId}:${path}`;
    folderPending.current = true;
    setFolderPath(path);
    setFolderRead("loading");
    setFolderError("");
    setFolders([]);
    try {
      const response = await listDriveFolders(jobId, path, { signal: controller.signal });
      if (!mounted.current || generation !== folderRequest.current.generation || driveJobRef.current?.id !== jobId) return false;
      setFolderPath(response.path);
      setFolders(response.folders);
      setFolderRead("ready");
      setFolderNotice((notice) => notice === CREATED_FOLDER_REFRESH_FAILED_NOTICE ? "" : notice);
      return true;
    } catch (reason) {
      if (mounted.current && generation === folderRequest.current.generation && driveJobRef.current?.id === jobId) { setFolderRead("error"); setFolderError(errorMessage(reason)); }
      return false;
    } finally { if (generation === folderRequest.current.generation) folderPending.current = false; }
  }, []);

  useEffect(() => {
    if (driveJob?.status === "selecting_folder" && folderRequest.current.key !== `${driveJob.id}:${folderPath}` && !folderPending.current && !busyRef.current) void loadFolders(driveJob.id, folderPath);
  }, [busy, driveJob?.id, driveJob?.status, folderPath, loadFolders]);

  const startOAuth = async (explicitReplacement = false) => {
    if (busyRef.current || completionRef.current || (driveUnknownRef.current && !explicitReplacement)) return;
    if (!acceptedOrigin) { setError("Enter a valid public origin in Server behavior before connecting Google Drive."); return; }
    busyRef.current = true;
    retireCallbackRead();
    const popup = window.open("about:blank", "streamhome-google-drive", "popup,width=620,height=760");
    if (!popup) {
      busyRef.current = false;
      setError("Google authorization needs a pop-up so this setup page can keep your progress. Allow pop-ups for StreamHome, then try again.");
      return;
    }
    oauthWindowRef.current = popup;
    setBusy(true);
    setError("");
    let startAcknowledged = false;
    try {
      const started = await startDriveOAuth({ clientId, clientSecret, remoteName, audience, publishingStatus, publicUrl: acceptedOrigin });
      startAcknowledged = true;
      driveEpoch.current += 1;
      folderRequest.current.generation += 1;
      folderRequest.current.controller?.abort();
      folderRequest.current.key = "";
      folderPending.current = false;
      setFolderPath(""); setFolders([]); setFolderRead("idle"); setFolderNotice(""); setFolderError("");
      markDriveUnknown(null);
      oauthJobIdRef.current = started.jobId;
      setResumeDriveJobId(started.jobId);
      setDriveJob(null);
      setDrivePath("");
      setDriveActivated(false);
      setRemotePath("");
      if (popup.closed) {
        throw new Error("The Google authorization window was closed. Start the connection again.");
      }
      popup.location.replace(started.authorizationUrl);
    } catch (reason) {
      popup.close();
      oauthWindowRef.current = null;
      oauthJobIdRef.current = "";
      if (!startAcknowledged && isUnknownSetupOutcome(reason)) { markDriveUnknown({ operation: "start" }); setError(""); }
      else setError(errorMessage(reason));
    } finally {
      busyRef.current = false;
      if (mounted.current) setBusy(false);
    }
  };

  const resetDriveConnection = () => {
    retireCallbackRead();
    driveEpoch.current += 1;
    setIgnoredQueryJob(true);
    setDriveJob(null); setDriveJobRead("idle"); setDriveJobReadError(""); setResumeDriveJobId(""); setDrivePath(""); setFolders([]); setFolderRead("idle"); setFolderError(""); setFolderNotice(""); setFolderPath(""); setDriveActivated(false);
    oauthWindowRef.current?.close(); oauthWindowRef.current = null; oauthJobIdRef.current = "";
  };

  const cancelOAuth = () => {
    if (driveUnknownRef.current) return;
    return run(async () => {
    retireCallbackRead();
    driveEpoch.current += 1;
    folderRequest.current.generation += 1;
    folderRequest.current.controller?.abort();
    folderRequest.current.key = "";
    folderPending.current = false;
    setFolderRead("error"); setFolderError("The previous folder listing was cancelled. Refresh to read it again.");
    if (driveJob) {
      try { await mutateDriveJob(() => cancelDriveJob(driveJob.id)); }
      catch (reason) { if (isUnknownSetupOutcome(reason)) { markDriveUnknown({ operation: "cancel", jobId: driveJob.id }); return; } throw reason; }
    }
    resetDriveConnection();
  });
  };

  const createFolder = () => {
    if (folderPending.current || driveUnknownRef.current || folderRead !== "ready" || !driveJob || folderRequest.current.key !== `${driveJob.id}:${folderPath}` || !newFolder.trim()) return;
    return run(async () => {
    if (!driveJob) return;
    const jobId = driveJob.id;
    setFolderNotice("");
    const path = [folderPath, newFolder.trim()].filter(Boolean).join("/");
    let created: { path: string };
    try { created = await createDriveFolder(driveJob.id, path); }
    catch (reason) { if (isUnknownSetupOutcome(reason)) { markDriveUnknown({ operation: "create", jobId, path }); return; } throw reason; }
    if (!mounted.current || driveJobRef.current?.id !== jobId) return;
    setNewFolder("");
    setDrivePath(created.path);
    const refreshed = await loadFolders(jobId, folderPath, true);
    if (!refreshed && mounted.current && driveJobRef.current?.id === jobId) setFolderNotice(CREATED_FOLDER_REFRESH_FAILED_NOTICE);
  });
  };

  const chooseFolder = (path: string) => {
    if (folderPending.current || driveUnknownRef.current || folderRead !== "ready") return;
    return run(async () => {
    if (!driveJob) return;
    let job: DriveSetupJob;
    try { job = await mutateDriveJob(() => selectDriveFolder(driveJob.id, path)); }
    catch (reason) { if (isUnknownSetupOutcome(reason)) { markDriveUnknown({ operation: "select", jobId: driveJob.id, path }); return; } throw reason; }
    setDriveJob(job);
    setDrivePath(path);
    setHealthConsent(false);
    setDriveActivated(false);
  });
  };

  const testDrive = () => run(async () => {
    if (!driveJob || driveUnknownRef.current || folderPending.current || !healthConsent) return;
    let tested: Awaited<ReturnType<typeof testDriveFolder>>;
    try { tested = await mutateDriveJob(() => testDriveFolder(driveJob.id)); }
    catch (reason) { if (isUnknownSetupOutcome(reason)) { markDriveUnknown({ operation: "test", jobId: driveJob.id }); return; } throw reason; }
    setDriveJob(tested.job);
    setRemotePath(tested.remotePath);
    setQuota(tested.quota ?? null);
  });

  const activate = () => run(async () => {
    if (!driveJob || driveUnknownRef.current || driveActivated || driveJob.status !== "ready") return;
    let activated: Awaited<ReturnType<typeof activateDrive>>;
    try { activated = await mutateDriveJob(() => activateDrive(driveJob.id)); }
    catch (reason) { if (isUnknownSetupOutcome(reason)) { markDriveUnknown({ operation: "activate", jobId: driveJob.id }); return; } throw reason; }
    setDriveJob(activated.job);
    setRemotePath(activated.remotePath);
    setDriveActivated(true);
  });

  const checkDriveOutcome = () => run(async () => {
    const uncertain = driveUnknownRef.current;
    if (!uncertain?.jobId || driveJobRef.current?.id !== uncertain.jobId) return;
    const epoch = driveEpoch.current;
    const job = await getDriveJob(uncertain.jobId);
    if (!mounted.current || epoch !== driveEpoch.current || driveUnknownRef.current !== uncertain) return;
    setDriveJob(job);
    if (uncertain.operation === "create") {
      const parent = uncertain.path?.split("/").slice(0, -1).join("/") ?? "";
      const listing = await listDriveFolders(uncertain.jobId, parent);
      if (!mounted.current || epoch !== driveEpoch.current || driveUnknownRef.current !== uncertain) return;
      setFolderPath(listing.path); setFolders(listing.folders); setFolderRead("ready"); setFolderError("");
      if (listing.folders.some((folder) => folder.path === uncertain.path)) { setDrivePath(uncertain.path ?? ""); setFolderNotice("The requested folder exists. Confirm its selection before testing Drive."); markDriveUnknown(null); return; }
    } else if (uncertain.operation === "select" && job.selectedPath === uncertain.path) {
      setDrivePath(job.selectedPath); setHealthConsent(false); setDriveActivated(false); markDriveUnknown(null); return;
    } else if (uncertain.operation === "test" && job.status === "ready") {
      markDriveUnknown(null); return;
    } else if (uncertain.operation === "cancel" && ["cancelled", "expired"].includes(job.status)) {
      resetDriveConnection(); markDriveUnknown(null); return;
    }
    setFolderNotice(uncertain.operation === "activate" ? "The job read cannot prove activation succeeded or that the earlier operation stopped. Do not repeat Drive changes; use the server operator recovery guidance." : "The requested outcome is not confirmed. The earlier operation may still finish. Check again or use the server operator recovery guidance before repeating the Drive change.");
  });

  useEffect(() => {
    if (enrollment.state.phase === "expired" && totpEnabled && step > 3 && !completionLocked) { setStep(3); setOpenStep(3); }
  }, [completionLocked, enrollment.state.phase, step, totpEnabled]);

  const finish = async () => {
    if (busyRef.current || completionRef.current || completionState !== "idle") return;
    if (driveUnknownRef.current) { goToStep(6); setError("Resolve the unknown Google Drive operation before initializing."); return; }
    if (!email.trim() || password.length < 6 || new TextEncoder().encode(password).length > 72 || password !== confirmPassword) { goToStep(2); setError("Re-enter and confirm the administrator credentials before initializing."); return; }
    if (!tmdbValid || !tmdbValidationToken) { goToStep(4); setError("Validate the TMDB token before initializing."); return; }
    if (storage === "CLOUD" && (!driveActivated || !driveJob || !remotePath)) { goToStep(6); setError("Verify and activate Google Drive before initializing."); return; }
    if (!acceptedOrigin || !validSetupPort(webPort)) { goToStep(5); setError("Enter a valid public origin and web port."); return; }
    if (totpEnabled && (!totpVerified || Date.now() >= totpExpiresAt * 1000)) { goToStep(3); setError("Generate and verify a current TOTP enrollment before initializing."); return; }
    retireCallbackRead();
    completionRef.current = true;
    setCompletionState("pending");
    setError("");
    // One immutable attempt owns the accepted configuration and eventual show-once response.
    const attempt = { email: email.trim(), password, tmdb_token: tmdbToken, tmdb_validation_token: tmdbValidationToken,
      web_port: webPort, public_url: acceptedOrigin, totp_enrollment_id: totpEnabled ? totpEnrollmentId : undefined,
      backup_enabled: backups, auto_update_enabled: updates, hevc_compression_mode: hevc, storage_engine: storage,
      rclone_remote_path: storage === "CLOUD" ? remotePath : undefined, drive_job_id: storage === "CLOUD" ? driveJob?.id : undefined };
    try {
      const response = await completeSetup(attempt);
      if (mounted.current) setResult(response);
    } catch (reason) {
      if (!mounted.current) return;
      if (isUnknownSetupOutcome(reason)) { setCompletionState("unknown"); setError("Initialization may still finish on the server. Do not initialize or change enrollment again. Check setup status before taking further action."); }
      else {
        completionRef.current = false;
        setCompletionState("idle");
        if (totpEnabled && isRetiredSetupEnrollment(reason)) { enrollment.rejectCompletion(); setStep(3); setOpenStep(3); }
        setError(errorMessage(reason));
      }
    }
  };

  const checkCompletionStatus = async () => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true);
    try {
      const status = await getSetupStatus();
      if (!mounted.current) return;
      if (!status.required) { clearSetupCheckpoint(); setCompletionState("completed-unavailable"); setError(""); }
      else setError("Setup is not reported complete yet. This does not prove the earlier operation stopped. Initialization and configuration changes remain blocked. Wait, check status again, or use the server operator recovery guidance before attempting setup in a new session.");
    } catch (reason) { if (mounted.current) setError(errorMessage(reason)); }
    finally { busyRef.current = false; if (mounted.current) setBusy(false); }
  };

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

  const secretsText = useMemo(() => result ? formatSetupSecrets(result) : "", [result]);

  const downloadSecrets = () => {
    const url = URL.createObjectURL(new Blob([secretsText], { type: "text/plain" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "streamhome-recovery.txt";
    link.click();
    URL.revokeObjectURL(url);
  };

  const copyCallback = async () => {
    if (!callbackUrl) return;
    setCallbackCopyStatus("");
    const copied = await copySetupText(callbackUrl);
    setCallbackCopyStatus(copied ? "copied" : "failed");
  };

  const copySecrets = async () => {
    setSecretsCopyStatus("");
    const copied = await copySetupText(secretsText);
    setSecretsCopyStatus(copied ? "copied" : "failed");
  };

  const returnToSetupProgress = () => {
    if (window.history.length > 1) {
      window.history.back();
      return;
    }
    const target = new URL("/setup", window.location.origin);
    window.location.assign(target.toString());
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
    <span className="setup-success-mark" aria-hidden="true">{queryDriveState === "connected" ? "✓" : "!"}</span>
    <p className="setup-eyebrow">GOOGLE DRIVE AUTHORIZATION</p>
    <h1>{queryDriveState === "connected" ? "Drive connected" : "Authorization needs attention"}</h1>
    <p>{queryDriveState === "connected" ? "Your original setup page is continuing at the Drive folder step. This window will close automatically." : "Return to the original setup page to review the Google authorization error and retry without losing your setup progress."}</p>
    <button className="setup-primary" onClick={() => window.close()}>Close this window</button>
  </section></main>;

  if (result) return <main className="setup-page setup-page--complete"><section className="setup-complete-panel">
    <span className="setup-success-mark" aria-hidden="true">✓</span>
    <p className="setup-eyebrow">INSTALLATION COMPLETE</p>
    <h1 ref={completionHeadingRef} tabIndex={-1}>{result.restartScheduled ? "StreamHome is restarting" : "StreamHome is ready"}</h1>
    <p>Save these values now. They will not be shown again.</p>
    <pre>{secretsText}</pre>
    <div className="setup-actions"><button onClick={copySecrets}>Copy</button><button onClick={downloadSecrets}>Download</button></div>
    {secretsCopyStatus && <p className="setup-copy-feedback" data-state={secretsCopyStatus} role="status">{secretsCopyStatus === "copied" ? "Secrets copied." : "Copy was blocked. Select the text above and copy it manually."}</p>}
    <label className="setup-check"><input type="checkbox" checked={secretsSaved} onChange={(event) => setSecretsSaved(event.target.checked)} /> I saved the ingestion token{result.recoveryCodes.length ? " and recovery codes" : ""}.</label>
    <button className="setup-primary" disabled={!secretsSaved} onClick={openLogin}>Open StreamHome</button>
  </section></main>;

  if (completionState === "unknown" || completionState === "completed-unavailable") return <main className="setup-page setup-page--complete"><section className="setup-complete-panel">
    <h1 ref={completionHeadingRef} tabIndex={-1}>{completionState === "completed-unavailable" ? "Setup completed" : "Initialization outcome is unknown"}</h1>
    <p>{completionState === "completed-unavailable" ? "The server reports setup complete, but the one-time token and recovery values were not retrieved. Sign in normally and use Admin security and integration settings to replace the values you need. They cannot be recovered from this page." : "The initialization response was lost. The earlier server operation may still finish. Initialization and configuration changes are blocked."}</p>
    {error && <p className="setup-error" role="alert">{error}</p>}
    {completionState === "unknown" ? <button className="setup-primary" disabled={busy} onClick={checkCompletionStatus}>Check setup status</button> : <button className="setup-primary" onClick={openLogin}>Open sign-in</button>}
  </section></main>;
  if (statusRead !== "ready") return <main className="setup-gate-state" aria-busy={statusRead === "loading"}><h1>{statusRead === "loading" ? "Checking setup state" : "Setup status is unavailable"}</h1>{statusRead === "loading" ? <p role="status">Checking the server before restoring setup…</p> : <><p role="alert">{statusError}</p><button onClick={() => setStatusRetry((value) => value + 1)}>Retry setup status</button></>}</main>;

  return <main className="setup-page" data-step={step}>
    <aside className="setup-sidebar">
      <BrandLogo className="setup-logo" />
      <div><p className="setup-eyebrow">SELF-HOSTED CONTROL PLANE</p><h1>Configure StreamHome</h1><span>Secure first-run provisioning for your private media server.</span></div>
      <ol ref={stepsRef} className="setup-step-accordion">{STEPS.map((label, index) => {
        const available = index <= step;
        const expanded = openStep === index;
        return <li key={label} data-active={index === step} data-done={index < step} data-locked={!available}>
          <button type="button" className="setup-step-trigger" aria-expanded={expanded} aria-current={index === step ? "step" : undefined} disabled={!available || completionLocked} onClick={() => {
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
        {resumeNotice && <div className="setup-resume-notice" role="status"><i aria-hidden="true">↻</i><div><strong>Setup progress restored</strong><span>{resumeNotice}</span></div></div>}
        {step === 0 && <><h2 ref={headingRef} tabIndex={-1}>Unlock this installation</h2><p>Enter the one-time bootstrap code from <code>{managedInstallation ? "sudo streamhome bootstrap" : "./start.sh"}</code>.</p><label>Bootstrap code<input autoFocus type="password" autoComplete="off" value={bootstrapCode} onChange={(event) => setBootstrapCode(event.target.value)} onKeyDown={(event) => event.key === "Enter" && void unlock()} /></label><button className="setup-primary" disabled={!bootstrapCode || busy} onClick={unlock}>Unlock setup</button></>}

        {step === 1 && <><h2 ref={headingRef} tabIndex={-1}>System readiness</h2><p>StreamHome checks its runtime without changing the established media layout.</p>{checks.length > 0 && <div className="setup-check-grid">{checks.map((check) => <article key={check.id} data-ready={check.ready}><i>{check.ready ? "✓" : "!"}</i><div><strong>{check.id.replace(/_/g, " ")}</strong><span>{check.detail}</span></div></article>)}</div>}<button className="setup-primary" disabled={busy} onClick={inspect}>{checks.length ? "Check again" : "Run system checks"}</button></>}

        {step === 2 && <form noValidate onSubmit={prepareSecurity}><h2 ref={headingRef} tabIndex={-1}>Create the administrator</h2><p>This account controls profiles, server settings, storage, and security.</p><div className="setup-form-grid"><label>Email address<input ref={emailInput} required type="email" autoComplete="username" aria-invalid={accountError === "email" || undefined} aria-describedby={accountError === "email" ? "setup-account-error" : undefined} value={email} onChange={(event) => changeAdministratorEmail(event.target.value)} /></label><label>Password<input ref={passwordInput} required type="password" autoComplete="new-password" aria-invalid={accountError === "password" || undefined} aria-describedby={accountError === "password" ? "setup-account-error" : undefined} value={password} onChange={(event) => setPassword(event.target.value)} /></label><label>Confirm password<input ref={confirmInput} required type="password" autoComplete="new-password" aria-invalid={accountError === "confirm" || undefined} aria-describedby={accountError === "confirm" ? "setup-account-error" : undefined} value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} /></label></div><button type="submit" className="setup-primary" disabled={busy || completionLocked}>Continue</button></form>}


        {step === 3 && <><h2 ref={headingRef} tabIndex={-1}>Local account security</h2><p>TOTP is optional. When enabled, setup creates ten one-time recovery codes.</p><fieldset className="setup-choice"><legend>Account security</legend><label data-selected={!totpEnabled}><input type="radio" name="setup-security" checked={!totpEnabled} disabled={completionLocked || totpPending} onChange={() => toggleTotp(false)} /><strong>Password only</strong><span>Enable TOTP later in Admin.</span></label><label data-selected={totpEnabled}><input type="radio" name="setup-security" checked={totpEnabled} disabled={completionLocked || totpPending} onChange={() => toggleTotp(true)} /><strong>Password + TOTP</strong><span>Recommended for remotely accessible servers.</span></label></fieldset>{totpEnabled && <div className="setup-secret setup-totp-enrollment">{totpQrImageUrl ? <><div className="setup-totp-qr"><img src={totpQrImageUrl} alt={`Scan this QR code to add StreamHome for ${email} to an authenticator app`} /><div><strong>Scan with your authenticator</strong><span>The QR code is generated by this StreamHome server and expires at {new Date(totpExpiresAt * 1000).toLocaleTimeString()}.</span></div></div><div className="setup-totp-manual"><span>Can&apos;t scan? Enter this setup key manually.</span><code>{totpManualKey}</code><button type="button" onClick={() => void copyTotpKey()}>{totpCopyStatus === "copied" ? "Copied" : "Copy setup key"}</button>{totpCopyStatus === "failed" && <small role="status">Copy was blocked. Select the key and copy it manually.</small>}</div><label>Six-digit authenticator code<input inputMode="numeric" autoComplete="one-time-code" maxLength={6} disabled={totpPending || completionLocked} value={totpCode} onChange={(event) => { setTotpCode(event.target.value.replace(/\D/g, "")); enrollment.clearVerification(); }} /></label><button type="button" onClick={verifyTotp} disabled={!totpEnrollmentId || totpCode.length !== 6 || totpPending || completionLocked}>{totpVerified ? "Verified ✓" : "Verify code"}</button></> : totpPending ? <p role="status">Generating secure TOTP enrollment…</p> : <button type="button" onClick={() => { setTotpCode(""); setTotpCopyStatus(""); enrollment.generate(); }}>{enrollment.state.phase === "unknown" ? "Start new enrollment" : enrollment.state.phase === "error" ? "Retry enrollment" : "Generate new enrollment"}</button>}{enrollment.state.message && <p className="setup-notice" role="status">{enrollment.state.message}</p>}</div>}<div className="setup-footer-actions"><button type="button" disabled={completionLocked} onClick={() => goToStep(2)}>Back</button><button type="button" className="setup-primary" disabled={busy || completionLocked || (totpEnabled && !totpVerified)} onClick={() => goToStep(4)}>Continue</button></div></>}


        {step === 4 && <><h2 ref={headingRef} tabIndex={-1}>Connect TMDB</h2><p>A valid TMDB API Read Access Token is required for catalog metadata and artwork.</p><p className="setup-token-help">In your TMDB account, open <a href="https://www.themoviedb.org/settings/api" target="_blank" rel="noreferrer">Settings → API</a> and copy the API Read Access Token. Paste the raw token without a Bearer prefix; this is different from the v3 API key or your password. <a href="https://developer.themoviedb.org/docs/authentication-application" target="_blank" rel="noreferrer">Read the official token guide</a>.</p><label>Read-access token<textarea value={tmdbToken} onChange={(event) => { setTmdbToken(event.target.value.trim()); setTmdbValid(false); setTmdbValidationToken(""); }} rows={5} /></label><button onClick={validateTmdb} disabled={!tmdbToken || busy}>{tmdbValid ? "TMDB connected ✓" : "Validate token"}</button><div className="setup-footer-actions"><button onClick={() => goToStep(3)}>Back</button><button className="setup-primary" disabled={!tmdbValid} onClick={() => goToStep(5)}>Continue</button></div></>}

        {step === 5 && <><h2 ref={headingRef} tabIndex={-1}>Server behavior</h2><p>Database and media locations remain standardized for reliable recovery.</p><div className="setup-paths"><span><b>Database</b>{paths.database}</span><span><b>Media</b>{paths.media}</span></div><div className="setup-form-grid"><label>Public StreamHome URL<input type="url" readOnly={managedInstallation} value={publicUrl} onChange={(event) => setPublicUrl(event.target.value.trim())} placeholder="https://watch.example.com" /></label><label>Web port<input type="number" readOnly={managedInstallation} min={managedInstallation ? 1024 : 1} max={65535} value={webPort} onChange={(event) => setWebPort(Number(event.target.value))} /></label><label>HEVC compression<select value={hevc} onChange={(event) => setHevc(event.target.value as typeof hevc)}>{Object.entries(HEVC_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label></div><small className="setup-field-note">The public origin creates the exact Google OAuth callback and must not include a path.{managedInstallation && <> Change the port or public URL with <code>sudo streamhome configure</code>.</>}</small><label className="setup-check"><input type="checkbox" checked={backups} onChange={(event) => setBackups(event.target.checked)} /> Enable automatic database backups</label><label className="setup-check"><input type="checkbox" checked={updates} onChange={(event) => setUpdates(event.target.checked)} /> Enable health-gated automatic updates when StreamHome is idle</label><div className="setup-footer-actions"><button onClick={() => goToStep(4)}>Back</button><button className="setup-primary" onClick={() => { if (!acceptedOrigin || !validSetupPort(webPort)) { setError("Use a valid HTTPS public origin (or HTTP for a private-network address) and an integer port from 1 to 65535."); return; } setPublicUrl(acceptedOrigin); goToStep(6); }}>Continue</button></div></>}

        {step === 6 && <><h2 ref={headingRef} tabIndex={-1}>Storage</h2><p>Keep media on this server, or connect Google Drive without installing Rclone on another computer.</p>
          {directCallbackVisit && <div className="setup-callback-notice" role="status"><i aria-hidden="true">↩</i><div><strong>This address belongs in Google Cloud</strong><span>It is an OAuth return address, not a page you need to open. Return to the setup tab you were using; its progress is still there.</span></div><button type="button" onClick={returnToSetupProgress}>Return to setup progress</button></div>}
          <fieldset className="setup-choice"><legend>Media storage</legend><label data-selected={storage === "LOCAL"}><input type="radio" name="setup-storage" checked={storage === "LOCAL"} disabled={busy || folderRead === "loading" || completionLocked} onChange={() => setStorage("LOCAL")} /><strong>Local storage</strong><span>Use the standard server/media catalog.</span></label><label data-selected={storage === "CLOUD"}><input type="radio" name="setup-storage" checked={storage === "CLOUD"} disabled={busy || folderRead === "loading" || completionLocked} onChange={() => setStorage("CLOUD")} /><strong>Google Drive</strong><span>Authorize Drive directly in this browser.</span></label></fieldset>
          {storage === "CLOUD" && <div className="setup-drive-config">
            {driveUnknown && <div className="setup-notice" role="status"><strong>Google Drive operation outcome is unknown.</strong><p>The earlier {driveUnknown.operation} request may still finish on the server. Further Drive changes are blocked until its outcome can be confirmed.{driveUnknown.operation === "start" ? " No job ID was received, so this page cannot cancel or inspect that request. Starting a new connection is an explicit replacement attempt and does not prove the earlier request stopped." : " A status read does not by itself prove the earlier request stopped."}</p>{driveUnknown.jobId ? <button type="button" disabled={busy || folderRead === "loading"} onClick={checkDriveOutcome}>Check Drive operation status</button> : <button type="button" disabled={busy || !acceptedOrigin} onClick={() => void startOAuth(true)}>Start new connection</button>}{folderNotice && <p>{folderNotice}</p>}</div>}
            {!driveJob && driveJobIdToLoad && <div className="setup-notice">{driveJobRead === "error" ? <><p role="alert">{driveJobReadError}</p><button type="button" disabled={busy} onClick={() => setDriveJobRetry((value) => value + 1)}>Retry Drive job status</button></> : <p role="status">Loading the existing Drive connection…</p>}</div>}
            <a className="setup-guide-link" href={driveGuideUrl} target="_blank" rel="noreferrer"><span>GOOGLE CLOUD GUIDE</span><strong>Learn how to create the required Client ID and OAuth application</strong><i>↗</i></a>
            <div className="setup-callback"><span>Authorized redirect URI · copy this into Google Cloud; do not open it directly</span><code>{callbackUrl || "Enter a valid public origin in Server behavior to obtain the callback URL."}</code><button disabled={!callbackUrl} type="button" className="setup-copy-button" onClick={copyCallback} aria-label="Copy authorized redirect URI"><svg aria-hidden="true" viewBox="0 0 24 24"><rect x="8" y="8" width="11" height="11" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" /></svg><span>{callbackCopyStatus === "copied" ? "Copied" : "Copy"}</span></button>{callbackCopyStatus && <small className="setup-copy-feedback" data-state={callbackCopyStatus} role="status">{callbackCopyStatus === "copied" ? "Callback URL copied." : "Copy was blocked. Select the URL and copy it manually."}</small>}</div>
            {!driveJob && !driveJobIdToLoad && <div className="setup-secret setup-drive-credentials"><div className="setup-form-grid"><label>Google client ID<input value={clientId} onChange={(event) => setClientId(event.target.value.trim())} autoComplete="off" placeholder="….apps.googleusercontent.com" /></label><label>Google client secret<input type="password" value={clientSecret} onChange={(event) => setClientSecret(event.target.value.trim())} autoComplete="new-password" /></label><label>Rclone remote name<input value={remoteName} onChange={(event) => setRemoteName(event.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, "").slice(0, 32))} /></label></div><div className="setup-form-grid"><label>OAuth audience<select value={audience} onChange={(event) => setAudience(event.target.value as typeof audience)}><option value="external">External</option><option value="internal">Internal workspace</option></select></label><label>Publishing status<select value={publishingStatus} onChange={(event) => setPublishingStatus(event.target.value as typeof publishingStatus)}><option value="production">Production</option><option value="testing">Testing</option></select></label></div>{audience === "external" && publishingStatus === "testing" && <div className="setup-notice">Add this Google account as an OAuth test user. Testing-mode authorization may require reconnecting later.</div>}<button className="setup-primary setup-drive-connect" disabled={busy || driveMutationBlocked || !acceptedOrigin || !clientId || !clientSecret || remoteName.length < 2} onClick={() => void startOAuth()}>Continue with Google</button><small>Google consent opens in a separate window. The client secret is sent to this server and is never saved in browser storage.</small></div>}
            {driveJob && <div className="setup-drive-job" data-status={driveJob.status}><header><span className="setup-drive-status-dot" /><div><strong>{driveJob.status === "ready" ? "Google Drive verified" : "Google Drive connection"}</strong><small>{driveJob.progress}</small></div><button disabled={busy || driveMutationBlocked} onClick={cancelOAuth}>Start over</button></header>
              {driveJob.errorCode && <div className="setup-error" role="alert"><i>!</i>{driveJob.errorCode.replace(/_/g, " ")}</div>}
              {driveJob.status === "selecting_folder" && <div className="setup-folder-picker"><div className="setup-folder-toolbar"><button disabled={!folderPath || busy || folderRead === "loading"} onClick={() => void loadFolders(driveJob.id, folderPath.split("/").slice(0, -1).join("/"))}>← Up</button><code>Drive / {folderPath || ""}</code><button disabled={busy || folderRead === "loading"} onClick={() => void loadFolders(driveJob.id, folderPath)}>Refresh</button></div><div className="setup-folder-list" aria-busy={folderRead === "loading"}>{folderRead === "loading" || folderRead === "idle" ? <p role="status">Loading Drive folders…</p> : folderRead === "error" ? <div><p role="alert">{folderError}</p><button disabled={busy} onClick={() => void loadFolders(driveJob.id, folderPath)}>Retry folder listing</button></div> : folders.length ? folders.map((folder) => <article key={folder.path} data-selected={drivePath === folder.path}><button className="setup-folder-open" disabled={busy} title={folder.name} onClick={() => void loadFolders(driveJob.id, folder.path)}><i>▰</i><span>{folder.name}</span></button><button disabled={busy || driveMutationBlocked || folderRead !== "ready"} onClick={() => chooseFolder(folder.path)}>{drivePath === folder.path ? "Selected" : "Use folder"}</button></article>) : <p>No folders here. Create the StreamHome folder below.</p>}</div><div className="setup-folder-create"><input aria-label="New folder name" value={newFolder} onChange={(event) => setNewFolder(event.target.value.replace(/[\\/:]/g, ""))} placeholder="StreamHome" /><button disabled={!newFolder.trim() || busy || driveMutationBlocked || folderRead !== "ready"} onClick={createFolder}>Create folder</button></div>{folderNotice && <p className="setup-notice" role="status">{folderNotice}</p>}{drivePath && <div className="setup-folder-selection"><span>Selected folder</span><strong>Drive / {drivePath}</strong><button disabled={busy || driveMutationBlocked || folderRead !== "ready"} onClick={() => chooseFolder(drivePath)}>Confirm selection</button></div>}</div>}
              {(driveJob.status === "selecting_folder" && driveJob.selectedPath) && <div className="setup-drive-test"><label className="setup-check"><input type="checkbox" checked={healthConsent} onChange={(event) => setHealthConsent(event.target.checked)} /> Allow StreamHome to create, verify, and delete one small health-check file in this folder.</label><button className="setup-primary" disabled={!healthConsent || busy || driveMutationBlocked || folderRead === "loading"} onClick={testDrive}>Run read/write test</button></div>}
              {driveJob.status === "ready" && <div className="setup-drive-ready"><div><span>Folder</span><strong>Drive / {driveJob.selectedPath}</strong></div>{quota && <div><span>Drive usage</span><strong>{formatBytes(quota.used)} used · {formatBytes(quota.free)} free</strong></div>}<button className="setup-primary" disabled={driveActivated || busy || driveMutationBlocked} onClick={activate}>{driveActivated ? "Drive activated ✓" : "Activate Google Drive"}</button></div>}
            </div>}
          </div>}
          <div className="setup-footer-actions"><button onClick={() => goToStep(5)}>Back</button><button className="setup-primary" disabled={storage === "CLOUD" && !driveActivated} onClick={() => goToStep(7)}>Review setup</button></div></>}

        {step === 7 && <><h2 ref={headingRef} tabIndex={-1}>Ready to initialize</h2><p>{managedInstallation ? "Review the configuration. Finishing creates the account and writes server secrets." : "Review the configuration. Finishing creates the account, writes server secrets, and restarts StreamHome."}</p><dl className="setup-review"><div><dt>Administrator</dt><dd>{email}</dd></div><div><dt>Security</dt><dd>{totpEnabled ? "Password + TOTP" : "Password"}</dd></div><div><dt>TMDB</dt><dd>Validated</dd></div><div><dt>Public URL</dt><dd>{publicUrl}</dd></div><div><dt>Web</dt><dd>Port {webPort}</dd></div><div><dt>HEVC compression</dt><dd>{HEVC_LABELS[hevc]}</dd></div><div><dt>Storage</dt><dd>{storage === "LOCAL" ? `Local ${paths.media}` : `Google Drive / ${drivePath}`}</dd></div><div><dt>Automation</dt><dd>{backups ? "Backups on" : "Backups off"} · {updates ? "idle automatic updates" : "manual updates"}</dd></div></dl><div className="setup-footer-actions"><button disabled={completionLocked} onClick={() => goToStep(6)}>Back</button><button className="setup-primary" disabled={busy || completionLocked || (totpEnabled && !totpVerified)} onClick={finish}>Initialize StreamHome</button></div></>}

        {error && <div id="setup-account-error" className="setup-error" role="alert"><i>!</i>{error}</div>}
        {(busy || completionState === "pending") && <div className="setup-busy" role="status"><span className="setup-spinner" />Working…</div>}
      </div>
    </section>
  </main>;
}
