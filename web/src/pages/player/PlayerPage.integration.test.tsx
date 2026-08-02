import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useProfileStore } from "../../stores/profileStore";
import type { Profile } from "../../types/api";
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
    nextSequenceNumber: 1,
  },
};


describe("mounted player lifecycle", () => {
  beforeEach(() => {
    useProfileStore.setState({ profiles: [profile], activeProfile: profile, isAdmin: false });
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    useProfileStore.setState({ profiles: [], activeProfile: null, isAdmin: false });
    document.documentElement.removeAttribute("data-player-viewport-fullscreen");
    document.body.removeAttribute("data-player-viewport-fullscreen");
    Reflect.deleteProperty(document, "fullscreenEnabled");
    Reflect.deleteProperty(document, "webkitFullscreenEnabled");
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

  it("requests an idle quality only when the viewer selects it", async () => {
    const prepare = vi.spyOn(playbackApi, "preparePlaybackRendition").mockResolvedValue({ status: "preparing" });
    const onDemandPlayback: ResolvedPlayback = {
      ...playback,
      runResponse: {
        ...playback.runResponse,
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

    const qualityMenu = await screen.findByRole("button", { name: "Quality: Auto" });
    fireEvent.click(qualityMenu);
    fireEvent.click(screen.getByRole("option", { name: /240p/ }));

    expect(prepare).toHaveBeenCalledOnce();
    expect(prepare).toHaveBeenCalledWith("mounted-player-run", "video_240p");
    view.unmount();
  });

  it("enters and exits viewport fullscreen from the real player controls", async () => {
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

    fireEvent.keyDown(window, { key: "Escape" });

    await waitFor(() => expect(screen.getByRole("button", { name: "Fullscreen" })).toBeTruthy());
    expect(player.getAttribute("data-player-viewport-fullscreen")).toBeNull();
    expect(document.documentElement.getAttribute("data-player-viewport-fullscreen")).toBeNull();
    expect(document.body.getAttribute("data-player-viewport-fullscreen")).toBeNull();
    view.unmount();
  });
});
