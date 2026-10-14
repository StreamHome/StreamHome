import React from "react";
import { MemoryRouter } from "react-router-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getAdminProfileData } from "../../../api/adminProfiles";
import type { AdminProfileData } from "../../../types/api";
import { ProfileDataPanel } from "./ProfileDataPanel";
import { ApiError } from "../../../api/client";

vi.mock("../../../api/adminProfiles", () => ({ getAdminProfileData: vi.fn() }));
let finishAuthorization: (() => void | Promise<void>) | undefined;
vi.mock("../SudoModal", () => ({ SudoModal: ({ isOpen, onSuccess, onCancel }: { isOpen: boolean; onSuccess: () => void | Promise<void>; onCancel: () => void }) => {
  if (!isOpen) return null;
  finishAuthorization = onSuccess;
  return <div role="dialog" aria-label="Confirm your identity"><button onClick={() => void onSuccess()}>Complete confirmation</button><button onClick={onCancel}>Cancel confirmation</button></div>;
} }));

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
  beforeEach(() => { vi.resetAllMocks(); finishAuthorization = undefined; vi.mocked(getAdminProfileData).mockResolvedValue(data); });

  it("loads the selected profile and exposes every durable data category", async () => {
    render(<MemoryRouter initialEntries={["/?profile=1&view=admin&section=profiles&subject=child"]}><ProfileDataPanel profileId="child" /></MemoryRouter>);
    expect(await screen.findByText("10m")).toBeTruthy();
    expect(getAdminProfileData).toHaveBeenCalledWith("child", expect.any(AbortSignal));

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    expect(screen.getByText("1 history record")).toBeTruthy();
    expect(screen.getAllByText("Cached title").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "Recommendation data" }));
    expect(screen.getByText("Because you like science fiction")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "TMDB cache" }));
    expect(screen.getByText("Shared catalog artwork and metadata")).toBeTruthy();
    expect(screen.getByText("Recommendation pool")).toBeTruthy();
  });
  it("opens the URL category and distinguishes missing refresh data from an empty pool", async () => {
    vi.mocked(getAdminProfileData).mockResolvedValue({ ...data, recommendations: { ...data.recommendations, items: [], tastes: [] } });
    render(<MemoryRouter initialEntries={["/?profile=1&view=admin&section=profiles&subject=child&category=recommendations"]}><ProfileDataPanel profileId="child" /></MemoryRouter>);
    expect(await screen.findByText("No refresh status recorded")).toBeTruthy();
    expect(screen.queryByText("Pool current")).toBeNull();
    expect(screen.getByText("Preference version").parentElement?.textContent).toContain("—");
    expect(screen.getByText(/No learned tastes yet/)).toBeTruthy();
    expect(screen.getByText(/No recommendation candidates/)).toBeTruthy();
  });
  it("switches the URL-backed category through the narrow-layout selection control", async () => {
    render(<MemoryRouter initialEntries={["/?profile=1&view=admin&section=profiles&subject=child"]}><ProfileDataPanel profileId="child" /></MemoryRouter>);
    await screen.findByText("10m");
    const category = screen.getByRole("combobox", { name: "Data category" });
    fireEvent.click(category);
    fireEvent.click(screen.getByRole("option", { name: "History" }));
    expect(screen.getByText("1 history record")).toBeTruthy();
    expect(category.textContent).toBe("History");
    expect(screen.getByRole("button", { name: "History" }).getAttribute("aria-current")).toBe("page");
    expect(getAdminProfileData).toHaveBeenCalledTimes(1);
  });
  it("explains separately empty resume and activity datasets", async () => {
    vi.mocked(getAdminProfileData).mockResolvedValue({ ...data, history: { ...data.history, items: [], resumeStates: [] }, activity: { ...data.activity, events: [], exposures: [], playbackRuns: [] } });
    render(<MemoryRouter initialEntries={["/?profile=1&view=admin&section=profiles&subject=child&category=history"]}><ProfileDataPanel profileId="child" /></MemoryRouter>);
    expect(await screen.findByText(/No viewing history/)).toBeTruthy();
    expect(screen.getByText(/No playback positions/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", {name: "Activity"}));
    expect(screen.getByText(/No interaction events/)).toBeTruthy();
    expect(screen.getByText(/No recommendation exposures/)).toBeTruthy();
    expect(screen.getByText(/No playback runs/)).toBeTruthy();
  });

  it("keeps the record map out of the page flow until its labeled details dialog opens", async () => {
    render(<MemoryRouter><ProfileDataPanel profileId="child" /></MemoryRouter>);
    await screen.findByText("10m");
    expect(screen.queryByText("What is durable, shared, or temporary")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Stored records and data locations" }));
    expect(screen.getByRole("dialog", { name: "Profile records and data locations" })).toBeTruthy();
    expect(screen.getByText("What is durable, shared, or temporary")).toBeTruthy();
    expect(screen.getByText("SQLite")).toBeTruthy();
    expect(getAdminProfileData).toHaveBeenCalledTimes(1);
  });

  it("offers identity confirmation after an expired read and retries the current category without changing profiles", async () => {
    vi.mocked(getAdminProfileData).mockRejectedValueOnce(new ApiError("Confirm your password and authenticator code to continue.", 403, "reauthentication_required"));
    render(<MemoryRouter initialEntries={["/?profile=1&view=admin&section=profiles&subject=child&category=history"]}><ProfileDataPanel profileId="child" /></MemoryRouter>);
    await screen.findByRole("dialog", { name: "Confirm your identity" });
    expect(screen.queryByText(/password and authenticator code/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Cancel confirmation" }));
    expect(getAdminProfileData).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Confirm identity" }));
    fireEvent.click(screen.getByRole("button", { name: "Complete confirmation" }));
    expect(await screen.findByText("1 history record")).toBeTruthy();
    expect(getAdminProfileData).toHaveBeenCalledTimes(2);
    expect(vi.mocked(getAdminProfileData).mock.calls.every(([subject]) => subject === "child")).toBe(true);
    expect(screen.getByRole("button", { name: "History" }).getAttribute("aria-current")).toBe("page");
  });

  it("aborts an earlier subject's read and ignores its late response", async () => {
    let finish!: (value: AdminProfileData) => void;
    vi.mocked(getAdminProfileData).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const { rerender } = render(<MemoryRouter><ProfileDataPanel profileId="child" /></MemoryRouter>);
    await waitFor(() => expect(getAdminProfileData).toHaveBeenCalledTimes(1));
    const oldSignal = vi.mocked(getAdminProfileData).mock.calls[0][1]!;
    vi.mocked(getAdminProfileData).mockResolvedValue({ ...data, overview: { ...data.overview, watchSeconds: 120 } });
    rerender(<MemoryRouter><ProfileDataPanel profileId="other" /></MemoryRouter>);
    expect(await screen.findByText("2m")).toBeTruthy();
    expect(oldSignal.aborted).toBe(true);
    await act(async () => finish(data));
    expect(screen.queryByText("10m")).toBeNull();
    expect(screen.getByText("2m")).toBeTruthy();
  });

  it("does not retry an old subject after navigation cancels a pending confirmation", async () => {
    vi.mocked(getAdminProfileData).mockRejectedValueOnce(new ApiError("Confirmation required", 403, "reauthentication_required"));
    const { rerender } = render(<MemoryRouter><ProfileDataPanel profileId="child" /></MemoryRouter>);
    await screen.findByRole("dialog", { name: "Confirm your identity" });
    const oldSuccess = finishAuthorization!;
    rerender(<MemoryRouter><ProfileDataPanel profileId="other" /></MemoryRouter>);
    await screen.findByText("10m");
    await act(async () => oldSuccess());
    expect(getAdminProfileData).toHaveBeenCalledTimes(2);
    expect(vi.mocked(getAdminProfileData).mock.calls.map(([subject]) => subject)).toEqual(["child", "other"]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

});
