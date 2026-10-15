import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AppQueryState } from "../../navigation/queryState";
import type { Movie, PlaybackSession, Profile, RecommendationFeed } from "../../types/api";
import { getPlaybackSessionRevision, invalidatePlaybackSessions } from "../../api/playbackSessionEvents";
import { useCatalogController } from "./useCatalogController";

const mocks = vi.hoisted(() => ({
  getMovie: vi.fn(), getMovies: vi.fn(), getPlaybackSessions: vi.fn(), getWatchlist: vi.fn(), getRecommendations: vi.fn(), getMediaPreferences: vi.fn(), setMediaPreference: vi.fn(), search: vi.fn(),
}));

vi.mock("../../api/movies", () => ({ getMovie: mocks.getMovie, getMovies: mocks.getMovies, search: mocks.search }));
vi.mock("../../api/playback", () => ({ getPlaybackSessions: mocks.getPlaybackSessions }));
vi.mock("../../api/watchlist", () => ({ getWatchlist: mocks.getWatchlist }));
vi.mock("../../api/recommendations", () => ({ getRecommendations: mocks.getRecommendations, getMediaPreferences: mocks.getMediaPreferences, setMediaPreference: mocks.setMediaPreference }));

const profile: Profile = { id: "profile one", name: "Viewer", avatarColor: "", theme: "ember", pinEnabled: false };

function movie(id: string): Movie {
  return { id, title: id, description: "", thumbnailUrl: "", bannerUrl: null, videoUrl: `/media/${id}`, genres: ["Drama"], duration: "", releaseYear: 2025, rating: null, cast: [], director: null, type: "movie", quality: "", languages: [], subtitles: [], voteAverage: 0, voteCount: 0, skipMarkers: {}, availability: "available" };
}

