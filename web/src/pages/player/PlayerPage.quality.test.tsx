import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useProfileStore } from "../../stores/profileStore";
import * as moviesApi from "../../api/movies";
import * as playbackApi from "../../api/playback";
import { PlayerPage, type ResolvedPlayback } from "./PlayerPage";

const engine = vi.hoisted(() => {
  const Events = Object.fromEntries([
    "MEDIA_ATTACHED", "MANIFEST_PARSED", "LEVEL_SWITCHING", "LEVEL_SWITCHED", "LEVEL_UPDATED",
    "AUDIO_TRACKS_UPDATED", "AUDIO_TRACK_SWITCHING", "AUDIO_TRACK_SWITCHED", "LEVEL_LOADED",
    "FRAG_LOADED", "FRAG_BUFFERED", "ERROR",
  ].map((event) => [event, event]));
  class Hls {
    static Events = Events;
    static isSupported = () => true;
    static instances: Hls[] = [];
    static ErrorTypes = { NETWORK_ERROR: "network", MEDIA_ERROR: "media" };
    levels = [
      { height: 720, bitrate: 2_000_000, url: "/video_720p/playlist.m3u8" },
      { height: 1040, bitrate: 4_000_000, url: "/video_original/playlist.m3u8" },
    ];
    current = 1;
    manualLevel = -1;
    bandwidthEstimate = 10_000_000;
    listeners = new Map<string, Array<(event: string, data: Record<string, unknown>) => void>>();
    immediateSwitch = vi.fn();
    nextSwitch = vi.fn();
    startLoad = vi.fn();
    destroy = vi.fn(() => this.listeners.clear());
    constructor() { Hls.instances.push(this); }
    get currentLevel() { return this.current; }
    set currentLevel(level: number) { this.manualLevel = level; this.immediateSwitch(level); }
    set nextLevel(level: number) { this.manualLevel = level; this.nextSwitch(level); }
    set loadLevel(level: number) { this.manualLevel = level; }
    on(event: string, listener: (event: string, data: Record<string, unknown>) => void) {
      this.listeners.set(event, [...this.listeners.get(event) ?? [], listener]);
    }
    emit(event: string, data: Record<string, unknown> = {}) {
      for (const listener of this.listeners.get(event) ?? []) listener(event, data);
    }
    attachMedia(video: HTMLVideoElement) {
      video.src = "blob:quality-regression";
      queueMicrotask(() => this.emit(Events.MEDIA_ATTACHED));
    }
    loadSource() { this.emit(Events.MANIFEST_PARSED, { levels: this.levels }); }
  }
  return { Hls, Events };
});

vi.mock("hls.js", () => ({ default: engine.Hls }));

const profile = { id: "quality-regression", name: "Quality regression", avatarColor: "orange", theme: "ember", pinEnabled: false };
const preferencesKey = `streamhome_player_preferences_${profile.id}`;
const playback: ResolvedPlayback = {
  asset: { id: "quality-media", movieId: "quality-media", title: "Quality regression", subtitle: "", durationLabel: "2m", skipMarkers: {} },
  episodeSequence: [],
  runResponse: {
    runId: "quality-run", mediaId: "quality-media", movieId: "quality-media", episodeId: null,
    sourceFingerprint: "quality-source", resumePosition: 0,
    sourceMetadata: { duration: 120, container: "mp4", codec: "hevc", width: 1920, height: 1040, frameRate: 24, sourceFormat: "MP4", progressiveCompatible: false },
    tracks: [], subtitles: [],
    renditions: [
      { id: "video_original", label: "1080p", height: 1040, width: 1920, original: true, ready: true, status: "ready" },
      { id: "video_720p", label: "720p", height: 720, width: 1328, original: false, ready: true, status: "ready" },
    ],
    ticket: "quality-fixture-ticket", ticketExpiresAt: 4_102_444_800,
    manifestUrl: "/quality-master.m3u8", progressiveUrl: "", nextEpisodeId: null,
    preparationState: "ready", preparationError: null,
    preparationProgress: { stage: "streamable", queuePosition: 0, readySegments: 30, activeWorkers: 0 },
    seekableUntil: 120, resumeReady: true, switchingReady: true, fullyPrepared: true, nextSequenceNumber: 1,
  },
};

