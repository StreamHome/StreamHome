import { afterEach, describe, expect, it, vi } from "vitest";
import { getMovies, normalizeAnalysis } from "./movies";

afterEach(() => vi.unstubAllGlobals());

describe("catalog API", () => {
  it("requests backend-personalized ordering when a profile is supplied", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("[]", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(getMovies("profile one")).resolves.toEqual([]);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/movies?profile_id=profile%20one");
  });

  it("keeps independent catalog lookups unpersonalized", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("[]", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await getMovies();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/movies");
  });
});

describe("measured media analysis", () => {
  it("keeps final measurements and sender provenance separate", () => {
    expect(normalizeAnalysis({ quality: "720p", quality_provenance: "final_probe", sender_quality: "2160p", width: 1280, height: 720, video_stream_index: 0, video_bitrate: 3000000, container_bitrate: 3200000, probed_duration: 12.5, duration_provenance: "selected_stream" })).toEqual({ quality: "720p", qualityProvenance: "final_probe", senderQuality: "2160p", width: 1280, height: 720, videoStreamIndex: 0, frameRate: null, videoBitrate: 3000000, containerBitrate: 3200000, bitrateUnit: "bits_per_second", durationSeconds: 12.5, durationProvenance: "selected_stream" });
    expect(normalizeAnalysis({ probed_duration: 1, duration_provenance: "selected_stream_packets" }))
      .toMatchObject({ durationSeconds: 1, durationProvenance: "selected_stream_packets" });
  });
  it("retains fallback provenance without inventing a selected-stream measurement", () => {
    expect(normalizeAnalysis({ probed_duration: 12.5, duration_provenance: "container_fallback" }))
      .toMatchObject({ durationSeconds: 12.5, durationProvenance: "container_fallback", senderQuality: null });
    expect(normalizeAnalysis({ durationSeconds: 12.5, durationProvenance: "selected_stream_tag" })?.durationProvenance)
      .toBe("selected_stream_tag");
    expect(normalizeAnalysis({ probed_duration: 0 })?.durationProvenance).toBe("unknown");
    expect(normalizeAnalysis({ duration_provenance: true })?.durationProvenance).toBe("unknown");
  });
  it("preserves unknown and invalid measurements as null", () => {
    expect(normalizeAnalysis({ video_bitrate: -1, container_bitrate: Infinity, height: 0 })?.videoBitrate).toBeNull();
    expect(normalizeAnalysis({ container_bitrate: Infinity })?.containerBitrate).toBeNull();
    expect(normalizeAnalysis(undefined)).toBeNull();
  });
});
