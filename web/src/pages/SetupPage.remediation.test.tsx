import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import * as api from "../api/setup";
import { SetupPage } from "./SetupPage";
import { SETUP_CHECKPOINT_KEY, writeSetupCheckpoint } from "./setupResume";
import { formatSetupSecrets } from "./setupValidation";
vi.mock("../api/setup", () => ({ activateDrive: vi.fn(), beginSetupTOTP: vi.fn(), cancelDriveJob: vi.fn(), cancelSetupTOTP: vi.fn(), completeSetup: vi.fn(), createDriveFolder: vi.fn(), getDriveJob: vi.fn(), getSetupReadiness: vi.fn(), getSetupStatus: vi.fn(), listDriveFolders: vi.fn(), selectDriveFolder: vi.fn(), startDriveOAuth: vi.fn(), testDriveFolder: vi.fn(), unlockSetup: vi.fn(), validateSetupTMDB: vi.fn(), verifySetupTOTP: vi.fn() }));
const status = { required: true, unlocked: true, webPort: 3000, serverVersion: "synthetic", mediaPath: "server/media", databasePath: "server/database.db", publicUrl: "https://watch.example.test" };
const job: api.DriveSetupJob = { id: "synthetic-job", status: "selecting_folder", remoteName: "streamhome-drive", selectedPath: "", progress: "Select a folder", audience: "external", publishingStatus: "production", expiresAt: 9999999999 };
const resultValue: api.SetupCompleteResponse = { complete: true, restartScheduled: true, webPort: 3000, recoveryCodes: ["synthetic-recovery"], ingestionToken: "synthetic-ingestion" };
const deferred = <T,>() => { let resolve!: (value: T) => void; let reject!: (reason: unknown) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function checkpoint(step = 2) { writeSetupCheckpoint({ version: 1, step, email: "admin@example.test", publicUrl: "https://watch.example.test", webPort: 3000, hevc: "auto", backups: false, updates: false, storage: "LOCAL", remoteName: "streamhome-drive", audience: "external", publishingStatus: "production", driveJobId: "", drivePath: "" }); }
async function account(email = "admin@example.test", password = "abcdef") { await screen.findByRole("heading", { name: "Create the administrator" }); fireEvent.change(screen.getByLabelText("Email address"), { target: { value: email } }); fireEvent.change(screen.getByLabelText("Password", { exact: true }), { target: { value: password } }); fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: password } }); fireEvent.submit(screen.getByLabelText("Email address").closest("form")!); }
async function toReview(totp = false) { checkpoint(); render(<SetupPage />); await account(); if (totp) { fireEvent.click(screen.getByRole("radio", { name: /Password \+ TOTP/ })); await screen.findByLabelText("Six-digit authenticator code"); fireEvent.change(screen.getByLabelText("Six-digit authenticator code"), { target: { value: "123456" } }); fireEvent.click(screen.getByRole("button", { name: "Verify code" })); await screen.findByRole("button", { name: "Verified ✓" }); } fireEvent.click(screen.getByRole("button", { name: "Continue" })); fireEvent.change(screen.getByLabelText("Read-access token"), { target: { value: "synthetic-tmdb" } }); fireEvent.click(screen.getByRole("button", { name: "Validate token" })); await screen.findByRole("button", { name: "TMDB connected ✓" }); fireEvent.click(screen.getByRole("button", { name: "Continue" })); fireEvent.click(screen.getByRole("button", { name: "Continue" })); fireEvent.click(screen.getByRole("button", { name: "Review setup" })); await screen.findByRole("heading", { name: "Ready to initialize" }); }
async function connectedDrive() {
  await toReview(); fireEvent.click(screen.getByRole("button", { name: "Back" })); fireEvent.click(screen.getByRole("radio", { name: /Google Drive/ }));
  fireEvent.change(screen.getByLabelText("Google client ID"), { target: { value: "synthetic-client" } }); fireEvent.change(screen.getByLabelText("Google client secret"), { target: { value: "synthetic-secret" } });
  const popup = { closed: false, location: { replace: vi.fn() }, close: vi.fn() };
  vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
  vi.mocked(api.startDriveOAuth).mockResolvedValue({ jobId: "synthetic-job", authorizationUrl: "https://synthetic.invalid/authorization", expiresAt: 9999999999 });
  fireEvent.click(screen.getByRole("button", { name: "Continue with Google" })); await screen.findByText(/No folders here/); return popup;
}
function oauthMessage(popup: unknown) {
  const event = new MessageEvent("message", { origin: window.location.origin, data: { type: "streamhome:drive-oauth-complete", jobId: "synthetic-job" } });
  Object.defineProperty(event, "source", { value: popup }); act(() => { window.dispatchEvent(event); });
}
beforeEach(() => { vi.resetAllMocks(); sessionStorage.clear(); window.history.replaceState({}, "", "/setup"); Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() }); vi.mocked(api.getSetupStatus).mockResolvedValue(status); vi.mocked(api.getSetupReadiness).mockResolvedValue({ ready: true, checks: [] }); vi.mocked(api.validateSetupTMDB).mockResolvedValue({ valid: true, validationToken: "synthetic-validation" }); vi.mocked(api.getDriveJob).mockResolvedValue(job); vi.mocked(api.listDriveFolders).mockResolvedValue({ path: "", folders: [] }); vi.mocked(api.beginSetupTOTP).mockImplementation(async () => ({ enrollmentId: "synthetic-enrollment", manualKey: "SYNTHETICKEY", qrImageUrl: "/synthetic-qr", expiresAt: Date.now() / 1000 + 600 })); vi.mocked(api.verifySetupTOTP).mockResolvedValue({ valid: true, enrollmentId: "synthetic-enrollment" }); vi.mocked(api.cancelSetupTOTP).mockResolvedValue(undefined); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

