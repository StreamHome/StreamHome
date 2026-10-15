import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useProfileStore } from "../../stores/profileStore";
import * as moviesApi from "../../api/movies";
import * as playbackApi from "../../api/playback";
import { PlayerPage, STARTUP_QUALITY_MAX_WAIT_MS, type ResolvedPlayback } from "./PlayerPage";

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
    loadingEnabled = true;
    bufferingEnabled = true;
    audioTrack = 0;
    listeners = new Map<string, Array<(event: string, data: Record<string, unknown>) => void>>();
    immediateSwitch = vi.fn();
    nextSwitch = vi.fn();
    startLoad = vi.fn();
    resumeBuffering = vi.fn();
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
    loadSource() {
      this.emit(Events.MANIFEST_PARSED, { levels: this.levels });
      this.emit(Events.AUDIO_TRACKS_UPDATED, { audioTracks: [{ name: "English", lang: "en" }, { name: "Turkish", lang: "tr" }] });
    }
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
    tracks: [
      { id: "audio_en", label: "English", language: "en", channels: 2, default: true, source: "embedded", streamIndex: 0, ready: true, status: "ready" },
      { id: "audio_tr", label: "Turkish", language: "tr", channels: 2, default: false, source: "embedded", streamIndex: 1, ready: true, status: "ready" },
    ], subtitles: [],
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
    vi.spyOn(HTMLVideoElement.prototype, "videoHeight", "get").mockReturnValue(1040);
    vi.spyOn(HTMLVideoElement.prototype, "videoWidth", "get").mockReturnValue(1920);
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
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
    Object.defineProperty(video, "readyState", { configurable: true, value: 2 });
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

  it("resumes a healthy loader without restarting it and restarts only a stopped loader", async () => {
    const { hls } = await mountPlayer();
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    fireEvent.click(screen.getByRole("button", { name: "Play" }));
    expect(hls.startLoad).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    hls.loadingEnabled = false;
    fireEvent.click(screen.getByRole("button", { name: "Play" }));
    expect(hls.startLoad).toHaveBeenCalledTimes(1);
  });

  it("honors saved quality after a bounded warmup even below the bandwidth threshold", async () => {
    localStorage.setItem(preferencesKey, JSON.stringify({ qualityHeight: 1040 }));
    const { hls, video } = await mountPlayer();
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    vi.useFakeTimers();
    hls.bandwidthEstimate = 1_000_000;
    Object.defineProperty(video, "readyState", { configurable: true, value: 2 });
    fireEvent.loadedData(video);
    expect(hls.manualLevel).toBe(-1);
    act(() => vi.advanceTimersByTime(STARTUP_QUALITY_MAX_WAIT_MS));
    expect(hls.manualLevel).toBe(1);
    expect(hls.immediateSwitch).not.toHaveBeenCalled();
    expect(video.paused).toBe(true);
    expect(savedQuality()).toBe(1040);
  });

  it("restores quality with the short buffer available near the end of a title", async () => {
    localStorage.setItem(preferencesKey, JSON.stringify({ qualityHeight: 1040 }));
    const { hls, video } = await mountPlayer();
    hls.current = 0;
    video.currentTime = 114;
    Object.defineProperty(video, "readyState", { configurable: true, value: 2 });
    Object.defineProperty(video, "buffered", { configurable: true, value: { length: 1, start: () => 112, end: () => 120 } });
    fireEvent.loadedData(video);
    expect(hls.manualLevel).toBe(1);
    expect(hls.nextSwitch).toHaveBeenCalledExactlyOnceWith(1);
    expect(hls.immediateSwitch).not.toHaveBeenCalled();
  });

  it("cancels deferred quality on Auto and retains switching feedback until acknowledgement", async () => {
    localStorage.setItem(preferencesKey, JSON.stringify({ qualityHeight: 1040 }));
    const { hls, video } = await mountPlayer();
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    vi.useFakeTimers();
    hls.bandwidthEstimate = 1_000_000;
    Object.defineProperty(video, "readyState", { configurable: true, value: 2 });
    fireEvent.loadedData(video);
    selectQuality("Auto");
    act(() => vi.advanceTimersByTime(STARTUP_QUALITY_MAX_WAIT_MS));
    expect(hls.manualLevel).toBe(-1);
    hls.current = 0;
    Object.defineProperty(video, "videoHeight", { configurable: true, value: 720 });
    Object.defineProperty(video, "videoWidth", { configurable: true, value: 1328 });
    act(() => hls.emit(engine.Events.LEVEL_SWITCHED, { level: 0 }));
    selectQuality("1080p · Original");
    act(() => vi.advanceTimersByTime(5000));
    expect(screen.getByRole("button", { name: "Quality: 1080p · Original" }).getAttribute("aria-description")).toBe("Switching · Playing 720p");
    hls.current = 1;
    act(() => hls.emit(engine.Events.LEVEL_SWITCHED, { level: 1 }));
    expect(screen.getByRole("button", { name: "Quality: 1080p · Original" }).getAttribute("aria-description")).toBe("Switching · Playing 720p");
    Object.defineProperty(video, "videoHeight", { configurable: true, value: 1040 });
    Object.defineProperty(video, "videoWidth", { configurable: true, value: 1920 });
    fireEvent(video, new Event("resize"));
    expect(screen.getByRole("button", { name: "Quality: 1080p · Original" }).getAttribute("aria-description")).toBe("Playing 1080p · Original");
  });

  it("honors English -> Turkish -> English while the first audio switch is pending", async () => {
    const { hls } = await mountPlayer();
    const choose = (name: string) => {
      fireEvent.click(screen.getByRole("button", { name: /^Audio language:/ }));
      fireEvent.click(screen.getByRole("option", { name }));
    };
    choose("Turkish");
    expect(hls.audioTrack).toBe(1);
    choose("English");
    expect(hls.audioTrack).toBe(0);
    act(() => hls.emit(engine.Events.AUDIO_TRACK_SWITCHED, { id: 1 }));
    expect(JSON.parse(localStorage.getItem(preferencesKey)!).audioLanguage).toBe("en");
    act(() => hls.emit(engine.Events.AUDIO_TRACK_SWITCHED, { id: 0 }));
    act(() => hls.emit(engine.Events.AUDIO_TRACK_SWITCHED, { id: 1 }));
    expect(screen.getByRole("button", { name: "Audio language: English" })).toBeTruthy();
    expect(JSON.parse(localStorage.getItem(preferencesKey)!).audioLanguage).toBe("en");
  });

  it("keeps the latest audio preference when HLS publishes its tracks again", async () => {
    localStorage.setItem(preferencesKey, JSON.stringify({ audioTrackId: "audio_en", audioLanguage: "en" }));
    const { hls } = await mountPlayer();
    fireEvent.click(screen.getByRole("button", { name: /^Audio language:/ }));
    fireEvent.click(screen.getByRole("option", { name: "Turkish" }));
    act(() => hls.emit(engine.Events.AUDIO_TRACK_SWITCHED, { id: 1 }));
    act(() => hls.emit(engine.Events.AUDIO_TRACKS_UPDATED, { audioTracks: [{ name: "English", lang: "en" }, { name: "Turkish", lang: "tr" }] }));
    expect(hls.audioTrack).toBe(1);
    expect(JSON.parse(localStorage.getItem(preferencesKey)!).audioLanguage).toBe("tr");
  });

  it("keeps ordinary selection feedback off the picture and puts navigation above transport", async () => {
    const { container } = await mountPlayer();
    selectQuality("1080p · Original");
    const quality = screen.getByRole("button", { name: "Quality: 1080p · Original" });
    expect(quality.textContent).toBe("1080p");
    expect(container.querySelector(".player-error-notice")).toBeNull();
    expect(screen.getByText("1080p · Original quality active").classList.contains("sr-only")).toBe(true);
    expect(screen.getByRole("button", { name: "Exit player" }).closest("header")).toBe(
      screen.getByRole("heading", { name: "Quality regression" }).closest("header"),
    );
    expect(container.querySelector(".player-controls h1")).toBeNull();
    expect(container.querySelector(".player-controls__source")).toBeNull();
  });

  it("shows a real playback denial and clears it when Play retries without replacing transport", async () => {
    vi.mocked(HTMLMediaElement.prototype.play).mockRejectedValueOnce(new DOMException("Playback denied", "NotAllowedError"));
    const { video, hls } = await mountPlayer();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Press play");
    vi.useFakeTimers();
    act(() => vi.advanceTimersByTime(5000));
    expect(screen.getByRole("alert")).toBe(alert);
    vi.useRealTimers();
    const source = video.getAttribute("src");
    fireEvent.click(screen.getByRole("button", { name: "Play" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(video.getAttribute("src")).toBe(source);
    expect(hls.startLoad).not.toHaveBeenCalled();
  });
});
