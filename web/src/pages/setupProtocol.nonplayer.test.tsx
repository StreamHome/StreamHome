import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { activateDrive, beginSetupTOTP, createDriveFolder, getDriveJob, listDriveFolders, selectDriveFolder, startDriveOAuth, testDriveFolder, verifySetupTOTP } from "../api/setup";
import { SetupPage } from "./SetupPage";
import { useSetupEnrollment } from "./setupEnrollment";

const enrollment = { enrollmentId: "synthetic-enrollment", manualKey: "SYNTHETICKEY", qrImageUrl: "/synthetic-qr", expiresAt: Date.now() / 1000 + 600 };
const drivePayload = { clientId: "synthetic-client", clientSecret: "synthetic-secret", remoteName: "synthetic-drive", audience: "external" as const, publishingStatus: "production" as const, publicUrl: "https://synthetic.example" };
const driveResponse = { jobId: "synthetic-job", authorizationUrl: "https://synthetic.invalid/oauth", expiresAt: Date.now() / 1000 + 600 };
const respond = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("setup mutation acknowledgement boundaries", () => {
  it.each([null, {}, { ...enrollment, enrollmentId: "" }, { ...enrollment, enrollmentId: " " }, { ...enrollment, manualKey: "" }, { ...enrollment, qrImageUrl: "" }, { ...enrollment, expiresAt: "tomorrow" }, { ...enrollment, expiresAt: null }])("does not acknowledge a malformed enrollment200 %j", async (value) => {
    vi.stubGlobal("fetch", vi.fn(async () => respond(value)));
    await expect(beginSetupTOTP("owner@synthetic.example")).rejects.toMatchObject({ status: 0, code: "invalid_response" });
  });

  it.each([null, {}, { valid: true }, { valid: false, enrollmentId: "synthetic-enrollment" }, { valid: true, enrollmentId: "different-enrollment" }, { valid: true, enrollmentId: "" }])("requires a positive verification ack for the exact submitted enrollment %j", async (value) => {
    vi.stubGlobal("fetch", vi.fn(async () => respond(value)));
    await expect(verifySetupTOTP("synthetic-enrollment", "123456")).rejects.toMatchObject({ status: 0, code: "invalid_response" });
  });

  it.each([null, {}, { ...driveResponse, jobId: "" }, { ...driveResponse, authorizationUrl: "" }, { ...driveResponse, expiresAt: "tomorrow" }, { ...driveResponse, expiresAt: null }])("does not acknowledge malformed OAuth start200 %j", async (value) => {
    vi.stubGlobal("fetch", vi.fn(async () => respond(value)));
    await expect(startDriveOAuth(drivePayload)).rejects.toMatchObject({ status: 0, code: "invalid_response" });
  });

  it("preserves valid acknowledgement data and setup same-origin credentials", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(respond(enrollment)).mockResolvedValueOnce(respond({ valid: true, enrollmentId: enrollment.enrollmentId })).mockResolvedValueOnce(respond(driveResponse));
    vi.stubGlobal("fetch", fetchMock);
    await expect(beginSetupTOTP("owner@synthetic.example")).resolves.toEqual(enrollment);
    await expect(verifySetupTOTP(enrollment.enrollmentId, "123456")).resolves.toEqual({ valid: true, enrollmentId: enrollment.enrollmentId });
    await expect(startDriveOAuth(drivePayload)).resolves.toEqual(driveResponse);
    expect(fetchMock.mock.calls.map((call) => call[1].credentials)).toEqual(["same-origin", "same-origin", "same-origin"]);
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ enrollment_id: enrollment.enrollmentId, code: "123456" });
  });

  it("actual API plus enrollment hook treats missing-ID200 as unknown and email change never sends DELETE undefined", async () => {
    const fetchMock = vi.fn(async () => respond({}));
    vi.stubGlobal("fetch", fetchMock);
    const { result, rerender } = renderHook(({ email }) => useSetupEnrollment(email, false), { initialProps: { email: "owner@synthetic.example" } });
    act(() => result.current.choose(true));
    await waitFor(() => expect(result.current.state.phase).not.toBe("generating"));
    rerender({ email: "changed@synthetic.example" });
    act(() => { result.current.choose(false); result.current.choose(true); });
    await act(async () => undefined);
    const requests = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(requests.filter(([, options]) => options.method === "DELETE")).toHaveLength(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.current.state.enrollment).toBeUndefined();
    const [path, options] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe("/api/setup/totp/begin");
    expect(options.method).toBe("POST");
    expect(result.current.state.phase).toBe("unknown");
  });

  it("actual API plus enrollment hook never marks a different enrollment's verification as verified", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(respond(enrollment)).mockResolvedValueOnce(respond({ valid: true, enrollmentId: "different-enrollment" }));
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useSetupEnrollment("owner@synthetic.example", false));
    act(() => result.current.choose(true));
    await waitFor(() => expect(result.current.state.phase).toBe("ready"));
    act(() => result.current.verify("123456"));
    await waitFor(() => expect(result.current.state.phase).toBe("unknown"));
    expect(result.current.state.enrollment).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});


