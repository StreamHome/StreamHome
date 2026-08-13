import { describe, expect, it } from "vitest";
import { ApiError } from "../../api/client";
import type { Episode, PlaybackRunResponse } from "../../types/api";
import {
  advancingPlaybackDelta,
  adaptiveTransportIsGrowing,
  applySubtitleTrackSelection,
  authoritativePlaybackDuration,
  bufferedEndForTime,
  canUseProgressiveCompatibility,
  canUseProgressivePlayback,
  catalogDurationSeconds,
  clampGrowingPlaybackTime,
  clampPlaybackTime,
  externalAudioSyncPlan,
  isMeaningfulPointerActivity,
  initialPlaybackMode,
  isPlaybackTimeSeekable,
  matchAudioTrackIndexes,
  playbackTransportIsReady,
  mergePlaybackRunMetadata,
  nextPlayableEpisode,
  preparationStatusMessage,
  playbackQualityOptions,
  playbackProgressFailureAction,
  playbackStartupFailureMessage,
  progressiveAudioTrack,
  progressSequenceWasAccepted,
  shouldAutoHidePlayerControls,
  shouldAcceptObservedPlaybackTime,
  shouldApplyDeferredStartupQuality,
  shouldExtendPlaybackStartup,
  shouldRetryPlaybackStall,
  shouldRetryPlaybackStartup,
  shouldResumePlaybackAfterTransport,
} from "./PlayerPage";

describe("adaptive preparation status", () => {
  it("reports queue position and fast packaging progress truthfully", () => {
    expect(preparationStatusMessage({ stage: "queued", queuePosition: 3, readySegments: 0, activeWorkers: 2 }))
      .toBe("Playback preparation is queued at position 3.");
    expect(preparationStatusMessage({ stage: "packaging", queuePosition: 0, readySegments: 0, activeWorkers: 1 }))
      .toContain("without re-encoding");
    expect(preparationStatusMessage({ stage: "packaging", queuePosition: 0, readySegments: 2, activeWorkers: 1 }))
      .toContain("2 segments ready");
    expect(preparationStatusMessage({ stage: "audio", queuePosition: 0, readySegments: 2, activeWorkers: 1 }))
      .toContain("default audio track");
  });
});

describe("playback startup diagnostics", () => {
  it("names the failed adaptive stage and preserves the HLS response detail", () => {
    expect(playbackStartupFailureMessage("hls", "fragment-loaded", {
      type: "networkError",
      detail: "fragLoadError",
      httpStatus: 404,
    })).toBe(
      "Adaptive playback stopped during fragment-loaded. HTTP 404. HLS reported fragLoadError. Retry this title after checking the playback startup entry in backend.log.",
    );
  });

  it("explains a direct-source compatibility failure without calling it an adaptive error", () => {
    expect(playbackStartupFailureMessage("progressive", "direct-metadata", null)).toContain(
      "source container or selected audio codec",
    );
  });
});

