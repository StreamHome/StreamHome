import { afterEach, describe, expect, it, vi } from "vitest";
import { getMediaPreferences, getRecommendationDiagnostics, getRecommendationOnboarding, getRecommendations, setMediaPreference } from "./recommendations";

afterEach(() => vi.unstubAllGlobals());

describe("recommendation API", () => {
  it("requests an encoded scoped page and preserves item and Watch Again order", async () => {
    const signal = new AbortController().signal;
    const response = {
      profileId: "profile one", scope: "movies", category: "Science Fiction", generatedAt: 10,
      stale: false, total: 2, offset: 0, limit: 48,
      categories: [{ value: "recommended", label: "Recommended", affinity: 0, serverCount: 1, cachedCount: 1 }],
      items: [
        { media: { id: "first", title: "First", type: "movie" }, source: "tmdb_cache", availability: "cached", score: 2, reasons: ["First reason"] },
        { media: { id: "second", title: "Second", type: "movie" }, source: "server", availability: "available", score: 99, reasons: [] },
      ],
      watchAgain: [
        { media: { id: "recent", title: "Recent", type: "movie" }, source: "server", availability: "available", score: 1, reasons: ["ignored"] },
        { media: { id: "older", title: "Older", type: "movie" }, source: "server", availability: "available", score: 100, reasons: [] },
      ],
      algorithmVersion: "v2.1",
      vibeRails: [{ id: "vibe-banter", label: "Witty Banter & Bullets", tropeIds: ["neo_noir_buddy_action"], reasonCode: "trope_match", items: [
        { media: { id: "vibe", title: "Vibe", type: "movie" }, source: "server", availability: "available", score: 4, reasons: ["A match"], reasonDetails: [{ code: "trope_match", subject: "Neo-Noir Buddy Action", fallbackText: "A match" }] },
      ] }],
    };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const result = await getRecommendations({ profileId: "profile one", scope: "movies", category: "Science Fiction", signal });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/recommendations/profile%20one?scope=movies&category=Science+Fiction&limit=48&offset=0");
    expect(fetchMock.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal);
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(false);
    expect(result.items.map((entry) => entry.media.id)).toEqual(["first", "second"]);
    expect(result.watchAgain.map((entry) => entry.media.id)).toEqual(["recent", "older"]);
    expect(result.algorithmVersion).toBe("v2.1");
    expect(result.vibeRails?.[0]).toMatchObject({ label: "Witty Banter & Bullets", tropeIds: ["neo_noir_buddy_action"] });
    expect(result.vibeRails?.[0].items[0].media.recommendationReasonDetails?.[0].code).toBe("trope_match");
    expect(result.items[0].media).toMatchObject({ source: "tmdb_cache", availability: "cached", recommendationScore: 2, recommendationReasons: ["First reason"] });
  });

  it("normalizes snake-case fields and malformed optional collections", async () => {
    const response = { profile_id: "1", scope: "home", category: "recommended", generated_at: 5, total: 0, offset: 0, limit: 48, categories: [{ value: "Drama", label: "Drama", server_count: 2, cached_count: 3 }], items: null, watch_again: null };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(response), { status: 200 })));
    const result = await getRecommendations({ profileId: "1", scope: "home" });
    expect(result.categories[0]).toMatchObject({ serverCount: 2, cachedCount: 3 });
    expect(result.items).toEqual([]);
    expect(result.watchAgain).toEqual([]);
    expect(result.vibeRails).toEqual([]);
    expect(result.algorithmVersion).toBe("v1");
  });

  it("uses explicit preference and diagnostics contracts", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ preferences: { m_1: "love" } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ movie_id: "m_1", preference: "dislike" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ profile_id: "1", period_days: 30, exposures: 10, details_opens: 4, playback_starts: 2, completions: 1, play_rate: .2, completion_rate: .5, preferences: { like: 1, love: 2, dislike: 3 }, candidate_pool: 12, candidate_sources: { taste_affinity: 12 }, catalog: { total: 20, available: 8, cached: 12 }, top_tastes: [] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ genres: ["action"], title_ids: ["m_1"] }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await getMediaPreferences("1")).toEqual({ m_1: "love" });
    await setMediaPreference("1", "m_1", "dislike");
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ preference: "dislike" });
    expect(await getRecommendationDiagnostics("1")).toMatchObject({ periodDays: 30, candidatePool: 12, playRate: .2 });
    expect(await getRecommendationOnboarding("1")).toEqual({ genres: ["action"], titleIds: ["m_1"] });
  });
  it.each([{}, null, {scope:"series",category:"recommended",categories:[]}, {scope:"home",category:"Drama",categories:[]}, {scope:"home",category:"recommended",profile_id:"other",categories:[]}, {scope:"home",category:"recommended"}, {scope:"home",category:"recommended",categories:[{}]}, {scope:"home",category:"recommended",categories:[],items:{}}, {scope:"home",category:"recommended",categories:[],items:[{}]}])("does not grant category authority from an invalid or mismatched feed %j", async (value) => {
    vi.stubGlobal("fetch",vi.fn(async()=>new Response(JSON.stringify(value),{status:200})));
    await expect(getRecommendations({profileId:"1",scope:"home"})).rejects.toMatchObject({status:0,code:"invalid_response"});
  });
  it.each([{}, {preferences:[]}, {preferences:{m_1:null}}, {preferences:{m_1:"invalid"}}])("does not interpret malformed preferences as a known empty map %j", async (value) => {
    vi.stubGlobal("fetch",vi.fn(async()=>new Response(JSON.stringify(value),{status:200})));
    await expect(getMediaPreferences("1")).rejects.toMatchObject({status:0,code:"invalid_response"});
  });
  it("accepts an explicitly confirmed empty preference map", async () => {
    vi.stubGlobal("fetch",vi.fn(async()=>new Response(JSON.stringify({preferences:{}}),{status:200})));
    await expect(getMediaPreferences("1")).resolves.toEqual({});
  });
  it.each([{}, {movie_id:"other",preference:"love"}, {movie_id:"m_1",preference:"like"}])("classifies an incomplete or mismatched mutation acknowledgement as unknown %j", async (value) => {
    vi.stubGlobal("fetch",vi.fn(async()=>new Response(JSON.stringify(value),{status:200})));
    await expect(setMediaPreference("1","m_1","love")).rejects.toMatchObject({status:0,code:"invalid_response"});
  });
  it("accepts an acknowledged removal without inventing a replacement preference", async () => {
    vi.stubGlobal("fetch",vi.fn(async()=>new Response(JSON.stringify({movie_id:"m_1",preference:null}),{status:200})));
    await expect(setMediaPreference("1","m_1",null)).resolves.toBeUndefined();
  });

});


