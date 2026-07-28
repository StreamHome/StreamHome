import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getAdminProfileData } from "../../../api/adminProfiles";
import type { AdminProfileData } from "../../../types/api";
import { ProfileDataPanel } from "./ProfileDataPanel";

vi.mock("../../../api/adminProfiles", () => ({ getAdminProfileData: vi.fn() }));

const media = {
  id: "m_22",
  tmdbId: 22,
  title: "Cached title",
  type: "movie",
  releaseYear: 2026,
  thumbnailUrl: "/media/poster.jpg",
  catalogSource: "tmdb_cache",
  availability: "cached",
  cacheState: "ready",
};

const data: AdminProfileData = {
  overview: {
    profile: { id: "child", name: "Child", avatarColor: "blue", theme: "ember", pinEnabled: true, administrator: false },
    counts: { history: 1, resumeStates: 1, watchlist: 1, recommendations: 1, preferences: 1, events: 1, exposures: 1, playbackRuns: 1 },
    watchSeconds: 600,
    completedTitles: 0,
    lastActivityAt: 1_722_168_000,
    activePlaybackRuns: 1,
    persistence: [{ label: "Profile activity", location: "SQLite", durable: true }],
  },
  history: {
    total: 1,
    limit: 100,
    offset: 0,
    items: [{ id: "attempt", movie: media, episode: null, startedAt: 1, lastSeenAt: 2, maxCompletion: .5, durationWatched: 600, completedAt: null, earlyExitRecorded: false, rewatchReward: 0 }],
    resumeStates: [{ movie: media, movieId: media.id, episodeId: null, timestamp: 120, durationWatched: 600, completionRate: .5, updatedAt: "2026-07-28T12:00:00Z", finished: false }],
  },
  watchlist: { total: 1, limit: 200, offset: 0, items: [{ id: 1, createdAt: "2026-07-28T12:00:00Z", movie: media }] },
  recommendations: {
    total: 1,
    limit: 100,
    offset: 0,
    items: [{ movie: media, score: .9, reasons: ["Because you like science fiction"], reasonDetails: [], generatedAt: 2, candidateSource: "tmdb_related", sourceConfidence: .8, preference: "love" }],
    preferences: [{ movieId: media.id, movie: media, preference: "love", updatedAt: 2 }],
    onboarding: { genres: ["science fiction"], titleIds: [] },
    tastes: [{ kind: "genre", value: "science fiction", score: 3.5, updatedAt: 2 }],
    vibe: null,
    refresh: null,
    runtimeMetric: { top20Overlap: 17, meanDisplacement: 1.25, generatedAt: 2 },
  },
  activity: {
    total: 1,
    limit: 100,
    offset: 0,
    events: [{ id: 1, type: "card_click", movie: media, tmdbId: 22, timestamp: 2, metadata: {} }],
    exposures: [{ id: "exposure", movie: media, feedGeneration: "feed", surface: "home", scope: "home", category: "recommended", position: 0, shownAt: 2 }],
    playbackRuns: [{ id: "run", movie: media, episodeId: null, state: "active", createdAt: 1, updatedAt: 2, lastSeenAt: 2, secondsPlayed: 120 }],
  },
  cache: {
    total: 1,
    sharedCacheTotal: 4,
    unreferencedSharedTotal: 3,
    limit: 200,
    offset: 0,
    items: [{
      movie: media,
      associationSources: ["recommendation pool"],
      cachedAt: 1,
      metadataRefreshedAt: 2,
      cacheState: "ready",
      retryCount: 0,
      nextRetryAt: null,
      lastError: null,
      files: {
        poster: { url: "/media/poster.jpg", storedOnDisk: true, sizeBytes: 100 },
        backdrop: { url: "/media/backdrop.jpg", storedOnDisk: true, sizeBytes: 200 },
        metadata: { storedOnDisk: true, sizeBytes: 50 },
        totalSizeBytes: 350,
      },
      shared: true,
    }],
  },
};

describe("ProfileDataPanel", () => {
  beforeEach(() => vi.mocked(getAdminProfileData).mockResolvedValue(data));

  it("loads the selected profile and exposes every durable data category", async () => {
    render(<ProfileDataPanel profileId="child" />);
    expect(await screen.findByText("10m")).toBeTruthy();
    expect(getAdminProfileData).toHaveBeenCalledWith("child", expect.any(AbortSignal));

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    expect(screen.getByText("1 history records")).toBeTruthy();
    expect(screen.getAllByText("Cached title").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "Recommendations" }));
    expect(screen.getByText("Because you like science fiction")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "TMDB cache" }));
    expect(screen.getByText("TMDB cache is not profile-owned")).toBeTruthy();
    expect(screen.getByText("recommendation pool")).toBeTruthy();
  });
});
