import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cancelAdminDrive, getAdminDriveJob, getAdminDriveSetup, listAdminDriveFolders, saveAdminDrive, selectAdminDriveFolder, startAdminDrive, testAdminDriveFolder } from "./adminDrive";
const job = { id: "owned-job", status: "selecting_folder", remoteName: "streamhome-drive", pendingOperation: null, generation: 1, selectedPath: "Movies", progress: "Choose a folder", audience: "external", publishingStatus: "testing", expiresAt: 2000000000 };
let fetcher: ReturnType<typeof vi.fn>;
beforeEach(() => { fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher); });
afterEach(() => vi.unstubAllGlobals());
const reply = (body: unknown) => fetcher.mockResolvedValueOnce(new Response(JSON.stringify(body), { status: 200 }));
describe("Admin Drive response validation", () => {
  it("rejects malformed setup facts and unsafe supplied URLs", async () => {
    reply({ configured: false, publicUrl: "", callbackUrl: "javascript:alert(1)", guideUrl: "https://example.invalid", job: null });
    await expect(getAdminDriveSetup()).rejects.toMatchObject({ code: "invalid_response" });
    reply({ configured: false, publicUrl: "", callbackUrl: "", guideUrl: "https://example.invalid", job: null });
    expect((await getAdminDriveSetup()).callbackUrl).toBe("");
  });
  it("rejects unexpected job identities and statuses", async () => {
    reply({ ...job, id: "other-job" }); await expect(getAdminDriveJob(job.id)).rejects.toMatchObject({ code: "invalid_response" });
    reply({ ...job, status: "invented" }); await expect(getAdminDriveJob(job.id)).rejects.toMatchObject({ code: "invalid_response" });
  });
  it("accepts only Google's HTTPS authorization origin", async () => {
    const payload = { client_id: "synthetic", client_secret: "synthetic", audience: "external" as const, publishing_status: "testing" as const };
    reply({ jobId: job.id, authorizationUrl: "https://accounts.google.com.attacker.invalid/auth", expiresAt: 2000000000 });
    await expect(startAdminDrive(payload)).rejects.toMatchObject({ code: "invalid_response" });
  });
  it("requires matching folder path and exact selected target acknowledgement", async () => {
    reply({ path: "Other", folders: [] }); await expect(listAdminDriveFolders(job.id, "Movies")).rejects.toMatchObject({ code: "invalid_response" });
    reply({ ...job, selectedPath: "Other" }); await expect(selectAdminDriveFolder(job.id, "Movies")).rejects.toMatchObject({ code: "invalid_response" });
  });
  it("requires positive cancellation, ready verification and saved connection facts", async () => {
    reply({}); await expect(cancelAdminDrive(job.id)).rejects.toMatchObject({ code: "invalid_response" });
    reply({ valid: true, remotePath: "streamhome-drive:Movies", job }); await expect(testAdminDriveFolder(job.id)).rejects.toMatchObject({ code: "invalid_response" });
    reply({ valid: true, remotePath: "streamhome-drive:Movies", job: { ...job, status: "ready" } }); await expect(saveAdminDrive(job.id)).rejects.toMatchObject({ code: "invalid_response" });
  });
  it("rejects missing server operation ownership metadata", async () => {
    const { pendingOperation: _operation, ...missing } = job;
    reply(missing); await expect(getAdminDriveJob(job.id)).rejects.toMatchObject({ code: "invalid_response" });
  });
  it("retains exact server-authored saved state", async () => {
    reply({ valid: true, remotePath: "streamhome-drive:Movies", job: { ...job, status: "saved", libraryState: "streamhome" } });
    expect((await saveAdminDrive(job.id)).job.status).toBe("saved");
    expect(fetcher.mock.calls[0][0]).toBe("/api/system/drive/jobs/owned-job/save");
  });
});