describe("included recommendation display metadata boundaries", () => {
  const mediaValues = [{ id: "m_1", title: {} }, { id: "m_1", description: {} }, { id: "m_1", genres: [{}] }, { id: "m_1", cast: [{}] }, { id: "m_1", duration: {} }, { id: "m_1", episodes: [{ id: "ep_1", title: {} }] }];
  for (const collection of ["items", "watchAgain", "vibeRails"] as const) {
    it.each(mediaValues)(`rejects malformed ${collection} media before it reaches renderers %j`, async (media) => {
      const items = [{ media }];
      const body = { profileId: "1", scope: "home", category: "recommended", categories: [], [collection]: collection === "vibeRails" ? [{ id: "vibe", items }] : items };
      vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status: 200 })));
      await expect(getRecommendations({ profileId: "1", scope: "home" })).rejects.toMatchObject({ status: 0, code: "invalid_response" });
    });
  }
  it.each([
    { items: [{ media: { id: "m_1" }, reasons: [{}] }] },
    { items: [{ media: { id: "m_1" }, reasonDetails: [{ code: "taste", fallbackText: {} }] }] },
    { vibeRails: [{ id: "vibe", label: {}, items: [] }] },
    { vibeRails: [{ id: "vibe", tropeIds: [{}], items: [] }] },
    { total: {} }, { stale: "false" },
  ])("rejects malformed displayed feed metadata %j", async (fields) => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ profileId: "1", scope: "home", category: "recommended", categories: [], ...fields }), { status: 200 })));
    await expect(getRecommendations({ profileId: "1", scope: "home" })).rejects.toMatchObject({ status: 0, code: "invalid_response" });
  });
  it("keeps valid missing optional media metadata as defaults", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ profileId: "1", scope: "home", category: "recommended", categories: [], items: [{ media: { id: "m_1", title: "Minimal valid title", genres: null } }] }), { status: 200 })));
    const feed = await getRecommendations({ profileId: "1", scope: "home" });
    expect(feed.items[0].media).toMatchObject({ id: "m_1", title: "Minimal valid title", description: "", genres: [], cast: [], recommendationReasons: [] });
  });
});