function playback(movieId: string, overrides: Partial<PlaybackSession> = {}): PlaybackSession {
  return { movieId, profileId: profile.id, timestamp: 5970, durationWatched: 5970, completionRate: 0.968, updatedAt: "2026-10-15T10:00:00Z", episodeId: null, isFinished: false, ...overrides };
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
  it("unlocks the media catalog before recommendations and restores it immediately after remount", async () => {
    const cacheProfile = { ...profile, id: "catalog-bootstrap-profile" };
    let resolveRecommendation!: (value: RecommendationFeed) => void;
    mocks.getMovies.mockResolvedValue([movie("available-now")]);
    mocks.getRecommendations.mockImplementation(() => new Promise<RecommendationFeed>((resolve) => { resolveRecommendation = resolve; }));
    const query: AppQueryState = { profile: cacheProfile.id, view: "movies" };
    const first = renderHook(() => useCatalogController(cacheProfile, query));

    await waitFor(() => expect(first.result.current.loading).toBe(false));
    expect(first.result.current.movieItems.map((item) => item.id)).toEqual(["available-now"]);
    expect(first.result.current.recommendation).toBeNull();
    first.unmount();

    mocks.getMovies.mockImplementation(() => new Promise<Movie[]>(() => undefined));
    const second = renderHook(() => useCatalogController(cacheProfile, query));
    expect(second.result.current.loading).toBe(false);
    expect(second.result.current.movieItems.map((item) => item.id)).toEqual(["available-now"]);
    second.unmount();
    void resolveRecommendation;
  });

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

  it("resolves titles that are supplied only through a personalized vibe rail", async () => {
    const recommendation = feed("recommended", ["top-pick"]);
    recommendation.algorithmVersion = "v2.1";
    recommendation.vibeRails = [{
      id: "vibe-banter",
      label: "Witty Banter & Bullets",
      tropeIds: ["neo_noir_buddy_action"],
      reasonCode: "trope_match",
      items: [{ media: movie("vibe-only"), source: "server", availability: "available", score: 88, reasons: ["Matches your taste for witty action"] }],
    }];
    mocks.getRecommendations.mockResolvedValue(recommendation);

    const query: AppQueryState = { profile: profile.id, view: "home" };
    const { result } = renderHook(() => useCatalogController(profile, query));

    await waitFor(() => expect(result.current.resolveMovie("vibe-only")?.title).toBe("vibe-only"));
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

describe("useCatalogController playback acknowledgments", () => {
  const query: AppQueryState = { profile: profile.id, view: "details", media: "late-movie" };

  it("keeps unfinished late movies visible while retaining the series cutoff", async () => {
    mocks.getMovies.mockResolvedValue([movie("late-movie"), { ...movie("late-series"), type: "series" }, movie("finished-movie"), movie("episode-movie")]);
    mocks.getPlaybackSessions.mockResolvedValue([
      playback("late-movie"), playback("late-series", { episodeId: "ep_1" }),
      playback("finished-movie", { isFinished: true }), playback("episode-movie", { episodeId: "ep_2" }),
    ]);
    const { result } = renderHook(() => useCatalogController(profile, query));
    await waitFor(() => expect(result.current.sessions).toHaveLength(4));
    expect(result.current.continueWatching.map((item) => item.id)).toEqual(["late-movie"]);
    expect(result.current.sessions[0]).toMatchObject({ timestamp: 5970, completionRate: 0.968 });
  });

  it("refetches on a matching ACK and prevents the older mount response from overwriting it or the cache", async () => {
    const ackProfile = { ...profile, id: "late-close-ack-profile" };
    let resolveOld!: (value: PlaybackSession[]) => void;
    const old = new Promise<PlaybackSession[]>((resolve) => { resolveOld = resolve; });
    mocks.getMovies.mockResolvedValue([movie("late-movie")]);
    mocks.getPlaybackSessions.mockReturnValueOnce(old).mockResolvedValueOnce([playback("late-movie", { isFinished: true })]);
    const first = renderHook(() => useCatalogController(ackProfile, query));
    await waitFor(() => expect(mocks.getPlaybackSessions).toHaveBeenCalledTimes(1));
    const signal = mocks.getPlaybackSessions.mock.calls[0][1] as AbortSignal;
    const revision = getPlaybackSessionRevision(ackProfile.id);
    act(() => invalidatePlaybackSessions(ackProfile.id));
    expect(getPlaybackSessionRevision(ackProfile.id)).toBe(revision + 1);
    expect(signal.aborted).toBe(true);
    await waitFor(() => expect(first.result.current.sessions[0]?.isFinished).toBe(true));
    await act(async () => { resolveOld([playback("late-movie")]); });
    expect(first.result.current.sessions[0]?.isFinished).toBe(true);
    expect(first.result.current.continueWatching).toEqual([]);
    first.unmount();
    mocks.getPlaybackSessions.mockImplementation(() => new Promise<PlaybackSession[]>(() => undefined));
    const second = renderHook(() => useCatalogController(ackProfile, query));
    expect(second.result.current.sessions[0]?.isFinished).toBe(true);
    second.unmount();
    const requests = mocks.getPlaybackSessions.mock.calls.length;
    act(() => invalidatePlaybackSessions(ackProfile.id));
    expect(mocks.getPlaybackSessions).toHaveBeenCalledTimes(requests);
  });

  it("scopes ACKs and late responses to the mounted profile", async () => {
    const other = { ...profile, id: "other-playback-profile" };
    const firstProfile = { ...profile, id: "first-playback-profile" };
    let resolveOld!: (value: PlaybackSession[]) => void;
    mocks.getPlaybackSessions.mockReturnValueOnce(new Promise<PlaybackSession[]>((resolve) => { resolveOld = resolve; })).mockResolvedValueOnce([]).mockResolvedValueOnce([playback("other-movie")]);
    const { result, rerender } = renderHook(({ active }) => useCatalogController(active, query), { initialProps: { active: firstProfile } });
    await waitFor(() => expect(mocks.getPlaybackSessions).toHaveBeenCalledTimes(1));
    rerender({ active: other });
    await waitFor(() => expect(mocks.getPlaybackSessions).toHaveBeenCalledTimes(2));
    act(() => invalidatePlaybackSessions(firstProfile.id));
    expect(mocks.getPlaybackSessions).toHaveBeenCalledTimes(2);
    act(() => invalidatePlaybackSessions(other.id));
    await waitFor(() => expect(result.current.sessions[0]?.movieId).toBe("other-movie"));
    await act(async () => { resolveOld([playback("wrong-profile-movie")]); });
    expect(result.current.sessions[0]?.movieId).toBe("other-movie");
  });

  it("retains server-confirmed progress after a failed ACK refresh without retry polling", async () => {
    const failureProfile = { ...profile, id: "playback-refresh-failure" };
    mocks.getPlaybackSessions.mockResolvedValueOnce([playback("late-movie")]).mockRejectedValueOnce(new Error("offline"));
    const { result } = renderHook(() => useCatalogController(failureProfile, query));
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    act(() => invalidatePlaybackSessions(failureProfile.id));
    await waitFor(() => expect(mocks.getPlaybackSessions).toHaveBeenCalledTimes(2));
    expect(result.current.sessions[0]).toMatchObject({ isFinished: false, timestamp: 5970, completionRate: 0.968 });
  });
});
