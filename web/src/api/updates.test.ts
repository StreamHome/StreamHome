import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cancelPendingUpdate,
  checkForUpdates,
  installUpdate,
  reportBrowserPresence,
  updateUpdatePolicy,
} from "./updates";

afterEach(() => vi.unstubAllGlobals());

describe("update API contracts", () => {
  it("wires check, install, cancel, policy, and presence requests", async () => {
    const response = { phase: "up_to_date", policy: {} };
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(response), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
    vi.stubGlobal("fetch", fetchMock);

    await checkForUpdates();
    expect(fetchMock).toHaveBeenLastCalledWith("/api/update/check", expect.objectContaining({ method: "POST" }));

    await installUpdate("now", true);
    expect(JSON.parse(String(fetchMock.mock.calls[fetchMock.mock.calls.length - 1]?.[1]?.body))).toEqual({ retry_failed_target: true, mode: "now" });

    await cancelPendingUpdate();
    expect(fetchMock).toHaveBeenLastCalledWith("/api/update/pending", expect.objectContaining({ method: "DELETE" }));

    await updateUpdatePolicy({
      automaticUpdates: true,
      idleMinutes: 15,
      checkIntervalHours: 6,
      maintenanceStart: "02:00",
      maintenanceEnd: "04:00",
      branch: "main",
      requireSignedCommits: false,
    });
    expect(JSON.parse(String(fetchMock.mock.calls[fetchMock.mock.calls.length - 1]?.[1]?.body))).toEqual({
      automatic_updates: true,
      idle_minutes: 15,
      check_interval_hours: 6,
      maintenance_start: "02:00",
      maintenance_end: "04:00",
    });

    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await reportBrowserPresence(false);
    expect(fetchMock).toHaveBeenLastCalledWith("/api/update/presence", expect.objectContaining({
      method: "POST",
      credentials: "same-origin",
      keepalive: true,
    }));
  });
});
