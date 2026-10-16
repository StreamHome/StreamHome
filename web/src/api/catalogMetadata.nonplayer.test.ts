import { afterEach, describe, expect, it, vi } from "vitest";
import { getEpisodes, getFeatured, getMovie, getMovies, normalizeMovie } from "./movies";
afterEach(() => vi.unstubAllGlobals());
describe("included catalog metadata normalization", () => {
  it("retains canonical catalog identity and cached availability without inventing playable media", () => {
    const movie = normalizeMovie({ id: "m_42", title: "Metadata only", availability: "cached", source: "tmdb_cache", genres: ["Drama"], videoUrl: "", remoteThumbnailUrl: "/synthetic/poster.svg", releaseYear: 2025 });
    expect(movie).toMatchObject({ id: "m_42", title: "Metadata only", availability: "cached", source: "tmdb_cache", videoUrl: "", genres: ["Drama"], releaseYear: 2025, remoteThumbnailUrl: "/synthetic/poster.svg" });
    expect(movie.episodes).toBeNull();
  });
  it("preserves embedded series episode identity and availability-owned URLs", () => {
    const movie = normalizeMovie({ id: "tv_7", title: "Series", type: "series", availability: "processing", previewTaskId: "synthetic-job", episodes: [{ id: "ep_7_2_3", episodeNumber: 3, seasonNumber: 2, title: "Episode", videoUrl: "", previewTaskId: "synthetic-episode-job" }] });
    expect(movie.id).toBe("tv_7"); expect(movie.type).toBe("series"); expect(movie.availability).toBe("processing");
    expect(movie.episodes?.[0]).toMatchObject({ id: "ep_7_2_3", episodeNumber: 3, seasonNumber: 2, title: "Episode", videoUrl: "" });
  });
  it("normalizes catalog, canonical lookup and episode endpoint data through the actual client", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      const value = path.startsWith("/api/movies?") ? [{ id: "m_3", title: "Catalog", availability: "available", videoUrl: "/synthetic/catalog" }] : path.startsWith("/api/movies/") ? { id: "tv_8", title: "Canonical", type: "series", genres: null } : [{ id: "ep_8_1_1", title: "First", seasonNumber: 1, episodeNumber: 1 }];
      return new Response(JSON.stringify(value), { status: 200 });
    }); vi.stubGlobal("fetch", fetchMock);
    expect((await getMovies("profile with spaces"))[0]).toMatchObject({ id: "m_3", availability: "available", videoUrl: "/synthetic/catalog" });
    expect(await getMovie("tv_8")).toMatchObject({ id: "tv_8", genres: [], type: "series" });
    expect((await getEpisodes(8))[0]).toMatchObject({ id: "ep_8_1_1", seasonNumber: 1, episodeNumber: 1, videoUrl: "" });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/movies?profile_id=profile%20with%20spaces");
  });
});


describe("included catalog metadata response boundaries", () => {
  const malformed = [null, {}, { id: "" }, { id: "m_1", title: {} }, { id: "m_1", description: {} }, { id: "m_1", genres: [{}] }, { id: "m_1", cast: [{}] }, { id: "m_1", languages: [{}] }, { id: "m_1", releaseYear: "2025" }, { id: "m_1", director: {} }, { id: "m_1", episodes: [null] }, { id: "m_1", episodes: [{ id: "ep_1", title: {} }] }, { id: "m_1", crew: [{ name: {}, roles: [] }] }, { id: "m_1", recommendationReasonDetails: [{ code: "x", fallbackText: {} }] }];
  it.each(malformed)("rejects malformed catalog and canonical display metadata %j through the actual client", async (value) => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(String(input) === "/api/movies" ? [value] : value), { status: 200 })));
    await expect(getMovies()).rejects.toMatchObject({ status: 0, code: "invalid_response" });
    await expect(getMovie("m_1")).rejects.toMatchObject({ status: 0, code: "invalid_response" });
    if (value !== null) await expect(getFeatured()).rejects.toMatchObject({ status: 0, code: "invalid_response" });
  });
  it.each([null, {}, [null], [{ id: "ep_1", title: {} }], [{ id: "ep_1", seasonNumber: "2" }]])("rejects malformed episode-list display metadata %j", async (value) => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(value), { status: 200 })));
    await expect(getEpisodes(1)).rejects.toMatchObject({ status: 0, code: "invalid_response" });
  });
  it("preserves successful empty resources and the established optional defaults", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(String(input) === "/api/movies/featured" ? null : String(input) === "/api/movies/m_1" ? { id: "m_1", genres: null, cast: null, episodes: null, rating: null, director: null } : []), { status: 200 })));
    await expect(getMovies()).resolves.toEqual([]); await expect(getEpisodes(1)).resolves.toEqual([]); await expect(getFeatured()).resolves.toBeNull();
    expect(await getMovie("m_1")).toMatchObject({ id: "m_1", title: "", description: "", genres: [], cast: [], episodes: null, rating: null, director: null, releaseYear: 0 });
  });
});
