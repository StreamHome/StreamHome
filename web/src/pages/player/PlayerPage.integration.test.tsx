import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import type { Profile } from "../../types/api";
import * as moviesApi from "../../api/movies";
import * as playbackApi from "../../api/playback";
import { getPlaybackSessionRevision, subscribePlaybackSessions } from "../../api/playbackSessionEvents";
import { PLAYER_SKIP_COUNTDOWN_DELAY_MS, PLAYER_SKIP_COUNTDOWN_DURATION_MS } from "./PlayerMarkerControls";
import { MOBILE_SEEK_CONTROLS_GRACE_MS, MOBILE_SEEK_FEEDBACK_MS } from "./mobilePlayer";
import {
  PLAYER_CONTROLS_IDLE_MS,
  PLAYBACK_CLOCK_ADVANCE_TIMEOUT_MS,
  PlayerPage,
  type ResolvedPlayback,
} from "./PlayerPage";


const profile: Profile = {
  id: "mounted-player-profile",
  name: "Mounted Player",
  avatarColor: "from-orange-600 to-red-700",
  theme: "ember",
  pinEnabled: false,
};

const playback: ResolvedPlayback = {
  asset: {
    id: "mounted-player-media",
    movieId: "mounted-player-media",
    title: "Mounted player regression",
    subtitle: "",
    durationLabel: "2m",
    skipMarkers: {},
  },
  episodeSequence: [],
  runResponse: {
    runId: "mounted-player-run",
    mediaId: "mounted-player-media",
    movieId: "mounted-player-media",
    episodeId: null,
    sourceFingerprint: "mounted-player-fingerprint",
    resumePosition: 12,
    sourceMetadata: {
      duration: 120,
      container: "mov,mp4,m4a,3gp,3g2,mj2",
      codec: "h264",
      width: 640,
      height: 360,
      frameRate: 24,
      sourceFormat: "MP4",
      progressiveCompatible: true,
    },
    tracks: [
      {
        id: "audio_0_en",
        label: "English",
        language: "en",
        channels: 2,
        default: true,
        source: "embedded",
        streamIndex: 0,
        ready: true,
        status: "ready",
      },
    ],
    renditions: [
      {
        id: "video_original",
        label: "360p",
        height: 360,
        width: 640,
        original: true,
        ready: true,
        status: "ready",
      },
    ],
    subtitles: [],
    ticket: "mounted-player-ticket",
    ticketExpiresAt: 4_102_444_800,
    manifestUrl: null,
    progressiveUrl: "/api/playback/progressive/mounted-player-media?ticket=mounted-player-ticket",
    nextEpisodeId: null,
    preparationState: "ready",
    preparationError: null,
    preparationProgress: {
      stage: "streamable",
      queuePosition: 0,
      readySegments: 1,
      activeWorkers: 0,
    },
    seekableUntil: 120,
    resumeReady: true,
    switchingReady: true,
    fullyPrepared: true,
    nextSequenceNumber: 1,
  },
};

const playerPreferencesKey = `streamhome_player_preferences_${profile.id}`;

const markerPlayback: ResolvedPlayback = {
  ...playback,
  asset: {
    ...playback.asset,
    skipMarkers: {
      intro: [{ start: 10, end: 20 }],
      credits: [{ start: 90, end: 100 }, { start: 110, end: 120 }],
    },
  },
};

function pointerClick(target: HTMLElement) {
  fireEvent.pointerDown(target, { pointerType: "mouse", pointerId: 1 });
  act(() => target.focus());
  fireEvent.pointerUp(target, { pointerType: "mouse", pointerId: 1 });
  fireEvent.click(target, { detail: 1 });
}

function PlayerLocation() {
  return <output data-testid="player-location">{useLocation().search}</output>;
}

function OpenNextTitle() {
  const navigate = useNavigate();
  return <button type="button" onClick={() => navigate("/?profile=mounted-player-profile&view=watch&media=m_next_ack")}>Open next title</button>;
}

function pressFocusedKey(key: string, init: KeyboardEventInit = {}) {
  const down = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  act(() => { fireEvent(document.activeElement ?? document.body, down); });
  const up = new KeyboardEvent("keyup", { key, bubbles: true, cancelable: true, ...init });
  act(() => { fireEvent(document.activeElement ?? document.body, up); });
  return { down, up };
}

function videoCallCount(method: "play" | "pause" | "load", video: HTMLVideoElement) {
  return vi.mocked(HTMLMediaElement.prototype[method]).mock.contexts.filter((element) => element === video).length;
}

function observeVideoPosition(video: HTMLVideoElement) {
  let position = video.currentTime;
  const writes = vi.fn((value: number) => { position = value; });
  Object.defineProperty(video, "currentTime", { configurable: true, get: () => position, set: writes });
  return writes;
}

function timelinePointer(timeline: HTMLInputElement, type: string, clientX: number) {
  const event = new MouseEvent(type, { bubbles: true, clientX });
  Object.defineProperty(event, "pointerId", { value: 1 });
  fireEvent(timeline, event);
}

function phoneViewport() {
  const width = Object.getOwnPropertyDescriptor(window, "innerWidth")!;
  const height = Object.getOwnPropertyDescriptor(window, "innerHeight")!;
  const touchPoints = Object.getOwnPropertyDescriptor(navigator, "maxTouchPoints");
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 844 });
  Object.defineProperty(window, "innerHeight", { configurable: true, value: 390 });
  Object.defineProperty(navigator, "maxTouchPoints", { configurable: true, value: 1 });
  return () => {
    Object.defineProperty(window, "innerWidth", width);
    Object.defineProperty(window, "innerHeight", height);
    if (touchPoints) Object.defineProperty(navigator, "maxTouchPoints", touchPoints);
    else Reflect.deleteProperty(navigator, "maxTouchPoints");
  };
}

function mobileTap(zone: HTMLElement, cancelled = false, distance = 0) {
  for (const type of ["pointerdown", cancelled ? "pointercancel" : "pointerup"]) {
    const event = new MouseEvent(type, { bubbles: true, clientX: 100 + (type === "pointerup" ? distance : 0), clientY: 80 });
    Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: "touch" } });
    fireEvent(zone, event);
  }
}

function showPlayingVideo(video: HTMLVideoElement, time = 30) {
  Object.defineProperties(video, {
    duration: { configurable: true, value: 120 },
    readyState: { configurable: true, value: HTMLMediaElement.HAVE_FUTURE_DATA },
    paused: { configurable: true, value: false },
    currentTime: { configurable: true, writable: true, value: 0 },
  });
  fireEvent.loadedMetadata(video);
  fireEvent.seeked(video);
  fireEvent.play(video);
  fireEvent.playing(video);
  video.currentTime = time;
  fireEvent.timeUpdate(video);
}

function frameCanvasHarness() {
  const pixels = new WeakMap<HTMLCanvasElement, string>();
  const contexts = new WeakMap<HTMLCanvasElement, CanvasRenderingContext2D>();
  let presented = "initial picture";
  let rejectCapture = false;
  const draws = vi.fn();
  const publishedMutations = vi.fn();
  const recordMutation = (canvas: HTMLCanvasElement, operation: string) => {
    if (canvas.dataset.frameActive === "true" && canvas.closest("[data-frame-hold='true']")) publishedMutations(canvas, operation);
  };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(function (this: HTMLCanvasElement) {
    let context = contexts.get(this);
    if (!context) {
      const canvas = this;
      context = {
        drawImage: (source: CanvasImageSource) => {
          draws(canvas, source);
          if (source instanceof HTMLVideoElement && rejectCapture) throw new DOMException("Frame unavailable", "InvalidStateError");
          recordMutation(canvas, "draw");
          const picture = source instanceof HTMLCanvasElement ? pixels.get(source) : presented;
          if (picture === undefined) pixels.delete(canvas);
          else pixels.set(canvas, picture);
        },
        clearRect: () => pixels.delete(canvas),
      } as unknown as CanvasRenderingContext2D;
      contexts.set(canvas, context);
    }
    return context;
  });
  for (const dimension of ["width", "height"] as const) {
    const descriptor = Object.getOwnPropertyDescriptor(HTMLCanvasElement.prototype, dimension)!;
    vi.spyOn(HTMLCanvasElement.prototype, dimension, "set").mockImplementation(function (this: HTMLCanvasElement, value: number) {
      recordMutation(this, dimension);
      pixels.delete(this);
      descriptor.set!.call(this, value);
    });
  }
  return {
    draws,
    publishedMutations,
    picture: (canvas: HTMLCanvasElement | null) => canvas ? pixels.get(canvas) : undefined,
    present: (picture: string) => { presented = picture; },
    reject: (value: boolean) => { rejectCapture = value; },
  };
}

async function mountFramePlayer(fixture: ResolvedPlayback = playback, frameCallbacks = true) {
  vi.useFakeTimers();
  vi.spyOn(performance, "now").mockImplementation(() => Date.now());
  const visibility = Object.getOwnPropertyDescriptor(document, "visibilityState");
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  const frameRequests = new Map<number, VideoFrameRequestCallback>();
  let nextFrameId = 0;
  let presentedFrames = 0;
  const requestFrame = vi.fn((callback: VideoFrameRequestCallback) => {
    frameRequests.set(++nextFrameId, callback);
    return nextFrameId;
  });
  if (frameCallbacks) {
    Object.defineProperty(HTMLVideoElement.prototype, "requestVideoFrameCallback", { configurable: true, value: requestFrame });
    Object.defineProperty(HTMLVideoElement.prototype, "cancelVideoFrameCallback", { configurable: true, value: vi.fn((id: number) => frameRequests.delete(id)) });
  }
  const canvas = frameCanvasHarness();
  const view = render(<MemoryRouter><PlayerPage visualFixture={fixture} /></MemoryRouter>);
  await act(async () => { await import("hls.js"); });
  const video = view.container.querySelector("video") as HTMLVideoElement;
  const root = view.container.querySelector<HTMLElement>("[data-player-root]")!;
  Object.defineProperties(video, {
    duration: { configurable: true, value: 120 },
    readyState: { configurable: true, value: HTMLMediaElement.HAVE_FUTURE_DATA },
    paused: { configurable: true, value: false },
    videoWidth: { configurable: true, value: 640 },
    videoHeight: { configurable: true, value: 360 },
    seekable: { configurable: true, value: { length: 1, start: () => 0, end: () => 120 } },
  });
  const settle = () => {
    fireEvent.loadedMetadata(video);
    fireEvent.seeked(video);
    fireEvent.playing(video);
    video.currentTime += 0.1;
    fireEvent.timeUpdate(video);
  };
  const takeFrame = () => {
    const request = frameRequests.entries().next().value as [number, VideoFrameRequestCallback] | undefined;
    expect(request).toBeTruthy();
    frameRequests.delete(request![0]);
    return (time = video.currentTime) => {
      canvas.present(`picture at ${time.toFixed(2)}`);
      act(() => request![1](performance.now(), {
        mediaTime: time,
        presentedFrames: ++presentedFrames,
        presentationTime: performance.now(),
        expectedDisplayTime: performance.now(),
        width: video.videoWidth,
        height: video.videoHeight,
      } as VideoFrameCallbackMetadata));
    };
  };
  const frame = (time = video.currentTime) => takeFrame()(time);
  const advance = (milliseconds: number, frames = false, clock = true) => {
    for (let elapsed = 0; elapsed < milliseconds; elapsed += 200) {
      act(() => vi.advanceTimersByTime(Math.min(200, milliseconds - elapsed)));
      if (!view.container.querySelector("video")) break;
      if (clock) {
        video.currentTime += Math.min(200, milliseconds - elapsed) / 1000;
        fireEvent.timeUpdate(video);
      }
      if (frames) frame();
    }
  };
  const nextRecovery = () => {
    const baseline = videoCallCount("load", video);
    for (let elapsed = 0; elapsed < 12_000; elapsed += 200) {
      advance(200);
      if (videoCallCount("load", video) > baseline || view.container.querySelector(".player-state-view")) break;
    }
    return videoCallCount("load", video) - baseline;
  };
  const setVisibility = (state: "visible" | "hidden") => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: state });
    act(() => { fireEvent(document, new Event("visibilitychange")); });
  };
  settle();
  return {
    view, video, root, canvas, frame, takeFrame, advance, settle, nextRecovery, requestFrame, setVisibility,
    activeCanvas: () => view.container.querySelector<HTMLCanvasElement>(".player-last-frame[data-frame-active='true']"),
    close: () => {
      view.unmount();
      Reflect.deleteProperty(HTMLVideoElement.prototype, "requestVideoFrameCallback");
      Reflect.deleteProperty(HTMLVideoElement.prototype, "cancelVideoFrameCallback");
      if (visibility) Object.defineProperty(document, "visibilityState", visibility);
      else Reflect.deleteProperty(document, "visibilityState");
    },
  };
}

async function mountPhonePlayer() {
  const restore = phoneViewport();
  try {
    const player = await mountFramePlayer({
      ...playback,
      runResponse: { ...playback.runResponse, ticketExpiresAt: Math.floor(Date.now() / 1_000) + 3_600 },
    });
    return { ...player, close: () => { player.close(); restore(); } };
  } catch (error) {
    restore();
    throw error;
  }
}


