import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useProfileStore } from "../../stores/profileStore";
import type { Profile } from "../../types/api";
import * as moviesApi from "../../api/movies";
import * as playbackApi from "../../api/playback";
import { PlayerPage, type ResolvedPlayback } from "./PlayerPage";


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


describe("mounted player lifecycle", () => {
  beforeEach(() => {
    localStorage.removeItem(playerPreferencesKey);
    useProfileStore.setState({ profiles: [profile], activeProfile: profile, isAdmin: false });
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  });

  afterEach(() => {
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
    expect(video.currentTime).toBe(0);

    fireEvent.pointerDown(timeline, { pointerId: 1 });
    fireEvent.change(timeline, { target: { value: "60" } });
    expect(video.currentTime).toBe(0);
    fireEvent.pointerUp(timeline, { pointerId: 1 });

    expect(video.currentTime).toBe(60);
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
    await waitFor(() => expect(root.dataset.playerPhase).toBe("playing"));

    Object.defineProperty(video, "paused", { configurable: true, value: true });
    fireEvent.pause(video);
    play.mockClear();
    fullscreenButton.focus();
    fireEvent.keyDown(fullscreenButton, { key: " " });
    fireEvent.keyUp(fullscreenButton, { key: " " });
    await waitFor(() => expect(play).toHaveBeenCalled());

    fireEvent.keyDown(document.body, { key: "ArrowDown" });
    await waitFor(() => expect((screen.getByRole("slider", { name: "Volume" }) as HTMLInputElement).value).toBe("0.95"));
    fireEvent.keyDown(document.body, { key: "ArrowUp" });
    await waitFor(() => expect((screen.getByRole("slider", { name: "Volume" }) as HTMLInputElement).value).toBe("1"));

    video.currentTime = 30;
    fireEvent.timeUpdate(video);
    fireEvent.keyDown(document.body, { key: "ArrowRight" });
    expect(video.currentTime).toBe(40);
    fireEvent.keyDown(document.body, { key: "ArrowLeft" });
    expect(video.currentTime).toBe(30);
    view.unmount();
  });

  it("restores the profile audio level before playback and preserves changes across titles", async () => {
    localStorage.setItem(playerPreferencesKey, JSON.stringify({ volume: 0.36, muted: false }));
    const firstView = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=mounted-player-media"]}>
        <PlayerPage visualFixture={playback} />
      </MemoryRouter>,
    );

    const firstVideo = firstView.container.querySelector("video") as HTMLVideoElement;
    const firstVolume = screen.getByRole("slider", { name: "Volume" }) as HTMLInputElement;
    await waitFor(() => expect(firstVolume.value).toBe("0.36"));
    expect(firstVideo.volume).toBe(0.36);

    fireEvent.change(firstVolume, { target: { value: "0.24" } });
    await waitFor(() => expect(JSON.parse(localStorage.getItem(playerPreferencesKey) || "{}")).toMatchObject({
      volume: 0.24,
      muted: false,
    }));
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
      mediaId: "m_parallel-startup",
      movieId: "m_parallel-startup",
    });
    vi.spyOn(playbackApi, "closePlaybackRun").mockResolvedValue({
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

  it("keeps the first open mounted while complete HLS preparation becomes ready", async () => {
    const preparingRun = {
      ...playback.runResponse,
      runId: "first-open-run",
      mediaId: "m_first-open",
      movieId: "m_first-open",
      manifestUrl: null,
      preparationState: "preparing" as const,
      preparationProgress: {
        stage: "packaging" as const,
        queuePosition: 0,
        readySegments: 0,
        activeWorkers: 1,
      },
      seekableUntil: 4,
      resumeReady: false,
      switchingReady: false,
      fullyPrepared: false,
    };
    const readyRun = {
      ...preparingRun,
      manifestUrl: "/api/playback/manifest/m_first-open?ticket=mounted-player-ticket",
      preparationState: "ready" as const,
      preparationProgress: {
        stage: "streamable" as const,
        queuePosition: 0,
        readySegments: 2,
        activeWorkers: 0,
      },
      seekableUntil: 120,
      resumeReady: true,
      switchingReady: true,
      fullyPrepared: true,
    };
    vi.spyOn(moviesApi, "getMovie").mockResolvedValue({
      id: "m_first-open",
      title: "First open recovery",
      description: "",
      thumbnailUrl: "",
      bannerUrl: null,
      videoUrl: "/media/first-open.mp4",
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
    vi.spyOn(playbackApi, "createPlaybackRun").mockResolvedValue(preparingRun);
    const poll = vi.spyOn(playbackApi, "getPlaybackRun").mockResolvedValue(readyRun);
    const close = vi.spyOn(playbackApi, "closePlaybackRun").mockResolvedValue({
      status: "abandoned",
      acceptedSeconds: 0,
      nextSequenceNumber: 2,
    });

    const view = render(
      <MemoryRouter initialEntries={["/?profile=mounted-player-profile&view=watch&media=m_first-open"]}>
        <PlayerPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText("First open recovery")).toBeTruthy();
    const video = view.container.querySelector("video") as HTMLVideoElement;
    fireEvent.error(video);

    await waitFor(() => expect(poll).toHaveBeenCalled(), { timeout: 2_000 });
    await waitFor(() => expect(view.container.querySelector("video")).toBeTruthy(), { timeout: 2_000 });
    expect(screen.queryByText("Recovery required")).toBeNull();
    fireEvent(window, new Event("pagehide"));
    fireEvent(window, new Event("pagehide"));
    expect(close).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledWith("first-open-run", expect.objectContaining({ timestamp: 12, event: "exit" }));
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
    focusedFullscreenButton.focus();
    fireEvent.keyDown(focusedFullscreenButton, { key: " " });
    fireEvent.keyUp(focusedFullscreenButton, { key: " " });
    await waitFor(() => expect(play).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "Exit fullscreen" })).toBeTruthy();
    fireEvent.keyDown(focusedFullscreenButton, { key: "f" });
    fireEvent.keyUp(focusedFullscreenButton, { key: "f" });

    await waitFor(() => expect(screen.getByRole("button", { name: "Fullscreen" })).toBeTruthy());
    expect(player.getAttribute("data-player-viewport-fullscreen")).toBeNull();
    expect(document.documentElement.getAttribute("data-player-viewport-fullscreen")).toBeNull();
    expect(document.body.getAttribute("data-player-viewport-fullscreen")).toBeNull();
    view.unmount();
  });
});