describe("bandwidth-aware adaptive startup", () => {
  it("waits for both measured headroom and a safe forward buffer before restoring saved quality", () => {
    expect(shouldApplyDeferredStartupQuality(4_000_000, 5_000_000, 12)).toBe(false);
    expect(shouldApplyDeferredStartupQuality(4_000_000, 6_000_000, 4)).toBe(false);
    expect(shouldApplyDeferredStartupQuality(4_000_000, 6_000_000, 12)).toBe(true);
  });

  it("finds the buffer edge that contains the current playback position", () => {
    const ranges = {
      length: 2,
      start: (index: number) => [0, 30][index],
      end: (index: number) => [12, 50][index],
    };
    expect(bufferedEndForTime(ranges, 7)).toBe(12);
    expect(bufferedEndForTime(ranges, 35)).toBe(50);
    expect(bufferedEndForTime(ranges, 20)).toBe(20);
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

  it("requires an adaptive resume target to be inside a real seekable range", () => {
    const ranges = {
      length: 2,
      start: (index: number) => index === 0 ? 0 : 120,
      end: (index: number) => index === 0 ? 30 : 180,
    };
    expect(isPlaybackTimeSeekable(ranges, 20)).toBe(true);
    expect(isPlaybackTimeSeekable(ranges, 90)).toBe(false);
    expect(isPlaybackTimeSeekable(ranges, 150)).toBe(true);
  });

  it("keeps rapid seeks behind the published edge of a growing stream", () => {
    const ranges = {
      length: 1,
      end: () => 92,
    };
    expect(clampGrowingPlaybackTime(50, ranges, 96)).toBe(50);
    expect(clampGrowingPlaybackTime(100, ranges, 96)).toBe(91.5);
    expect(clampGrowingPlaybackTime(100, { length: 1, end: () => 110 }, 96)).toBe(95.5);
  });

  it("uses progressive seeking only for browser-compatible source media", () => {
    const metadata = { duration: 6_000, container: "mov,mp4,m4a", codec: "h264", width: 1920, height: 1080, frameRate: 24, sourceFormat: "MP4" };
    expect(canUseProgressiveCompatibility(metadata)).toBe(true);
    expect(canUseProgressiveCompatibility({ ...metadata, codec: "hevc" })).toBe(false);
    expect(canUseProgressiveCompatibility(
      { ...metadata, codec: "hevc" },
      { canPlayType: (type: string) => type.includes("hvc1") ? "probably" : "" },
    )).toBe(true);
    expect(canUseProgressiveCompatibility({ ...metadata, audioCodec: "dts" })).toBe(false);
    expect(canUseProgressiveCompatibility({ ...metadata, container: "matroska", sourceFormat: "MKV" })).toBe(false);
  });

  it("uses progressive playback for embedded audio and direct external dubbing", () => {
    const tracks = [
      { id: "audio_0_en", label: "English", language: "en", channels: 2, default: true, source: "embedded", streamIndex: 0, ready: true, status: "ready" },
      { id: "audio_0_tr", label: "Turkish", language: "tr", channels: 2, default: false, source: "external", streamIndex: 0, directUrl: "/api/playback/source/m_media?ticket=value&source_id=audio_0_tr", ready: true, status: "ready" },
    ] as const;
    expect(progressiveAudioTrack([...tracks], "", "")?.id).toBe("audio_0_en");
    expect(progressiveAudioTrack([...tracks], "audio_0_en", "en")?.id).toBe("audio_0_en");
    expect(progressiveAudioTrack([...tracks], "audio_0_tr", "tr")?.id).toBe("audio_0_tr");
    const multipleEmbedded = [
      tracks[0],
      { ...tracks[0], id: "audio_1_es", label: "Spanish", language: "es", streamIndex: 1, default: false },
    ];
    const response = {
      progressiveUrl: "/api/playback/progressive/m_media?ticket=value",
      sourceMetadata: { duration: 120, container: "mov,mp4", codec: "h264", width: 1280, height: 720, frameRate: 24, sourceFormat: "MP4" },
      tracks: multipleEmbedded,
    } as unknown as PlaybackRunResponse;
    expect(canUseProgressivePlayback(response, "audio_1_es", "es")).toBe(false);
    expect(canUseProgressivePlayback(response, "audio_0_en", "en")).toBe(true);
  });

  it("allows progressive startup without audio metadata but preserves external dubbing preferences", () => {
    const base = {
      progressiveUrl: "/api/playback/progressive/m_media?ticket=value",
      sourceMetadata: { duration: 120, container: "mov,mp4", codec: "h264", width: 1280, height: 720, frameRate: 24, sourceFormat: "MP4" },
      tracks: [],
    } as unknown as PlaybackRunResponse;
    expect(canUseProgressivePlayback(base, "", "")).toBe(true);
    expect(canUseProgressivePlayback({
      ...base,
      tracks: [{ id: "audio_0_tr", label: "Turkish", language: "tr", channels: 2, default: true, source: "external", streamIndex: 0, directUrl: "/api/playback/source/m_media?ticket=value&source_id=audio_0_tr", ready: true, status: "ready" }],
    }, "audio_0_tr", "tr")).toBe(true);
    const externalReady = {
      ...base,
      manifestUrl: "/api/playback/manifest/m_media?ticket=value",
      preparationState: "ready",
      tracks: [{ id: "audio_0_tr", label: "Turkish", language: "tr", channels: 2, default: true, source: "external", streamIndex: 0, directUrl: "/api/playback/source/m_media?ticket=value&source_id=audio_0_tr", ready: true, status: "ready" }],
    } as unknown as PlaybackRunResponse;
    expect(initialPlaybackMode(externalReady, "audio_0_tr", "tr")).toBe("hls");
    expect(initialPlaybackMode({ ...externalReady, manifestUrl: null }, "audio_0_tr", "tr")).toBe("progressive");
    expect(initialPlaybackMode({ ...externalReady, preparationState: "preparing" }, "audio_0_tr", "tr")).toBe("progressive");
  });

  it("recognizes growing HLS and extends startup only while bounded progress continues", () => {
    const response = {
      renditions: [{ id: "video_480p", status: "streamable" }],
      tracks: [],
    } as unknown as PlaybackRunResponse;
    expect(adaptiveTransportIsGrowing(response)).toBe(true);
    expect(shouldExtendPlaybackStartup(15_000, 0, 10_000)).toBe(true);
    expect(shouldExtendPlaybackStartup(31_000, 0, 30_000)).toBe(false);
    expect(shouldExtendPlaybackStartup(20_000, 0, 1_000)).toBe(false);
  });

  it("mounts direct playback immediately and adaptive playback as soon as its manifest exists", () => {
    const available = {
      preparationState: "ready",
      progressiveUrl: "/api/playback/progressive/m_media?ticket=value",
      manifestUrl: "/api/playback/manifest/m_media?ticket=value",
    } as const;
    expect(playbackTransportIsReady(available, "hls")).toBe(true);
    expect(playbackTransportIsReady(available, "progressive")).toBe(true);
    expect(playbackTransportIsReady({ ...available, manifestUrl: null }, "hls")).toBe(false);
    expect(playbackTransportIsReady({ ...available, preparationState: "preparing" }, "progressive")).toBe(true);
  });

  it("waits for adaptive playback when the protected source is not browser compatible", () => {
    const response = {
      progressiveUrl: "/api/playback/progressive/m_media?ticket=value",
      manifestUrl: null,
      sourceMetadata: { duration: 120, container: "matroska", codec: "hevc", width: 1280, height: 720, frameRate: 24, sourceFormat: "MKV" },
      tracks: [],
    } as unknown as PlaybackRunResponse;
    expect(initialPlaybackMode(response, "", "")).toBe("hls");
    expect(initialPlaybackMode({ ...response, manifestUrl: "/api/playback/manifest/m_media?ticket=value" }, "", "")).toBe("hls");
  });

  it("maps same-language audio tracks by stable rendition identity without reusing an index", () => {
    const indexes = matchAudioTrackIndexes([
      { id: "audio_0_en", language: "en", label: "English main" },
      { id: "audio_1_en", language: "en", label: "English commentary" },
    ], [
      { lang: "en", name: "English commentary", url: "/hls/audio_1_en/playlist.m3u8" },
      { lang: "en", name: "English main", url: "/hls/audio_0_en/playlist.m3u8" },
    ]);
    expect(indexes).toEqual([1, 0]);
  });

  it("distinguishes an accepted progress request from one that must be retried", () => {
    expect(progressSequenceWasAccepted(4, 5)).toBe(true);
    expect(progressSequenceWasAccepted(4, 4)).toBe(false);
    expect(playbackProgressFailureAction(new ApiError("Sequence conflict", 409, "PLAYBACK_SEQUENCE_MISMATCH"))).toBe("reconcile");
    expect(playbackProgressFailureAction(new ApiError("Run expired", 410, "PLAYBACK_RUN_EXPIRED"))).toBe("stop");
    expect(playbackProgressFailureAction(new ApiError("Source changed", 409, "PLAYBACK_SOURCE_CHANGED"))).toBe("stop");
    expect(playbackProgressFailureAction(new ApiError("Offline", 0, "server_unreachable"))).toBe("retry-later");
  });

  it("merges rendition status without replacing the active transport ticket or URLs", () => {
    const active = {
      runId: "run-1",
      sourceFingerprint: "fingerprint-a",
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

  it("replaces transport tickets and URLs when the source fingerprint changes", () => {
    const active = {
      runId: "run-1",
      sourceFingerprint: "fingerprint-a",
      ticket: "active-ticket",
      ticketExpiresAt: 100,
      manifestUrl: "/manifest?ticket=active",
      progressiveUrl: "/progressive?ticket=active",
    } as unknown as PlaybackRunResponse;
    const refreshed = {
      ...active,
      sourceFingerprint: "fingerprint-b",
      ticket: "replacement-ticket",
      ticketExpiresAt: 200,
      manifestUrl: "/manifest?ticket=replacement",
      progressiveUrl: "/progressive?ticket=replacement",
    };

    expect(mergePlaybackRunMetadata(active, refreshed)).toEqual(refreshed);
  });

  it("accepts a newly ready manifest instead of preserving an initial null URL", () => {
    const active = {
      runId: "run-1",
      sourceFingerprint: "fingerprint-a",
      ticket: "initial-ticket",
      ticketExpiresAt: 100,
      manifestUrl: null,
      progressiveUrl: "/progressive?ticket=initial",
    } as unknown as PlaybackRunResponse;
    const refreshed = {
      ...active,
      ticket: "ready-ticket",
      ticketExpiresAt: 200,
      manifestUrl: "/manifest?ticket=ready",
      progressiveUrl: "/progressive?ticket=ready",
    };

    expect(mergePlaybackRunMetadata(active, refreshed)).toEqual(refreshed);
  });

  it("auto-hides after inactivity unless a menu or scrub interaction is active", () => {
    expect(shouldAutoHidePlayerControls("playing", false, false)).toBe(true);
    expect(shouldAutoHidePlayerControls("paused", false, false)).toBe(false);
    expect(shouldAutoHidePlayerControls("buffering", false, false)).toBe(true);
    expect(shouldAutoHidePlayerControls("recovering", false, false)).toBe(true);
    expect(shouldAutoHidePlayerControls("playing", true, false)).toBe(false);
    expect(shouldAutoHidePlayerControls("playing", false, true)).toBe(false);
  });

  it("ignores stationary synthetic pointer events when deciding whether to reveal controls", () => {
    expect(isMeaningfulPointerActivity(null, { x: 10, y: 10 }, 0, 0)).toBe(false);
    expect(isMeaningfulPointerActivity({ x: 10, y: 10 }, { x: 10, y: 10 }, 0, 0)).toBe(false);
    expect(isMeaningfulPointerActivity({ x: 10, y: 10 }, { x: 10, y: 10 }, 1, 0)).toBe(true);
    expect(isMeaningfulPointerActivity({ x: 10, y: 10 }, { x: 11, y: 10 }, 0, 0)).toBe(true);
  });

  it("allows exactly one bounded playback-startup recovery attempt", () => {
    expect(shouldRetryPlaybackStartup(0)).toBe(true);
    expect(shouldRetryPlaybackStartup(1)).toBe(false);
    expect(shouldRetryPlaybackStartup(2)).toBe(false);
  });

  it("keeps forward-buffer stall recovery bounded and requires active play intent", () => {
    expect(shouldRetryPlaybackStall(true, HTMLMediaElement.HAVE_CURRENT_DATA, 0)).toBe(true);
    expect(shouldRetryPlaybackStall(true, HTMLMediaElement.HAVE_CURRENT_DATA, 1)).toBe(true);
    expect(shouldRetryPlaybackStall(true, HTMLMediaElement.HAVE_CURRENT_DATA, 2)).toBe(false);
    expect(shouldRetryPlaybackStall(false, HTMLMediaElement.HAVE_CURRENT_DATA, 0)).toBe(false);
    expect(shouldRetryPlaybackStall(true, HTMLMediaElement.HAVE_FUTURE_DATA, 0)).toBe(false);
  });

  it("keeps external audio at the selected speed and reserves hard seeks for explicit synchronization", () => {
    expect(externalAudioSyncPlan(30, 30.05, 1)).toEqual({ seekTime: null, playbackRate: 1 });
    expect(externalAudioSyncPlan(30, 29.95, 1)).toEqual({ seekTime: null, playbackRate: 1 });
    expect(externalAudioSyncPlan(30, 29.7, 1)).toEqual({ seekTime: null, playbackRate: 1 });
    expect(externalAudioSyncPlan(30, 30.3, 1)).toEqual({ seekTime: null, playbackRate: 1 });
    expect(externalAudioSyncPlan(30, 29.2, 1)).toEqual({ seekTime: null, playbackRate: 1 });
    expect(externalAudioSyncPlan(30, 30.8, 1)).toEqual({ seekTime: null, playbackRate: 1 });
    expect(externalAudioSyncPlan(30, 28.5, 1)).toEqual({ seekTime: null, playbackRate: 1 });
    expect(externalAudioSyncPlan(30, 30.05, 1, true)).toEqual({ seekTime: 30, playbackRate: 1 });
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

  it("shows every source capability while transport indexes are attached independently", () => {
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
    expect(options.find((item) => item.height === 240)?.status).toBe("failed");
    expect(options.find((item) => item.height === 720)?.index).toBe(1);
  });

  it("never resumes a paused or completed player after transport replacement", () => {
    expect(shouldResumePlaybackAfterTransport(true, false)).toBe(true);
    expect(shouldResumePlaybackAfterTransport(false, false)).toBe(false);
    expect(shouldResumePlaybackAfterTransport(true, true)).toBe(false);
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