describe("mounted setup remediation", () => {
  it("status pending/error/retry never overwrites the saved checkpoint or silently unlocks", async () => {
    checkpoint(6); const saved = sessionStorage.getItem(SETUP_CHECKPOINT_KEY); const pending = deferred<api.SetupStatus>(); vi.mocked(api.getSetupStatus).mockReturnValueOnce(pending.promise);
    render(<SetupPage />); expect(screen.getByRole("heading", { name: "Checking setup state" })).toBeTruthy(); expect(screen.queryByLabelText("Bootstrap code")).toBeNull();
    await act(async () => pending.reject(new ApiError("Offline", 503, "unavailable"))); expect(screen.getByRole("button", { name: "Retry setup status" })).toBeTruthy(); expect(sessionStorage.getItem(SETUP_CHECKPOINT_KEY)).toBe(saved);
    vi.mocked(api.getSetupStatus).mockResolvedValueOnce({ ...status, unlocked: false }); fireEvent.click(screen.getByRole("button", { name: "Retry setup status" })); await screen.findByLabelText("Bootstrap code");
  });
  it.each(["a@", "not-an-email", "a b@example.test"])("Account form retains invalid %s and focuses the linked email field", async (email) => { checkpoint(); render(<SetupPage />); await account(email); expect(screen.getByRole("heading", { name: "Create the administrator" })).toBeTruthy(); const input = screen.getByLabelText("Email address"); expect(document.activeElement).toBe(input); expect(input.getAttribute("aria-describedby")).toBe("setup-account-error"); expect(screen.getByRole("alert").textContent).toContain("valid administrator email"); });
  it.each(["admin@internal", "person+tag@example.test", " admin@example.test "])("Account form accepts native-valid %s once and focuses the next heading", async (email) => { checkpoint(); render(<SetupPage />); await account(email); const heading = screen.getByRole("heading", { name: "Local account security" }); expect(document.activeElement).toBe(heading); expect(screen.getByRole("radio", { name: /Password only/ }).getAttribute("checked")).not.toBeNull(); expect(document.querySelector('[aria-current="step"]')?.textContent).toContain("Security"); fireEvent.click(screen.getByRole("button", { name: "Back" })); expect(screen.getByLabelText<HTMLInputElement>("Email address").value).toBe(email.trim()); });
  it("Account rejects the UTF-8 byte limit and mismatch on the relevant field", async () => { checkpoint(); render(<SetupPage />); await account("admin@example.test", "é".repeat(37)); expect(document.activeElement).toBe(screen.getByLabelText("Password", { exact: true })); fireEvent.change(screen.getByLabelText("Password", { exact: true }), { target: { value: "abcdef" } }); fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "different" } }); fireEvent.submit(screen.getByLabelText("Email address").closest("form")!); expect(document.activeElement).toBe(screen.getByLabelText("Confirm password")); });
  it("shows official token help without validating or advancing and preserves the gate", async () => { checkpoint(); render(<SetupPage />); await account(); fireEvent.click(screen.getByRole("button", { name: "Continue" })); expect(screen.getByRole<HTMLAnchorElement>("link", { name: "Read the official token guide" }).href).toBe("https://developer.themoviedb.org/docs/authentication-application"); expect((screen.getByRole("button", { name: "Continue" }) as HTMLButtonElement).disabled).toBe(true); expect(api.validateSetupTMDB).not.toHaveBeenCalled(); });
  it("rejects invalid public origin and fractional port without advertising a callback", async () => { await toReview(); fireEvent.click(screen.getByRole("button", { name: /Server https/ })); fireEvent.change(screen.getByLabelText("Public StreamHome URL"), { target: { value: "http://public.example.test/path" } }); fireEvent.click(screen.getByRole("button", { name: "Continue" })); expect(screen.getByRole("heading", { name: "Server behavior" })).toBeTruthy(); fireEvent.change(screen.getByLabelText("Public StreamHome URL"), { target: { value: "https://WATCH.example:443/" } }); fireEvent.change(screen.getByLabelText("Web port"), { target: { value: "3000.5" } }); fireEvent.click(screen.getByRole("button", { name: "Continue" })); expect(screen.getByRole("heading", { name: "Server behavior" })).toBeTruthy(); fireEvent.change(screen.getByLabelText("Web port"), { target: { value: "3000" } }); fireEvent.click(screen.getByRole("button", { name: "Continue" })); fireEvent.click(screen.getByRole("radio", { name: /Google Drive/ })); expect(screen.getByText("https://WATCH.example:443/api/setup/rclone/drive/callback")).toBeTruthy(); });
  it.each([["auto", "Automatic"], ["on", "Always"], ["off", "Never"]])("reviews and submits the chosen HEVC mode %s", async (mode, label) => { await toReview(); fireEvent.click(screen.getByRole("button", { name: /Server https/ })); fireEvent.change(screen.getByLabelText("HEVC compression"), { target: { value: mode } }); fireEvent.click(screen.getByRole("button", { name: "Continue" })); fireEvent.click(screen.getByRole("button", { name: "Review setup" })); expect(screen.getByText(label)).toBeTruthy(); vi.mocked(api.completeSetup).mockResolvedValue(resultValue); fireEvent.click(screen.getByRole("button", { name: "Initialize StreamHome" })); await screen.findByRole("heading", { name: "StreamHome is restarting" }); expect(api.completeSetup).toHaveBeenCalledWith(expect.objectContaining({ hevc_compression_mode: mode })); });
  it("retains the valid show-once completion200 after expiry and guards duplicates/back/edit", async () => { await toReview(true); const pending = deferred<api.SetupCompleteResponse>(); vi.mocked(api.completeSetup).mockReturnValueOnce(pending.promise); fireEvent.click(screen.getByRole("button", { name: "Initialize StreamHome" })); fireEvent.click(screen.getByRole("button", { name: "Initialize StreamHome" })); fireEvent.click(screen.getByRole("button", { name: "Back" })); expect(screen.getByRole("heading", { name: "Ready to initialize" })).toBeTruthy(); vi.spyOn(Date, "now").mockReturnValue(Date.now() + 700000); await act(async () => pending.resolve(resultValue)); const heading = screen.getByRole("heading", { name: "StreamHome is restarting" }); expect(document.activeElement).toBe(heading); expect(heading.tabIndex).toBe(-1); expect(HTMLElement.prototype.scrollIntoView).toHaveBeenLastCalledWith({ block: "start", behavior: "instant" }); expect(document.querySelector("pre")?.textContent).toBe(formatSetupSecrets(resultValue)); expect(api.completeSetup).toHaveBeenCalledTimes(1); expect(api.cancelSetupTOTP).not.toHaveBeenCalled(); expect((screen.getByRole("button", { name: "Open StreamHome" }) as HTMLButtonElement).disabled).toBe(true); });
  it("structured enrollment rejection returns focused Security and explicit regeneration", async () => { await toReview(true); vi.mocked(api.completeSetup).mockRejectedValueOnce(new ApiError("Expired", 410, "totp_enrollment_expired")); fireEvent.click(screen.getByRole("button", { name: "Initialize StreamHome" })); const heading = await screen.findByRole("heading", { name: "Local account security" }); await waitFor(() => expect(document.activeElement).toBe(heading)); expect(screen.getByRole("button", { name: "Generate new enrollment" })).toBeTruthy(); expect(screen.queryByLabelText("Six-digit authenticator code")).toBeNull(); });
  it("unknown completion offers read-only reconciliation and never retries or fabricates secrets", async () => { await toReview(); vi.mocked(api.completeSetup).mockRejectedValueOnce(new ApiError("Lost", 0, "request_timeout")); fireEvent.click(screen.getByRole("button", { name: "Initialize StreamHome" })); const unknownHeading = await screen.findByRole("heading", { name: "Initialization outcome is unknown" }); expect(document.activeElement).toBe(unknownHeading); const check = screen.getByRole("button", { name: "Check setup status" }); check.focus(); fireEvent.click(check); await screen.findByText(/does not prove the earlier operation stopped/); expect(document.activeElement).toBe(check); expect(api.completeSetup).toHaveBeenCalledTimes(1); vi.mocked(api.getSetupStatus).mockResolvedValueOnce({ ...status, required: false }); fireEvent.click(screen.getByRole("button", { name: "Check setup status" })); const completedHeading = await screen.findByRole("heading", { name: "Setup completed" }); expect(document.activeElement).toBe(completedHeading); expect(HTMLElement.prototype.scrollIntoView).toHaveBeenLastCalledWith({ block: "start", behavior: "instant" }); expect(screen.getByText(/one-time token and recovery values were not retrieved/)).toBeTruthy(); expect(document.querySelector("pre")).toBeNull(); expect(api.beginSetupTOTP).not.toHaveBeenCalled(); });
  it("folder loading, error, retry and actual empty are distinct and do not loop", async () => { window.history.replaceState({}, "", "/setup?driveJob=synthetic-job"); const pending = deferred<api.DriveFolderList>(); vi.mocked(api.listDriveFolders).mockReturnValueOnce(pending.promise); render(<SetupPage />); await screen.findByText("Loading Drive folders…"); expect(screen.queryByText(/No folders here/)).toBeNull(); expect((screen.getByRole("button", { name: "Create folder" }) as HTMLButtonElement).disabled).toBe(true); await act(async () => pending.reject(new ApiError("List unavailable", 503, "unavailable"))); fireEvent.click(screen.getByRole("button", { name: "Retry folder listing" })); await screen.findByText(/No folders here/); expect(api.listDriveFolders).toHaveBeenCalledTimes(2); });
  it("acknowledged folder creation preserves its path until failed listing retries recover", async () => {
    window.history.replaceState({}, "", "/setup?driveJob=synthetic-job");
    vi.mocked(api.createDriveFolder).mockResolvedValueOnce({ path: "StreamHome" });
    render(<SetupPage />); await screen.findByText(/No folders here/);
    vi.mocked(api.listDriveFolders).mockRejectedValueOnce(new ApiError("Refresh unavailable", 503, "unavailable"));
    fireEvent.click(screen.getByRole("button", { name: "Create folder" }));
    fireEvent.click(screen.getByRole("button", { name: "Create folder" }));
    await screen.findByText(/Folder created. The list refresh failed/);
    expect(screen.getByText("Drive / StreamHome")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Confirm selection" }) as HTMLButtonElement).disabled).toBe(true);

    vi.mocked(api.listDriveFolders).mockRejectedValueOnce(new ApiError("Retry unavailable", 503, "unavailable"));
    fireEvent.click(screen.getByRole("button", { name: "Retry folder listing" }));
    await screen.findByText("Retry unavailable");
    expect(screen.getByText(/Folder created. The list refresh failed/)).toBeTruthy();
    expect(screen.getByText("Drive / StreamHome")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Confirm selection" }) as HTMLButtonElement).disabled).toBe(true);

    vi.mocked(api.listDriveFolders).mockResolvedValueOnce({ path: "", folders: [] });
    fireEvent.click(screen.getByRole("button", { name: "Retry folder listing" }));
    await screen.findByText(/No folders here/);
    expect(screen.queryByText(/Folder created. The list refresh failed/)).toBeNull();
    expect(screen.getByText("Drive / StreamHome")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Confirm selection" }) as HTMLButtonElement).disabled).toBe(false);
    expect(api.listDriveFolders).toHaveBeenCalledTimes(4);
    expect(api.createDriveFolder).toHaveBeenCalledTimes(1);
    expect(api.selectDriveFolder).not.toHaveBeenCalled();
    expect(api.testDriveFolder).not.toHaveBeenCalled();
  });
  it("start-over retires a pending folder read so its late result cannot repopulate the old job", async () => { window.history.replaceState({}, "", "/setup?driveJob=synthetic-job"); const pending = deferred<api.DriveFolderList>(); vi.mocked(api.listDriveFolders).mockReturnValueOnce(pending.promise); vi.mocked(api.cancelDriveJob).mockResolvedValueOnce(undefined); render(<SetupPage />); await screen.findByText("Loading Drive folders…"); fireEvent.click(screen.getByRole("button", { name: "Start over" })); await screen.findByRole("button", { name: "Continue with Google" }); await act(async () => pending.resolve({ path: "old", folders: [{ path: "old/folder", name: "Stale folder" }] })); expect(screen.queryByText("Stale folder")).toBeNull(); });
  it("screen, clipboard and download use the same labelled synthetic values; failed copy preserves acknowledgement", async () => {
    await toReview(); vi.mocked(api.completeSetup).mockResolvedValueOnce(resultValue);
    fireEvent.click(screen.getByRole("button", { name: "Initialize StreamHome" })); await screen.findByRole("heading", { name: "StreamHome is restarting" });
    const expected = document.querySelector("pre")!.textContent;
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    fireEvent.click(screen.getByRole("button", { name: "Copy" })); await screen.findByText("Secrets copied.");
    expect(writeText).toHaveBeenCalledWith(expected);
    const createObjectURL = vi.fn().mockReturnValue("blob:synthetic");
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const NativeBlob = globalThis.Blob; const blob = vi.fn(function(this: Blob, parts: BlobPart[], options?: BlobPropertyBag) { return new NativeBlob(parts, options); });
    vi.stubGlobal("Blob", blob); fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(blob).toHaveBeenCalledWith([expected], { type: "text/plain" }); vi.unstubAllGlobals();
    writeText.mockRejectedValueOnce(new Error("denied")); Object.defineProperty(document, "execCommand", { configurable: true, value: vi.fn().mockReturnValue(false) });
    fireEvent.click(screen.getByRole("button", { name: "Copy" })); await screen.findByText(/Copy was blocked/);
    expect((screen.getByRole("button", { name: "Open StreamHome" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("checkbox")); expect((screen.getByRole("button", { name: "Open StreamHome" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("a resumed Drive return cannot initialize without re-entering the account and service drafts", async () => {
    window.history.replaceState({}, "", "/setup?driveJob=synthetic-job");
    vi.mocked(api.getDriveJob).mockResolvedValue({ ...job, status: "ready", selectedPath: "StreamHome" });
    vi.mocked(api.activateDrive).mockResolvedValue({ valid: true, remotePath: "synthetic:StreamHome", job: { ...job, status: "ready", selectedPath: "StreamHome" } });
    render(<SetupPage />); fireEvent.click(await screen.findByRole("button", { name: "Activate Google Drive" }));
    await screen.findByRole("button", { name: "Drive activated ✓" }); fireEvent.click(screen.getByRole("button", { name: "Review setup" }));
    fireEvent.click(screen.getByRole("button", { name: "Initialize StreamHome" }));
    await screen.findByRole("heading", { name: "Create the administrator" }); expect(api.completeSetup).not.toHaveBeenCalled();
  });

  it("a committed create with a lost response blocks duplicate creates until a read confirms the requested path", async () => {
    window.history.replaceState({}, "", "/setup?driveJob=synthetic-job"); render(<SetupPage />); await screen.findByText(/No folders here/);
    vi.mocked(api.createDriveFolder).mockImplementationOnce(async () => { vi.mocked(api.listDriveFolders).mockResolvedValue({ path: "", folders: [{ path: "StreamHome", name: "StreamHome" }] }); throw new ApiError("Response body stalled after commit", 0, "request_timeout"); });
    fireEvent.click(screen.getByRole("button", { name: "Create folder" })); await screen.findByText("Google Drive operation outcome is unknown.");
    fireEvent.click(screen.getByRole("button", { name: "Create folder" })); expect(api.createDriveFolder).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Check Drive operation status" })); await screen.findByText(/requested folder exists/);
    expect(screen.queryByText("Google Drive operation outcome is unknown.")).toBeNull(); expect(api.createDriveFolder).toHaveBeenCalledTimes(1);
    const reads = vi.mocked(api.listDriveFolders).mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(api.listDriveFolders).toHaveBeenCalledTimes(reads + 1));
    await waitFor(() => expect(screen.queryByText("Loading Drive folders…")).toBeNull());
    expect(screen.getByText(/requested folder exists/)).toBeTruthy();
    expect(screen.getByText("Drive / StreamHome")).toBeTruthy();
    expect(api.createDriveFolder).toHaveBeenCalledTimes(1);
    expect(api.selectDriveFolder).not.toHaveBeenCalled();
  });
  it("a successful folder refresh preserves an unconfirmed unknown-create notice and mutation gate", async () => {
    window.history.replaceState({}, "", "/setup?driveJob=synthetic-job");
    vi.mocked(api.createDriveFolder).mockRejectedValueOnce(new ApiError("Response lost", 0, "request_timeout"));
    render(<SetupPage />); await screen.findByText(/No folders here/);
    fireEvent.click(screen.getByRole("button", { name: "Create folder" }));
    await screen.findByText("Google Drive operation outcome is unknown.");
    fireEvent.click(screen.getByRole("button", { name: "Check Drive operation status" }));
    await screen.findAllByText(/The requested outcome is not confirmed/);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(api.listDriveFolders).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(screen.queryByText("Loading Drive folders…")).toBeNull());
    expect(screen.getByText("Google Drive operation outcome is unknown.")).toBeTruthy();
    expect(screen.getAllByText(/The requested outcome is not confirmed/).length).toBeGreaterThan(0);
    expect((screen.getByRole("button", { name: "Create folder" }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.createDriveFolder).toHaveBeenCalledTimes(1);
    expect(api.selectDriveFolder).not.toHaveBeenCalled();
    expect(api.testDriveFolder).not.toHaveBeenCalled();
  });
  it("a committed folder selection with a lost response is reconciled by its server job without retry", async () => {
    window.history.replaceState({}, "", "/setup?driveJob=synthetic-job"); vi.mocked(api.listDriveFolders).mockResolvedValue({ path: "", folders: [{ name: "Movies", path: "Movies" }] });
    vi.mocked(api.selectDriveFolder).mockImplementationOnce(async () => { vi.mocked(api.getDriveJob).mockResolvedValue({ ...job, selectedPath: "Movies" }); throw new ApiError("Response body stalled after commit", 0, "request_timeout"); });
    render(<SetupPage />); fireEvent.click(await screen.findByRole("button", { name: "Use folder" })); await screen.findByText("Google Drive operation outcome is unknown.");
    fireEvent.click(screen.getByRole("button", { name: "Check Drive operation status" })); await waitFor(() => expect(screen.queryByText("Google Drive operation outcome is unknown.")).toBeNull());
    expect(screen.getByText("Drive / Movies")).toBeTruthy(); expect(api.selectDriveFolder).toHaveBeenCalledTimes(1);
  });
  it("activation uncertainty cannot be cleared merely because the job was already ready", async () => {
    window.history.replaceState({}, "", "/setup?driveJob=synthetic-job"); vi.mocked(api.getDriveJob).mockResolvedValue({ ...job, status: "ready", selectedPath: "StreamHome" });
    vi.mocked(api.activateDrive).mockRejectedValueOnce(new ApiError("Response lost", 0, "request_timeout")); render(<SetupPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Activate Google Drive" })); await screen.findByText("Google Drive operation outcome is unknown.");
    fireEvent.click(screen.getByRole("button", { name: "Check Drive operation status" })); await screen.findByText(/job read cannot prove activation/);
    expect((screen.getByRole("button", { name: "Activate Google Drive" }) as HTMLButtonElement).disabled).toBe(true); expect(api.activateDrive).toHaveBeenCalledTimes(1);
  });
  it("an acknowledged start with failed job read retains its ID and offers only a read retry", async () => {
    window.history.replaceState({}, "", "/setup?driveJob=synthetic-job"); vi.mocked(api.getDriveJob).mockRejectedValueOnce(new ApiError("Read failed", 503, "unavailable")); render(<SetupPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Retry Drive job status" })); await screen.findByText(/No folders here/);
    expect(api.startDriveOAuth).not.toHaveBeenCalled(); expect(api.getDriveJob).toHaveBeenLastCalledWith("synthetic-job", expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  it("a no-ID unknown OAuth start never cancels an invented job or repeats without an explicit new attempt", async () => {
    await toReview(); fireEvent.click(screen.getByRole("button", { name: "Back" })); fireEvent.click(screen.getByRole("radio", { name: /Google Drive/ }));
    fireEvent.change(screen.getByLabelText("Google client ID"), { target: { value: "synthetic-client" } }); fireEvent.change(screen.getByLabelText("Google client secret"), { target: { value: "synthetic-secret" } });
    vi.spyOn(window, "open").mockImplementation(() => ({ closed: false, location: { replace: vi.fn() }, close: vi.fn() }) as unknown as Window);
    vi.mocked(api.startDriveOAuth).mockRejectedValueOnce(new ApiError("Start response lost", 0, "request_timeout"));
    fireEvent.click(screen.getByRole("button", { name: "Continue with Google" })); await screen.findByText("Google Drive operation outcome is unknown.");
    expect(api.startDriveOAuth).toHaveBeenCalledTimes(1); expect(api.cancelDriveJob).not.toHaveBeenCalled(); expect((screen.getByRole("button", { name: "Continue with Google" }) as HTMLButtonElement).disabled).toBe(true);
    vi.mocked(api.startDriveOAuth).mockResolvedValueOnce({ jobId: "synthetic-job", authorizationUrl: "https://synthetic.invalid/authorization", expiresAt: 9999999999 });
    fireEvent.click(screen.getByRole("button", { name: "Start new connection" })); await screen.findByText(/No folders here/);
    expect(api.startDriveOAuth).toHaveBeenCalledTimes(2); expect(api.cancelDriveJob).not.toHaveBeenCalled();
  });

  it("polls cannot dispatch inside a pending selection and cannot overwrite its acknowledgement", async () => {
    vi.useFakeTimers(); window.history.replaceState({}, "", "/setup?driveJob=synthetic-job");
    vi.mocked(api.listDriveFolders).mockResolvedValue({ path: "", folders: [{ name: "B", path: "B" }] });
    const selection = deferred<api.DriveSetupJob>(); const oldPoll = deferred<api.DriveSetupJob>();
    vi.mocked(api.selectDriveFolder).mockReturnValueOnce(selection.promise); vi.mocked(api.getDriveJob).mockResolvedValueOnce(job).mockReturnValue(oldPoll.promise);
    await act(async () => { render(<SetupPage />); }); fireEvent.click(screen.getByRole("button", { name: "Use folder" }));
    await act(async () => { vi.advanceTimersByTime(1500); }); expect(api.getDriveJob).toHaveBeenCalledTimes(1);
    await act(async () => selection.resolve({ ...job, selectedPath: "B" })); await act(async () => oldPoll.resolve({ ...job, selectedPath: "A" }));
    expect(screen.getByText("Drive / B")).toBeTruthy(); expect(screen.queryByText("Drive / A")).toBeNull();
  });
  it("a poll already pending before selection cannot overwrite the acknowledged folder", async () => {
    vi.useFakeTimers(); window.history.replaceState({}, "", "/setup?driveJob=synthetic-job");
    vi.mocked(api.listDriveFolders).mockResolvedValue({ path: "", folders: [{ name: "B", path: "B" }] });
    const oldPoll = deferred<api.DriveSetupJob>(); vi.mocked(api.getDriveJob).mockResolvedValueOnce(job).mockReturnValueOnce(oldPoll.promise);
    vi.mocked(api.selectDriveFolder).mockResolvedValueOnce({ ...job, selectedPath: "B" });
    await act(async () => { render(<SetupPage />); }); await act(async () => { vi.advanceTimersByTime(1500); }); expect(api.getDriveJob).toHaveBeenCalledTimes(2);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Use folder" })); });
    await act(async () => oldPoll.resolve({ ...job, selectedPath: "A" })); expect(screen.getByText("Drive / B")).toBeTruthy();
  });
  it("polls cannot dispatch inside a pending read-write test or regress its ready acknowledgement", async () => {
    vi.useFakeTimers(); window.history.replaceState({}, "", "/setup?driveJob=synthetic-job");
    vi.mocked(api.getDriveJob).mockResolvedValue({ ...job, selectedPath: "Movies" });
    const testing = deferred<api.DriveTestResult>(); vi.mocked(api.testDriveFolder).mockReturnValueOnce(testing.promise);
    await act(async () => { render(<SetupPage />); }); fireEvent.click(screen.getByRole("checkbox", { name: /Allow StreamHome/ })); fireEvent.click(screen.getByRole("button", { name: "Run read/write test" }));
    await act(async () => { vi.advanceTimersByTime(1500); }); expect(api.getDriveJob).toHaveBeenCalledTimes(1);
    await act(async () => testing.resolve({ valid: true, remotePath: "synthetic:Movies", job: { ...job, selectedPath: "Movies", status: "ready" } }));
    expect(screen.getByRole("button", { name: "Activate Google Drive" })).toBeTruthy(); expect(screen.queryByRole("button", { name: "Run read/write test" })).toBeNull();
  });
  it("a stalled callback read is aborted by Start over and cannot resurrect an old job after replacement", async () => {
    const popup = await connectedDrive(); const callback = deferred<api.DriveSetupJob>(); vi.mocked(api.getDriveJob).mockReturnValueOnce(callback.promise);
    oauthMessage(popup); const signal = vi.mocked(api.getDriveJob).mock.calls[vi.mocked(api.getDriveJob).mock.calls.length - 1]?.[1]?.signal;
    fireEvent.click(screen.getByRole("button", { name: "Start over" })); await screen.findByRole("button", { name: "Continue with Google" }); expect(signal?.aborted).toBe(true);
    vi.mocked(api.startDriveOAuth).mockResolvedValueOnce({ jobId: "replacement-job", authorizationUrl: "https://synthetic.invalid/authorization", expiresAt: 9999999999 });
    vi.mocked(api.getDriveJob).mockResolvedValue({ ...job, id: "replacement-job" }); fireEvent.click(screen.getByRole("button", { name: "Continue with Google" })); await screen.findByText(/No folders here/);
    await act(async () => callback.resolve({ ...job, selectedPath: "Stale folder" })); expect(screen.queryByText("Drive / Stale folder")).toBeNull(); expect(api.getDriveJob).toHaveBeenLastCalledWith("replacement-job", expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });
  it("unmount aborts callback ownership and ignores its late completion", async () => {
    const popup = await connectedDrive(); const callback = deferred<api.DriveSetupJob>(); vi.mocked(api.getDriveJob).mockReturnValueOnce(callback.promise);
    oauthMessage(popup); const calls = vi.mocked(api.getDriveJob).mock.calls; const signal = calls[calls.length - 1][1]?.signal;
    cleanup(); expect(signal?.aborted).toBe(true); await act(async () => callback.resolve({ ...job, selectedPath: "Stale folder" })); expect(screen.queryByText("Drive / Stale folder")).toBeNull();
  });
  it("completion freezes callback navigation and prevents an additional job read", async () => {
    const popup = await connectedDrive(); fireEvent.click(screen.getByRole("radio", { name: /Local storage/ })); fireEvent.click(screen.getByRole("button", { name: "Review setup" }));
    const completion = deferred<api.SetupCompleteResponse>(); vi.mocked(api.completeSetup).mockReturnValueOnce(completion.promise); fireEvent.click(screen.getByRole("button", { name: "Initialize StreamHome" }));
    const calls = vi.mocked(api.getDriveJob).mock.calls.length; oauthMessage(popup); expect(api.getDriveJob).toHaveBeenCalledTimes(calls); expect(screen.getByRole("heading", { name: "Ready to initialize" })).toBeTruthy();
    await act(async () => completion.resolve(resultValue));
  });

  it("same-task external Refresh is blocked during create while its acknowledged internal refresh remains allowed", async () => {
    window.history.replaceState({}, "", "/setup?driveJob=synthetic-job"); const creation = deferred<{ path: string }>(); vi.mocked(api.createDriveFolder).mockReturnValueOnce(creation.promise);
    render(<SetupPage />); await screen.findByText(/No folders here/); const create = screen.getByRole("button", { name: "Create folder" }); const refresh = screen.getByRole("button", { name: "Refresh" });
    act(() => { fireEvent.click(create); fireEvent.click(refresh); }); expect(api.createDriveFolder).toHaveBeenCalledTimes(1); expect(api.listDriveFolders).toHaveBeenCalledTimes(1);
    await act(async () => creation.resolve({ path: "StreamHome" })); expect(api.listDriveFolders).toHaveBeenCalledTimes(2); expect(screen.getByText("Drive / StreamHome")).toBeTruthy();
  });


  it.each(["resolve", "reject"] as const)("retires an initial OAuth job read after a newer callback and selection (%s)", async (outcome) => {
    await toReview(); fireEvent.click(screen.getByRole("button", { name: "Back" })); fireEvent.click(screen.getByRole("radio", { name: /Google Drive/ }));
    fireEvent.change(screen.getByLabelText("Google client ID"), { target: { value: "synthetic-client" } }); fireEvent.change(screen.getByLabelText("Google client secret"), { target: { value: "synthetic-secret" } });
    const popup = { closed: false, location: { replace: vi.fn() }, close: vi.fn() };
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    vi.mocked(api.startDriveOAuth).mockResolvedValueOnce({ jobId: job.id, authorizationUrl: "https://synthetic.invalid/authorization", expiresAt: job.expiresAt });
    const initial = deferred<api.DriveSetupJob>(); vi.mocked(api.getDriveJob).mockReturnValueOnce(initial.promise).mockResolvedValueOnce(job);
    vi.mocked(api.listDriveFolders).mockResolvedValue({ path: "", folders: [{ name: "Current folder", path: "Current folder" }] });
    vi.mocked(api.selectDriveFolder).mockResolvedValueOnce({ ...job, selectedPath: "Current folder", progress: "Current selection acknowledged" });
    fireEvent.click(screen.getByRole("button", { name: "Continue with Google" }));
    await waitFor(() => expect(api.getDriveJob).toHaveBeenCalledTimes(1));
    oauthMessage(popup); await screen.findByRole("button", { name: "Use folder" });
    fireEvent.click(screen.getByRole("button", { name: "Use folder" })); await screen.findByText("Current selection acknowledged");
    await act(async () => { if (outcome === "resolve") initial.resolve({ ...job, status: "authorizing", selectedPath: "Old folder", progress: "Stale authorizing state" }); else initial.reject(new Error("Stale initial read failure")); });
    expect(document.querySelector(".setup-drive-job")?.getAttribute("data-status")).toBe("selecting_folder");
    expect(screen.getByText("Drive / Current folder")).toBeTruthy();
    expect(screen.queryByText("Drive / Old folder")).toBeNull(); expect(screen.queryByText("Stale authorizing state")).toBeNull(); expect(screen.queryByText("Stale initial read failure")).toBeNull();
    expect(api.selectDriveFolder).toHaveBeenCalledTimes(1); expect(api.getDriveJob).toHaveBeenCalledTimes(2);
  });

  it.each(["initial", "callback"] as const)("keeps the sole %s Drive read live during unrelated token validation", async (reader) => {
    await toReview(); fireEvent.click(screen.getByRole("button", { name: "Back" })); fireEvent.click(screen.getByRole("radio", { name: /Google Drive/ }));
    fireEvent.change(screen.getByLabelText("Google client ID"), { target: { value: "synthetic-client" } }); fireEvent.change(screen.getByLabelText("Google client secret"), { target: { value: "synthetic-secret" } });
    const popup = { closed: false, location: { replace: vi.fn() }, close: vi.fn() }; vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    vi.mocked(api.startDriveOAuth).mockResolvedValueOnce({ jobId: job.id, authorizationUrl: "https://synthetic.invalid/authorization", expiresAt: job.expiresAt });
    const initial = deferred<api.DriveSetupJob>(); const callback = deferred<api.DriveSetupJob>();
    vi.mocked(api.getDriveJob).mockReturnValueOnce(initial.promise).mockReturnValueOnce(callback.promise);
    fireEvent.click(screen.getByRole("button", { name: "Continue with Google" })); await waitFor(() => expect(api.getDriveJob).toHaveBeenCalledTimes(1));
    if (reader === "callback") { oauthMessage(popup); await waitFor(() => expect(api.getDriveJob).toHaveBeenCalledTimes(2)); }
    fireEvent.click(screen.getByRole("button", { name: /TMDB Connected/ }));
    fireEvent.change(screen.getByLabelText("Read-access token"), { target: { value: "replacement-synthetic-token" } });
    const validation = deferred<{ valid: true; validationToken: string }>(); vi.mocked(api.validateSetupTMDB).mockReturnValueOnce(validation.promise);
    fireEvent.click(screen.getByRole("button", { name: "Validate token" }));
    await act(async () => (reader === "callback" ? callback : initial).resolve({ ...job, progress: "Drive read retained during token validation" }));
    await act(async () => validation.resolve({ valid: true, validationToken: "replacement-synthetic-validation" }));
    fireEvent.click(screen.getByRole("button", { name: "Continue" })); fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await screen.findByText("Drive read retained during token validation"); await screen.findByText(/No folders here/);
    expect(screen.queryByText("Loading Google Drive connection…")).toBeNull();
    if (reader === "callback") await act(async () => initial.resolve({ ...job, status: "authorizing", progress: "Stale initial state" }));
    expect(screen.queryByText("Stale initial state")).toBeNull(); expect(api.startDriveOAuth).toHaveBeenCalledTimes(1);
  });

  it("ignores OAuth callback reads dispatched during an owned Drive mutation", async () => {
    const popup = await connectedDrive(); const selecting = deferred<api.DriveSetupJob>();
    vi.mocked(api.listDriveFolders).mockResolvedValueOnce({ path: "", folders: [{ name: "Media", path: "Media" }] });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" })); await screen.findByRole("button", { name: "Use folder" });
    vi.mocked(api.selectDriveFolder).mockReturnValueOnce(selecting.promise);
    fireEvent.click(screen.getByRole("button", { name: "Use folder" })); const reads = vi.mocked(api.getDriveJob).mock.calls.length;
    oauthMessage(popup); expect(api.getDriveJob).toHaveBeenCalledTimes(reads);
    await act(async () => selecting.resolve({ ...job, selectedPath: "Media", progress: "Folder selected once" }));
    expect(screen.getByText("Drive / Media")).toBeTruthy(); expect(api.selectDriveFolder).toHaveBeenCalledTimes(1);
  });
});
