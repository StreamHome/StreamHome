import { describe, expect, it } from "vitest";
import type { Episode, PlaybackRunResponse } from "../../types/api";
import {
  advancingPlaybackDelta,
  applySubtitleTrackSelection,
  authoritativePlaybackDuration,
  catalogDurationSeconds,
  clampPlaybackTime,
  mergePlaybackRunMetadata,
  nextPlayableEpisode,
  preparationStatusMessage,
  playbackQualityOptions,
  shouldAutoHidePlayerControls,
  shouldAcceptObservedPlaybackTime,
} from "./PlayerPage";

describe("adaptive preparation status", () => {
  it("reports queue position and fast packaging progress truthfully", () => {
    expect(preparationStatusMessage({ stage: "queued", queuePosition: 3, readySegments: 0, activeWorkers: 2 }))
      .toBe("Playback preparation is queued at position 3.");
    expect(preparationStatusMessage({ stage: "packaging", queuePosition: 0, readySegments: 0, activeWorkers: 1 }))
      .toContain("without re-encoding");
    expect(preparationStatusMessage({ stage: "packaging", queuePosition: 0, readySegments: 2, activeWorkers: 1 }))
      .toContain("2 segments ready");
  });
});

function episode(id: string, seasonNumber: number, episodeNumber: number, videoUrl = `/media/${id}.mp4`): Episode {
  return { id, seasonNumber, episodeNumber, videoUrl, title: id, description: "", thumbnailUrl: "", duration: "", quality: "", languages: [], subtitles: [], skipMarkers: {} };
}

describe("series playback sequence", () => {
  it("sorts episodes and skips entries without playable server media", () => {
    const episodes = [episode("ep3", 1, 3), episode("ep1", 1, 1), episode("ep2", 1, 2, ""), episode("ep4", 2, 1)];
    expect(nextPlayableEpisode(episodes, "ep1")?.id).toBe("ep3");
    expect(nextPlayableEpisode(episodes, "ep3")?.id).toBe("ep4");
  });

  it("returns no episode at the end of the playable sequence", () => {
    expect(nextPlayableEpisode([episode("ep1", 1, 1)], "ep1")).toBeNull();
    expect(nextPlayableEpisode([episode("ep1", 1, 1)], "missing")).toBeNull();
  });

  it("treats an application-owned ingestion preview as playable", () => {
    const preview = { ...episode("ep2", 1, 2, ""), previewTaskId: "preview-task" };
    expect(nextPlayableEpisode([episode("ep1", 1, 1), preview], "ep1")?.id).toBe("ep2");
  });
});

describe("actual watched-time accounting", () => {
  it("counts bounded advancing playback but ignores pauses, rewinds, and forward seeks", () => {
    expect(advancingPlaybackDelta(1_000, 10, 2_000, 11, true)).toBe(1);
    expect(advancingPlaybackDelta(1_000, 10, 2_000, 11, false)).toBe(0);
    expect(advancingPlaybackDelta(1_000, 10, 2_000, 9, true)).toBe(0);
    expect(advancingPlaybackDelta(1_000, 10, 2_000, 40, true)).toBe(0);
  });

  it("never attributes more than two seconds to one delayed browser update", () => {
    expect(advancingPlaybackDelta(1_000, 10, 11_000, 12, true)).toBe(2);
  });
});