describe("mounted player lifecycle", () => {
  beforeEach(() => {
    localStorage.removeItem(playerPreferencesKey);
    useProfileStore.setState({ profiles: [profile], activeProfile: profile, isAdmin: false });
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  });

  afterEach(() => {
    vi.useRealTimers();
    localStorage.removeItem(playerPreferencesKey);
    vi.restoreAllMocks();
    useProfileStore.setState({ profiles: [], activeProfile: null, isAdmin: false });
    document.documentElement.removeAttribute("data-player-viewport-fullscreen");
    document.body.removeAttribute("data-player-viewport-fullscreen");
    Reflect.deleteProperty(document, "fullscreenEnabled");
    Reflect.deleteProperty(document, "webkitFullscreenEnabled");
    Reflect.deleteProperty(document, "pictureInPictureElement");
  });

  it("mounts the real page and commits one desktop seek when dragging ends", () => {
    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={playback} />
      </MemoryRouter>,
    );

    expect(screen.getByText("Mounted player regression")).toBeTruthy();
    const timeline = screen.getByRole("slider", { name: "Playback position" }) as HTMLInputElement;
    const video = view.container.querySelector("video") as HTMLVideoElement;
    vi.spyOn(timeline, "getBoundingClientRect").mockReturnValue({
      x: 100,
      y: 0,
      left: 100,
      right: 500,
      top: 0,
      bottom: 16,
      width: 400,
      height: 16,
      toJSON: () => ({}),
    });
    const dispatchTimelinePointer = (type: string, clientX: number) => {
      const event = new MouseEvent(type, { bubbles: true, clientX });
      Object.defineProperty(event, "pointerId", { value: 1 });
      fireEvent(timeline, event);
    };
    expect(video.currentTime).toBe(0);

    dispatchTimelinePointer("pointerdown", 100);
    expect(document.activeElement).toBe(timeline);
    dispatchTimelinePointer("pointermove", 300);
    expect(video.currentTime).toBe(0);
    expect(screen.getByText("1:00")).toBeTruthy();
    dispatchTimelinePointer("pointerup", 300);

    expect(video.currentTime).toBe(60);
    expect(document.activeElement).toBe(view.container.querySelector("[data-player-root='true']"));
    pressFocusedKey("ArrowLeft");
    expect(video.currentTime).toBe(50);
    view.unmount();
  });

  it("restores shortcut ownership after a cancelled pointer scrub without committing its preview", () => {
    const view = render(<MemoryRouter><PlayerPage visualFixture={playback} /></MemoryRouter>);
    const timeline = screen.getByRole("slider", { name: "Playback position" }) as HTMLInputElement;
    const video = view.container.querySelector("video") as HTMLVideoElement;
    showPlayingVideo(video);
    vi.spyOn(timeline, "getBoundingClientRect").mockReturnValue({
      x: 0, y: 0, left: 0, right: 120, top: 0, bottom: 16, width: 120, height: 16, toJSON: () => ({}),
    });
    const down = new MouseEvent("pointerdown", { bubbles: true, clientX: 90 });
    Object.defineProperty(down, "pointerId", { value: 1 });
    fireEvent(timeline, down);
    expect(document.activeElement).toBe(timeline);
    expect(video.currentTime).toBe(30);
    const cancel = new MouseEvent("pointercancel", { bubbles: true });
    Object.defineProperty(cancel, "pointerId", { value: 1 });
    fireEvent(timeline, cancel);
    expect(video.currentTime).toBe(30);
    expect(document.activeElement).toBe(view.container.querySelector("[data-player-root='true']"));
    pressFocusedKey("ArrowRight");
    expect(video.currentTime).toBe(40);
    view.unmount();
  });

  it.each(["ember", "aurora", "cinema"] as const)("skips an active intro once while preserving paused %s playback", async (theme) => {
    const previousTheme = useThemeStore.getState().activeTheme;
    useThemeStore.setState({ activeTheme: theme });
    localStorage.setItem(playerPreferencesKey, JSON.stringify({ playbackRate: 1.5 }));
    const view = render(<MemoryRouter><PlayerPage visualFixture={markerPlayback} /></MemoryRouter>);
    try {
      await act(async () => { await import("hls.js"); });
      const root = view.container.querySelector("[data-player-root]") as HTMLElement;
      const video = view.container.querySelector("video") as HTMLVideoElement;
      showPlayingVideo(video, 15);
      pointerClick(screen.getByRole("button", { name: "Pause" }));
      Object.defineProperty(video, "paused", { configurable: true, value: true });
      fireEvent.pause(video);
      const source = video.getAttribute("src");
      const loads = videoCallCount("load", video);
      const plays = videoCallCount("play", video);
      const writes = observeVideoPosition(video);
      pointerClick(screen.getByRole("button", { name: "Skip intro" }));
      expect(writes).toHaveBeenCalledExactlyOnceWith(20);
      expect(video.currentTime).toBe(20);
      expect(video.paused).toBe(true);
      expect(root.dataset.playerPhase).toBe("paused");
      expect(document.activeElement).toBe(root);
      expect(view.container.querySelector("video")).toBe(video);
      expect(video.getAttribute("src")).toBe(source);
      expect(videoCallCount("load", video)).toBe(loads);
      expect(videoCallCount("play", video)).toBe(plays);
      expect(video.playbackRate).toBe(1.5);
      fireEvent.seeked(video);
      expect(root.dataset.playerPhase).toBe("paused");
      await waitFor(() => expect(screen.queryByRole("button", { name: "Skip intro" })).toBeNull());
    } finally {
      view.unmount();
      useThemeStore.setState({ activeTheme: previousTheme });
    }
  });

  it("dismisses Intro only after chrome idle and countdown, then restores it on activity without changing playback", async () => {
    const fixture: ResolvedPlayback = { ...markerPlayback, asset: { ...markerPlayback.asset, skipMarkers: { intro: [{ start: 0, end: 100 }] } } };
    const player = await mountFramePlayer(fixture);
    try {
      player.frame();
      const source = player.video.getAttribute("src");
      const loads = videoCallCount("load", player.video);
      const plays = videoCallCount("play", player.video);
      const start = player.video.currentTime;
      const control = player.view.container.querySelector<HTMLElement>(".player-skip-control")!;
      player.advance(PLAYER_CONTROLS_IDLE_MS, true);
      expect(player.root.dataset.controlsVisible).toBe("false");
      expect(control.dataset.countdownStage).toBe("waiting");
      player.advance(PLAYER_SKIP_COUNTDOWN_DELAY_MS - 1, true);
      expect(control.dataset.countdownStage).toBe("waiting");
      player.advance(1, true);
      expect(control.dataset.countdownStage).toBe("filling");
      player.advance(PLAYER_SKIP_COUNTDOWN_DURATION_MS - 1, true);
      expect(screen.getByRole("button", { name: "Skip intro" })).toBeTruthy();
      player.advance(1, true);
      expect(screen.queryByRole("button", { name: "Skip intro" })).toBeNull();
      expect(control.hidden).toBe(true);
      expect(player.video.currentTime).toBeCloseTo(start + (PLAYER_CONTROLS_IDLE_MS + PLAYER_SKIP_COUNTDOWN_DELAY_MS + PLAYER_SKIP_COUNTDOWN_DURATION_MS) / 1000);
      expect(player.root.dataset.playerPhase).toBe("playing");
      fireEvent.mouseMove(player.root, { clientX: 50, clientY: 40 });
      expect(player.root.dataset.controlsVisible).toBe("true");
      expect(screen.getByRole("button", { name: "Skip intro" })).toBeTruthy();
      expect(control.dataset.countdownStage).toBe("idle");
      pointerClick(screen.getByRole("button", { name: "Pause" }));
      Object.defineProperty(player.video, "paused", { configurable: true, value: true });
      fireEvent.pause(player.video);
      const pausedAt = player.video.currentTime;
      player.advance(12_000, true, false);
      expect(player.video.currentTime).toBe(pausedAt);
      expect(player.root.dataset.playerPhase).toBe("paused");
      expect(control.hidden).toBe(false);
      expect(control.dataset.countdownStage).toBe("idle");
      expect(player.view.container.querySelector("video")).toBe(player.video);
      expect(player.video.getAttribute("src")).toBe(source);
      expect(videoCallCount("load", player.video)).toBe(loads);
      expect(videoCallCount("play", player.video)).toBe(plays);
    } finally {
      player.close();
    }
  });

  it("starts a fresh Intro action after a confirmed seek within a dismissed region", async () => {
    const fixture: ResolvedPlayback = { ...markerPlayback, asset: { ...markerPlayback.asset, skipMarkers: { intro: [{ start: 0, end: 100 }] } } };
    const player = await mountFramePlayer(fixture);
    try {
      player.frame();
      const source = player.video.getAttribute("src");
      const loads = videoCallCount("load", player.video);
      const plays = videoCallCount("play", player.video);
      player.advance(PLAYER_CONTROLS_IDLE_MS, true);
      player.advance(PLAYER_SKIP_COUNTDOWN_DELAY_MS, true);
      player.advance(PLAYER_SKIP_COUNTDOWN_DURATION_MS, true);
      expect(screen.queryByRole("button", { name: "Skip intro" })).toBeNull();
      const oldAction = player.view.container.querySelector(".player-skip-control");
      fireEvent.keyDown(player.root, { key: "ArrowRight" });
      expect(screen.queryByRole("button", { name: "Skip intro" })).toBeNull();
      fireEvent.seeked(player.video);
      player.frame();
      const action = player.view.container.querySelector<HTMLElement>(".player-skip-control")!;
      expect(action).not.toBe(oldAction);
      expect(action.dataset.countdownStage).toBe("idle");
      expect(screen.getByRole("button", { name: "Skip intro" })).toBeTruthy();
      expect(player.video.getAttribute("src")).toBe(source);
      expect(videoCallCount("load", player.video)).toBe(loads);
      expect(videoCallCount("play", player.video)).toBe(plays);
    } finally {
      player.close();
    }
  });

  it("uses confirmed playback markers while the scrub preview visits a different region", async () => {
    const view = render(<MemoryRouter><PlayerPage visualFixture={markerPlayback} /></MemoryRouter>);
    try {
      await act(async () => { await import("hls.js"); });
      const video = view.container.querySelector("video") as HTMLVideoElement;
      const timeline = screen.getByRole("slider", { name: "Playback position" }) as HTMLInputElement;
      showPlayingVideo(video, 15);
      vi.spyOn(timeline, "getBoundingClientRect").mockReturnValue({
        x: 0, y: 0, left: 0, right: 120, top: 0, bottom: 16, width: 120, height: 16, toJSON: () => ({}),
      });
      expect(screen.getByRole("button", { name: "Skip intro" })).toBeTruthy();
      timelinePointer(timeline, "pointerdown", 95);
      expect(video.currentTime).toBe(15);
      expect(timeline.getAttribute("aria-valuetext")).toContain("Credits");
      expect(view.container.querySelector(".player-timeline-preview")?.textContent).toContain("Credits");
      expect(screen.getByRole("button", { name: "Skip intro" })).toBeTruthy();
      expect(screen.queryByRole("button", { name: "Skip credits" })).toBeNull();
      timelinePointer(timeline, "pointercancel", 95);

      video.currentTime = 20;
      fireEvent.timeUpdate(video);
      await waitFor(() => expect(screen.queryByRole("button", { name: "Skip intro" })).toBeNull());
      timelinePointer(timeline, "pointerdown", 15);
      expect(video.currentTime).toBe(20);
      expect(view.container.querySelector(".player-timeline-preview")?.textContent).toContain("Intro");
      expect(screen.queryByRole("button", { name: "Skip intro" })).toBeNull();
      timelinePointer(timeline, "pointercancel", 15);
    } finally {
      view.unmount();
    }
  });

  it("keeps marker regions decorative while the native timeline owns navigation and labeled previews", async () => {
    const view = render(<MemoryRouter><PlayerPage visualFixture={markerPlayback} /></MemoryRouter>);
    try {
      await act(async () => { await import("hls.js"); });
      const root = view.container.querySelector("[data-player-root]") as HTMLElement;
      const video = view.container.querySelector("video") as HTMLVideoElement;
      const timeline = screen.getByRole("slider", { name: "Playback position" }) as HTMLInputElement;
      showPlayingVideo(video);
      const markers = view.container.querySelector(".player-timeline-markers");
      expect(markers?.getAttribute("aria-hidden")).toBe("true");
      expect(markers?.querySelectorAll("[data-marker-id]")).toHaveLength(3);
      expect(markers?.querySelectorAll("[data-marker-kind='intro']")).toHaveLength(1);
      expect(markers?.querySelectorAll("[data-marker-kind='credits']")).toHaveLength(2);
      expect(markers?.querySelector("button, input, a, [tabindex], [role='button']")).toBeNull();
      expect(timeline.type).toBe("range");
      act(() => timeline.focus());
      const native = pressFocusedKey("ArrowRight");
      expect(native.down.defaultPrevented).toBe(false);
      expect(native.up.defaultPrevented).toBe(false);
      expect(video.currentTime).toBe(30);
      vi.spyOn(timeline, "getBoundingClientRect").mockReturnValue({
        x: 0, y: 0, left: 0, right: 120, top: 0, bottom: 16, width: 120, height: 16, toJSON: () => ({}),
      });
      timelinePointer(timeline, "pointermove", 15);
      expect(view.container.querySelector(".player-timeline-preview")?.textContent).toContain("Intro");
      expect(video.currentTime).toBe(30);
      timelinePointer(timeline, "pointerdown", 15);
      expect(timeline.getAttribute("aria-valuetext")).toContain("Intro");
      expect(screen.queryByRole("button", { name: "Skip intro" })).toBeNull();
      timelinePointer(timeline, "pointercancel", 15);
      expect(document.activeElement).toBe(root);
      pressFocusedKey("ArrowRight");
      expect(video.currentTime).toBe(40);
    } finally {
      view.unmount();
    }
  });

  it("preserves native keyboard Skip activation and releases focus when its marker disappears", async () => {
    const view = render(<MemoryRouter><PlayerPage visualFixture={markerPlayback} /></MemoryRouter>);
    try {
      await act(async () => { await import("hls.js"); });
      const root = view.container.querySelector("[data-player-root]") as HTMLElement;
      const video = view.container.querySelector("video") as HTMLVideoElement;
      showPlayingVideo(video, 15);
      const button = screen.getByRole("button", { name: "Skip intro" });
      const writes = observeVideoPosition(video);
      act(() => button.focus());
      const activation = pressFocusedKey("Enter");
      expect(activation.down.defaultPrevented).toBe(false);
      expect(activation.up.defaultPrevented).toBe(false);
      expect(writes).not.toHaveBeenCalled();
      fireEvent.click(button, { detail: 0 });
      expect(writes).toHaveBeenCalledExactlyOnceWith(20);
      fireEvent.seeked(video);
      await waitFor(() => expect(document.activeElement).toBe(root));
      pressFocusedKey("ArrowRight");
      expect(video.currentTime).toBe(30);
    } finally {
      view.unmount();
    }
  });

  it("keeps earlier credits skippable across a scene gap and hides terminal credits until native ended", async () => {
    const view = render(<MemoryRouter><PlayerPage visualFixture={markerPlayback} /></MemoryRouter>);
    try {
      await act(async () => { await import("hls.js"); });
      const root = view.container.querySelector("[data-player-root]") as HTMLElement;
      const video = view.container.querySelector("video") as HTMLVideoElement;
      showPlayingVideo(video, 95);
      const writes = observeVideoPosition(video);
      pointerClick(screen.getByRole("button", { name: "Skip credits" }));
      expect(writes).toHaveBeenCalledExactlyOnceWith(100);
      fireEvent.seeked(video);
      expect(root.dataset.playerPhase).not.toBe("ended");
      expect(screen.queryByRole("button", { name: "Replay" })).toBeNull();
      await waitFor(() => expect(screen.queryByRole("button", { name: "Skip credits" })).toBeNull());

      video.currentTime = 105;
      fireEvent.timeUpdate(video);
      expect(screen.queryByRole("button", { name: "Skip credits" })).toBeNull();
      video.currentTime = 112;
      fireEvent.timeUpdate(video);
      writes.mockClear();
      expect(screen.queryByRole("button", { name: "Skip credits" })).toBeNull();
      expect(writes).not.toHaveBeenCalled();
      expect(video.currentTime).toBe(112);
      expect(root.dataset.playerPhase).not.toBe("ended");
      expect(screen.queryByRole("button", { name: "Replay" })).toBeNull();
      fireEvent.ended(video);
      expect(root.dataset.playerPhase).toBe("ended");
      expect(screen.getByRole("button", { name: "Replay" })).toBeTruthy();
    } finally {
      view.unmount();
    }
  });

  it.each(["ember", "aurora", "cinema"] as const)("hides terminal credits without disturbing paused %s transport or leaving focus on body", async (theme) => {
    const previousTheme = useThemeStore.getState().activeTheme;
    useThemeStore.setState({ activeTheme: theme });
    const Hls = (await import("hls.js")).default;
    const attachMedia = vi.spyOn(Hls.prototype, "attachMedia");
    const loadSource = vi.spyOn(Hls.prototype, "loadSource");
    const view = render(<MemoryRouter><PlayerPage visualFixture={markerPlayback} /></MemoryRouter>);
    try {
      await act(async () => { await import("hls.js"); });
      const root = view.container.querySelector("[data-player-root]") as HTMLElement;
      const video = view.container.querySelector("video") as HTMLVideoElement;
      showPlayingVideo(video, 95);
      pointerClick(screen.getByRole("button", { name: "Pause" }));
      Object.defineProperty(video, "paused", { configurable: true, value: true });
      fireEvent.pause(video);
      act(() => screen.getByRole("button", { name: "Skip credits" }).focus());
      const source = video.getAttribute("src");
      const loads = videoCallCount("load", video);
      const plays = videoCallCount("play", video);

      // A confirmed native position update replaces the focused earlier-credit
      // action with terminal-credit presentation; it must not trigger a seek.
      video.currentTime = 112;
      const writes = observeVideoPosition(video);
      fireEvent.timeUpdate(video);
      await waitFor(() => expect(screen.queryByRole("button", { name: "Skip credits" })).toBeNull());
      expect(document.activeElement).toBe(root);
      expect(document.activeElement).not.toBe(document.body);
      expect(root.dataset.playerPhase).toBe("paused");
      expect(video.paused).toBe(true);
      expect(video.currentTime).toBe(112);
      expect(writes).not.toHaveBeenCalled();
      expect(view.container.querySelector("video")).toBe(video);
      expect(video.getAttribute("src")).toBe(source);
      expect(videoCallCount("load", video)).toBe(loads);
      expect(videoCallCount("play", video)).toBe(plays);
      expect(attachMedia).not.toHaveBeenCalled();
      expect(loadSource).not.toHaveBeenCalled();
      expect(view.container.querySelectorAll("[data-marker-kind='credits']")).toHaveLength(2);
      expect(screen.queryByRole("button", { name: "Replay" })).toBeNull();
    } finally {
      view.unmount();
      useThemeStore.setState({ activeTheme: previousTheme });
    }
  });

  it("renders selected captions through the player overlay with profile timing", () => {
    localStorage.setItem(playerPreferencesKey, JSON.stringify({
      subtitleTrackId: "subtitle-en",
      subtitleOffset: -0.5,
    }));
    const captionPlayback: ResolvedPlayback = {
      ...playback,
      runResponse: {
        ...playback.runResponse,
        resumePosition: 0,
        subtitles: [{ id: "subtitle-en", language: "en", label: "English" }],
      },
    };
    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={captionPlayback} />
      </MemoryRouter>,
    );
    const video = view.container.querySelector("video") as HTMLVideoElement;
    const track = view.container.querySelector("track[data-subtitle-id='subtitle-en']") as HTMLTrackElement;
    const cue = { startTime: 10, endTime: 12, text: "<i>Early caption</i>" };
    Object.defineProperty(track, "track", {
      configurable: true,
      value: Object.assign(new EventTarget(), { mode: "disabled", cues: { 0: cue, length: 1 } }),
    });

    fireEvent.load(track);
    video.currentTime = 9.5;
    fireEvent.timeUpdate(video);

    expect(screen.getByRole("region", { name: "Subtitles" }).textContent).toBe("Early caption");
    expect(track.track.mode).not.toBe("showing");
    view.unmount();
  });

  it("keeps an active caption mounted when idle playing controls hide", () => {
    vi.useFakeTimers();
    localStorage.setItem(playerPreferencesKey, JSON.stringify({ subtitleTrackId: "subtitle-en" }));
    const captionPlayback: ResolvedPlayback = {
      ...playback,
      runResponse: {
        ...playback.runResponse,
        resumePosition: 0,
        subtitles: [{ id: "subtitle-en", language: "en", label: "English" }],
      },
    };
    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={captionPlayback} />
      </MemoryRouter>,
    );
    const root = view.container.querySelector("[data-player-root='true']") as HTMLElement;
    const video = view.container.querySelector("video") as HTMLVideoElement;
    const track = view.container.querySelector("track[data-subtitle-id='subtitle-en']") as HTMLTrackElement;
    Object.defineProperty(track, "track", {
      configurable: true,
      value: Object.assign(new EventTarget(), { mode: "disabled", cues: { 0: { startTime: 0, endTime: 120, text: "Persistent caption" }, length: 1 } }),
    });
    Object.defineProperty(video, "duration", { configurable: true, value: 120 });
    Object.defineProperty(video, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_FUTURE_DATA });
    Object.defineProperty(video, "paused", { configurable: true, value: false });
    Object.defineProperty(video, "currentTime", { configurable: true, writable: true, value: 1 });

    fireEvent.load(track);
    fireEvent.loadedMetadata(video);
    fireEvent.play(video);
    fireEvent.playing(video);
    fireEvent.timeUpdate(video);
    expect(screen.getByRole("region", { name: "Subtitles" }).textContent).toBe("Persistent caption");

    fireEvent.mouseMove(root, { clientX: 20, clientY: 20 });
    fireEvent.mouseMove(root, { clientX: 21, clientY: 20 });
    for (let i = 0; i < 4; i += 1) {
      video.currentTime = 2 + i;
      fireEvent.timeUpdate(video);
      act(() => vi.advanceTimersByTime(800));
    }

    expect(root.dataset.controlsVisible).toBe("false");
    expect(screen.getByRole("region", { name: "Subtitles" }).textContent).toBe("Persistent caption");
    view.unmount();
  });

  it("autostarts when media becomes ready and accepts native and global playback controls", async () => {
    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={playback} />
      </MemoryRouter>,
    );
    const root = view.container.querySelector("[data-player-root='true']") as HTMLElement;
    const video = view.container.querySelector("video") as HTMLVideoElement;
    const play = vi.mocked(HTMLMediaElement.prototype.play);
    const pause = vi.mocked(HTMLMediaElement.prototype.pause);
    play.mockClear();
    pause.mockClear();
    Object.defineProperty(video, "duration", { configurable: true, value: 120 });
    Object.defineProperty(video, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_FUTURE_DATA });

    fireEvent.loadedMetadata(video);
    fireEvent.canPlay(video);
    await waitFor(() => expect(play).toHaveBeenCalled());

    Object.defineProperty(video, "paused", { configurable: true, value: false });
    fireEvent.play(video);
    fireEvent.playing(video);
    Object.defineProperty(video, "currentTime", { configurable: true, writable: true, value: 12.5 });
    fireEvent.timeUpdate(video);
    await waitFor(() => expect(root.dataset.playerPhase).toBe("playing"));

    const fullscreenButton = screen.getByRole("button", { name: "Fullscreen" });
    fullscreenButton.focus();
    fireEvent.keyDown(fullscreenButton, { key: "k" });
    fireEvent.keyUp(fullscreenButton, { key: "k" });
    expect(pause).toHaveBeenCalled();
    Object.defineProperty(video, "paused", { configurable: true, value: true });
    fireEvent.pause(video);
    expect(root.dataset.playerPhase).toBe("paused");

    Object.defineProperty(document, "pictureInPictureElement", { configurable: true, value: video });
    Object.defineProperty(video, "paused", { configurable: true, value: false });
    fireEvent.play(video);
    fireEvent.playing(video);
    Object.defineProperty(video, "currentTime", { configurable: true, writable: true, value: 13 });
    fireEvent.timeUpdate(video);
    await waitFor(() => expect(root.dataset.playerPhase).toBe("playing"));

    Object.defineProperty(video, "paused", { configurable: true, value: true });
    fireEvent.pause(video);
    play.mockClear();
    pointerClick(screen.getByRole("button", { name: "Rewind 10 seconds" }));
    fireEvent.seeked(video);
    expect(document.activeElement).toBe(root);
    pressFocusedKey(" ");
    fireEvent.loadedMetadata(video);
    fireEvent.canPlay(video);
    await waitFor(() => expect(play).toHaveBeenCalled());

    pressFocusedKey("ArrowDown");
    await waitFor(() => expect(video.volume).toBe(0.95));
    pressFocusedKey("ArrowUp");
    await waitFor(() => expect(video.volume).toBe(1));
    expect(screen.queryByRole("slider", { name: "Volume" })).toBeNull();

    video.currentTime = 30;
    fireEvent.timeUpdate(video);
    pressFocusedKey("ArrowRight");
    expect(video.currentTime).toBe(40);
    pressFocusedKey("ArrowLeft");
    expect(video.currentTime).toBe(30);
    view.unmount();
  });

  it("keeps keyboard button activation native and restores shortcuts after a pointer action", async () => {
    const view = render(<MemoryRouter><PlayerPage visualFixture={playback} /></MemoryRouter>);
    await act(async () => { await import("hls.js"); });
    const video = view.container.querySelector("video") as HTMLVideoElement;
    showPlayingVideo(video);
    const source = video.getAttribute("src");
    const loads = videoCallCount("load", video);
    const pauses = videoCallCount("pause", video);
    const rewind = screen.getByRole("button", { name: "Rewind 10 seconds" });

    act(() => rewind.focus());
    const activation = pressFocusedKey(" ");
    expect(activation.down.defaultPrevented).toBe(false);
    expect(activation.up.defaultPrevented).toBe(false);
    expect(videoCallCount("pause", video)).toBe(pauses);
    fireEvent.click(rewind, { detail: 0 });
    expect(video.currentTime).toBe(20);
    expect(document.activeElement).toBe(rewind);

    pressFocusedKey("ArrowRight");
    expect(video.currentTime).toBe(30);
    pointerClick(rewind);
    expect(video.currentTime).toBe(20);
    expect(document.activeElement).toBe(view.container.querySelector("[data-player-root='true']"));
    pressFocusedKey(" ");
    expect(videoCallCount("pause", video)).toBe(pauses + 1);
    expect(video.getAttribute("src")).toBe(source);
    expect(videoCallCount("load", video)).toBe(loads);
    view.unmount();
  });

  it.each(["Playback position", "Volume", "Subtitle timing"])("keeps native %s navigation while routing unrelated playback shortcuts", async (label) => {
    localStorage.setItem(playerPreferencesKey, JSON.stringify({ subtitleTrackId: "subtitle-en" }));
    const captionPlayback: ResolvedPlayback = {
      ...playback,
      runResponse: { ...playback.runResponse, subtitles: [{ id: "subtitle-en", language: "en", label: "English" }] },
    };
    const view = render(<MemoryRouter><PlayerPage visualFixture={captionPlayback} /></MemoryRouter>);
    await act(async () => { await import("hls.js"); });
    const video = view.container.querySelector("video") as HTMLVideoElement;
    showPlayingVideo(video);
    if (label === "Volume") act(() => screen.getByRole("button", { name: "Volume" }).focus());
    if (label === "Subtitle timing") pointerClick(screen.getByRole("button", { name: "Player settings" }));
    const slider = screen.getByRole("slider", { name: label });
    act(() => slider.focus());
    const volume = video.volume;
    const source = video.getAttribute("src");
    const loads = videoCallCount("load", video);
    const pauses = videoCallCount("pause", video);

    for (const key of ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"]) {
      const native = pressFocusedKey(key);
      expect(native.down.defaultPrevented).toBe(false);
      expect(native.up.defaultPrevented).toBe(false);
    }
    expect(video.currentTime).toBe(30);
    expect(video.volume).toBe(volume);

    for (const init of [{ ctrlKey: true }, { metaKey: true }, { altKey: true }, { isComposing: true }]) {
      const preserved = pressFocusedKey("k", init);
      expect(preserved.down.defaultPrevented).toBe(false);
      expect(preserved.up.defaultPrevented).toBe(false);
    }
    expect(videoCallCount("pause", video)).toBe(pauses);
    pressFocusedKey("m");
    expect(video.muted).toBe(true);
    const space = pressFocusedKey(" ");
    expect(space.down.defaultPrevented).toBe(true);
    expect(space.up.defaultPrevented).toBe(true);
    expect(videoCallCount("pause", video)).toBe(pauses + 1);
    expect(document.activeElement).toBe(slider);
    Object.defineProperty(video, "paused", { configurable: true, value: true });
    fireEvent.pause(video);
    const plays = videoCallCount("play", video);
    pressFocusedKey("k");
    await waitFor(() => expect(videoCallCount("play", video)).toBe(plays + 1));
    expect(video.getAttribute("src")).toBe(source);
    expect(videoCallCount("load", video)).toBe(loads);
    view.unmount();
  });

  it.each(["Audio language", "Subtitles"])("restores live shortcut ownership after a pointer %s selection", async (label) => {
    const captionPlayback: ResolvedPlayback = {
      ...playback,
      runResponse: { ...playback.runResponse, subtitles: [{ id: "subtitle-en", language: "en", label: "English" }] },
    };
    const view = render(<MemoryRouter><PlayerPage visualFixture={captionPlayback} /></MemoryRouter>);
    await act(async () => { await import("hls.js"); });
    const video = view.container.querySelector("video") as HTMLVideoElement;
    showPlayingVideo(video);
    const source = video.getAttribute("src");
    const loads = videoCallCount("load", video);
    const pauses = videoCallCount("pause", video);
    pointerClick(screen.getByRole("button", { name: new RegExp(`^${label}:`) }));
    pointerClick(screen.getByRole("option", { name: "English" }));
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(document.activeElement).toBe(view.container.querySelector("[data-player-root='true']"));
    pressFocusedKey(" ");
    expect(videoCallCount("pause", video)).toBe(pauses + 1);
    expect(video.getAttribute("src")).toBe(source);
    expect(videoCallCount("load", video)).toBe(loads);
    view.unmount();
  });

  it("allows playing chrome to hide after pointer focus is released", () => {
    vi.useFakeTimers();
    const view = render(<MemoryRouter><PlayerPage visualFixture={playback} /></MemoryRouter>);
    const root = view.container.querySelector("[data-player-root='true']") as HTMLElement;
    const video = view.container.querySelector("video") as HTMLVideoElement;
    showPlayingVideo(video);
    pointerClick(screen.getByRole("button", { name: "Forward 10 seconds" }));
    fireEvent.seeked(video);
    expect(document.activeElement).toBe(root);
    for (let update = 1; update <= 4; update += 1) {
      video.currentTime = 40 + update;
      fireEvent.timeUpdate(video);
      act(() => vi.advanceTimersByTime(800));
    }
    expect(root.dataset.playerPhase).toBe("playing");
    expect(root.dataset.controlsVisible).toBe("false");
    view.unmount();
  });

  it("keeps an explicit pause authoritative while a pending play request becomes ready", async () => {
    let resolvePendingPlay!: () => void;
    const pendingPlay = new Promise<void>((resolve) => {
      resolvePendingPlay = resolve;
    });
    const play = vi.mocked(HTMLMediaElement.prototype.play);
    const pause = vi.mocked(HTMLMediaElement.prototype.pause);
    const load = vi.mocked(HTMLMediaElement.prototype.load);
    play.mockImplementationOnce(() => pendingPlay).mockResolvedValue(undefined);

    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={playback} />
      </MemoryRouter>,
    );
    const root = view.container.querySelector("[data-player-root='true']") as HTMLElement;
    const video = view.container.querySelector("video") as HTMLVideoElement;
    Object.defineProperty(video, "duration", { configurable: true, value: 120 });
    Object.defineProperty(video, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_FUTURE_DATA });

    fireEvent.loadedMetadata(video);
    await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
    Object.defineProperty(video, "paused", { configurable: true, value: false });
    Object.defineProperty(video, "currentTime", { configurable: true, writable: true, value: 42 });
    fireEvent.timeUpdate(video);
    const loadCountBeforePause = load.mock.calls.length;
    const sourceBeforePause = video.getAttribute("src");
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));

    await waitFor(() => {
      expect(root.dataset.playerPhase).toBe("paused");
      expect(video.preload).toBe("auto");
    });
    expect(load).toHaveBeenCalledTimes(loadCountBeforePause);
    expect(video.getAttribute("src")).toBe(sourceBeforePause);
    expect(video.currentTime).toBe(42);
    expect((screen.getByRole("slider", { name: "Playback position" }) as HTMLInputElement).value).toBe("42");

    await act(async () => {
      resolvePendingPlay();
      await pendingPlay;
    });
    Object.defineProperty(video, "paused", { configurable: true, value: false });
    fireEvent.play(video);
    fireEvent.canPlay(video);
    fireEvent.playing(video);

    expect(root.dataset.playerPhase).toBe("paused");
    expect(pause).toHaveBeenCalled();
    expect(play).toHaveBeenCalledTimes(1);

    Object.defineProperty(video, "paused", { configurable: true, value: true });
    fireEvent.click(screen.getByRole("button", { name: "Play" }));
    await waitFor(() => expect(play).toHaveBeenCalledTimes(2));
    expect(video.currentTime).toBe(42);
    expect(video.getAttribute("src")).toBe(sourceBeforePause);
    expect(load).toHaveBeenCalledTimes(loadCountBeforePause);

    view.unmount();
  });

  it("restores the profile audio level and preserves popup changes across titles without replacing transport", async () => {
    localStorage.setItem(playerPreferencesKey, JSON.stringify({ volume: 0.36, muted: false }));
    const firstView = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={playback} />
      </MemoryRouter>,
    );

    await act(async () => { await import("hls.js"); });
    const firstVideo = firstView.container.querySelector("video") as HTMLVideoElement;
    await waitFor(() => expect(firstVideo.volume).toBe(0.36));
    const source = firstVideo.getAttribute("src");
    const loadCalls = vi.mocked(HTMLMediaElement.prototype.load).mock.calls.length;
    const playCalls = vi.mocked(HTMLMediaElement.prototype.play).mock.calls.length;
    const pauseCalls = vi.mocked(HTMLMediaElement.prototype.pause).mock.calls.length;
    expect(screen.queryByRole("slider", { name: "Volume" })).toBeNull();
    act(() => screen.getByRole("button", { name: "Volume" }).focus());
    const firstVolume = screen.getByRole("slider", { name: "Volume" }) as HTMLInputElement;
    await waitFor(() => expect(firstVolume.value).toBe("0.36"));
    expect(firstVideo.volume).toBe(0.36);

    fireEvent.change(firstVolume, { target: { value: "0.24" } });
    await waitFor(() => expect(JSON.parse(localStorage.getItem(playerPreferencesKey) || "{}")).toMatchObject({
      volume: 0.24,
      muted: false,
    }));
    expect(firstVideo.volume).toBe(0.24);
    fireEvent.click(screen.getByRole("button", { name: "Volume" }));
    expect(firstVideo.muted).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Volume" }));
    expect(firstVideo.muted).toBe(false);
    expect(firstVideo.volume).toBe(0.24);
    fireEvent.keyDown(firstVolume, { key: "Escape" });
    expect(screen.queryByRole("slider", { name: "Volume" })).toBeNull();
    expect(firstVideo.getAttribute("src")).toBe(source);
    expect(HTMLMediaElement.prototype.load).toHaveBeenCalledTimes(loadCalls);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(playCalls);
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(pauseCalls);
    firstView.unmount();

    const nextTitle: ResolvedPlayback = {
      ...playback,
      asset: {
        ...playback.asset,
        id: "mounted-player-next-media",
        movieId: "mounted-player-next-media",
        title: "Mounted player next title",
      },
    };
    const secondView = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-next-media"]}>
        <PlayerPage visualFixture={nextTitle} />
      </MemoryRouter>,
    );

    const secondVideo = secondView.container.querySelector("video") as HTMLVideoElement;
    expect(screen.queryByRole("slider", { name: "Volume" })).toBeNull();
    act(() => screen.getByRole("button", { name: "Volume" }).focus());
    await waitFor(() => expect((screen.getByRole("slider", { name: "Volume" }) as HTMLInputElement).value).toBe("0.24"));
    expect(secondVideo.volume).toBe(0.24);
    secondView.unmount();
  });

  it("clamps rapid seeks to a growing preview edge and pauses synchronized audio", () => {
    const growingPlayback: ResolvedPlayback = {
      ...playback,
      runResponse: {
        ...playback.runResponse,
        resumePosition: 80,
        sourceMetadata: {
          ...playback.runResponse.sourceMetadata,
          container: "hls",
          codec: "h264",
          sourceFormat: "HLS preview",
        },
        manifestUrl: "/api/playback/preview/mounted-player-media/playlist.m3u8?ticket=mounted-player-ticket",
        progressiveUrl: "",
        preparationState: "ready",
        preparationProgress: {
          stage: "streamable",
          queuePosition: 0,
          readySegments: 24,
          activeWorkers: 1,
        },
        seekableUntil: 96,
        resumeReady: true,
        switchingReady: true,
        fullyPrepared: false,
      },
    };
    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={growingPlayback} />
      </MemoryRouter>,
    );
    const video = view.container.querySelector("video") as HTMLVideoElement;
    Object.defineProperty(video, "seekable", {
      configurable: true,
      value: {
        length: 1,
        start: () => 0,
        end: () => 92,
      },
    });
    const pause = vi.mocked(HTMLMediaElement.prototype.pause);
    pause.mockClear();

    fireEvent.keyDown(document.body, { key: "ArrowRight" });
    fireEvent.keyDown(document.body, { key: "ArrowRight" });

    expect(video.currentTime).toBe(91.5);
    expect(screen.getByText("The download is still expanding. Jumped to the latest available point.")).toBeTruthy();
    expect(pause).toHaveBeenCalled();
    view.unmount();
  });

  it("keeps the authoritative position through transient zero and out-of-order rapid seek events", () => {
    const resumedPlayback: ResolvedPlayback = {
      ...playback,
      runResponse: { ...playback.runResponse, resumePosition: 107 },
    };
    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={resumedPlayback} />
      </MemoryRouter>,
    );
    const video = view.container.querySelector("video") as HTMLVideoElement;
    const timeline = screen.getByRole("slider", { name: "Playback position" }) as HTMLInputElement;
    Object.defineProperty(video, "duration", { configurable: true, value: 120 });
    Object.defineProperty(video, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_FUTURE_DATA });
    Object.defineProperty(video, "paused", { configurable: true, value: false });

    fireEvent.loadedMetadata(video);
    expect(video.currentTime).toBe(107);
    const source = video.getAttribute("src");
    const loads = videoCallCount("load", video);
    video.currentTime = 0;
    fireEvent.playing(video);
    expect(view.container.querySelector("[data-player-root]")?.getAttribute("data-player-phase")).toBe("recovering");
    const indicator = view.container.querySelector(".player-loading-overlay");
    expect(indicator?.getAttribute("aria-hidden")).toBe("true");
    expect(indicator?.textContent).toBe("");
    expect(indicator?.querySelectorAll(".player-loading-spinner")).toHaveLength(1);
    expect(screen.getByText("Switching to compatibility playback").classList.contains("sr-only")).toBe(true);
    expect(video.getAttribute("src")).toBe(source);
    expect(videoCallCount("load", video)).toBe(loads);
    fireEvent.timeUpdate(video);
    expect(timeline.value).toBe("107");

    video.currentTime = 107;
    fireEvent.seeked(video);
    fireEvent.keyDown(document.body, { key: "ArrowLeft" });
    fireEvent.keyDown(document.body, { key: "ArrowLeft" });
    expect(timeline.value).toBe("87");

    video.currentTime = 97;
    fireEvent.seeked(video);
    expect(timeline.value).toBe("87");
    video.currentTime = 87;
    fireEvent.seeked(video);
    expect(timeline.value).toBe("87");

    video.currentTime = 0;
    fireEvent.timeUpdate(video);
    expect(timeline.value).toBe("87");
    view.unmount();
  });

  it.each(["ember", "aurora", "cinema"] as const)("uses one topbar Settings control without disturbing paused %s playback", async (theme) => {
    const previousTheme = useThemeStore.getState().activeTheme;
    useThemeStore.setState({ activeTheme: theme });
    const settingsPlayback = {
      ...playback,
      runResponse: { ...playback.runResponse, ticketExpiresAt: Math.floor(Date.now() / 1_000) + 3_600 },
    };
    const view = render(<MemoryRouter><PlayerPage visualFixture={settingsPlayback} /></MemoryRouter>);
    try {
      await act(async () => { await import("hls.js"); });
      const root = view.container.querySelector<HTMLElement>("[data-player-root='true']")!;
      const video = view.container.querySelector("video") as HTMLVideoElement;
      showPlayingVideo(video);
      pointerClick(screen.getByRole("button", { name: "Pause" }));
      Object.defineProperty(video, "paused", { configurable: true, value: true });
      fireEvent.pause(video);
      const source = video.getAttribute("src");
      const loads = videoCallCount("load", video);
      const plays = videoCallCount("play", video);
      const pauses = videoCallCount("pause", video);
      const settings = screen.getByRole("button", { name: "Player settings" });
      expect(screen.getAllByRole("button", { name: "Player settings" })).toHaveLength(1);
      expect(settings.closest(".player-topbar")).not.toBeNull();
      expect(view.container.querySelector(".player-controls button[aria-label='Player settings']")).toBeNull();
      expect(settings.querySelector("svg[aria-hidden='true']")).not.toBeNull();
      expect(settings.textContent).toBe("");

      pointerClick(settings);
      pointerClick(screen.getByRole("button", { name: "1.5×" }));
      expect(screen.queryByRole("dialog", { name: "Player settings" })).toBeNull();
      expect(document.activeElement).toBe(root);
      expect(root.dataset.playerPhase).toBe("paused");
      expect(video.paused).toBe(true);
      expect(video.currentTime).toBe(30);
      expect(video.playbackRate).toBe(1.5);
      expect(view.container.querySelector("video")).toBe(video);
      expect(video.getAttribute("src")).toBe(source);
      expect(videoCallCount("load", video)).toBe(loads);
      expect(videoCallCount("play", video)).toBe(plays);
      expect(videoCallCount("pause", video)).toBe(pauses);
    } finally {
      view.unmount();
      useThemeStore.setState({ activeTheme: previousTheme });
    }
  });

  it("dismisses and reveals paused phone chrome without touching the mounted transport", async () => {
    const player = await mountPhonePlayer();
    try {
      pointerClick(screen.getByRole("button", { name: "Pause" }));
      Object.defineProperty(player.video, "paused", { configurable: true, value: true });
      const source = player.video.getAttribute("src");
      const time = player.video.currentTime;
      const calls = ["play", "pause", "load"].map((method) => videoCallCount(method as "play" | "pause" | "load", player.video));
      const play = screen.getByRole("button", { name: "Play" });
      act(() => play.focus());
      const center = player.view.container.querySelector<HTMLElement>(".mobile-player-gesture-zone--center")!;
      mobileTap(center);
      await act(async () => { await vi.advanceTimersByTimeAsync(200); });
      const chrome = player.view.container.querySelector(".mobile-player-chrome")!;
      expect(player.root.dataset.controlsVisible).toBe("false");
      expect(chrome.getAttribute("data-visible")).toBe("false");
      expect(chrome.getAttribute("aria-hidden")).toBe("true");
      expect(chrome.hasAttribute("inert")).toBe(true);
      expect(document.activeElement).toBe(player.root);
      mobileTap(center);
      await act(async () => { await vi.advanceTimersByTimeAsync(200); });
      expect(chrome.getAttribute("data-visible")).toBe("true");
      expect(chrome.hasAttribute("inert")).toBe(false);
      expect(player.root.dataset.playerPhase).toBe("paused");
      expect(player.video.currentTime).toBe(time);
      expect(player.video.getAttribute("src")).toBe(source);
      expect(player.view.container.querySelector("video")).toBe(player.video);
      expect(["play", "pause", "load"].map((method) => videoCallCount(method as "play" | "pause" | "load", player.video))).toEqual(calls);
    } finally { player.close(); }
  });

  it("restores phone auto-hide after manual hide/reveal and retains buffering controls", async () => {
    const player = await mountPhonePlayer();
    try {
      const center = player.view.container.querySelector<HTMLElement>(".mobile-player-gesture-zone--center")!;
      mobileTap(center);
      expect(player.root.dataset.controlsVisible).toBe("false");
      mobileTap(center);
      expect(player.root.dataset.controlsVisible).toBe("true");
      player.advance(PLAYER_CONTROLS_IDLE_MS + 200, true);
      expect(player.root.dataset.controlsVisible).toBe("false");
      fireEvent.waiting(player.video);
      expect(player.root.dataset.playerPhase).toBe("buffering");
      expect(player.root.dataset.controlsVisible).toBe("true");
      mobileTap(center);
      expect(player.root.dataset.controlsVisible).toBe("true");
    } finally { player.close(); }
  });

  it.each([
    ["playing", "right", 10],
    ["playing", "left", -10],
    ["paused", "right", 10],
    ["paused", "left", -10],
  ] as const)("retains hidden %s phone controls through %s repeated-tap seek events", async (intent, side, delta) => {
    const player = await mountPhonePlayer();
    try {
      if (intent === "paused") {
        pointerClick(screen.getByRole("button", { name: "Pause" }));
        Object.defineProperty(player.video, "paused", { configurable: true, value: true });
      }
      const source = player.video.getAttribute("src");
      const loads = videoCallCount("load", player.video);
      const plays = videoCallCount("play", player.video);
      const start = player.video.currentTime;
      mobileTap(player.view.container.querySelector<HTMLElement>(".mobile-player-gesture-zone--center")!);
      const zone = player.view.container.querySelector<HTMLElement>(`.mobile-player-gesture-zone--${side}`)!;
      mobileTap(zone);
      act(() => vi.advanceTimersByTime(80));
      mobileTap(zone);
      expect(player.video.currentTime).toBeCloseTo(start + delta);
      fireEvent.seeking(player.video);
      fireEvent.waiting(player.video);
      expect(player.root.dataset.controlsVisible).toBe("false");
      if (intent === "playing") fireEvent.playing(player.video);
      expect(player.root.dataset.controlsVisible).toBe("false");
      fireEvent.seeked(player.video);
      if (intent === "playing") {
        fireEvent.playing(player.video);
        player.video.currentTime += 0.1;
        fireEvent.timeUpdate(player.video);
      }
      expect(player.root.dataset.controlsVisible).toBe("false");
      player.advance(MOBILE_SEEK_CONTROLS_GRACE_MS + 100, intent === "playing", intent === "playing");
      expect(player.root.dataset.controlsVisible).toBe("false");
      expect(player.root.dataset.playerPhase).toBe(intent);
      expect(player.video.paused).toBe(intent === "paused");
      expect(player.video.getAttribute("src")).toBe(source);
      expect(videoCallCount("load", player.video)).toBe(loads);
      expect(videoCallCount("play", player.video)).toBe(plays);
    } finally { player.close(); }
  });

  it("reveals phone controls when gesture-seek buffering outlasts its bounded grace", async () => {
    const player = await mountPhonePlayer();
    try {
      mobileTap(player.view.container.querySelector<HTMLElement>(".mobile-player-gesture-zone--center")!);
      const right = player.view.container.querySelector<HTMLElement>(".mobile-player-gesture-zone--right")!;
      mobileTap(right);
      act(() => vi.advanceTimersByTime(80));
      mobileTap(right);
      fireEvent.waiting(player.video);
      act(() => vi.advanceTimersByTime(MOBILE_SEEK_CONTROLS_GRACE_MS - 1));
      expect(player.root.dataset.controlsVisible).toBe("false");
      act(() => vi.advanceTimersByTime(1));
      expect(player.root.dataset.controlsVisible).toBe("true");
      expect(screen.getByRole("button", { name: "Pause" })).toBeTruthy();
    } finally { player.close(); }
  });

  it("allows a single phone tap and explicit Pause during gesture-seek loading", async () => {
    const player = await mountPhonePlayer();
    try {
      const center = player.view.container.querySelector<HTMLElement>(".mobile-player-gesture-zone--center")!;
      const right = player.view.container.querySelector<HTMLElement>(".mobile-player-gesture-zone--right")!;
      mobileTap(center);
      mobileTap(right);
      act(() => vi.advanceTimersByTime(80));
      mobileTap(right);
      fireEvent.waiting(player.video);
      expect(player.root.dataset.controlsVisible).toBe("false");
      mobileTap(center);
      expect(player.root.dataset.controlsVisible).toBe("true");
      pointerClick(screen.getByRole("button", { name: "Pause" }));
      Object.defineProperty(player.video, "paused", { configurable: true, value: true });
      const plays = videoCallCount("play", player.video);
      fireEvent.seeked(player.video);
      fireEvent.canPlay(player.video);
      player.advance(MOBILE_SEEK_CONTROLS_GRACE_MS + 100, false, false);
      expect(player.root.dataset.playerPhase).toBe("paused");
      expect(player.root.dataset.controlsVisible).toBe("true");
      expect(videoCallCount("play", player.video)).toBe(plays);
    } finally { player.close(); }
  });

  it("keeps accumulated phone seek feedback beyond the tap-chain window while preserving Pause", async () => {
    const player = await mountPhonePlayer();
    try {
      pointerClick(screen.getByRole("button", { name: "Pause" }));
      Object.defineProperty(player.video, "paused", { configurable: true, value: true });
      const right = player.view.container.querySelector<HTMLElement>(".mobile-player-gesture-zone--right")!;
      const source = player.video.getAttribute("src");
      const plays = videoCallCount("play", player.video);
      const loads = videoCallCount("load", player.video);
      const origin = player.video.currentTime;
      mobileTap(right);
      act(() => vi.advanceTimersByTime(80));
      mobileTap(right);
      const feedback = player.view.container.querySelector(".mobile-player-seek-feedback")!;
      expect(feedback.textContent).toBe("+10 s");
      expect(player.video.currentTime).toBeCloseTo(origin + 10);
      act(() => vi.advanceTimersByTime(80));
      mobileTap(right);
      expect(player.view.container.querySelector(".mobile-player-seek-feedback")).toBe(feedback);
      expect(feedback.textContent).toBe("+20 s");
      expect(player.video.currentTime).toBeCloseTo(origin + 20);
      expect(player.root.dataset.controlsVisible).toBe("true");
      await act(async () => { await vi.advanceTimersByTimeAsync(400); });
      expect(player.view.container.querySelector(".mobile-player-seek-feedback")).toBe(feedback);
      expect(player.root.dataset.playerPhase).toBe("paused");
      await act(async () => { await vi.advanceTimersByTimeAsync(MOBILE_SEEK_FEEDBACK_MS); });
      const fadingFeedback = player.view.container.querySelector<HTMLElement>(".mobile-player-seek-feedback");
      expect(fadingFeedback === null || fadingFeedback.style.opacity === "0").toBe(true);
      expect(screen.queryByText("Forward 20 seconds")).toBeNull();
      expect(player.video.getAttribute("src")).toBe(source);
      expect(videoCallCount("play", player.video)).toBe(plays);
      expect(videoCallCount("load", player.video)).toBe(loads);
    } finally { player.close(); }
  });

  it("dismisses phone menus without also hiding/seeking and rejects cancelled or dragged taps", async () => {
    const player = await mountPhonePlayer();
    try {
      pointerClick(screen.getByRole("button", { name: "Pause" }));
      Object.defineProperty(player.video, "paused", { configurable: true, value: true });
      const center = player.view.container.querySelector<HTMLElement>(".mobile-player-gesture-zone--center")!;
      const right = player.view.container.querySelector<HTMLElement>(".mobile-player-gesture-zone--right")!;
      const time = player.video.currentTime;
      pointerClick(screen.getByRole("button", { name: "Player settings" }));
      expect(screen.getByRole("dialog", { name: "Player settings" })).toBeTruthy();
      mobileTap(right);
      await act(async () => { await vi.advanceTimersByTimeAsync(400); });
      expect(screen.queryByRole("dialog", { name: "Player settings" })).toBeNull();
      expect(player.root.dataset.controlsVisible).toBe("true");
      mobileTap(center, true);
      mobileTap(right, false, 40);
      await act(async () => { await vi.advanceTimersByTimeAsync(400); });
      expect(player.root.dataset.controlsVisible).toBe("true");
      expect(player.video.currentTime).toBe(time);
      expect(player.view.container.querySelector(".mobile-player-seek-feedback")).toBeNull();
    } finally { player.close(); }
  });

  it("places one icon-only phone Settings control in the topbar", () => {
    const width = Object.getOwnPropertyDescriptor(window, "innerWidth")!;
    const height = Object.getOwnPropertyDescriptor(window, "innerHeight")!;
    const touchPoints = Object.getOwnPropertyDescriptor(navigator, "maxTouchPoints");
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 844 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 390 });
    Object.defineProperty(navigator, "maxTouchPoints", { configurable: true, value: 1 });
    let view: ReturnType<typeof render> | undefined;
    try {
      const settingsPlayback = {
        ...playback,
        runResponse: { ...playback.runResponse, ticketExpiresAt: Math.floor(Date.now() / 1_000) + 3_600 },
      };
      view = render(<MemoryRouter><PlayerPage visualFixture={settingsPlayback} /></MemoryRouter>);
      const settings = screen.getByRole("button", { name: "Player settings" });
      expect(screen.getAllByRole("button", { name: "Player settings" })).toHaveLength(1);
      expect(view.container.querySelector<HTMLElement>("[data-player-root]")?.dataset.mobilePlayer).toBe("true");
      expect(settings.closest(".mobile-player-topbar")).not.toBeNull();
      expect(settings.closest(".mobile-player-bottom")).toBeNull();
      expect(settings.closest(".player-topbar")).toBeNull();
      expect(settings.textContent).toBe("");
      pointerClick(settings);
      expect(screen.getByRole("dialog", { name: "Player settings" })).toBeTruthy();
    } finally {
      view?.unmount();
      Object.defineProperty(window, "innerWidth", width);
      Object.defineProperty(window, "innerHeight", height);
      if (touchPoints) Object.defineProperty(navigator, "maxTouchPoints", touchPoints);
      else Reflect.deleteProperty(navigator, "maxTouchPoints");
    }
  });

  it("retains playing chrome for focused or open topbar Settings and releases it after pointer completion", async () => {
    const settingsPlayback = {
      ...playback,
      runResponse: { ...playback.runResponse, ticketExpiresAt: Math.floor(Date.now() / 1_000) + 3_600 },
    };
    const view = render(<MemoryRouter><PlayerPage visualFixture={settingsPlayback} /></MemoryRouter>);
    try {
      await act(async () => { await import("hls.js"); });
      vi.useFakeTimers();
      const root = view.container.querySelector<HTMLElement>("[data-player-root='true']")!;
      const video = view.container.querySelector("video") as HTMLVideoElement;
      showPlayingVideo(video);
      const source = video.getAttribute("src");
      const loads = videoCallCount("load", video);
      const settings = screen.getByRole("button", { name: "Player settings" });
      const advancePlaying = () => {
        for (let update = 0; update < 4; update += 1) {
          video.currentTime += 1;
          fireEvent.timeUpdate(video);
          act(() => vi.advanceTimersByTime(800));
        }
      };

      pointerClick(screen.getByRole("button", { name: /Audio language/ }));
      expect(screen.getByRole("listbox", { name: "Audio language" })).toBeTruthy();
      pointerClick(settings);
      expect(screen.queryByRole("listbox", { name: "Audio language" })).toBeNull();
      const dialog = screen.getByRole("dialog", { name: "Player settings" });
      fireEvent.keyDown(dialog, { key: "Escape" });
      expect(document.activeElement).toBe(settings);
      advancePlaying();
      expect(root.dataset.controlsVisible).toBe("true");
      expect(document.activeElement).toBe(settings);

      pressFocusedKey("ArrowDown");
      act(() => vi.advanceTimersByTime(20));
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "0.5×" }));
      advancePlaying();
      expect(root.dataset.controlsVisible).toBe("true");
      expect(screen.getByRole("dialog", { name: "Player settings" })).toBeTruthy();

      pointerClick(screen.getByRole("button", { name: "1.5×" }));
      expect(screen.queryByRole("dialog", { name: "Player settings" })).toBeNull();
      expect(document.activeElement).toBe(root);
      advancePlaying();
      expect(root.dataset.playerPhase).toBe("playing");
      expect(root.dataset.controlsVisible).toBe("false");
      expect(view.container.querySelector("video")).toBe(video);
      expect(video.getAttribute("src")).toBe(source);
      expect(videoCallCount("load", video)).toBe(loads);
    } finally {
      view.unmount();
    }
  });

  it("keeps paused and keyboard-focused controls visible", () => {
    vi.useFakeTimers();
    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={playback} />
      </MemoryRouter>,
    );
    const root = view.container.querySelector("[data-player-root='true']") as HTMLElement;
    const video = view.container.querySelector("video") as HTMLVideoElement;
    const timeline = screen.getByRole("slider", { name: "Playback position" }) as HTMLInputElement;

    Object.defineProperty(video, "duration", { configurable: true, value: 120 });
    Object.defineProperty(video, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_FUTURE_DATA });
    Object.defineProperty(video, "paused", { configurable: true, value: false });
    fireEvent.loadedMetadata(video);
    video.currentTime = 12;
    fireEvent.seeked(video);
    fireEvent.pause(video);
    fireEvent.mouseMove(root, { clientX: 20, clientY: 20 });
    act(() => vi.advanceTimersByTime(PLAYER_CONTROLS_IDLE_MS + 1));
    expect(root.dataset.controlsVisible).toBe("true");

    fireEvent.play(video);
    fireEvent.playing(video);
    video.currentTime = 13;
    fireEvent.timeUpdate(video);
    const audioMenu = screen.getByRole("button", { name: /Audio language/ });
    audioMenu.focus();
    act(() => vi.advanceTimersByTime(PLAYER_CONTROLS_IDLE_MS + 1));
    expect(root.dataset.controlsVisible).toBe("true");
    expect(document.activeElement).toBe(audioMenu);

    const exit = screen.getByRole("button", { name: "Exit player" });
    act(() => exit.focus());
    video.currentTime = 14;
    fireEvent.timeUpdate(video);
    act(() => vi.advanceTimersByTime(PLAYER_CONTROLS_IDLE_MS + 1));
    expect(root.dataset.controlsVisible).toBe("true");
    expect(document.activeElement).toBe(exit);

    expect(timeline.isConnected).toBe(true);
    view.unmount();
  });

  it("hides idle playing controls while recurring media updates continue", () => {
    vi.useFakeTimers();
    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={playback} />
      </MemoryRouter>,
    );
    const root = view.container.querySelector("[data-player-root='true']") as HTMLElement;
    const video = view.container.querySelector("video") as HTMLVideoElement;

    Object.defineProperty(video, "duration", { configurable: true, value: 120 });
    Object.defineProperty(video, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_FUTURE_DATA });
    Object.defineProperty(video, "paused", { configurable: true, value: false });
    Object.defineProperty(video, "currentTime", { configurable: true, writable: true, value: 12 });
    fireEvent.loadedMetadata(video);
    fireEvent.play(video);
    fireEvent.playing(video);
    fireEvent.timeUpdate(video);
    expect(root.dataset.playerPhase).toBe("playing");

    fireEvent.mouseMove(root, { clientX: 20, clientY: 20 });
    fireEvent.mouseMove(root, { clientX: 21, clientY: 20 });
    expect(root.dataset.controlsVisible).toBe("true");

    for (let update = 1; update <= 4; update += 1) {
      act(() => vi.advanceTimersByTime(750));
      video.currentTime = 12 + update;
      fireEvent.timeUpdate(video);
    }
    act(() => vi.advanceTimersByTime(1));

    expect(root.dataset.playerPhase).toBe("playing");
    expect(root.dataset.controlsVisible).toBe("false");
    fireEvent.keyDown(document, { key: "Tab" });
    act(() => vi.advanceTimersByTime(20));
    expect(root.dataset.controlsVisible).toBe("true");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Exit player" }));
    view.unmount();
  });

  it("keeps an already-ready HLS quality actionable without a preparation request", async () => {
    const onDemandPlayback: ResolvedPlayback = {
      ...playback,
      runResponse: {
        ...playback.runResponse,
        manifestUrl: "/api/playback/manifest/mounted-player-media?ticket=mounted-player-ticket",
        renditions: [
          ...playback.runResponse.renditions,
          {
            id: "video_240p",
            label: "240p",
            height: 240,
            width: 426,
            original: false,
            ready: true,
            status: "ready",
          },
        ],
      },
    };
    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={onDemandPlayback} />
      </MemoryRouter>,
    );

    const qualityMenu = await screen.findByRole("button", { name: "Quality: Auto" });
    fireEvent.click(qualityMenu);
    const uncachedQuality = screen.getByRole("option", { name: /240p/ });
    expect(uncachedQuality.getAttribute("aria-disabled")).not.toBe("true");
    fireEvent.click(uncachedQuality);

    view.unmount();
  });

  it("does not expose qualities that are absent from the prepared HLS master", async () => {
    const onDemandPlayback: ResolvedPlayback = {
      ...playback,
      runResponse: {
        ...playback.runResponse,
        fullyPrepared: false,
        renditions: [
          ...playback.runResponse.renditions,
          {
            id: "video_240p",
            label: "240p",
            height: 240,
            width: 426,
            original: false,
            ready: false,
            status: "idle",
          },
        ],
      },
    };
    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={onDemandPlayback} />
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Quality: Auto" }));
    expect(screen.queryByRole("option", { name: /240p/ })).toBeNull();
    view.unmount();
  });

  it("starts an incompatible source on its ready adaptive transport without probing progressive playback", async () => {
    const adaptivePlayback: ResolvedPlayback = {
      ...playback,
      runResponse: {
        ...playback.runResponse,
        sourceMetadata: {
          ...playback.runResponse.sourceMetadata,
          container: "matroska,webm",
          codec: "hevc",
          sourceFormat: "MKV",
        },
        manifestUrl: "/api/playback/manifest/mounted-player-media?ticket=mounted-player-ticket",
      },
    };
    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={adaptivePlayback} />
      </MemoryRouter>,
    );

    const video = view.container.querySelector("video") as HTMLVideoElement;
    await waitFor(() => expect(video.getAttribute("src")).not.toBe(playback.runResponse.progressiveUrl));
    expect(screen.queryByText("The source stream was not playable. Switching to the prepared adaptive stream.")).toBeNull();

    view.unmount();
  });

  it("creates the playback run without waiting for the catalog request to finish", async () => {
    let resolveMovie!: (movie: Awaited<ReturnType<typeof moviesApi.getMovie>>) => void;
    vi.spyOn(moviesApi, "getMovie").mockReturnValue(new Promise((resolve) => {
      resolveMovie = resolve;
    }));
    const createRun = vi.spyOn(playbackApi, "createPlaybackRun").mockResolvedValue({
      ...playback.runResponse,
      runId: "parallel-startup-run",
      mediaId: "m_parallel-startup",
      movieId: "m_parallel-startup",
    });
    const close = vi.spyOn(playbackApi, "closePlaybackRun").mockResolvedValue({
      status: "abandoned",
      acceptedSeconds: 0,
      nextSequenceNumber: 2,
    });

    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=m_parallel-startup"]}>
        <PlayerPage />
      </MemoryRouter>,
    );

    await waitFor(() => expect(createRun).toHaveBeenCalledWith(
      "m_parallel-startup",
      profile.id,
      undefined,
      expect.any(AbortSignal),
    ));
    expect(screen.queryByText("Parallel startup")).toBeNull();

    resolveMovie({
      id: "m_parallel-startup",
      title: "Parallel startup",
      description: "",
      thumbnailUrl: "",
      bannerUrl: null,
      videoUrl: "/media/parallel-startup.mp4",
      genres: [],
      duration: "2m",
      releaseYear: 2026,
      rating: null,
      cast: [],
      director: null,
      type: "movie",
      quality: "360p",
      languages: ["en"],
      subtitles: [],
      voteAverage: 0,
      voteCount: 0,
      skipMarkers: {},
    });
    expect(await screen.findByText("Parallel startup")).toBeTruthy();

    view.unmount();
    await waitFor(() => expect(close).toHaveBeenCalledWith(
      "parallel-startup-run",
      expect.objectContaining({ event: "exit" }),
    ));
  });

  it("keeps completed progressive playback mounted without preparation polling", async () => {
    const incompleteRun = {
      ...playback.runResponse,
      runId: "progressive-poll-run",
      mediaId: "m_progressive-poll",
      movieId: "m_progressive-poll",
      ticketExpiresAt: Math.floor(Date.now() / 1_000) + 3_600,
      renditions: [],
      manifestUrl: null,
      preparationState: "ready" as const,
      preparationProgress: {
        stage: "streamable" as const,
        queuePosition: 0,
        readySegments: 0,
        activeWorkers: 1,
      },
      switchingReady: false,
      fullyPrepared: false,
    };
    vi.spyOn(moviesApi, "getMovie").mockResolvedValue({
      id: "m_progressive-poll",
      title: "Progressive polling",
      description: "",
      thumbnailUrl: "",
      bannerUrl: null,
      videoUrl: "/media/progressive-poll.mp4",
      genres: [],
      duration: "2m",
      releaseYear: 2026,
      rating: null,
      cast: [],
      director: null,
      type: "movie",
      quality: "360p",
      languages: ["en"],
      subtitles: [],
      voteAverage: 0,
      voteCount: 0,
      skipMarkers: {},
    });
    vi.spyOn(playbackApi, "createPlaybackRun").mockResolvedValue(incompleteRun);
    const poll = vi.spyOn(playbackApi, "getPlaybackRun").mockImplementation(async () => ({
      ...incompleteRun,
      preparationProgress: { ...incompleteRun.preparationProgress },
    }));
    const progress = vi.spyOn(playbackApi, "updatePlaybackProgress").mockResolvedValue({
      status: "ok",
      acceptedSeconds: 0,
      nextSequenceNumber: 2,
    });
    const intervals = vi.spyOn(window, "setInterval");
    const close = vi.spyOn(playbackApi, "closePlaybackRun").mockResolvedValue({
      status: "abandoned",
      acceptedSeconds: 0,
      nextSequenceNumber: 2,
    });

    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=m_progressive-poll"]}>
        <PlayerPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Progressive polling")).toBeTruthy();
    const video = view.container.querySelector("video") as HTMLVideoElement;
    await waitFor(() => expect(video.getAttribute("src")).toBe(incompleteRun.progressiveUrl));
    const load = vi.mocked(HTMLMediaElement.prototype.load);
    const initialVideoLoadCount = load.mock.contexts.filter((context) => context === video).length;
    await act(async () => new Promise((resolve) => window.setTimeout(resolve, 400)));
    expect(poll).not.toHaveBeenCalled();
    expect(video.getAttribute("src")).toBe(incompleteRun.progressiveUrl);
    expect(load.mock.contexts.filter((context) => context === video)).toHaveLength(initialVideoLoadCount);
    const heartbeatIntervals = intervals.mock.calls.filter(([, delay]) => delay === 10_000);
    expect(heartbeatIntervals).toHaveLength(1);
    await act(async () => {
      (heartbeatIntervals[0][0] as () => void)();
      await Promise.resolve();
    });
    await waitFor(() => expect(progress).toHaveBeenCalledWith(
      "progressive-poll-run",
      expect.objectContaining({ event: "heartbeat" }),
      false,
    ));

    view.unmount();
    await waitFor(() => expect(close).toHaveBeenCalledWith(
      "progressive-poll-run",
      expect.objectContaining({ event: "exit" }),
    ));
  });

  it("does not report playing until the media clock advances and detects a frozen clock", async () => {
    vi.useFakeTimers();
    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={playback} />
      </MemoryRouter>,
    );
    const root = view.container.querySelector("[data-player-root='true']") as HTMLElement;
    const video = view.container.querySelector("video") as HTMLVideoElement;
    Object.defineProperty(video, "duration", { configurable: true, value: 120 });
    Object.defineProperty(video, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_FUTURE_DATA });
    fireEvent.loadedMetadata(video);
    fireEvent.canPlay(video);
    await act(async () => undefined);
    Object.defineProperty(video, "paused", { configurable: true, value: false });
    fireEvent.play(video);
    fireEvent.playing(video);

    expect(root.dataset.playerPhase).toBe("loading");
    expect(view.container.querySelector(".player-loading-overlay")).toBeNull();
    expect(screen.getByText("Loading stream").classList.contains("sr-only")).toBe(true);
    act(() => vi.advanceTimersByTime(PLAYBACK_CLOCK_ADVANCE_TIMEOUT_MS + 1));
    expect(root.dataset.playerPhase).toBe("buffering");
    expect(view.container.querySelector(".player-loading-overlay .player-loading-spinner")).not.toBeNull();
    expect(screen.getByText("Buffering").classList.contains("sr-only")).toBe(true);

    view.unmount();
  });

  it("activates a direct external dubbing file without replacing the video transport", async () => {
    const dualAudioPlayback: ResolvedPlayback = {
      ...playback,
      runResponse: {
        ...playback.runResponse,
        manifestUrl: null,
        tracks: [
          ...playback.runResponse.tracks,
          {
            id: "audio_0_tr",
            label: "Turkish",
            language: "tr",
            channels: 2,
            default: false,
            source: "external",
            streamIndex: 0,
            directUrl: "/api/playback/source/mounted-player-media?ticket=mounted-player-ticket&source_id=audio_0_tr",
            ready: true,
            status: "ready",
          },
        ],
      },
    };
    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={dualAudioPlayback} />
      </MemoryRouter>,
    );

    const audioMenu = await screen.findByRole("button", { name: /Audio language/ });
    fireEvent.click(audioMenu);
    const turkish = screen.getByRole("option", { name: /Turkish/ });
    expect(turkish.getAttribute("aria-disabled")).not.toBe("true");
    fireEvent.click(turkish);

    const video = view.container.querySelector("video") as HTMLVideoElement;
    const audio = view.container.querySelector("audio") as HTMLAudioElement;
    expect(audio.getAttribute("src")).toContain("source_id=audio_0_tr");
    expect(video.getAttribute("src")).toBe(playback.runResponse.progressiveUrl);
    expect(video.muted).toBe(true);

    Object.defineProperty(video, "paused", { configurable: true, value: false });
    Object.defineProperty(video, "currentTime", { configurable: true, writable: true, value: 30 });
    Object.defineProperty(audio, "currentTime", { configurable: true, writable: true, value: 27 });
    Object.defineProperty(audio, "readyState", {
      configurable: true,
      value: HTMLMediaElement.HAVE_FUTURE_DATA,
    });
    fireEvent.seeked(video);
    audio.currentTime = 27;
    fireEvent.playing(video);
    fireEvent.playing(audio);
    expect(audio.currentTime).toBe(30);

    view.unmount();
  });

  it("realigns external dubbing only when the newest rapid seek settles", async () => {
    const dualAudioPlayback: ResolvedPlayback = {
      ...playback,
      runResponse: {
        ...playback.runResponse,
        tracks: [
          ...playback.runResponse.tracks,
          {
            id: "audio_0_tr",
            label: "Turkish",
            language: "tr",
            channels: 2,
            default: false,
            source: "external",
            streamIndex: 0,
            directUrl: "/api/playback/source/mounted-player-media?ticket=mounted-player-ticket&source_id=audio_0_tr",
            ready: true,
            status: "ready",
          },
        ],
      },
    };
    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={dualAudioPlayback} />
      </MemoryRouter>,
    );
    const video = view.container.querySelector("video") as HTMLVideoElement;
    const audio = view.container.querySelector("audio") as HTMLAudioElement;
    Object.defineProperty(video, "duration", { configurable: true, value: 120 });
    Object.defineProperty(video, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_FUTURE_DATA });
    Object.defineProperty(video, "paused", { configurable: true, value: false });
    Object.defineProperty(audio, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_FUTURE_DATA });
    Object.defineProperty(audio, "currentTime", { configurable: true, writable: true, value: 0 });
    fireEvent.loadedMetadata(video);
    video.currentTime = 12;
    fireEvent.seeked(video);

    fireEvent.click(await screen.findByRole("button", { name: /Audio language/ }));
    fireEvent.click(screen.getByRole("option", { name: /Turkish/ }));
    audio.currentTime = 3;
    fireEvent.keyDown(document.body, { key: "ArrowRight" });
    expect(video.currentTime).toBe(22);

    video.currentTime = 12;
    fireEvent.seeked(video);
    expect(audio.currentTime).toBe(3);
    video.currentTime = 22;
    fireEvent.seeked(video);
    expect(audio.currentTime).toBe(22);

    audio.currentTime = 3;
    video.currentTime = 23;
    fireEvent.timeUpdate(video);
    expect(audio.currentTime).toBe(23);
    audio.currentTime = 3;
    video.currentTime = 24;
    fireEvent.timeUpdate(video);
    expect(audio.currentTime).toBe(3);
    view.unmount();
  });

  it("keeps video playing when an external dubbing file repeatedly buffers", async () => {
    const dualAudioPlayback: ResolvedPlayback = {
      ...playback,
      runResponse: {
        ...playback.runResponse,
        manifestUrl: null,
        tracks: [
          ...playback.runResponse.tracks,
          {
            id: "audio_0_tr",
            label: "Turkish",
            language: "tr",
            channels: 2,
            default: false,
            source: "external",
            streamIndex: 0,
            directUrl: "/api/playback/source/mounted-player-media?ticket=mounted-player-ticket&source_id=audio_0_tr",
            ready: true,
            status: "ready",
          },
        ],
      },
    };
    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={dualAudioPlayback} />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole("button", { name: /Audio language/ }));
    fireEvent.click(screen.getByRole("option", { name: /Turkish/ }));
    const root = view.container.querySelector("[data-player-root='true']") as HTMLElement;
    const video = view.container.querySelector("video") as HTMLVideoElement;
    const audio = view.container.querySelector("audio") as HTMLAudioElement;
    Object.defineProperty(audio, "readyState", {
      configurable: true,
      value: HTMLMediaElement.HAVE_FUTURE_DATA,
    });
    const play = vi.mocked(HTMLMediaElement.prototype.play);
    let pendingAudioPlayCaptured = false;
    let rejectPendingAudioPlay = (_reason?: unknown): void => {
      throw new Error("Expected an external-audio play request to be pending.");
    };
    play.mockImplementation(function (this: HTMLMediaElement) {
      if (this === audio) {
        return new Promise<void>((_resolve, reject) => {
          pendingAudioPlayCaptured = true;
          rejectPendingAudioPlay = reject;
        });
      }
      return Promise.resolve();
    });
    play.mockClear();
    Object.defineProperty(video, "duration", { configurable: true, value: 120 });
    fireEvent.loadedMetadata(video);
    fireEvent.canPlay(video);
    Object.defineProperty(video, "paused", { configurable: true, value: false });
    fireEvent.play(video);
    fireEvent.playing(video);
    Object.defineProperty(video, "currentTime", { configurable: true, writable: true, value: 12.5 });
    fireEvent.timeUpdate(video);
    await waitFor(() => expect(root.dataset.playerPhase).toBe("playing"));
    fireEvent.canPlay(audio);
    fireEvent.canPlay(audio);
    expect(play.mock.contexts.filter((context) => context === audio)).toHaveLength(1);

    const pause = vi.mocked(HTMLMediaElement.prototype.pause);
    pause.mockClear();
    fireEvent.waiting(audio);
    fireEvent.stalled(audio);
    fireEvent.waiting(audio);

    expect(pause).not.toHaveBeenCalled();
    expect(root.dataset.playerPhase).toBe("playing");
    expect(screen.queryByText("Buffering")).toBeNull();

    fireEvent.waiting(video);
    expect(root.dataset.playerPhase).toBe("buffering");
    expect(screen.getAllByText("Buffering").length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: "Pause" }).length).toBeGreaterThan(0);
    expect(pendingAudioPlayCaptured).toBe(true);
    rejectPendingAudioPlay(new DOMException("The play() request was interrupted by a call to pause().", "AbortError"));
    await waitFor(() => expect(root.dataset.playerPhase).toBe("buffering"));
    expect(screen.queryByText("Press play once to allow the selected dubbing track.")).toBeNull();
    view.unmount();
  });

  it("enters viewport fullscreen, releases button focus, and honors global fullscreen keys", async () => {
    Object.defineProperty(document, "fullscreenEnabled", { configurable: true, value: false });
    Object.defineProperty(document, "webkitFullscreenEnabled", { configurable: true, value: false });
    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={playback} />
      </MemoryRouter>,
    );

    const player = view.container.querySelector(".player-view") as HTMLElement;
    const video = view.container.querySelector("video") as HTMLVideoElement;
    Object.defineProperties(player, {
      requestFullscreen: { configurable: true, value: undefined },
      webkitRequestFullscreen: { configurable: true, value: undefined },
    });
    Object.defineProperties(video, {
      requestFullscreen: { configurable: true, value: undefined },
      webkitEnterFullscreen: { configurable: true, value: undefined },
    });
    fireEvent.click(screen.getByRole("button", { name: "Fullscreen" }));

    await waitFor(
      () => expect(screen.getByRole("button", { name: "Exit fullscreen" })).toBeTruthy(),
      { timeout: 4_000 },
    );
    expect(player.getAttribute("data-player-viewport-fullscreen")).toBe("true");
    expect(document.documentElement.getAttribute("data-player-viewport-fullscreen")).toBe("true");
    expect(document.body.getAttribute("data-player-viewport-fullscreen")).toBe("true");
    fireEvent(document, new Event("fullscreenerror"));
    expect(screen.queryByText("The browser rejected the fullscreen request. Check the fullscreen permission for this site.")).toBeNull();

    const focusedFullscreenButton = screen.getByRole("button", { name: "Exit fullscreen" });
    expect(document.activeElement).not.toBe(focusedFullscreenButton);
    expect(document.activeElement).toBe(player);
    const play = vi.mocked(HTMLMediaElement.prototype.play);
    play.mockClear();
    pressFocusedKey(" ");
    pressFocusedKey(" ");
    await waitFor(() => expect(play).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "Exit fullscreen" })).toBeTruthy();
    act(() => focusedFullscreenButton.focus());
    pressFocusedKey("f");

    await waitFor(() => expect(screen.getByRole("button", { name: "Fullscreen" })).toBeTruthy());
    expect(player.getAttribute("data-player-viewport-fullscreen")).toBeNull();
    expect(document.documentElement.getAttribute("data-player-viewport-fullscreen")).toBeNull();
    expect(document.body.getAttribute("data-player-viewport-fullscreen")).toBeNull();
    view.unmount();
  });
  it("renews a paused run without loading or replacing its stable transport", async () => {
    const run = { ...playback.runResponse, resumePosition: 0, streamBaseUrl: "/api/playback/streams/renew-run", runId: "renew-run", ticketExpiresAt: Math.ceil(Date.now() / 1000) + 182, progressiveUrl: "/api/playback/streams/renew-run/progressive" };
    vi.spyOn(moviesApi, "getMovie").mockReturnValue(new Promise(() => undefined));
    vi.spyOn(playbackApi, "createPlaybackRun").mockResolvedValue(run);
    const refreshed = vi.spyOn(playbackApi, "getPlaybackRun").mockResolvedValue({ ...run, ticket: "new-ticket", ticketExpiresAt: run.ticketExpiresAt + 900 });
    vi.spyOn(playbackApi, "closePlaybackRun").mockResolvedValue({ status: "abandoned", nextSequenceNumber: 2 });
    const timers = vi.spyOn(window, "setTimeout");
    const view = render(<MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=m_renew"]}><PlayerPage /></MemoryRouter>);
    await waitFor(() => expect(view.container.querySelector("video")?.getAttribute("src")).toBe(run.progressiveUrl));
    const video = view.container.querySelector("video")!;
    Object.defineProperty(video, "duration", { configurable: true, value: 120 });
    Object.defineProperty(video, "readyState", { configurable: true, value: 4 });
    fireEvent.loadedMetadata(video);
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    const loads = vi.mocked(HTMLMediaElement.prototype.load);
    const before = loads.mock.contexts.filter((element) => element === video).length;
    const renewal = timers.mock.calls.find(([,delay]) => Number(delay) === 30_000);
    expect(renewal).toBeDefined();
    await act(async () => { (renewal![0] as () => void)(); await Promise.resolve(); });
    expect(refreshed).toHaveBeenCalledWith("renew-run");
    expect(video.getAttribute("src")).toBe(run.progressiveUrl);
    expect(loads.mock.contexts.filter((element) => element === video)).toHaveLength(before);
    expect(view.container.querySelector("[data-player-root]")?.getAttribute("data-player-phase")).toBe("paused");
    const play = vi.mocked(HTMLMediaElement.prototype.play); play.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Play" }));
    await waitFor(() => expect(play).toHaveBeenCalled());
    view.unmount();
  });

  it("closes an obsolete resolve without mounting its transport", async () => {
    let resolveRun!: (value: typeof playback.runResponse) => void;
    vi.spyOn(moviesApi, "getMovie").mockReturnValue(new Promise(() => undefined));
    vi.spyOn(playbackApi, "createPlaybackRun").mockReturnValue(new Promise((resolve) => { resolveRun = resolve; }));
    const close = vi.spyOn(playbackApi, "closePlaybackRun").mockResolvedValue({ status: "abandoned", nextSequenceNumber: 2 });
    const view = render(<MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=m_stale"]}><PlayerPage /></MemoryRouter>);
    view.unmount();
    await act(async () => { resolveRun(playback.runResponse); await Promise.resolve(); });
    expect(close).toHaveBeenCalledWith(playback.runResponse.runId, expect.objectContaining({ event: "exit", totalSecondsPlayed: 0 }), true);
    expect(vi.mocked(HTMLMediaElement.prototype.load)).not.toHaveBeenCalled();
  });

  it.each(["acknowledged", "rejected"] as const)("handles an Exit close that is %s before refreshing profile sessions and sends the confirmed seek clock", async (outcome) => {
    vi.spyOn(moviesApi, "getMovie").mockReturnValue(new Promise(() => undefined));
    vi.spyOn(playbackApi, "createPlaybackRun").mockResolvedValue(playback.runResponse);
    vi.spyOn(playbackApi, "updatePlaybackProgress").mockResolvedValue({ status: "ok", nextSequenceNumber: 2 });
    let resolveClose!: (value: Awaited<ReturnType<typeof playbackApi.closePlaybackRun>>) => void;
    let rejectClose!: (reason: Error) => void;
    const close = vi.spyOn(playbackApi, "closePlaybackRun").mockReturnValue(new Promise((resolve, reject) => {
      resolveClose = resolve;
      rejectClose = reject;
    }));
    const refreshProfile = vi.fn();
    const refreshOtherProfile = vi.fn();
    const unsubscribe = subscribePlaybackSessions(profile.id, refreshProfile);
    const unsubscribeOther = subscribePlaybackSessions("other-profile", refreshOtherProfile);
    const revision = getPlaybackSessionRevision(profile.id);
    const view = render(<MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=m_exit_ack"]}>
      <PlayerPage /><PlayerLocation />
    </MemoryRouter>);
    try {
      await waitFor(() => expect(view.container.querySelector("video")?.getAttribute("src")).toBe(playback.runResponse.progressiveUrl));
      const video = view.container.querySelector("video")!;
      showPlayingVideo(video, 30);
      const timeline = screen.getByRole("slider", { name: "Playback position" });
      fireEvent.change(timeline, { target: { value: "112" } });
      expect(video.currentTime).toBe(112);
      // No seeked event confirms the requested credit position before Exit.
      pointerClick(screen.getByRole("button", { name: "Exit player" }));
      expect(screen.getByTestId("player-location").textContent).toContain("view=home");
      expect(close).toHaveBeenCalledExactlyOnceWith(playback.runResponse.runId, expect.objectContaining({
        timestamp: 30, event: "exit", isFinished: false,
      }));
      expect(refreshProfile).not.toHaveBeenCalled();
      expect(getPlaybackSessionRevision(profile.id)).toBe(revision);

      await act(async () => {
        if (outcome === "acknowledged") resolveClose({ status: "finished", nextSequenceNumber: 3 });
        else rejectClose(new Error("Close request failed"));
        await Promise.resolve();
      });
      expect(refreshProfile).toHaveBeenCalledTimes(outcome === "acknowledged" ? 1 : 0);
      expect(getPlaybackSessionRevision(profile.id)).toBe(revision + (outcome === "acknowledged" ? 1 : 0));
      expect(refreshOtherProfile).not.toHaveBeenCalled();
    } finally {
      view.unmount();
      unsubscribe();
      unsubscribeOther();
    }
  });

  it("refreshes the ended run's profile after another run replaces it without changing the new run's sequence", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
    vi.spyOn(moviesApi, "getMovie").mockReturnValue(new Promise(() => undefined));
    const nextRun = { ...playback.runResponse, runId: "next-ack-run", mediaId: "m_next_ack", movieId: "m_next_ack",
      nextSequenceNumber: 7, progressiveUrl: "/api/playback/progressive/m_next_ack?ticket=next-ticket" };
    const create = vi.spyOn(playbackApi, "createPlaybackRun").mockResolvedValueOnce(playback.runResponse).mockResolvedValue(nextRun);
    let acknowledgeEnded!: (value: Awaited<ReturnType<typeof playbackApi.updatePlaybackProgress>>) => void;
    const endedResponse = new Promise<Awaited<ReturnType<typeof playbackApi.updatePlaybackProgress>>>((resolve) => { acknowledgeEnded = resolve; });
    const progress = vi.spyOn(playbackApi, "updatePlaybackProgress").mockImplementation((runId, request) =>
      runId === playback.runResponse.runId && request.event === "ended" ? endedResponse
        : Promise.resolve({ status: "ok", nextSequenceNumber: runId === nextRun.runId ? 8 : 2 }));
    const close = vi.spyOn(playbackApi, "closePlaybackRun").mockResolvedValue({ status: "abandoned", nextSequenceNumber: 3 });
    const refresh = vi.fn();
    const unsubscribe = subscribePlaybackSessions(profile.id, refresh);
    const revision = getPlaybackSessionRevision(profile.id);
    const view = render(<MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=m_ended_ack"]}>
      <PlayerPage /><OpenNextTitle />
    </MemoryRouter>);
    try {
      await waitFor(() => expect(view.container.querySelector("video")?.getAttribute("src")).toBe(playback.runResponse.progressiveUrl));
      const video = view.container.querySelector("video")!;
      showPlayingVideo(video, 30);
      video.currentTime = 120;
      fireEvent.timeUpdate(video);
      fireEvent.ended(video);
      await waitFor(() => expect(progress).toHaveBeenCalledWith(playback.runResponse.runId,
        expect.objectContaining({ event: "ended", isFinished: true }), true));
      expect(refresh).not.toHaveBeenCalled();
      pointerClick(screen.getByRole("button", { name: "Open next title" }));
      await waitFor(() => expect(view.container.querySelector("video")?.getAttribute("src")).toBe(nextRun.progressiveUrl));
      expect(create).toHaveBeenCalledTimes(2);
      expect(close).not.toHaveBeenCalled();
      await act(async () => { acknowledgeEnded({ status: "finished", nextSequenceNumber: 99 }); await Promise.resolve(); });
      expect(refresh).toHaveBeenCalledTimes(1);
      expect(getPlaybackSessionRevision(profile.id)).toBe(revision + 1);
      const nextVideo = view.container.querySelector("video")!;
      showPlayingVideo(nextVideo, 45);
      fireEvent.pause(nextVideo);
      await waitFor(() => expect(progress).toHaveBeenCalledWith(nextRun.runId,
        expect.objectContaining({ event: "pause", timestamp: 45, sequenceNumber: 7 }), false));
    } finally {
      view.unmount();
      unsubscribe();
    }
  });

  it("applies the preferred native audio and keeps manual quality changes paused", async () => {
    localStorage.setItem(playerPreferencesKey, JSON.stringify({ audioTrackId: "audio_0_en", audioLanguage: "en" }));
    vi.spyOn(HTMLMediaElement.prototype, "canPlayType").mockImplementation((type) => type.includes("mpegurl") ? "probably" : "");
    const tracks = [{ language: "tr", label: "Turkish", enabled: true }, { language: "en", label: "English", enabled: false }];
    Object.defineProperty(HTMLMediaElement.prototype, "audioTracks", { configurable: true, get: () => tracks });
    const native: ResolvedPlayback = { ...playback, runResponse: { ...playback.runResponse, progressiveUrl: "", manifestUrl: "/api/playback/streams/native-run/manifest", sourceMetadata: { ...playback.runResponse.sourceMetadata, progressiveCompatible: false }, tracks: [...playback.runResponse.tracks, { id: "audio_1_tr", label: "Turkish", language: "tr", channels: 2, default: false, source: "embedded", streamIndex: 1, ready: true, status: "ready" }] } };
    const view = render(<MemoryRouter><PlayerPage visualFixture={native} /></MemoryRouter>);
    try {
      const video = view.container.querySelector("video")!;
      Object.defineProperty(video, "duration", { configurable: true, value: 120 });
      Object.defineProperty(video, "readyState", { configurable: true, value: 4 });
      fireEvent.loadedMetadata(video);
      await act(async () => undefined);
      expect(tracks.map((track) => track.enabled)).toEqual([false, true]);
      fireEvent.seeked(video);
      fireEvent.click(screen.getByRole("button", { name: "Pause" }));
      fireEvent.click(screen.getByRole("button", { name: "Quality: Auto" }));
      fireEvent.click(screen.getByRole("option", { name: /360p/ }));
      expect(video.src).toContain("rendition_id=video_original");
      fireEvent.loadedMetadata(video); fireEvent.seeked(video);
      expect(view.container.querySelector("[data-player-root]")?.getAttribute("data-player-phase")).toBe("paused");
      expect(JSON.parse(localStorage.getItem(playerPreferencesKey)!).qualityHeight).toBe(360);
    } finally { view.unmount(); Reflect.deleteProperty(HTMLMediaElement.prototype, "audioTracks"); }
  });

  it("serializes Replay, blocks old heartbeats, and respects Pause during the request", async () => {
    vi.spyOn(moviesApi, "getMovie").mockReturnValue(new Promise(() => undefined));
    vi.spyOn(playbackApi, "createPlaybackRun").mockResolvedValue({ ...playback.runResponse, resumePosition: 0 });
    const progress = vi.spyOn(playbackApi, "updatePlaybackProgress").mockResolvedValue({ status: "ok", nextSequenceNumber: 2 });
    vi.spyOn(playbackApi, "closePlaybackRun").mockResolvedValue({ status: "abandoned", nextSequenceNumber: 3 });
    let finishReplay!: (value: { status: string }) => void;
    const replay = vi.spyOn(playbackApi, "startOverPlaybackRun").mockReturnValue(new Promise((resolve) => { finishReplay = resolve; }));
    const timers = vi.spyOn(window, "setInterval");
    const view = render(<MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=m_replay"]}><PlayerPage /></MemoryRouter>);
    await waitFor(() => expect(view.container.querySelector("video")?.getAttribute("src")).toBe(playback.runResponse.progressiveUrl));
    const video = view.container.querySelector("video")!;
    Object.defineProperty(video, "duration", { configurable: true, value: 120 });
    Object.defineProperty(video, "readyState", { configurable: true, value: 4 });
    fireEvent.loadedMetadata(video); video.currentTime = 120; fireEvent.timeUpdate(video); fireEvent.ended(video);
    await act(async () => undefined); progress.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Replay" }));
    await waitFor(() => expect(replay).toHaveBeenCalled());
    const heartbeat = timers.mock.calls.find(([,delay]) => delay === 10_000)![0] as () => void;
    await act(async () => { heartbeat(); await Promise.resolve(); });
    expect(progress).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    const play = vi.mocked(HTMLMediaElement.prototype.play); play.mockClear();
    await act(async () => { finishReplay({ status: "ok" }); await Promise.resolve(); });
    fireEvent.seeked(video);
    expect(video.currentTime).toBe(0);
    expect(view.container.querySelector("[data-player-root]")?.getAttribute("data-player-phase")).toBe("paused");
    expect(play).not.toHaveBeenCalled();
    await act(async () => { heartbeat(); await Promise.resolve(); });
    for (const [,request] of progress.mock.calls) expect(request.timestamp).toBe(0);
    view.unmount();
  });

  it.each([
    { label: "no frames", oneFramePerTransport: false },
    { label: "one isolated frame", oneFramePerTransport: true },
  ])("bounds picture recovery when each transport presents $label", async ({ oneFramePerTransport }) => {
    const player = await mountFramePlayer();
    try {
      const initialLoads = videoCallCount("load", player.video);
      let recoveryLoads = 0;
      if (oneFramePerTransport) player.frame();
      for (let attempt = 0; attempt < 2; attempt += 1) {
        const loads = player.nextRecovery();
        expect(loads).toBeGreaterThan(0);
        recoveryLoads += loads;
        player.settle();
        if (oneFramePerTransport) player.frame();
      }
      expect(player.nextRecovery()).toBe(0);
      expect(videoCallCount("load", player.video)).toBe(initialLoads + recoveryLoads);
      expect(player.view.container.querySelector(".player-state-view")?.textContent).toMatch(/stalled/i);
    } finally {
      player.close();
    }
  });

  it("renews the picture recovery budget only after sustained advancing rendered frames", async () => {
    const player = await mountFramePlayer();
    try {
      for (let attempt = 0; attempt < 2; attempt += 1) {
        expect(player.nextRecovery()).toBeGreaterThan(0);
        player.settle();
        player.frame();
      }
      const loads = videoCallCount("load", player.video);
      player.advance(3_200, true);
      expect(player.root.dataset.playerPhase).toBe("playing");
      expect(videoCallCount("load", player.video)).toBe(loads);
      expect(player.nextRecovery()).toBeGreaterThan(0);
      expect(videoCallCount("load", player.video)).toBeGreaterThan(loads);
      expect(player.view.container.querySelector(".player-state-view")).toBeNull();
    } finally {
      player.close();
    }
  });

  it.each(["clock", "picture"])("cancels pending %s recovery while hidden and lets Pause cancel its fresh visible deadline", async (kind) => {
    const player = await mountFramePlayer();
    const timeouts = vi.spyOn(window, "setTimeout");
    try {
      if (kind === "picture") player.frame();
      player.advance(3_200, false, kind === "picture");
      expect(player.root.dataset.playerPhase).toBe("buffering");
      const recovery = timeouts.mock.calls.filter(([, delay]) => delay === 4_000).slice(-1)[0]?.[0];
      expect(typeof recovery).toBe("function");
      const loads = videoCallCount("load", player.video);
      player.setVisibility("hidden");
      act(() => { (recovery as () => void)(); });
      player.advance(8_000, false, false);
      expect(videoCallCount("load", player.video)).toBe(loads);
      expect(player.view.container.querySelector(".player-state-view")).toBeNull();

      player.setVisibility("visible");
      player.advance(1_000, false, kind === "picture");
      expect(videoCallCount("load", player.video)).toBe(loads);
      player.advance(3_200, false, kind === "picture");
      expect(player.root.dataset.playerPhase).toBe("buffering");
      const freshRecovery = timeouts.mock.calls.filter(([, delay]) => delay === 4_000).slice(-1)[0]?.[0];
      expect(typeof freshRecovery).toBe("function");
      expect(freshRecovery).not.toBe(recovery);
      pointerClick(screen.getByRole("button", { name: "Pause" }));
      Object.defineProperty(player.video, "paused", { configurable: true, value: true });
      act(() => { (freshRecovery as () => void)(); });
      player.advance(12_000, false, false);
      expect(player.root.dataset.playerPhase).toBe("paused");
      expect(videoCallCount("load", player.video)).toBe(loads);
      expect(player.view.container.querySelector(".player-state-view")).toBeNull();
    } finally {
      player.close();
    }
  });

  it("holds the paused presented frame across visibility changes without playing, loading, or seeking", async () => {
    const player = await mountFramePlayer();
    try {
      player.frame();
      let position = player.video.currentTime;
      const seek = vi.fn((value: number) => { position = value; });
      Object.defineProperty(player.video, "currentTime", { configurable: true, get: () => position, set: seek });
      const source = player.video.getAttribute("src");
      const loads = videoCallCount("load", player.video);
      const plays = videoCallCount("play", player.video);
      const pauses = videoCallCount("pause", player.video);
      const expectedPicture = `picture at ${position.toFixed(2)}`;
      pointerClick(screen.getByRole("button", { name: "Pause" }));
      Object.defineProperty(player.video, "paused", { configurable: true, value: true });
      fireEvent.pause(player.video);
      expect(player.root.dataset.frameHold).toBe("true");
      expect(player.canvas.picture(player.activeCanvas())).toBe(expectedPicture);

      player.setVisibility("hidden");
      player.setVisibility("visible");
      const draws = player.canvas.draws.mock.calls.length;
      player.advance(6_000, false, false);
      expect(player.canvas.draws).toHaveBeenCalledTimes(draws);
      expect(player.root.dataset.playerPhase).toBe("paused");
      expect(player.root.dataset.frameHold).toBe("true");
      expect(player.canvas.picture(player.activeCanvas())).toBe(expectedPicture);
      expect(player.view.container.querySelector("video")).toBe(player.video);
      expect(player.video.getAttribute("src")).toBe(source);
      expect(videoCallCount("play", player.video)).toBe(plays);
      expect(videoCallCount("load", player.video)).toBe(loads);
      expect(videoCallCount("pause", player.video)).toBe(pauses + 1);
      expect(seek).not.toHaveBeenCalled();
    } finally {
      player.close();
    }
  });

  it("invalidates the paused snapshot during a seek and publishes the newly presented paused frame", async () => {
    const player = await mountFramePlayer();
    try {
      player.frame();
      pointerClick(screen.getByRole("button", { name: "Pause" }));
      Object.defineProperty(player.video, "paused", { configurable: true, value: true });
      fireEvent.pause(player.video);
      const oldPicture = player.canvas.picture(player.activeCanvas());
      const source = player.video.getAttribute("src");
      const loads = videoCallCount("load", player.video);
      const plays = videoCallCount("play", player.video);
      pointerClick(screen.getByRole("button", { name: "Forward 10 seconds" }));
      expect(player.root.dataset.frameHold).toBe("false");
      player.canvas.present(`picture at ${player.video.currentTime.toFixed(2)}`);
      fireEvent.seeked(player.video);
      player.frame();
      expect(player.root.dataset.playerPhase).toBe("paused");
      expect(player.root.dataset.frameHold).toBe("true");
      expect(player.canvas.picture(player.activeCanvas())).not.toBe(oldPicture);
      expect(player.canvas.picture(player.activeCanvas())).toBe(`picture at ${player.video.currentTime.toFixed(2)}`);

      let position = player.video.currentTime + 5;
      const nativeTarget = position;
      const unexpectedSeek = vi.fn((value: number) => { position = value; });
      Object.defineProperty(player.video, "currentTime", { configurable: true, get: () => position, set: unexpectedSeek });
      Object.defineProperty(player.video, "seeking", { configurable: true, value: true });
      fireEvent.seeking(player.video);
      expect(player.root.dataset.frameHold).toBe("false");
      player.advance(1_000, false, false);
      expect(player.root.dataset.frameHold).toBe("false");
      expect(player.root.dataset.playerPhase).toBe("paused");
      expect(player.video.currentTime).toBe(nativeTarget);
      expect(unexpectedSeek).not.toHaveBeenCalled();
      Object.defineProperty(player.video, "seeking", { configurable: true, value: false });
      player.canvas.present(`picture at ${nativeTarget.toFixed(2)}`);
      fireEvent.seeked(player.video);
      player.frame();
      expect(player.root.dataset.frameHold).toBe("true");
      expect(player.canvas.picture(player.activeCanvas())).toBe(`picture at ${nativeTarget.toFixed(2)}`);
      expect(unexpectedSeek).not.toHaveBeenCalled();
      expect(player.video.getAttribute("src")).toBe(source);
      expect(videoCallCount("load", player.video)).toBe(loads);
      expect(videoCallCount("play", player.video)).toBe(plays);
    } finally {
      player.close();
    }
  });

  it.each([
    { origin: "application", readyState: HTMLMediaElement.HAVE_METADATA, snapshot: "pause" },
    { origin: "application", readyState: HTMLMediaElement.HAVE_CURRENT_DATA, snapshot: "visibility" },
    { origin: "native", readyState: HTMLMediaElement.HAVE_METADATA, snapshot: "visibility" },
    { origin: "native", readyState: HTMLMediaElement.HAVE_CURRENT_DATA, snapshot: "pause" },
  ])("keeps the old $snapshot picture hidden throughout a playing $origin seek at readyState $readyState", async ({ origin, readyState, snapshot }) => {
    const player = await mountFramePlayer();
    try {
      player.frame();
      if (snapshot === "pause") {
        pointerClick(screen.getByRole("button", { name: "Pause" }));
        Object.defineProperty(player.video, "paused", { configurable: true, value: true });
        fireEvent.pause(player.video);
        expect(player.root.dataset.frameHold).toBe("true");
        pointerClick(screen.getByRole("button", { name: "Play" }));
        Object.defineProperty(player.video, "paused", { configurable: true, value: false });
        fireEvent.play(player.video);
        fireEvent.playing(player.video);
      } else {
        player.setVisibility("hidden");
        player.setVisibility("visible");
      }
      const oldPicture = player.canvas.picture(player.activeCanvas());
      expect(oldPicture).toBeDefined();
      player.video.currentTime += 0.2;
      fireEvent.timeUpdate(player.video);
      expect(player.root.dataset.playerPhase).toBe("playing");
      const oldTime = player.video.currentTime;
      const source = player.video.getAttribute("src");
      const loads = videoCallCount("load", player.video);
      const plays = videoCallCount("play", player.video);
      const draws = player.canvas.draws.mock.calls.length;
      Object.defineProperty(player.video, "readyState", { configurable: true, value: readyState });

      if (origin === "application") pointerClick(screen.getByRole("button", { name: "Forward 10 seconds" }));
      else player.video.currentTime += 10;
      const target = player.video.currentTime;
      Object.defineProperty(player.video, "seeking", { configurable: true, value: true });
      fireEvent.seeking(player.video);
      expect(player.root.dataset.frameHold).toBe("false");
      fireEvent.waiting(player.video);
      expect(player.root.dataset.frameHold).toBe("false");
      fireEvent.stalled(player.video);
      expect(player.root.dataset.frameHold).toBe("false");

      Object.defineProperty(player.video, "seeking", { configurable: true, value: false });
      fireEvent.seeked(player.video);
      fireEvent.playing(player.video);
      expect(player.root.dataset.frameHold).toBe("false");
      fireEvent.timeUpdate(player.video);
      fireEvent.waiting(player.video);
      expect(player.root.dataset.frameHold).toBe("false");
      Object.defineProperty(player.video, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_CURRENT_DATA });
      player.frame(oldTime);
      fireEvent.waiting(player.video);
      expect(player.root.dataset.frameHold).toBe("false");
      expect(player.canvas.draws).toHaveBeenCalledTimes(draws);

      player.frame(target);
      fireEvent.waiting(player.video);
      expect(player.root.dataset.frameHold).toBe("true");
      expect(player.canvas.picture(player.activeCanvas())).toBe(`picture at ${target.toFixed(2)}`);
      expect(player.canvas.picture(player.activeCanvas())).not.toBe(oldPicture);
      fireEvent.playing(player.video);
      player.video.currentTime += 0.1;
      fireEvent.timeUpdate(player.video);
      const playingDraws = player.canvas.draws.mock.calls.length;
      player.advance(1_200, true);
      expect(player.canvas.draws).toHaveBeenCalledTimes(playingDraws);
      pointerClick(screen.getByRole("button", { name: "Pause" }));
      Object.defineProperty(player.video, "paused", { configurable: true, value: true });
      fireEvent.pause(player.video);
      player.frame();
      const pausedDraws = player.canvas.draws.mock.calls.length;
      player.advance(1_200, true, false);
      expect(player.root.dataset.playerPhase).toBe("paused");
      expect(player.root.dataset.frameHold).toBe("true");
      expect(player.canvas.draws).toHaveBeenCalledTimes(pausedDraws);
      expect(player.view.container.querySelector("video")).toBe(player.video);
      expect(player.video.getAttribute("src")).toBe(source);
      expect(videoCallCount("load", player.video)).toBe(loads);
      expect(videoCallCount("play", player.video)).toBe(plays);
    } finally {
      player.close();
    }
  });

  it("rejects delayed frames from earlier rapid seeks without reviving their old picture", async () => {
    const player = await mountFramePlayer();
    try {
      player.frame();
      player.setVisibility("hidden");
      player.setVisibility("visible");
      const oldPicture = player.canvas.picture(player.activeCanvas());
      const source = player.video.getAttribute("src");
      const loads = videoCallCount("load", player.video);
      const plays = videoCallCount("play", player.video);
      const delayedFrame = player.takeFrame();
      Object.defineProperty(player.video, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_CURRENT_DATA });
      pointerClick(screen.getByRole("button", { name: "Forward 10 seconds" }));
      const firstTarget = player.video.currentTime;
      fireEvent.seeked(player.video);
      pointerClick(screen.getByRole("button", { name: "Forward 10 seconds" }));
      const finalTarget = player.video.currentTime;
      fireEvent.seeked(player.video);
      fireEvent.playing(player.video);
      delayedFrame(firstTarget);
      fireEvent.waiting(player.video);
      expect(player.root.dataset.frameHold).toBe("false");
      player.frame(firstTarget);
      fireEvent.waiting(player.video);
      expect(player.root.dataset.frameHold).toBe("false");
      player.frame(finalTarget);
      fireEvent.waiting(player.video);
      expect(player.root.dataset.frameHold).toBe("true");
      expect(player.canvas.picture(player.activeCanvas())).toBe(`picture at ${finalTarget.toFixed(2)}`);
      expect(player.canvas.picture(player.activeCanvas())).not.toBe(oldPicture);
      expect(player.view.container.querySelector("video")).toBe(player.video);
      expect(player.video.getAttribute("src")).toBe(source);
      expect(videoCallCount("load", player.video)).toBe(loads);
      expect(videoCallCount("play", player.video)).toBe(plays);
    } finally {
      player.close();
    }
  });

  it("keeps a seek-invalidated picture hidden when the fresh capture fails and Pause remains authoritative", async () => {
    const player = await mountFramePlayer();
    try {
      player.frame();
      player.setVisibility("hidden");
      player.setVisibility("visible");
      const oldCanvas = player.activeCanvas();
      const oldPicture = player.canvas.picture(oldCanvas);
      const source = player.video.getAttribute("src");
      const loads = videoCallCount("load", player.video);
      const plays = videoCallCount("play", player.video);
      pointerClick(screen.getByRole("button", { name: "Forward 10 seconds" }));
      Object.defineProperty(player.video, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_CURRENT_DATA });
      fireEvent.seeked(player.video);
      player.canvas.reject(true);
      player.frame();
      fireEvent.waiting(player.video);
      expect(player.root.dataset.frameHold).toBe("false");
      expect(player.canvas.picture(oldCanvas)).toBe(oldPicture);
      pointerClick(screen.getByRole("button", { name: "Pause" }));
      Object.defineProperty(player.video, "paused", { configurable: true, value: true });
      fireEvent.pause(player.video);
      fireEvent.playing(player.video);
      fireEvent.canPlay(player.video);
      player.advance(1_000, false, false);
      expect(player.root.dataset.playerPhase).toBe("paused");
      expect(player.root.dataset.frameHold).toBe("false");
      player.canvas.reject(false);
      player.frame();
      expect(player.root.dataset.frameHold).toBe("true");
      expect(player.canvas.picture(player.activeCanvas())).toBe(`picture at ${player.video.currentTime.toFixed(2)}`);
      expect(player.view.container.querySelector("video")).toBe(player.video);
      expect(player.video.getAttribute("src")).toBe(source);
      expect(videoCallCount("load", player.video)).toBe(loads);
      expect(videoCallCount("play", player.video)).toBe(plays);
    } finally {
      player.close();
    }
  });

  it("keeps Pause authoritative while a playing seek waits for its first target frame", async () => {
    const player = await mountFramePlayer();
    try {
      player.frame();
      player.setVisibility("hidden");
      player.setVisibility("visible");
      const oldTime = player.video.currentTime;
      const source = player.video.getAttribute("src");
      const loads = videoCallCount("load", player.video);
      const plays = videoCallCount("play", player.video);
      pointerClick(screen.getByRole("button", { name: "Forward 10 seconds" }));
      Object.defineProperty(player.video, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_CURRENT_DATA });
      pointerClick(screen.getByRole("button", { name: "Pause" }));
      Object.defineProperty(player.video, "paused", { configurable: true, value: true });
      fireEvent.pause(player.video);
      expect(player.root.dataset.frameHold).toBe("false");
      fireEvent.seeked(player.video);
      fireEvent.playing(player.video);
      fireEvent.waiting(player.video);
      player.frame(oldTime);
      expect(player.root.dataset.playerPhase).toBe("paused");
      expect(player.root.dataset.frameHold).toBe("false");
      player.frame();
      expect(player.root.dataset.frameHold).toBe("true");
      expect(player.canvas.picture(player.activeCanvas())).toBe(`picture at ${player.video.currentTime.toFixed(2)}`);
      player.advance(2_000, true, false);
      expect(player.root.dataset.playerPhase).toBe("paused");
      expect(player.view.container.querySelector("video")).toBe(player.video);
      expect(player.video.getAttribute("src")).toBe(source);
      expect(videoCallCount("load", player.video)).toBe(loads);
      expect(videoCallCount("play", player.video)).toBe(plays);
    } finally {
      player.close();
    }
  });

  it("uses fresh decoded-data readiness after a metadata-only seek when frame callbacks are unavailable", async () => {
    const player = await mountFramePlayer(playback, false);
    try {
      player.canvas.present("old picture before native seek");
      player.setVisibility("hidden");
      player.setVisibility("visible");
      const source = player.video.getAttribute("src");
      const loads = videoCallCount("load", player.video);
      const plays = videoCallCount("play", player.video);
      Object.defineProperty(player.video, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_METADATA });
      player.video.currentTime += 10;
      Object.defineProperty(player.video, "seeking", { configurable: true, value: true });
      fireEvent.seeking(player.video);
      fireEvent.waiting(player.video);
      expect(player.root.dataset.frameHold).toBe("false");
      Object.defineProperty(player.video, "seeking", { configurable: true, value: false });
      fireEvent.seeked(player.video);
      fireEvent.playing(player.video);
      fireEvent.timeUpdate(player.video);
      fireEvent.waiting(player.video);
      expect(player.root.dataset.frameHold).toBe("false");
      Object.defineProperty(player.video, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_CURRENT_DATA });
      fireEvent.timeUpdate(player.video);
      fireEvent.waiting(player.video);
      expect(player.root.dataset.frameHold).toBe("false");
      player.canvas.present("fresh decoded native-seek picture");
      fireEvent.loadedData(player.video);
      fireEvent.waiting(player.video);
      expect(player.root.dataset.frameHold).toBe("true");
      expect(player.canvas.picture(player.activeCanvas())).toBe("fresh decoded native-seek picture");
      pointerClick(screen.getByRole("button", { name: "Pause" }));
      Object.defineProperty(player.video, "paused", { configurable: true, value: true });
      fireEvent.pause(player.video);
      player.advance(2_000, false, false);
      expect(player.root.dataset.playerPhase).toBe("paused");
      expect(player.view.container.querySelector("video")).toBe(player.video);
      expect(player.video.getAttribute("src")).toBe(source);
      expect(videoCallCount("load", player.video)).toBe(loads);
      expect(videoCallCount("play", player.video)).toBe(plays);
    } finally {
      player.close();
    }
  });

  it("replaces a paused quality snapshot only after a frame from the new native transport", async () => {
    vi.spyOn(HTMLMediaElement.prototype, "canPlayType").mockReturnValue("probably");
    const native: ResolvedPlayback = {
      ...playback,
      runResponse: {
        ...playback.runResponse,
        progressiveUrl: "",
        manifestUrl: "/api/playback/streams/paused-picture/manifest",
        sourceMetadata: { ...playback.runResponse.sourceMetadata, progressiveCompatible: false },
        renditions: [
          ...playback.runResponse.renditions,
          { id: "video_240p", label: "240p", height: 240, width: 426, original: false, ready: true, status: "ready" },
        ],
      },
    };
    const player = await mountFramePlayer(native);
    try {
      expect(player.root.dataset.playerTransport).toBe("native-hls");
      player.frame();
      pointerClick(screen.getByRole("button", { name: "Pause" }));
      Object.defineProperty(player.video, "paused", { configurable: true, value: true });
      fireEvent.pause(player.video);
      expect(player.root.dataset.frameHold).toBe("true");
      const plays = videoCallCount("play", player.video);
      pointerClick(screen.getByRole("button", { name: /^Quality:/ }));
      pointerClick(screen.getByRole("option", { name: "240p" }));
      expect(player.root.dataset.frameHold).toBe("false");
      Object.defineProperties(player.video, {
        videoWidth: { configurable: true, value: 426 },
        videoHeight: { configurable: true, value: 240 },
      });
      player.canvas.present("fresh 240p picture");
      fireEvent.loadedMetadata(player.video);
      fireEvent.seeked(player.video);
      fireEvent.loadedData(player.video);
      expect(player.root.dataset.playerPhase).toBe("paused");
      expect(player.root.dataset.frameHold).toBe("true");
      expect(player.canvas.picture(player.activeCanvas())).toBe("fresh 240p picture");
      expect(player.view.container.querySelector("video")).toBe(player.video);
      expect(videoCallCount("play", player.video)).toBe(plays);
    } finally {
      player.close();
    }
  });

  it.each([false, true])("keeps paused capture failure safe when a valid snapshot already exists: %s", async (existingSnapshot) => {
    const player = await mountFramePlayer();
    try {
      player.frame();
      if (existingSnapshot) {
        pointerClick(screen.getByRole("button", { name: "Pause" }));
        Object.defineProperty(player.video, "paused", { configurable: true, value: true });
        fireEvent.pause(player.video);
      }
      const oldCanvas = player.activeCanvas();
      const oldPicture = player.canvas.picture(oldCanvas);
      const oldWidth = oldCanvas?.width;
      const oldHeight = oldCanvas?.height;
      player.canvas.reject(true);
      if (existingSnapshot) player.setVisibility("visible");
      else {
        pointerClick(screen.getByRole("button", { name: "Pause" }));
        Object.defineProperty(player.video, "paused", { configurable: true, value: true });
        fireEvent.pause(player.video);
      }
      expect(player.root.dataset.frameHold).toBe(existingSnapshot ? "true" : "false");
      expect(player.canvas.picture(player.activeCanvas())).toBe(oldPicture);
      Object.defineProperties(player.video, {
        videoWidth: { configurable: true, value: 1280 },
        videoHeight: { configurable: true, value: 720 },
      });
      fireEvent.resize(player.video);
      expect(player.root.dataset.frameHold).toBe("false");
      if (existingSnapshot) {
        expect(player.canvas.picture(oldCanvas)).toBe(oldPicture);
        expect(oldCanvas?.width).toBe(oldWidth);
        expect(oldCanvas?.height).toBe(oldHeight);
      }
      expect(player.root.dataset.playerPhase).toBe("paused");
    } finally {
      player.close();
    }
  });

  it("does not mutate the painted canvas when a second capture fails in the same event batch", async () => {
    const player = await mountFramePlayer();
    try {
      player.frame();
      fireEvent.waiting(player.video);
      expect(player.root.dataset.frameHold).toBe("true");
      player.canvas.publishedMutations.mockClear();
      act(() => {
        player.canvas.present("valid refreshed picture");
        fireEvent.waiting(player.video);
        player.canvas.reject(true);
        Object.defineProperties(player.video, {
          videoWidth: { configurable: true, value: 1280 },
          videoHeight: { configurable: true, value: 720 },
        });
        fireEvent.stalled(player.video);
      });
      expect(player.canvas.publishedMutations).not.toHaveBeenCalled();
      expect(player.canvas.picture(player.activeCanvas())).toBe("valid refreshed picture");
      expect(player.activeCanvas()?.width).toBe(640);
      expect(player.activeCanvas()?.height).toBe(360);
      expect(player.root.dataset.frameHold).toBe("true");
    } finally {
      player.close();
    }
  });

  it("detects a stopped frame callback while the media clock keeps advancing", () => {
    vi.useFakeTimers(); vi.spyOn(performance, "now").mockImplementation(() => Date.now());
    let frame!: (now: number, metadata: VideoFrameCallbackMetadata) => void;
    Object.defineProperty(HTMLVideoElement.prototype, "requestVideoFrameCallback", { configurable: true, value: vi.fn((callback) => { frame = callback; return 1; }) });
    Object.defineProperty(HTMLVideoElement.prototype, "cancelVideoFrameCallback", { configurable: true, value: vi.fn() });
    const view = render(<MemoryRouter><PlayerPage visualFixture={playback} /></MemoryRouter>);
    try {
      const video = view.container.querySelector("video")!;
      Object.defineProperty(video, "duration", { configurable: true, value: 120 });
      Object.defineProperty(video, "readyState", { configurable: true, value: 4 });
      Object.defineProperty(video, "paused", { configurable: true, value: false });
      fireEvent.loadedMetadata(video); fireEvent.seeked(video); fireEvent.playing(video); video.currentTime = 13; fireEvent.timeUpdate(video);
      act(() => frame(performance.now(), { mediaTime: 13 } as VideoFrameCallbackMetadata));
      for (let index=0;index<4;index++) { act(() => vi.advanceTimersByTime(800)); video.currentTime += 1; fireEvent.timeUpdate(video); }
      act(() => vi.advanceTimersByTime(1000));
      expect(view.container.querySelector("[data-player-root]")?.getAttribute("data-player-phase")).toBe("buffering");
      const observedTime = video.currentTime;
      video.currentTime = 1; fireEvent.seeked(video);
      video.currentTime = observedTime + 1; fireEvent.timeUpdate(video);
      expect(view.container.querySelector("[data-player-root]")?.getAttribute("data-player-phase")).toBe("buffering");
      const loads = vi.mocked(HTMLMediaElement.prototype.load).mock.calls.length;
      for (let index=0;index<4;index++) { act(() => vi.advanceTimersByTime(800)); video.currentTime += 1; fireEvent.timeUpdate(video); }
      expect(vi.mocked(HTMLMediaElement.prototype.load).mock.calls.length).toBeGreaterThan(loads);
      for (let index=0;index<45;index++) { act(() => vi.advanceTimersByTime(400)); video.currentTime += .4; fireEvent.timeUpdate(video); }
      expect(view.container.querySelector(".player-state-view")?.textContent).toContain("Playback connection stalled");
    } finally {
      view.unmount(); Reflect.deleteProperty(HTMLVideoElement.prototype, "requestVideoFrameCallback"); Reflect.deleteProperty(HTMLVideoElement.prototype, "cancelVideoFrameCallback");
    }
  });

  it("accepts a native backward seek and keeps the displayed clock confirmed", () => {
    const view = render(<MemoryRouter><PlayerPage visualFixture={playback} /></MemoryRouter>);
    const video = view.container.querySelector("video")!;
    Object.defineProperty(video, "duration", { configurable: true, value: 120 });
    Object.defineProperty(video, "readyState", { configurable: true, value: 4 });
    Object.defineProperty(video, "paused", { configurable: true, value: false });
    fireEvent.loadedMetadata(video);
    fireEvent.seeked(video);
    video.currentTime = 90;
    fireEvent.timeUpdate(video);
    video.currentTime = 40;
    fireEvent.seeking(video);
    fireEvent.seeked(video);
    expect((screen.getByRole("slider", { name: "Playback position" }) as HTMLInputElement).value).toBe("40");
    view.unmount();
  });

  it("lets menu triggers own ArrowDown and preserves browser shortcuts", () => {
    const view = render(<MemoryRouter><PlayerPage visualFixture={playback} /></MemoryRouter>);
    const trigger = screen.getByRole("button", { name: /Audio language/ });
    act(() => trigger.focus());
    fireEvent.keyDown(trigger, { key: "ArrowDown", code: "ArrowDown" });
    expect(screen.getByRole("listbox", { name: "Audio language" })).toBeTruthy();
    fireEvent.keyDown(screen.getByRole("listbox"), { key: "Escape" });
    const event = new KeyboardEvent("keydown", { key: "f", code: "KeyF", ctrlKey: true, bubbles: true, cancelable: true });
    act(() => { document.dispatchEvent(event); });
    expect(event.defaultPrevented).toBe(false);
    view.unmount();
  });

  it("detects a frozen clock after playback has already advanced", () => {
    vi.useFakeTimers();
    const view = render(<MemoryRouter><PlayerPage visualFixture={playback} /></MemoryRouter>);
    const video = view.container.querySelector("video")!;
    Object.defineProperty(video, "duration", { configurable: true, value: 120 });
    Object.defineProperty(video, "readyState", { configurable: true, value: 4 });
    Object.defineProperty(video, "paused", { configurable: true, value: false });
    fireEvent.loadedMetadata(video);
    fireEvent.seeked(video);
    fireEvent.playing(video);
    video.currentTime = 13;
    fireEvent.timeUpdate(video);
    act(() => vi.advanceTimersByTime(PLAYBACK_CLOCK_ADVANCE_TIMEOUT_MS + 20));
    expect((view.container.querySelector("[data-player-root]") as HTMLElement).dataset.playerPhase).toBe("buffering");
    view.unmount();
  });

  it("reveals idle chrome when the master video begins buffering", async () => {
    vi.useFakeTimers();
    const view = render(<MemoryRouter><PlayerPage visualFixture={playback} /></MemoryRouter>);
    await act(async () => { await import("hls.js"); });
    const video = view.container.querySelector("video")!;
    Object.defineProperty(video, "duration", { configurable: true, value: 120 });
    Object.defineProperty(video, "readyState", { configurable: true, value: 4 });
    Object.defineProperty(video, "paused", { configurable: true, value: false });
    fireEvent.loadedMetadata(video);
    fireEvent.seeked(video);
    fireEvent.playing(video);
    for (let i = 0; i < 4; i++) {
      video.currentTime = 13 + i;
      fireEvent.timeUpdate(video);
      act(() => vi.advanceTimersByTime(800));
    }
    const root = view.container.querySelector("[data-player-root]") as HTMLElement;
    const source = video.getAttribute("src");
    const loads = videoCallCount("load", video);
    expect(root.dataset.controlsVisible).toBe("false");
    fireEvent.waiting(video);
    expect(root.dataset.controlsVisible).toBe("true");
    const indicator = view.container.querySelector(".player-loading-overlay");
    expect(indicator?.getAttribute("aria-hidden")).toBe("true");
    expect(indicator?.textContent).toBe("");
    expect(indicator?.querySelectorAll(".player-loading-spinner")).toHaveLength(1);
    expect(indicator?.querySelector("button, input, a, [tabindex]")).toBeNull();
    expect(screen.getByText("Buffering").classList.contains("sr-only")).toBe(true);

    video.currentTime = 17;
    fireEvent.playing(video);
    fireEvent.timeUpdate(video);
    await act(async () => { await vi.advanceTimersByTimeAsync(400); });
    expect(root.dataset.playerPhase).toBe("playing");
    const playingExit = view.container.querySelector<HTMLElement>(".player-loading-overlay");
    expect(playingExit === null || playingExit.style.opacity === "0").toBe(true);

    fireEvent.waiting(video);
    const pauses = videoCallCount("pause", video);
    pointerClick(screen.getByRole("button", { name: "Pause" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(400); });
    expect(root.dataset.playerPhase).toBe("paused");
    expect(videoCallCount("pause", video)).toBe(pauses + 1);
    const pausedExit = view.container.querySelector<HTMLElement>(".player-loading-overlay");
    expect(pausedExit === null || pausedExit.style.opacity === "0").toBe(true);
    fireEvent.waiting(video);
    expect(root.dataset.playerPhase).toBe("paused");
    const lateExit = view.container.querySelector<HTMLElement>(".player-loading-overlay");
    expect(lateExit === null || lateExit.style.opacity === "0").toBe(true);
    expect(view.container.querySelector("video")).toBe(video);
    expect(video.getAttribute("src")).toBe(source);
    expect(videoCallCount("load", video)).toBe(loads);
    view.unmount();
  });

});
