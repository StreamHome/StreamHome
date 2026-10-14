import { afterEach, describe, expect, it, vi } from "vitest";
import {
  getUpdateStatus,
  normalizeUpdateStatus,
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


describe("update server clock normalization", () => {
  const clock = { timeZone: "EEST", utcOffset: "UTC+03:00", localTime: "2026-10-13 15:30:00" };

  it("preserves the server wall time without converting it into browser time", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ phase: "idle", serverClock: clock }), {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const status = await getUpdateStatus();
    expect(status.serverClock).toEqual(clock);
    expect(fetchMock).toHaveBeenCalledWith("/api/update/status", expect.objectContaining({ credentials: "same-origin" }));
  });

  it("keeps older servers compatible and treats incomplete clock data as unavailable", () => {
    for (const serverClock of [undefined, null, { ...clock, utcOffset: "" }, { ...clock, localTime: 42 }]) {
      expect(normalizeUpdateStatus({ phase: "idle", serverClock } as import("../types/api").UpdateStatus).serverClock).toBeNull();
    }
  });
});
