import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AppQueryState } from "../../navigation/queryState";
import type { Movie, Profile, RecommendationFeed } from "../../types/api";
import { useCatalogController } from "./useCatalogController";

const mocks = vi.hoisted(() => ({
  getMovie: vi.fn(), getMovies: vi.fn(), getPlaybackSessions: vi.fn(), getWatchlist: vi.fn(), getRecommendations: vi.fn(), getMediaPreferences: vi.fn(), setMediaPreference: vi.fn(), search: vi.fn(),
}));

vi.mock("../../api/movies", () => ({ getMovie: mocks.getMovie, getMovies: mocks.getMovies, search: mocks.search }));
vi.mock("../../api/playback", () => ({ getPlaybackSessions: mocks.getPlaybackSessions }));
vi.mock("../../api/watchlist", () => ({ getWatchlist: mocks.getWatchlist }));
vi.mock("../../api/recommendations", () => ({ getRecommendations: mocks.getRecommendations, getMediaPreferences: mocks.getMediaPreferences, setMediaPreference: mocks.setMediaPreference }));

const profile: Profile = { id: "profile one", name: "Viewer", avatarColor: "", theme: "ember", pinEnabled: false, pin: null };

function movie(id: string): Movie {
  return { id, title: id, description: "", thumbnailUrl: "", bannerUrl: null, videoUrl: `/media/${id}`, genres: ["Drama"], duration: "", releaseYear: 2025, rating: null, cast: [], director: null, type: "movie", quality: "", languages: [], subtitles: [], voteAverage: 0, voteCount: 0, skipMarkers: {}, availability: "available" };
}

function feed(category: string, ids: string[], total = ids.length, watchAgain: string[] = []): RecommendationFeed {
  return {
    profileId: profile.id, scope: "home", category, generatedAt: 1, stale: false, total, offset: 0, limit: 48,
    categories: [
      { value: "recommended", label: "Recommended", affinity: 0, serverCount: 3, cachedCount: 0 },
      { value: "all", label: "All Releases", affinity: 0, serverCount: 3, cachedCount: 0 },
      { value: "Drama", label: "Drama", affinity: 1, serverCount: 3, cachedCount: 0 },
    ],
    items: ids.map((id, index) => ({ media: movie(id), source: "server", availability: "available", score: 100 - index, reasons: [] })),
    watchAgain: watchAgain.map((id, index) => ({ media: movie(id), source: "server", availability: "available", score: index, reasons: [] })),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getMovies.mockResolvedValue([]);
  mocks.getPlaybackSessions.mockResolvedValue([]);
  mocks.getWatchlist.mockResolvedValue([]);
  mocks.getMediaPreferences.mockResolvedValue({});
  mocks.setMediaPreference.mockResolvedValue(undefined);
  mocks.search.mockResolvedValue([]);
  mocks.getMovie.mockRejectedValue(new Error("Media not found"));
});

