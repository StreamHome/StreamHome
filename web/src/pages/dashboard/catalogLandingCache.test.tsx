import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "../../stores/authStore";
import { landingCacheGeneration, readLandingFeed, saveLandingFeed } from "./catalogLandingCache";
import { catalogFeed, catalogMovie, catalogProfile, deferred } from "./catalogTestFixtures.nonplayer";
import { useCatalogController } from "./useCatalogController";

const reply = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
let sequence = 0;
beforeEach(() => {
  useAuthStore.setState({ isAuthenticated: true, email: `landing-${++sequence}@example.invalid` });
});
afterEach(() => { cleanup(); useAuthStore.setState({ isAuthenticated: false, email: null }); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
describe("landing feed display cache", () => {
  it("expires, bounds and partitions feeds and rejects a response from a retired login", () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(1_000);
    const owner = landingCacheGeneration();
    saveLandingFeed("viewer:home:recommended", catalogFeed(["Home"]), owner);
    expect(readLandingFeed("viewer:movies:recommended")).toBeNull();
    expect(readLandingFeed("viewer:home:Drama")).toBeNull();
    expect(readLandingFeed("other:home:recommended")).toBeNull();
    now.mockReturnValue(301_001); expect(readLandingFeed("viewer:home:recommended")).toBeNull();
    for (let index = 0; index < 25; index++) saveLandingFeed(`key-${index}`, catalogFeed(), owner);
    expect(readLandingFeed("key-0")).toBeNull(); expect(readLandingFeed("key-24")).toBeTruthy();
    useAuthStore.setState({ isAuthenticated: false });
    useAuthStore.setState({ isAuthenticated: true, email: "another@example.invalid" });
    saveLandingFeed("retired", catalogFeed(), owner);
    expect(readLandingFeed("retired")).toBeNull(); expect(readLandingFeed("key-24")).toBeNull();
  });
  it("reuses a feed while revalidating but never treats cached categories as current authority", async () => {
    const profile = { ...catalogProfile, id: `landing-controller-${sequence}` };
    const pending = deferred<Response>();
    let delay = false;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/preferences")) return Promise.resolve(reply({ preferences: {} }));
      if (url.startsWith("/api/movies?")) return Promise.resolve(reply([catalogMovie("Base")]));
      if (url.startsWith("/api/recommendations/")) return delay ? pending.promise : Promise.resolve(reply({ ...catalogFeed(["Cached feature"]), profileId: profile.id }));
      return Promise.resolve(reply([]));
    });
    vi.stubGlobal("fetch", fetchMock);
    const query = { profile: profile.id, view: "movies" as const };
    const first = renderHook(() => useCatalogController(profile, query, true));
    await waitFor(() => expect(first.result.current.categoriesReady).toBe(true));
    first.unmount(); delay = true;
    const second = renderHook(() => useCatalogController(profile, query, true));
    expect(second.result.current.recommendation?.items[0].media.id).toBe("Cached feature");
    expect(second.result.current.categoriesReady).toBe(false);
    await act(async () => pending.resolve(reply({ detail: "Unavailable" }, 503)));
    await waitFor(() => expect(second.result.current.recommendationError).toBeTruthy());
    expect(second.result.current.recommendation?.items[0].media.id).toBe("Cached feature");
    expect(second.result.current.categoriesReady).toBe(false);
    second.unmount();
    // A controller without automatic landing does not opt into feed reuse.
    const legacy = renderHook(() => useCatalogController(profile, query));
    expect(legacy.result.current.recommendation).toBeNull();
    await waitFor(() => expect(legacy.result.current.recommendationError).toBeTruthy());
  });
  it("expires reuse when returning through a non-catalog view in the same mounted controller", async () => {
    const clock = vi.spyOn(Date, "now").mockReturnValue(1_000);
    const profile = { ...catalogProfile, id: `return-${sequence}` };
    const pending = deferred<Response>(); let delay = false;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/preferences")) return Promise.resolve(reply({ preferences: {} }));
      if (url.startsWith("/api/recommendations/")) return delay ? pending.promise : Promise.resolve(reply({ ...catalogFeed(["Previous visit"]), profileId: profile.id }));
      return Promise.resolve(reply([]));
    }));
    const { result, rerender } = renderHook(({ view }) => useCatalogController(profile, { profile: profile.id, view }, true), { initialProps: { view: "movies" as "movies" | "watchlist" } });
    await waitFor(() => expect(result.current.categoriesReady).toBe(true));
    rerender({ view: "watchlist" }); clock.mockReturnValue(301_001); delay = true;
    rerender({ view: "movies" });
    expect(result.current.recommendation).toBeNull();
    expect(result.current.categoriesReady).toBe(false);
    expect(result.current.recommendationLoading).toBe(true);
    await act(async () => pending.resolve(reply({ ...catalogFeed(["Fresh visit"]), profileId: profile.id })));
    await waitFor(() => expect(result.current.recommendation?.items[0].media.id).toBe("Fresh visit"));
  });
  it("ignores old scope completions and retires catalog reads on profile change", async () => {
    const requests: Array<{ url: URL; response: ReturnType<typeof deferred<Response>> }> = [];
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/preferences")) return Promise.resolve(reply({ preferences: {} }));
      if (url.startsWith("/api/recommendations/") && url.includes("?")) { const request = { url: new URL(url, "http://synthetic.invalid"), response: deferred<Response>() }; requests.push(request); return request.response.promise; }
      if (url.startsWith("/api/movies?")) return Promise.resolve(reply([catalogMovie(url)]));
      return Promise.resolve(reply([]));
    }));
    const profile = { ...catalogProfile, id: `scope-${sequence}` };
    const { result, rerender } = renderHook(({ view, id }) => useCatalogController({ ...profile, id }, { profile: id, view }, true), { initialProps: { view: "movies" as "movies" | "series", id: profile.id } });
    await waitFor(() => expect(requests).toHaveLength(1));
    rerender({ view: "series", id: `${profile.id}-next` });
    await waitFor(() => expect(requests).toHaveLength(2));
    await act(async () => requests[0].response.resolve(reply({ ...catalogFeed(["Old"]), profileId: profile.id })));
    expect(result.current.recommendation).toBeNull();
    await act(async () => requests[1].response.resolve(reply({ ...catalogFeed(["New"]), profileId: `${profile.id}-next`, scope: "series" })));
    await waitFor(() => expect(result.current.recommendation?.items[0].media.id).toBe("New"));
    expect(result.current.catalogItems.every((movie) => movie.id.includes(`${profile.id}-next`))).toBe(true);
  });
});
