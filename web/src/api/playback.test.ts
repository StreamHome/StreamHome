import { afterEach, describe, expect, it, vi } from "vitest";
import { closePlaybackRun, createPlaybackRun, updatePlaybackProgress, normalizePlaybackRun } from "./playback";

afterEach(() => vi.unstubAllGlobals());

describe("playback tracking", () => {
  it("normalizes only exact same-origin run URLs and keeps legacy contracts", () => {
    const wire = { runId: "run-1", streamBaseUrl: "/api/playback/streams/run-1", manifestUrl: "/ticket-manifest", progressiveUrl: "/ticket-video", tracks: [{ id: "external_tr", directUrl: "/ticket-audio" }] } as unknown as import("../types/api").PlaybackRunResponse;
    const normalized = normalizePlaybackRun(wire);
    expect(normalized.manifestUrl).toBe("/api/playback/streams/run-1/manifest");
    expect(normalized.progressiveUrl).toBe("/api/playback/streams/run-1/progressive");
    expect(normalized.tracks[0].directUrl).toBe("/api/playback/streams/run-1/source/external_tr");
    const untrusted = { ...wire, streamBaseUrl: "https://other.invalid/streams/run-1" };
    expect(normalizePlaybackRun(untrusted).streamBaseUrl).toBeNull();
    expect(normalizePlaybackRun(untrusted).manifestUrl).toBe(wire.manifestUrl);
    const legacy = { ...wire, streamBaseUrl: null };
    expect(normalizePlaybackRun(legacy)).toBe(legacy);
  });
  it("creates authenticated playback runs with canonical movie, profile, and episode fields", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ runId: "run-1" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await createPlaybackRun("tv_1", "profile-1", "ep_1_s1_e1");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/playback/runs");
    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toEqual({
      movie_id: "tv_1",
      profile_id: "profile-1",
      episode_id: "ep_1_s1_e1",
    });
  });

  it("sends sequenced event progress with keepalive for final lifecycle updates", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "ok", nextSequenceNumber: 8 }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await updatePlaybackProgress("run-1", {
      timestamp: 91.5,
      durationWatched: 9,
      isFinished: false,
      sequenceNumber: 7,
      event: "visibility",
    }, true);
    const request = fetchMock.mock.calls[0][1] as RequestInit;
    expect(request.keepalive).toBe(true);
    expect(JSON.parse(String(request.body))).toEqual({
      timestamp: 91.5,
      duration_watched: 9,
      is_finished: false,
      sequence_number: 7,
      event: "visibility",
    });
  });

  it("closes unfinished runs with confirmed progress and keepalive delivery", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "abandoned", nextSequenceNumber: 4 }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await closePlaybackRun("run-1", {
      timestamp: 42,
      durationWatched: 3,
      isFinished: false,
      sequenceNumber: 3,
      event: "exit",
    });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/playback/runs/run-1/close");
    const request = fetchMock.mock.calls[0][1] as RequestInit;
    expect(request.keepalive).toBe(true);
    expect(JSON.parse(String(request.body))).toEqual({
      timestamp: 42,
      duration_watched: 3,
      is_finished: false,
      sequence_number: 3,
      event: "exit",
    });
  });
});
