import React, { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DriveSetupCard } from "./DriveSetupCard";
import { StoragePanel } from "./StoragePanel";
import type { AdminDriveJob, AdminDriveSetup } from "../../../api/adminDrive";

vi.mock("../SudoModal", () => ({ SudoModal: ({ isOpen, onSuccess, onCancel }: { isOpen: boolean; onSuccess: () => void; onCancel: () => void }) => isOpen ? <div role="dialog" aria-label="Confirm your identity"><button onClick={onSuccess}>Authorize Drive</button><button onClick={onCancel}>Close authorization</button></div> : null }));
const root = "/api/system/drive";
const sampleJob = (status: AdminDriveJob["status"] = "selecting_folder"): AdminDriveJob => ({ id: "job-1", status, remoteName: "streamhome-drive", pendingOperation: null, generation: 1, selectedPath: "Movies", progress: "Authorization complete", audience: "external", publishingStatus: "testing", expiresAt: Date.now() / 1000 + 600, libraryState: "empty" });
const setupFor = (job: AdminDriveJob | null, configured = false): AdminDriveSetup => ({ configured, publicUrl: "https://fixture.invalid", callbackUrl: "https://fixture.invalid/api/setup/rclone/drive/callback", guideUrl: "https://console.cloud.google.com/apis/credentials", job });
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
let current: AdminDriveJob | null;
let configured: boolean;
let requests: Array<{ path: string; method: string; body?: Record<string, unknown> }>;
let override: ((path: string, method: string, body?: Record<string, unknown>) => Promise<Response> | Response | undefined) | undefined;
const refresh = vi.fn();
const closed = vi.fn();
let popup: { location: { href: string }; close: typeof closed; closed: boolean };
let fetcher: ReturnType<typeof vi.fn>;
const settings = { storageEngine: "LOCAL", hevcCompressionMode: "auto", driveConfigured: false, driveReachable: false, rcloneRemotePath: "" };

beforeEach(() => {
  current = null; configured = false; requests = []; override = undefined; refresh.mockReset(); closed.mockReset();
  popup = { location: { href: "about:blank" }, close: closed, closed: false };
  vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
  fetcher = vi.fn(async (input: string, init: RequestInit = {}) => {
    const method = init.method || "GET"; const body = init.body ? JSON.parse(String(init.body)) as Record<string, unknown> : undefined;
    requests.push({ path: input, method, body });
    const replaced = override?.(input, method, body); if (replaced) return replaced;
    if (input === "/api/admin/storage/status") return json({ policy: { revision: 0, local_budget_bytes: null, cloud_budget_bytes: null, offload_enabled: false, prefetch_next_episode: false, hevc_compression_mode: "auto", disk_reserve_bytes: 0 }, local: { written_bytes: null, reserved_bytes: null, protected_bytes: null, free_bytes: null, disk_reserve_bytes: 0, categories: [], measured_at: null, stale: false, error_code: null }, cloud: { configured, reachable: null, health_state: "unknown", folder_name: null, used_bytes: null, account_total_bytes: null, account_free_bytes: null, measured_at: null, stale: false, error_code: null }, offload: { state: "paused", reason: null }, capabilities: { offload: true, keep_local: true, cleanup: true }, operation: null });
    if (input === "/api/admin/storage/retention") return json({ items: [], next_cursor: null });
    if (input === "/api/system/settings") return json({ ...settings, driveConfigured: configured });
    if (input === `${root}/setup`) return json(setupFor(current, configured));
    if (input === `${root}/jobs` && method === "POST") { current = sampleJob(); return json({ jobId: current.id, authorizationUrl: "https://accounts.google.com/o/oauth2/v2/auth?state=synthetic", expiresAt: current.expiresAt }); }
    if (input === `${root}/jobs/job-1` && method === "GET") return json(current);
    if (input === `${root}/jobs/job-1` && method === "DELETE") { current = { ...current!, status: "cancelled", pendingOperation: null }; return json(current); }
    if (input.includes("/folders?")) return json({ path: decodeURIComponent(input.split("path=")[1]), folders: [{ name: "Movies", path: "Movies" }] });
    if (input.endsWith("/folders") && method === "POST") return json({ path: body?.path });
    if (input.endsWith("/select-folder")) { current = { ...current!, selectedPath: String(body?.path), status: "selecting_folder" }; return json(current); }
    if (input.endsWith("/test")) { current = { ...current!, status: "ready" }; return json({ valid: true, remotePath: "streamhome-drive:Movies", job: current }); }
    if (input.endsWith("/save")) { configured = true; current = { ...current!, status: "saved" }; return json({ valid: true, remotePath: "streamhome-drive:Movies", job: current }); }
    throw new Error(`Unexpected synthetic request: ${method} ${input}`);
  });
  vi.stubGlobal("fetch", fetcher);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
async function fillCredentials() {
  await waitFor(() => expect(screen.queryByText("Checking Drive setup…")).toBeNull());
  const reveal = screen.queryByRole("button", { name: "Connect Google Drive" });
  if (reveal) { await waitFor(() => expect((reveal as HTMLButtonElement).disabled).toBe(false)); fireEvent.click(reveal); }
  await screen.findByRole("button", { name: "Authorize Google Drive" });
  await waitFor(() => expect(screen.queryByText("Checking Drive setup…")).toBeNull());
  fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "synthetic-client" } });
  fireEvent.change(screen.getByLabelText("Client secret"), { target: { value: "synthetic-secret" } });
}
async function connect() { await fillCredentials(); fireEvent.click(screen.getByRole("button", { name: "Authorize Google Drive" })); await screen.findByRole("button", { name: "Use this folder" }); }
function card() { return render(<DriveSetupCard settings={null} onSettingsRefresh={refresh} />); }