describe("useCatalogController recommendations", () => {
  it("requests the active scope/category and keeps Watch Again in server order", async () => {
    mocks.getRecommendations.mockResolvedValue(feed("Drama", ["ranked"], 1, ["recent", "older"]));
    const query: AppQueryState = { profile: profile.id, view: "movies", genre: "Drama" };
    const { result } = renderHook(() => useCatalogController(profile, query));
    await waitFor(() => expect(result.current.recommendation?.category).toBe("Drama"));
    expect(mocks.getRecommendations).toHaveBeenCalledWith(expect.objectContaining({ profileId: profile.id, scope: "movies", category: "Drama", limit: 48, offset: 0 }));
    expect(result.current.recommendation?.watchAgain.map((entry) => entry.media.id)).toEqual(["recent", "older"]);
  });

  it("aborts an obsolete category request and ignores its late response", async () => {
    let resolveDrama!: (value: RecommendationFeed) => void;
    let resolveAction!: (value: RecommendationFeed) => void;
    const drama = new Promise<RecommendationFeed>((resolve) => { resolveDrama = resolve; });
    const action = new Promise<RecommendationFeed>((resolve) => { resolveAction = resolve; });
    mocks.getRecommendations.mockImplementation(({ category }: { category: string }) => category === "Drama" ? drama : action);
    const initial: AppQueryState = { profile: profile.id, view: "home", genre: "Drama" };
    const { result, rerender } = renderHook(({ query }) => useCatalogController(profile, query), { initialProps: { query: initial } });
    await waitFor(() => expect(mocks.getRecommendations).toHaveBeenCalledTimes(1));
    const firstSignal = mocks.getRecommendations.mock.calls[0][0].signal as AbortSignal;
    rerender({ query: { ...initial, genre: "Action" } });
    await waitFor(() => expect(mocks.getRecommendations).toHaveBeenCalledTimes(2));
    expect(firstSignal.aborted).toBe(true);
    await act(async () => { resolveAction(feed("Action", ["action"])); });
    await waitFor(() => expect(result.current.recommendation?.items[0].media.id).toBe("action"));
    await act(async () => { resolveDrama(feed("Drama", ["late-drama"])); });
    expect(result.current.recommendation?.items[0].media.id).toBe("action");
  });

  it("appends pages in server order and refetches after returning from playback", async () => {
    mocks.getRecommendations
      .mockResolvedValueOnce(feed("all", ["first"], 3))
      .mockResolvedValueOnce({ ...feed("all", ["second", "third"], 3), offset: 1 })
      .mockResolvedValueOnce(feed("all", ["third", "first", "second"], 3));
    const initial: AppQueryState = { profile: profile.id, view: "home", genre: "all" };
    const { result, rerender } = renderHook(({ query }) => useCatalogController(profile, query), { initialProps: { query: initial } });
    await waitFor(() => expect(result.current.recommendation?.items.map((entry) => entry.media.id)).toEqual(["first"]));
    await act(async () => { await result.current.loadMoreRecommendations(); });
    expect(result.current.recommendation?.items.map((entry) => entry.media.id)).toEqual(["first", "second", "third"]);
    expect(mocks.getRecommendations.mock.calls[1][0]).toEqual(expect.objectContaining({ offset: 1, category: "all" }));
    rerender({ query: { profile: profile.id, view: "watch", media: "first" } });
    rerender({ query: initial });
    await waitFor(() => expect(mocks.getRecommendations).toHaveBeenCalledTimes(3));
  });

  it("retains a search-only title while navigating to details and refreshes its canonical record", async () => {
    mocks.getRecommendations.mockResolvedValue(feed("recommended", []));
    const searched = {
      id: "m_42", tmdbId: 42, title: "Search Only", description: "Remote metadata",
      thumbnailUrl: "https://image.tmdb.org/t/p/w500/poster.jpg", bannerUrl: null,
      genres: ["Drama"], duration: "2h", releaseYear: 2024, rating: "PG-13",
      voteAverage: 8, voteCount: 100, director: null, cast: [], type: "movie" as const,
      source: "tmdb_cache", availability: "cached", cacheState: "queued" as const,
    };
    mocks.search.mockResolvedValue([searched]);
    mocks.getMovie.mockResolvedValue({ ...movie("m_42"), title: "Search Only", videoUrl: "", availability: "cached", cacheState: "ready" });
    const initial: AppQueryState = { profile: profile.id, view: "search", q: "search only" };
    const { result, rerender } = renderHook(({ query }) => useCatalogController(profile, query), { initialProps: { query: initial } });
    await waitFor(() => expect(result.current.resolveMovie("m_42")?.title).toBe("Search Only"));
    rerender({ query: { profile: profile.id, view: "details", media: "m_42" } });
    expect(result.current.resolveMovie("m_42")?.title).toBe("Search Only");
    await waitFor(() => expect(mocks.getMovie).toHaveBeenCalledWith("m_42", expect.any(AbortSignal)));
    await waitFor(() => expect(result.current.resolveMovie("m_42")?.cacheState).toBe("ready"));
  });

  it("optimistically removes a disliked title without reordering Watch Again", async () => {
    mocks.getRecommendations
      .mockResolvedValueOnce(feed("recommended", ["liked", "hidden"], 2, ["hidden", "liked"]))
      .mockResolvedValue({ ...feed("recommended", ["liked"], 1, ["hidden", "liked"]), watchAgain: feed("recommended", [], 0, ["hidden", "liked"]).watchAgain.map((item) => item.media.id === "hidden" ? { ...item, viewerPreference: "dislike" } : item) });
    const query: AppQueryState = { profile: profile.id, view: "home" };
    const { result } = renderHook(() => useCatalogController(profile, query));
    await waitFor(() => expect(result.current.recommendation?.items).toHaveLength(2));
    await act(async () => { await result.current.updatePreference("hidden", "dislike"); });
    expect(mocks.setMediaPreference).toHaveBeenCalledWith(profile.id, "hidden", "dislike");
    await waitFor(() => expect(result.current.recommendation?.items.map((entry) => entry.media.id)).toEqual(["liked"]));
    expect(result.current.recommendation?.watchAgain.map((entry) => entry.media.id)).toEqual(["hidden", "liked"]);
    expect(result.current.recommendation?.watchAgain[0].viewerPreference).toBe("dislike");
  });
});