const driveJob = { id: "synthetic-job", status: "selecting_folder" as const, remoteName: "synthetic-drive", selectedPath: "", progress: "Select a folder", audience: "external" as const, publishingStatus: "production" as const, expiresAt: 9999999999 };
describe("actual Drive response boundaries", () => {
  const operations = [
    ["job read", () => getDriveJob(driveJob.id)],
    ["folder listing", () => listDriveFolders(driveJob.id)],
    ["folder creation", () => createDriveFolder(driveJob.id, "Media")],
    ["folder selection", () => selectDriveFolder(driveJob.id, "Media")],
    ["read/write test", () => testDriveFolder(driveJob.id)],
    ["activation", () => activateDrive(driveJob.id)],
  ] as const;
  for (const [name, operation] of operations) {
    it.each([null, {}])(`rejects malformed successful ${name} %j as an unknown outcome`, async (value) => {
      vi.stubGlobal("fetch", vi.fn(async () => respond(value)));
      await expect(operation()).rejects.toMatchObject({ status: 0, code: "invalid_response" });
    });
  }
  it.each([{ ...driveJob, id: "another-job" }, { ...driveJob, status: "invented" }, { ...driveJob, progress: null }])("rejects unowned or malformed job state %j", async (value) => {
    vi.stubGlobal("fetch", vi.fn(async () => respond(value)));
    await expect(getDriveJob(driveJob.id)).rejects.toMatchObject({ code: "invalid_response" });
  });
  it.each([{ path: "", folders: null }, { path: "", folders: [{}] }, { folders: [] }])("rejects malformed listing rows %j", async (value) => {
    vi.stubGlobal("fetch", vi.fn(async () => respond(value)));
    await expect(listDriveFolders(driveJob.id)).rejects.toMatchObject({ code: "invalid_response" });
  });
  it("preserves valid empty reads, selected folder acknowledgement, ready results and same-origin credentials", async () => {
    const selected = { ...driveJob, selectedPath: "Media" };
    const tested = { valid: true, remotePath: "synthetic-drive:Media", job: { ...selected, status: "ready" }, quota: { total: 0, used: 0, free: 0 } };
    const fetchMock = vi.fn().mockResolvedValueOnce(respond(driveJob)).mockResolvedValueOnce(respond({ path: "", folders: [] })).mockResolvedValueOnce(respond({ path: "Media" })).mockResolvedValueOnce(respond(selected)).mockResolvedValueOnce(respond(tested)).mockResolvedValueOnce(respond(tested));
    vi.stubGlobal("fetch", fetchMock);
    await expect(getDriveJob(driveJob.id)).resolves.toEqual(driveJob); await expect(listDriveFolders(driveJob.id)).resolves.toEqual({ path: "", folders: [] });
    await expect(createDriveFolder(driveJob.id, "Media")).resolves.toEqual({ path: "Media" }); await expect(selectDriveFolder(driveJob.id, "Media")).resolves.toEqual(selected);
    await expect(testDriveFolder(driveJob.id)).resolves.toEqual(tested); await expect(activateDrive(driveJob.id)).resolves.toEqual(tested);
    expect(fetchMock.mock.calls.every(([, options]) => options.credentials === "same-origin")).toBe(true);
  });
  it("the actual mounted page renders a retryable malformed folder read without a false empty list or a crash", async () => {
    sessionStorage.clear(); window.history.replaceState({}, "", "/setup?driveJob=synthetic-job");
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
    let foldersRead = 0; const unexpected: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: string, options?: RequestInit) => {
      const method = options?.method ?? "GET";
      if (method === "GET" && input === "/api/setup/status") return respond({ required: true, unlocked: true, webPort: 3000, serverVersion: "synthetic", mediaPath: "synthetic-media", databasePath: "synthetic-db", publicUrl: "https://synthetic.example" });
      if (method === "GET" && input === "/api/setup/rclone/drive/jobs/synthetic-job") return respond(driveJob);
      if (method === "GET" && input === "/api/setup/rclone/drive/jobs/synthetic-job/folders?path=") return respond(++foldersRead === 1 ? {} : { path: "", folders: [] });
      unexpected.push(`${method} ${input}`); throw new Error("Unexpected request");
    }));
    await act(async () => { render(<SetupPage />); }); const retry = await screen.findByRole("button", { name: "Retry folder listing" });
    expect(screen.queryByText(/No folders here/)).toBeNull(); expect(screen.getByRole<HTMLButtonElement>("button", { name: "Create folder" }).disabled).toBe(true);
    fireEvent.click(retry); await screen.findByText(/No folders here/); expect(foldersRead).toBe(2); expect(unexpected).toEqual([]);
  });
});