async function mountPlayer() {
  const view = render(<MemoryRouter><PlayerPage visualFixture={playback} /></MemoryRouter>);
  await waitFor(() => expect(engine.Hls.instances).toHaveLength(1));
  await waitFor(() => expect(engine.Hls.instances[0].listeners.has(engine.Events.LEVEL_SWITCHED)).toBe(true));
  return { ...view, hls: engine.Hls.instances[0], video: view.container.querySelector("video")! };
}

function selectQuality(name: string) {
  fireEvent.click(screen.getByRole("button", { name: /^Quality:/ }));
  fireEvent.click(screen.getByRole("option", { name }));
}

function savedQuality() { return JSON.parse(localStorage.getItem(preferencesKey)!).qualityHeight; }

describe("mounted HLS quality policy", () => {
  beforeEach(() => {
    engine.Hls.instances = [];
    localStorage.removeItem(preferencesKey);
    useProfileStore.setState({ profiles: [profile], activeProfile: profile, isAdmin: false });
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  });
  afterEach(() => {
    cleanup();
    localStorage.removeItem(preferencesKey);
    useProfileStore.setState({ profiles: [], activeProfile: null, isAdmin: false });
    vi.restoreAllMocks();
  });

  it("pins Auto's already playing 1080p immediately without a decoded-level event or buffer flush", async () => {
    const { hls, video } = await mountPlayer();
    const source = video.getAttribute("src");
    const loads = vi.mocked(video.load).mock.calls.length;
    selectQuality("1080p · Original");
    expect(screen.getByRole("button", { name: "Quality: 1080p · Original" })).toBeTruthy();
    expect(savedQuality()).toBe(1040);
    expect(hls.manualLevel).toBe(1);
    expect(hls.immediateSwitch).not.toHaveBeenCalled();
    expect(hls.nextSwitch).not.toHaveBeenCalled();
    expect(video.getAttribute("src")).toBe(source);
    expect(vi.mocked(video.load).mock.calls).toHaveLength(loads);
  });

  it("keeps a paused manual selection through pending and late decoded-level events", async () => {
    const { hls, video } = await mountPlayer();
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    video.currentTime = 102;
    const source = video.getAttribute("src");
    const plays = vi.mocked(video.play).mock.calls.length;
    const loads = vi.mocked(video.load).mock.calls.length;
    selectQuality("720p");
    selectQuality("720p");
    expect(savedQuality()).toBe(720);
    expect(hls.nextSwitch).toHaveBeenCalledExactlyOnceWith(0);
    act(() => hls.emit(engine.Events.LEVEL_SWITCHED, { level: 1 }));
    expect(screen.getByRole("button", { name: "Quality: 720p" })).toBeTruthy();
    act(() => hls.emit(engine.Events.LEVEL_SWITCHED, { level: 0 }));
    act(() => hls.emit(engine.Events.LEVEL_SWITCHED, { level: 1 }));
    expect(screen.getByRole("button", { name: "Quality: 720p" })).toBeTruthy();
    expect(savedQuality()).toBe(720);
    expect(video.paused).toBe(true);
    expect(video.currentTime).toBe(102);
    expect(video.getAttribute("src")).toBe(source);
    expect(vi.mocked(video.play).mock.calls).toHaveLength(plays);
    expect(vi.mocked(video.load).mock.calls).toHaveLength(loads);
    expect(hls.immediateSwitch).not.toHaveBeenCalled();
  });

  it("retains only the latest rapid selection and releases Auto without flushing", async () => {
    const { hls } = await mountPlayer();
    selectQuality("720p");
    selectQuality("1080p · Original");
    act(() => hls.emit(engine.Events.LEVEL_SWITCHED, { level: 0 }));
    expect(savedQuality()).toBe(1040);
    expect(screen.getByRole("button", { name: "Quality: 1080p · Original" })).toBeTruthy();
    selectQuality("Auto");
    act(() => hls.emit(engine.Events.LEVEL_SWITCHED, { level: 1 }));
    expect(savedQuality()).toBe("auto");
    expect(hls.manualLevel).toBe(-1);
    expect(screen.getByRole("button", { name: "Quality: Auto" })).toBeTruthy();
    expect(hls.immediateSwitch).not.toHaveBeenCalled();
    expect(hls.nextSwitch).toHaveBeenCalledTimes(1);
  });

  it("restores the saved policy while fast startup warms up, even without a new level event", async () => {
    const first = await mountPlayer();
    selectQuality("1080p · Original");
    first.unmount();
    engine.Hls.instances = [];
    const { hls, video } = await mountPlayer();
    expect(screen.getByRole("button", { name: "Quality: 1080p · Original" })).toBeTruthy();
    expect(savedQuality()).toBe(1040);
    expect(hls.manualLevel).toBe(-1);
    act(() => hls.emit(engine.Events.LEVEL_SWITCHED, { level: 0 }));
    expect(savedQuality()).toBe(1040);
    expect(screen.getByRole("button", { name: "Quality: 1080p · Original" })).toBeTruthy();
    Object.defineProperty(video, "buffered", { configurable: true, value: { length: 1, start: () => 0, end: () => 30 } });
    act(() => hls.emit(engine.Events.FRAG_BUFFERED));
    expect(hls.manualLevel).toBe(1);
    expect(hls.immediateSwitch).not.toHaveBeenCalled();
    expect(hls.nextSwitch).not.toHaveBeenCalled();
    expect(savedQuality()).toBe(1040);
  });

  it("keeps manual quality and the paused mounted transport through credential renewal", async () => {
    const run = { ...playback.runResponse, streamBaseUrl: "/api/playback/streams/quality-run", ticketExpiresAt: Math.ceil(Date.now() / 1000) + 182 };
    vi.spyOn(moviesApi, "getMovie").mockReturnValue(new Promise(() => undefined));
    vi.spyOn(playbackApi, "createPlaybackRun").mockResolvedValue(run);
    const renew = vi.spyOn(playbackApi, "getPlaybackRun").mockResolvedValue({ ...run, ticket: "renewed-fixture-ticket", ticketExpiresAt: run.ticketExpiresAt + 900 });
    vi.spyOn(playbackApi, "closePlaybackRun").mockResolvedValue({ status: "abandoned", nextSequenceNumber: 2 });
    const timers = vi.spyOn(window, "setTimeout");
    const view = render(<MemoryRouter initialEntries={["/?profile=quality-regression&view=watch&media=m_quality"]}><PlayerPage /></MemoryRouter>);
    await waitFor(() => expect(engine.Hls.instances).toHaveLength(1));
    await waitFor(() => expect(engine.Hls.instances[0].listeners.has(engine.Events.LEVEL_SWITCHED)).toBe(true));
    const hls = engine.Hls.instances[0];
    const video = view.container.querySelector("video")!;
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    selectQuality("1080p · Original");
    const loads = vi.mocked(video.load).mock.calls.length;
    const source = video.getAttribute("src");
    const renewal = timers.mock.calls.find(([, delay]) => Number(delay) === 30_000);
    expect(renewal).toBeDefined();
    await act(async () => { (renewal![0] as () => void)(); await Promise.resolve(); });
    expect(renew).toHaveBeenCalledWith("quality-run");
    expect(engine.Hls.instances).toHaveLength(1);
    expect(hls.destroy).not.toHaveBeenCalled();
    expect(video.getAttribute("src")).toBe(source);
    expect(vi.mocked(video.load).mock.calls).toHaveLength(loads);
    expect(savedQuality()).toBe(1040);
    expect(screen.getByRole("button", { name: "Quality: 1080p · Original" })).toBeTruthy();
    expect(view.container.querySelector("[data-player-root]")?.getAttribute("data-player-phase")).toBe("paused");
  });
});