describe("Admin Drive actual-client protocol", () => {
  it("connects, chooses, verifies and saves without submitting storage or HEVC preferences", async () => {
    render(<StoragePanel />);
    await screen.findByRole("region", { name: "Local storage" });
    fireEvent.click(screen.getByRole("link", { name: "Policy" }));
    await screen.findByRole("spinbutton", { name: "Total local budget" });
    const hevc = await screen.findByRole("combobox", { name: "HEVC compression" });
    fireEvent.click(hevc); fireEvent.click(screen.getByRole("option", { name: "Disabled" }));
    fireEvent.click(screen.getByRole("link", { name: "Overview" }));
    fireEvent.click(screen.getByRole("button", { name: "Connect Google Drive" }));
    await connect();
    expect(screen.getByLabelText("Client secret").getAttribute("value")).toBe("");
    expect(popup.location.href).toContain("https://accounts.google.com/");
    fireEvent.click(screen.getByRole("combobox", { name: "Browse subfolders" })); fireEvent.click(screen.getByRole("option", { name: "Movies" }));
    await waitFor(() => expect(requests.some(item => item.path.endsWith("folders?path=Movies"))).toBe(true));
    fireEvent.click(await screen.findByRole("button", { name: "Use this folder" }));
    await waitFor(() => expect(current?.selectedPath).toBe("Movies"));
    fireEvent.click(screen.getByRole("checkbox", { name: /Allow StreamHome/ }));
    fireEvent.click(screen.getByRole("button", { name: "Verify folder" }));
    const checkbox = await screen.findByRole("checkbox", { name: /Use this folder/ });
    expect((screen.getByRole("button", { name: "Save connection" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(checkbox); fireEvent.click(screen.getByRole("button", { name: "Save connection" }));
    await screen.findByText("Connection saved");
    fireEvent.click(screen.getByRole("link", { name: "Policy" }));
    expect(screen.getByRole("combobox", { name: "HEVC compression" }).textContent).toBe("Disabled");
    expect(screen.getByText("Local storage always stays active.")).toBeTruthy();
    expect(requests.filter(item => item.path.startsWith("/api/admin/storage/policy") && item.method !== "GET")).toHaveLength(0);
    expect(requests.filter(item => item.path === "/api/system/settings" && item.method === "POST")).toHaveLength(0);
    expect(requests.filter(item => item.path.endsWith("/save"))).toHaveLength(1);
  });
  it("keeps OAuth settings directly visible and uses keyboard accessible custom selectors", async () => {
    card(); await fillCredentials();
    expect(document.querySelector("details, select")).toBeNull();
    const audience = screen.getByRole("combobox", { name: "Audience" });
    fireEvent.keyDown(audience, { key: "End" }); fireEvent.keyDown(audience, { key: "Enter" });
    expect(audience.textContent).toContain("Internal");
    expect(screen.getByText(/does not change Google's Audience or Publishing status/)).toBeTruthy();
    expect(screen.getByText(/7 days/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Authorize Google Drive" }));
    await screen.findByRole("button", { name: "Use this folder" });
    expect(screen.getByRole("heading", { name: "OAuth app settings" })).toBeTruthy();
    expect(requests.find(item => item.path === `${root}/jobs`)?.body).toEqual({ client_id: "synthetic-client", client_secret: "synthetic-secret", audience: "internal", publishing_status: "testing" });
  });
  it("does not start a job when the popup is blocked", async () => {
    vi.mocked(window.open).mockReturnValue(null); card(); await fillCredentials();
    fireEvent.click(screen.getByRole("button", { name: "Authorize Google Drive" }));
    await screen.findByText(/Allow popups/);
    expect(requests.some(item => item.method === "POST")).toBe(false);
  });
  it("reconciles a lost start with read-only setup status and never invents a cancellation ID", async () => {
    override = (path, method) => { if (path === `${root}/jobs` && method === "POST") { current = sampleJob(); return Promise.reject(new TypeError("lost response")); } };
    card(); await fillCredentials(); fireEvent.click(screen.getByRole("button", { name: "Authorize Google Drive" }));
    await screen.findByText(/its result is unknown/);
    fireEvent.click(screen.getByRole("button", { name: "Authorize Google Drive" }));
    expect(requests.filter(item => item.method === "POST")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Check status" }));
    await screen.findByRole("button", { name: "Use this folder" });
    expect(requests.some(item => item.method === "DELETE")).toBe(false);
  });
  it("keeps missing-job start uncertainty blocked after an explicit status check", async () => {
    override = (path, method) => path === `${root}/jobs` && method === "POST" ? Promise.reject(new Error("lost")) : undefined;
    card(); await fillCredentials(); fireEvent.click(screen.getByRole("button", { name: "Authorize Google Drive" }));
    await screen.findByRole("button", { name: "Check status" }); fireEvent.click(screen.getByRole("button", { name: "Check status" }));
    await screen.findByText(/still unconfirmed/);
    expect((screen.getByRole("button", { name: "Authorize Google Drive" }) as HTMLButtonElement).disabled).toBe(true);
    expect(requests.filter(item => item.method === "POST")).toHaveLength(1);
  });
  it("serializes double-clicked mutations and focuses the step heading after acknowledgement", async () => {
    current = sampleJob(); let finish!: (response: Response) => void;
    override = (path, method) => path.endsWith("/select-folder") && method === "POST" ? new Promise(resolve => { finish = resolve; }) : undefined;
    card(); await screen.findByRole("combobox", { name: "Browse subfolders" });
    fireEvent.click(screen.getByRole("combobox", { name: "Browse subfolders" })); fireEvent.click(screen.getByRole("option", { name: "Movies" }));
    const select = await screen.findByRole("button", { name: "Use this folder" });
    fireEvent.click(select); fireEvent.click(select);
    await waitFor(() => expect(requests.filter(item => item.path.endsWith("/select-folder"))).toHaveLength(1));
    expect(screen.queryByRole("button", { name: "Verify folder" })?.getAttribute("disabled")).not.toBeNull();
    await act(async () => finish(json(current)));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Google Drive connection" })));
  });
  it("reconciles unknown select, verification and save responses without replaying mutations", async () => {
    current = sampleJob(); card(); await screen.findByRole("combobox", { name: "Browse subfolders" });
    fireEvent.click(screen.getByRole("combobox", { name: "Browse subfolders" })); fireEvent.click(screen.getByRole("option", { name: "Movies" }));
    await screen.findByRole("button", { name: "Use this folder" });
    override = (path, method) => {
      if (method !== "POST") return;
      if (path.endsWith("/select-folder")) { current = { ...current!, selectedPath: "Movies" }; return Promise.reject(new Error("lost")); }
      if (path.endsWith("/test")) { current = { ...current!, status: "ready" }; return Promise.reject(new Error("lost")); }
      if (path.endsWith("/save")) { current = { ...current!, status: "saved" }; configured = true; return Promise.reject(new Error("lost")); }
    };
    for (const name of ["Use this folder", "Verify folder", "Save connection"]) {
      if (name === "Save connection") fireEvent.click(screen.getByRole("checkbox", { name: /Use this folder/ }));
      if (name === "Verify folder") fireEvent.click(screen.getByRole("checkbox", { name: /Allow StreamHome/ }));
      fireEvent.click(screen.getByRole("button", { name }));
      await screen.findByRole("button", { name: "Check status" });
      fireEvent.click(screen.getByRole("button", { name: "Check status" }));
      if (name === "Use this folder") await screen.findByRole("button", { name: "Verify folder" });
      else if (name === "Verify folder") await screen.findByRole("checkbox", { name: /Use this folder/ });
      else await screen.findByText("Connection saved");
    }
    for (const suffix of ["select-folder", "test", "save"]) expect(requests.filter(item => item.path.endsWith(`/${suffix}`) && item.method === "POST")).toHaveLength(1);
  });
  it("requires reauthentication after authorization expires and never replays the protected action", async () => {
    current = sampleJob(); let reject = true;
    override = (path, method) => reject && path.endsWith("/test") && method === "POST" ? json({ detail: { code: "reauthentication_required", message: "Confirm identity" } }, 403) : undefined;
    card(); fireEvent.click(await screen.findByRole("checkbox", { name: /Allow StreamHome/ })); fireEvent.click(screen.getByRole("button", { name: "Verify folder" }));
    await screen.findByRole("dialog", { name: "Confirm your identity" }, { timeout: 5000 }); reject = false;
    fireEvent.click(screen.getByRole("button", { name: "Authorize Drive" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(requests.filter(item => item.path.endsWith("/test"))).toHaveLength(1);
    expect(screen.queryByRole("checkbox", { name: /Use this folder/ })).toBeNull();
  });
  it("ignores stale initial status readers under StrictMode", async () => {
    let finish!: (response: Response) => void; let calls = 0;
    override = path => { if (path === `${root}/setup` && ++calls === 1) return new Promise(resolve => { finish = resolve; }); };
    render(<StrictMode><DriveSetupCard settings={null} onSettingsRefresh={refresh} /></StrictMode>);
    await fillCredentials(); await act(async () => finish(json(setupFor(sampleJob("saved"), true))));
    expect(screen.queryByText("Connection saved")).toBeNull();
    expect(screen.getByRole("button", { name: "Authorize Google Drive" })).toBeTruthy();
  });
  it("ignores folder readers after navigation and unmount", async () => {
    current = sampleJob(); let finish!: (response: Response) => void;
    override = path => path.endsWith("folders?path=") ? new Promise(resolve => { finish = resolve; }) : undefined;
    const view = card(); await screen.findByText("Loading folders…"); view.unmount();
    await act(async () => finish(json({ path: "", folders: [{ name: "Old", path: "Old" }] })));
    expect(screen.queryByText("Old")).toBeNull(); expect(refresh).not.toHaveBeenCalled();
  });
  it("shows an expired job with cancellation available for temporary-file cleanup", async () => {
    current = sampleJob("expired"); card(); await screen.findByText(/authorization expired/);
    expect(screen.getByRole("button", { name: "Cancel connection" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Verify folder" })).toBeNull();
  });
  it("does not reconcile a lost new start against an older terminal job", async () => {
    current = { ...sampleJob("cancelled"), id: "older-job" };
    override = (path, method) => path === `${root}/jobs` && method === "POST" ? Promise.reject(new Error("lost")) : undefined;
    card(); await fillCredentials(); fireEvent.click(screen.getByRole("button", { name: "Authorize Google Drive" }));
    await screen.findByText(/its result is unknown/);
    fireEvent.click(screen.getByRole("button", { name: "Check status" }));
    await screen.findByText(/still unconfirmed/);
    expect((screen.getByRole("button", { name: "Authorize Google Drive" }) as HTMLButtonElement).disabled).toBe(true);
  });
  it("respects a server-owned operation after remount until a read confirms settlement", async () => {
    current = { ...sampleJob(), pendingOperation: "mkdir", generation: 2 };
    card(); await screen.findByText("Authorization complete");
    expect(screen.queryByRole("button", { name: "Use this folder" })).toBeNull();
    expect((screen.getByRole("button", { name: "Verify folder" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Cancel connection" }) as HTMLButtonElement).disabled).toBe(true);
    current = { ...current, pendingOperation: null };
    fireEvent.click(screen.getByRole("button", { name: "Check status" }));
    await screen.findByRole("combobox", { name: "Browse subfolders" });
    expect(requests.filter(item => item.method !== "GET")).toHaveLength(0);
  });
  it("requires temporary-file consent and resets it when the selected target changes", async () => {
    current = sampleJob();
    override = path => path.includes("/folders?") ? json({ path: decodeURIComponent(path.split("path=")[1]), folders: [{ name: "Series", path: "Series" }] }) : undefined;
    card(); await screen.findByRole("combobox", { name: "Browse subfolders" });
    expect((screen.getByRole("button", { name: "Verify folder" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("checkbox", { name: /Allow StreamHome/ }));
    expect((screen.getByRole("button", { name: "Verify folder" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole("combobox", { name: "Browse subfolders" })); fireEvent.click(screen.getByRole("option", { name: "Series" }));
    await waitFor(() => expect(requests.some(item => item.path.endsWith("folders?path=Series"))).toBe(true));
    fireEvent.click(await screen.findByRole("button", { name: "Use this folder" }));
    await waitFor(() => expect(current?.selectedPath).toBe("Series"));
    await waitFor(() => expect((screen.getByRole("checkbox", { name: /Allow StreamHome/ }) as HTMLInputElement).checked).toBe(false));
    expect((screen.getByRole("button", { name: "Verify folder" }) as HTMLButtonElement).disabled).toBe(true);
  });
  it("reconciles a lost folder-create acknowledgement by listing without replaying mkdir", async () => {
    current = sampleJob(); let created = false;
    override = (path, method, body) => {
      if (path.endsWith("/folders") && method === "POST") { created = true; return Promise.reject(new Error("lost")); }
      if (created && path.includes("/folders?")) return json({ path: decodeURIComponent(path.split("path=")[1]), folders: [{ name: "Fresh", path: "Fresh" }] });
    };
    card(); await screen.findByLabelText("New folder name");
    fireEvent.change(screen.getByLabelText("New folder name"), { target: { value: "Fresh" } });
    fireEvent.click(screen.getByRole("button", { name: "Create folder" }));
    await screen.findByText(/its result is unknown/); fireEvent.click(screen.getByRole("button", { name: "Check status" }));
    await screen.findByText(/server confirmed the current state/);
    expect(requests.filter(item => item.path.endsWith("/folders") && item.method === "POST")).toHaveLength(1);
  });
  it("does not let a stale poll resurrect a cancelled connection", async () => {
    current = sampleJob("authorizing"); let finish!: (value: Response) => void;
    override = (path, method) => path === `${root}/jobs/job-1` && method === "GET" ? new Promise(resolve => { finish = resolve; }) : undefined;
    card(); await screen.findByRole("button", { name: "Cancel connection" });
    await waitFor(() => expect(finish).toBeTypeOf("function"), { timeout: 2500 });
    fireEvent.click(screen.getByRole("button", { name: "Cancel connection" }));
    await screen.findByRole("button", { name: "Authorize Google Drive" });
    await act(async () => finish(json(sampleJob())));
    expect(screen.queryByRole("button", { name: "Use this folder" })).toBeNull();
    expect(requests.filter(item => item.method === "DELETE")).toHaveLength(1);
  });

  it("allows an explicit cancellation of a server-confirmed uncertain mkdir", async () => {
    current = { ...sampleJob(), pendingOperation: "mkdir_uncertain", pendingFolderPath: "Fresh", generation: 2 };
    card(); const cancel = await screen.findByRole("button", { name: "Cancel connection" });
    expect((cancel as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole("button", { name: "Verify folder" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(cancel);
    await screen.findByRole("button", { name: "Authorize Google Drive" });
    expect(requests.filter(item => item.method === "DELETE")).toHaveLength(1);
    expect(requests.some(item => item.path.endsWith("/test"))).toBe(false);
  });
  it("does not publish a save acknowledgement after the owning component unmounts", async () => {
    current = sampleJob("ready"); let finish!: (value: Response) => void;
    override = (path, method) => path.endsWith("/save") && method === "POST" ? new Promise(resolve => { finish = resolve; }) : undefined;
    const view = card(); fireEvent.click(await screen.findByRole("checkbox", { name: /Use this folder/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(finish).toBeTypeOf("function")); view.unmount();
    await act(async () => finish(json({ valid: true, remotePath: "streamhome-drive:Movies", job: sampleJob("saved") })));
    expect(refresh).not.toHaveBeenCalled();
  });

  it("retires stale ready controls after a definite verification failure until an authoritative status read", async () => {
    current = sampleJob("ready");
    override = (path, method) => {
      if (path.endsWith("/test") && method === "POST") {
        current = { ...current!, status: "selecting_folder", progress: "Verification failed" };
        return json({ detail: { code: "drive_test_failed", message: "The selected folder could not be verified." } }, 422);
      }
    };
    card(); fireEvent.click(await screen.findByRole("checkbox", { name: /Use this folder/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Allow StreamHome/ }));
    fireEvent.click(screen.getByRole("button", { name: "Verify folder" }));
    await screen.findByText("The selected folder could not be verified.");
    expect((screen.getByRole("button", { name: "Save connection" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Verify folder" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "Confirm identity" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save connection" }));
    expect(requests.some(item => item.path.endsWith("/save"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Check status" }));
    await screen.findByText("Verification failed");
    expect(screen.queryByRole("button", { name: "Save connection" })).toBeNull();
    expect(requests.filter(item => item.path.endsWith("/test"))).toHaveLength(1);
  });
  it("preserves a slow owned folder listing across multiple polling intervals", async () => {
    current = sampleJob(); let finish!: (value: Response) => void;
    override = (path, method) => {
      if (path.includes("/folders?") && method === "GET") return new Promise(resolve => { finish = resolve; });
      if (path === `${root}/jobs/job-1` && method === "GET") return json({ ...current!, pendingOperation: "list" });
    };
    vi.useFakeTimers();
    await act(async () => { card(); await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("Loading folders…")).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(4600); });
    expect(requests.filter(item => item.path === `${root}/jobs/job-1` && item.method === "GET")).toHaveLength(0);
    await act(async () => finish(json({ path: "", folders: [{ name: "Movies", path: "Movies" }] })));
    expect(screen.getByRole("combobox", { name: "Browse subfolders" })).toBeTruthy();
    expect(requests.filter(item => item.path.includes("/folders?") && item.method === "GET")).toHaveLength(1);
  });

});