describe("player interaction contracts", () => {
  it("keeps rapid opposite seeks anchored to the stable clock when media duration is transiently zero", () => {
    const forwarded = clampPlaybackTime(110, 0, 7_200);
    const rewound = clampPlaybackTime(forwarded - 10, 0, 7_200);

    expect(forwarded).toBe(110);
    expect(rewound).toBe(100);
    expect(clampPlaybackTime(850, 10, 7_200)).toBe(850);
    expect(clampPlaybackTime(-10, 0, 7_200)).toBe(0);
    expect(clampPlaybackTime(8_000, 0, 7_200)).toBe(7_200);
  });

  it("rejects transient zero and stale seek events while transport state is settling", () => {
    expect(shouldAcceptObservedPlaybackTime(0, 850, true, null)).toBe(false);
    expect(shouldAcceptObservedPlaybackTime(100, 110, false, 110)).toBe(false);
    expect(shouldAcceptObservedPlaybackTime(110, 110, false, 110)).toBe(true);
    expect(shouldAcceptObservedPlaybackTime(851, 850, true, null)).toBe(true);
  });

  it("merges rendition status without replacing the active transport ticket or URLs", () => {
    const active = {
      runId: "run-1",
      ticket: "active-ticket",
      ticketExpiresAt: 100,
      manifestUrl: "/manifest?ticket=active",
      progressiveUrl: "/progressive?ticket=active",
      renditions: [{ id: "video_480p", status: "preparing" }],
    } as unknown as PlaybackRunResponse;
    const refreshed = {
      ...active,
      ticket: "poll-ticket",
      ticketExpiresAt: 200,
      manifestUrl: "/manifest?ticket=poll",
      progressiveUrl: "/progressive?ticket=poll",
      renditions: [{ id: "video_480p", status: "ready" }],
    } as unknown as PlaybackRunResponse;

    const merged = mergePlaybackRunMetadata(active, refreshed);
    expect(merged.ticket).toBe("active-ticket");
    expect(merged.ticketExpiresAt).toBe(100);
    expect(merged.manifestUrl).toBe("/manifest?ticket=active");
    expect(merged.progressiveUrl).toBe("/progressive?ticket=active");
    expect(merged.renditions[0]?.status).toBe("ready");
  });

  it("auto-hides only during uninterrupted playback", () => {
    expect(shouldAutoHidePlayerControls("playing", false, false)).toBe(true);
    expect(shouldAutoHidePlayerControls("paused", false, false)).toBe(false);
    expect(shouldAutoHidePlayerControls("buffering", false, false)).toBe(true);
    expect(shouldAutoHidePlayerControls("recovering", false, false)).toBe(true);
    expect(shouldAutoHidePlayerControls("playing", true, false)).toBe(false);
    expect(shouldAutoHidePlayerControls("playing", false, true)).toBe(false);
  });

  it("keeps the complete runtime stable while an adaptive playlist grows", () => {
    expect(catalogDurationSeconds("1h 44m")).toBe(6240);
    expect(catalogDurationSeconds("104m")).toBe(6240);
    expect(catalogDurationSeconds("1:44")).toBe(6240);
    expect(authoritativePlaybackDuration(6240, "1h 44m", "hls", 272)).toBe(6240);
    expect(authoritativePlaybackDuration(0, "1h 44m", "hls", 272)).toBe(6240);
    expect(authoritativePlaybackDuration(0, "", "hls", 272)).toBe(0);
    expect(authoritativePlaybackDuration(0, "", "progressive", 272)).toBe(272);
  });

  it("shows the complete source-bounded ladder while marking unfinished levels", () => {
    const options = playbackQualityOptions([
      { id: "video_original", label: "1080p", height: 800, width: 1920, original: true, ready: true, status: "ready" },
      { id: "video_720p", label: "720p", height: 720, width: 1728, original: false, ready: true, status: "streamable" },
      { id: "video_480p", label: "480p", height: 480, width: 1152, original: false, ready: false, status: "preparing" },
      { id: "video_360p", label: "360p", height: 360, width: 864, original: false, ready: false, status: "preparing" },
      { id: "video_240p", label: "240p", height: 240, width: 576, original: false, ready: false, status: "failed" },
      { id: "video_144p", label: "144p", height: 144, width: 346, original: false, ready: false, status: "preparing" },
    ], [
      { height: 800, url: "/api/playback/hls/movie/video_original/playlist.m3u8" },
      { height: 612, url: "/api/playback/hls/movie/video_720p/playlist.m3u8" },
    ]);

    expect(options.map((item) => item.height)).toEqual(["auto", 800, 720, 480, 360, 240, 144]);
    expect(options.find((item) => item.id === "video_original")?.label).toBe("1080p · Original");
    expect(options.find((item) => item.id === "video_720p")?.ready).toBe(true);
    expect(options.find((item) => item.height === 144)?.ready).toBe(false);
    expect(options.find((item) => item.height === 720)?.index).toBe(1);
  });

  it("enables exactly the selected subtitle element by stable track id", () => {
    const video = document.createElement("video");
    const english = document.createElement("track");
    const spanish = document.createElement("track");
    const englishState = { mode: "disabled" };
    const spanishState = { mode: "disabled" };
    english.dataset.subtitleId = "en";
    spanish.dataset.subtitleId = "es";
    Object.defineProperty(english, "track", { value: englishState });
    Object.defineProperty(spanish, "track", { value: spanishState });
    video.append(english, spanish);

    applySubtitleTrackSelection(video, "en");
    expect(englishState.mode).toBe("showing");
    expect(spanishState.mode).toBe("disabled");
    applySubtitleTrackSelection(video, "es");
    expect(englishState.mode).toBe("disabled");
    expect(spanishState.mode).toBe("showing");
  });
});
