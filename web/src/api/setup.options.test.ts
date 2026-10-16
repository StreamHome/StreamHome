import { afterEach, expect, it, vi } from "vitest";
import { completeSetup, getDriveJob, getSetupStatus, listDriveFolders, type SetupCompleteRequest } from "./setup";
afterEach(() => vi.unstubAllGlobals());
it("preserves setup credentials while forwarding read cancellation", async () => {
  const fetchMock = vi.fn(async (path: string) => new Response(JSON.stringify(path.endsWith("/status")
    ? { required: true, unlocked: false, webPort: 3000, serverVersion: "fixture", mediaPath: "", databasePath: "" }
    : path.includes("/folders?") ? { path: "Folder", folders: [] }
    : { id: "fixture-job", status: "selecting_folder", remoteName: "fixture", selectedPath: "", progress: "Choose folder", audience: "external", publishingStatus: "production", expiresAt: 9999999999 })));
  vi.stubGlobal("fetch", fetchMock);
  const caller = new AbortController();
  await getSetupStatus({ signal: caller.signal, credentials: "omit" });
  await getDriveJob("fixture-job", { signal: caller.signal });
  await listDriveFolders("fixture-job", "Folder", { signal: caller.signal });
  expect(fetchMock).toHaveBeenCalledTimes(3);
  expect(fetchMock.mock.calls.map((call) => (call as unknown as [string, RequestInit])[1].credentials)).toEqual(["same-origin", "same-origin", "same-origin"]);
});

it.each([{}, { complete: true }, { complete: true, restartScheduled: true, webPort: 3000, recoveryCodes: [], ingestionToken: "" }])("treats an incomplete show-once completion payload as unconfirmed %j", async (response) => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(response))));
  await expect(completeSetup({} as SetupCompleteRequest)).rejects.toMatchObject({ status: 0, code: "invalid_response" });
});

it.each([{}, {required: "false", unlocked: true}, {required: true, unlocked: "yes"}, null])("rejects malformed setup status instead of granting complete/unlocked state %j", async (value) => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(value))));
  await expect(getSetupStatus()).rejects.toMatchObject({ code: "invalid_response" });
});
it("rejects an empty successful setup status body", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("")));
  await expect(getSetupStatus()).rejects.toMatchObject({ code: "invalid_response" });
});
