import React, { useEffect } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppQueryState } from "../../navigation/queryState";
import { catalogFeed, catalogMovie, catalogProfile, deferred } from "./catalogTestFixtures.nonplayer";
import { MotionProvider } from "../../motion/motionSystem";
import { useProfileStore } from "../../stores/profileStore";
import { DashboardRouter } from "./DashboardRouter";
import { RecommendationFeedback, RecommendationFeedbackProvider, feedbackPreference } from "./RecommendationFeedback";
import { useCatalogController } from "./useCatalogController";

const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
let routes: Map<string, () => Promise<Response> | Response>;
let fetchMock: ReturnType<typeof vi.fn>;
let serial = 0;
const viewer = () => ({ ...catalogProfile, id: `read-${++serial}` });
beforeEach(() => {
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) });
  Object.defineProperty(window, "scrollTo", { configurable: true, value: vi.fn() });
  routes = new Map();
  fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/preferences")) return Promise.resolve(reply({ preferences: {} }));
    const selected = [...routes].find(([key]) => url.includes(key));
    if (selected) return Promise.resolve(selected[1]());
    if (url.startsWith("/api/track/")) return Promise.resolve(reply([]));
    if (url.includes("/preferences")) return Promise.resolve(reply({ preferences: {} }));
    if (url.startsWith("/api/watchlist/")) return Promise.resolve(reply([]));
    if (url.startsWith("/api/movies?")) return Promise.resolve(reply([catalogMovie("base")]));
    if (url.startsWith("/api/recommendations/")) {
      const parsed = new URL(url, "http://synthetic.invalid");
      return Promise.resolve(reply({ ...catalogFeed(undefined, parsed.searchParams.get("category") ?? "recommended"), profileId: decodeURIComponent(parsed.pathname.split("/").pop()!), scope: parsed.searchParams.get("scope") }));
    }
    throw new Error(`Unexpected synthetic request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function CurrentRoute({ changes }: { changes: string[] }) {
  const location = useLocation(); useEffect(() => { changes.push(location.search); }, [changes, location.search]);
  return <output aria-label="Catalog route">{location.search}</output>;
}
function PreferenceHarness({ profile }: { profile: typeof catalogProfile }) {
  const controller = useCatalogController(profile, { profile: profile.id, view: "home" });
  const context = { profileId: profile.id, preferences: controller.preferences, preferencesKnown: controller.preferencesKnown, preferenceKnownIds: controller.preferenceKnownIds, preferenceOperations: controller.preferenceOperations, onCheck: controller.checkPreference, onChange: controller.updatePreference, feedGeneration: "", scope: "home", category: "recommended" };
  return <RecommendationFeedbackProvider value={context}><RecommendationFeedback movieId="base" preference={feedbackPreference(context, "base", "like")} onChange={controller.updatePreference} /></RecommendationFeedbackProvider>;
}
type DeferredRecommendationRequest = { url: URL; signal: AbortSignal | null; response: ReturnType<typeof deferred<Response>> };
function captureRecommendationRequests() {
  const requests: DeferredRecommendationRequest[] = [];
  const normal = fetchMock.getMockImplementation()!;
  fetchMock.mockImplementation((input: RequestInfo | URL, options?: RequestInit) => {
    const path = String(input);
    if (!path.startsWith("/api/recommendations/") || !path.includes("?")) return normal(input);
    const request = { url: new URL(path, "http://synthetic.invalid"), signal: options?.signal ?? null, response: deferred<Response>() };
    requests.push(request);
    return request.response.promise;
  });
  return requests;
}
function recommendationPage(request: DeferredRecommendationRequest, ids: string[], total = 3) {
  return reply({
    ...catalogFeed(ids, request.url.searchParams.get("category") ?? "recommended"),
    profileId: decodeURIComponent(request.url.pathname.split("/").pop()!),
    scope: request.url.searchParams.get("scope"), offset: Number(request.url.searchParams.get("offset")), total,
  });
}
describe("included catalog request ownership", () => {
  it.each(["love", null] as const)("keeps an unacknowledged departed preference %s unknown until a matching read confirms it", async (desired) => {
    const profile = viewer(); const normal = fetchMock.getMockImplementation()!; const mutation = deferred<Response>();
    let saved: "like" | "love" | null = "like";
    fetchMock.mockImplementation((input: RequestInfo | URL, options?: RequestInit) => {
      const path = String(input);
      if (path === `/api/recommendations/${profile.id}/preferences`) return Promise.resolve(reply({ preferences: saved ? { base: saved } : {} }));
      if (options?.method === "PUT" && path.endsWith("/preferences/base")) return mutation.promise;
      return normal(input);
    });
    const first = render(<PreferenceHarness profile={profile} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Remove like for this title" }).getAttribute("aria-pressed")).toBe("true"));
    fireEvent.click(screen.getByRole("button", { name: desired === null ? "Remove like for this title" : "Set love for this title" }));
    expect(screen.getByText("Saving preference…")).toBeTruthy(); first.unmount();
    render(<PreferenceHarness profile={profile} />);
    fireEvent.click(await screen.findByRole("button", { name: "Check preference" }));
    await screen.findByText(/still unconfirmed/);
    for (const button of screen.getAllByRole("button", { name: /for this title/ })) {
      expect(button.hasAttribute("disabled")).toBe(true); expect(button.getAttribute("aria-pressed")).toBe("false");
    }
    fireEvent.click(screen.getByRole("button", { name: "Set dislike for this title" }));
    expect(fetchMock.mock.calls.filter(([, options]) => options?.method === "PUT")).toHaveLength(1);
    saved = desired; await act(async () => { mutation.resolve(reply({ movie_id: "base", preference: desired })); });
    expect(screen.getByRole("button", { name: "Check preference" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Set love for this title" }).getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "Check preference" }));
    await screen.findByText("Preference checked. The saved preference is confirmed.");
    expect(screen.queryByRole("button", { name: "Check preference" })).toBeNull();
    if (desired === "love") expect(screen.getByRole("button", { name: "Remove love for this title" }).getAttribute("aria-pressed")).toBe("true");
    else expect(screen.getAllByRole("button", { name: /for this title/ }).every((button) => button.getAttribute("aria-pressed") === "false" && !button.hasAttribute("disabled"))).toBe(true);
    expect(fetchMock.mock.calls.filter(([, options]) => options?.method === "PUT")).toHaveLength(1);
  });

  it("retains definitive rejection meaning when its pending reconciliation is retired and remounted", async () => {
    const profile = viewer(); const query: AppQueryState = { profile: profile.id, view: "home" }; const normal = fetchMock.getMockImplementation()!;
    const retiredRead = deferred<Response>(); let reads = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, options?: RequestInit) => {
      const path = String(input);
      if (path === `/api/recommendations/${profile.id}/preferences`) return ++reads === 2 ? retiredRead.promise : Promise.resolve(reply({ preferences: { base: "like" } }));
      if (options?.method === "PUT" && path.endsWith("/preferences/base")) return Promise.resolve(reply({ detail: "Preference rejected" }, 400));
      return normal(input);
    });
    const first = renderHook(() => useCatalogController(profile, query));
    await waitFor(() => expect(first.result.current.preferences.base).toBe("like"));
    let rejected!: Promise<void>; act(() => { rejected = first.result.current.updatePreference("base", "love").catch(() => undefined); });
    await waitFor(() => expect(first.result.current.preferenceOperations?.base.status).toBe("checking"));
    expect(first.result.current.preferenceOperations?.base.uncertain).toBe(false); first.unmount();
    const next = renderHook(() => useCatalogController(profile, query));
    expect(next.result.current.preferenceOperations?.base).toMatchObject({ status: "unknown", uncertain: false });
    await act(async () => { await next.result.current.checkPreference?.("base"); });
    expect(next.result.current.preferences.base).toBe("like"); expect(next.result.current.preferenceOperations?.base).toMatchObject({ status: "error", uncertain: false });
    await act(async () => { retiredRead.resolve(reply({ preferences: { base: "love" } })); await rejected; });
    expect(next.result.current.preferences.base).toBe("like");
    expect(fetchMock.mock.calls.filter(([, options]) => options?.method === "PUT")).toHaveLength(1);
  });

  it("keeps malformed recommendation media out of the mounted controller and recovers with a read retry", async () => {
    const profile = viewer(); const query: AppQueryState = { profile: profile.id, view: "movies" };
    routes.set("/api/recommendations/", () => reply({ ...catalogFeed(), profileId: profile.id, scope: "movies", items: [{ media: { id: "m_1", title: {}, description: {}, genres: [{}] } }] }));
    const { result } = renderHook(() => useCatalogController(profile, query));
    await waitFor(() => expect(result.current.recommendationError).toContain("invalid or outdated recommendation feed"));
    expect(result.current.recommendation).toBeNull(); expect(result.current.categoriesReady).toBe(false);
    expect(result.current.movies.map((movie) => movie.id)).toEqual(["base"]);
    routes.set("/api/recommendations/", () => reply({ ...catalogFeed(["valid-after-retry"]), profileId: profile.id, scope: "movies" }));
    act(() => result.current.refreshRecommendations());
    await waitFor(() => expect(result.current.categoriesReady).toBe(true));
    expect(result.current.recommendation?.items.map((item) => item.media.id)).toEqual(["valid-after-retry"]);
    expect(result.current.recommendationError).toBe("");
  });

  it("keeps category authority pending until the current successful feed, independently of catalog", async () => {
    const feed = deferred<Response>(); routes.set("/api/recommendations/", () => feed.promise);
    const profile = viewer(); const query: AppQueryState = { profile: profile.id, view: "movies", genre: "Drama" };
    const { result } = renderHook(() => useCatalogController(profile, query));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.categoriesReady).toBe(false);
    expect(result.current.catalogItems.map((item) => item.id)).toEqual(["base"]);
    await act(async () => feed.resolve(reply({ ...catalogFeed(["remote"], "Drama"), profileId: profile.id, scope: "movies" })));
    await waitFor(() => expect(result.current.categoriesReady).toBe(true));
    expect(result.current.categories).toContain("Drama");
    expect(result.current.catalogItems.map((item) => item.id)).toEqual(["base"]);
    expect(result.current.movies.map((item) => item.id)).toEqual(["base", "remote"]);
  });
  it("does not reuse another scope's successful category authority and ignores stale completions", async () => {
    const old = deferred<Response>(); const next = deferred<Response>();
    routes.set("/api/recommendations/", () => old.promise);
    const profile = viewer(); const initial: AppQueryState = { profile: profile.id, view: "movies", genre: "Drama" };
    const { result, rerender } = renderHook(({ query }) => useCatalogController(profile, query), { initialProps: { query: initial } });
    routes.set("/api/recommendations/", () => next.promise);
    rerender({ query: { ...initial, view: "series" } });
    expect(result.current.categoriesReady).toBe(false);
    await act(async () => old.resolve(reply({ ...catalogFeed(["old"], "Drama"), profileId: profile.id, scope: "movies" })));
    expect(result.current.categoriesReady).toBe(false);
    await act(async () => next.resolve(reply({ detail: "Feed offline" }, 503)));
    await waitFor(() => expect(result.current.recommendationError).toBe("Feed offline"));
    expect(result.current.categoriesReady).toBe(false);
  });
  it.each(["category", "profile", "scope", "explicit refresh"] as const)("retires pending pagination on %s replacement without releasing the next page's ownership", async (replacement) => {
    const requests = captureRecommendationRequests();
    const profile = viewer(); const otherProfile = viewer();
    const query: AppQueryState = { profile: profile.id, view: "movies", genre: "Drama" };
    const { result, rerender } = renderHook(({ profile, query }) => useCatalogController(profile, query), { initialProps: { profile, query } });
    await waitFor(() => expect(requests).toHaveLength(1));
    await act(async () => requests[0].response.resolve(recommendationPage(requests[0], ["old-head"])));
    await waitFor(() => expect(result.current.recommendationLoading).toBe(false));
    expect(result.current.recommendationHasMore).toBe(true);
    let oldPage!: Promise<void>;
    act(() => { oldPage = result.current.loadMoreRecommendations(); });
    expect(requests).toHaveLength(2);
    const previousPage = requests[1];
    expect(previousPage.url.searchParams.get("offset")).toBe("1");
    expect(previousPage.signal?.aborted).toBe(false);
    expect(result.current.recommendationLoadingMore).toBe(true);

    if (replacement === "category") rerender({ profile, query: { ...query, genre: "all" } });
    else if (replacement === "profile") rerender({ profile: otherProfile, query: { ...query, profile: otherProfile.id } });
    else if (replacement === "scope") rerender({ profile, query: { ...query, view: "series" } });
    else act(() => result.current.refreshRecommendations());
    await waitFor(() => expect(requests).toHaveLength(3));
    const replacementHead = requests[2];
    expect(previousPage.signal?.aborted).toBe(true);
    expect(result.current.recommendationLoadingMore).toBe(false);
    expect(result.current.recommendationLoading).toBe(true);
    expect(replacementHead.url.pathname).toBe(`/api/recommendations/${replacement === "profile" ? otherProfile.id : profile.id}`);
    expect(replacementHead.url.searchParams.get("scope")).toBe(replacement === "scope" ? "series" : "movies");
    expect(replacementHead.url.searchParams.get("category")).toBe(replacement === "category" ? "all" : "Drama");
    expect(replacementHead.url.searchParams.get("offset")).toBe("0");
    await act(async () => result.current.loadMoreRecommendations());
    expect(requests).toHaveLength(3);

    await act(async () => replacementHead.response.resolve(recommendationPage(replacementHead, ["new-head"])));
    await waitFor(() => expect(result.current.recommendationLoading).toBe(false));
    expect(result.current.recommendation?.items.map((item) => item.media.id)).toEqual(["new-head"]);
    let newPage!: Promise<void>;
    act(() => { newPage = result.current.loadMoreRecommendations(); });
    expect(requests).toHaveLength(4);
    const currentPage = requests[3];
    expect(currentPage.url.pathname).toBe(replacementHead.url.pathname);
    expect(currentPage.url.searchParams.get("scope")).toBe(replacementHead.url.searchParams.get("scope"));
    expect(currentPage.url.searchParams.get("category")).toBe(replacementHead.url.searchParams.get("category"));
    expect(currentPage.url.searchParams.get("offset")).toBe("1");
    expect(currentPage.signal?.aborted).toBe(false);
    expect(result.current.recommendationLoadingMore).toBe(true);

    // Resolve the cancelled fetch after its replacement page has acquired ownership.
    await act(async () => { previousPage.response.resolve(recommendationPage(previousPage, ["stale-page"])); await oldPage; });
    expect(result.current.recommendation?.items.map((item) => item.media.id)).toEqual(["new-head"]);
    expect(result.current.recommendationError).toBe("");
    expect(result.current.recommendationLoadingMore).toBe(true);
    expect(currentPage.signal?.aborted).toBe(false);
    await act(async () => result.current.loadMoreRecommendations());
    expect(requests).toHaveLength(4);

    // A later refresh must still reach the current page's cancellation controller.
    act(() => result.current.refreshRecommendations());
    expect(requests).toHaveLength(5);
    expect(currentPage.signal?.aborted).toBe(true);
    expect(result.current.recommendationLoadingMore).toBe(false);
    const finalHead = requests[4];
    await act(async () => { currentPage.response.resolve(recommendationPage(currentPage, ["new-page"], 2)); await newPage; });
    expect(result.current.recommendation?.items.map((item) => item.media.id)).toEqual(["new-head"]);
    expect(result.current.recommendationLoading).toBe(true);
    await act(async () => finalHead.response.resolve(recommendationPage(finalHead, ["final-head"], 1)));
    await waitFor(() => expect(result.current.recommendationLoading).toBe(false));
    expect(result.current.recommendation?.items.map((item) => item.media.id)).toEqual(["final-head"]);
    expect(result.current.recommendationHasMore).toBe(false);
  });
  it("cancels pending pagination when leaving catalog scope and permits pagination after returning", async () => {
    const requests = captureRecommendationRequests();
    const profile = viewer(); const query: AppQueryState = { profile: profile.id, view: "movies", genre: "Drama" };
    const { result, rerender } = renderHook(({ query }) => useCatalogController(profile, query), { initialProps: { query } });
    await waitFor(() => expect(requests).toHaveLength(1));
    await act(async () => requests[0].response.resolve(recommendationPage(requests[0], ["old-head"])));
    await waitFor(() => expect(result.current.recommendationLoading).toBe(false));
    let oldPage!: Promise<void>;
    act(() => { oldPage = result.current.loadMoreRecommendations(); });
    expect(requests).toHaveLength(2);
    expect(result.current.recommendationLoadingMore).toBe(true);
    const previousPage = requests[1];

    rerender({ query: { ...query, view: "watchlist" } });
    expect(previousPage.signal?.aborted).toBe(true);
    expect(result.current.recommendationLoadingMore).toBe(false);
    expect(result.current.recommendationLoading).toBe(false);
    await act(async () => result.current.loadMoreRecommendations());
    expect(requests).toHaveLength(2);
    await act(async () => { previousPage.response.resolve(recommendationPage(previousPage, ["stale-page"])); await oldPage; });
    expect(result.current.recommendationLoadingMore).toBe(false);
    expect(result.current.recommendationError).toBe("");

    rerender({ query });
    await waitFor(() => expect(requests).toHaveLength(3));
    const replacementHead = requests[2];
    await act(async () => replacementHead.response.resolve(recommendationPage(replacementHead, ["returned-head"])));
    await waitFor(() => expect(result.current.recommendationLoading).toBe(false));
    let currentPage!: Promise<void>;
    act(() => { currentPage = result.current.loadMoreRecommendations(); });
    expect(requests).toHaveLength(4);
    expect(result.current.recommendationLoadingMore).toBe(true);
    await act(async () => { requests[3].response.resolve(recommendationPage(requests[3], ["returned-page"], 2)); await currentPage; });
    expect(result.current.recommendation?.items.map((item) => item.media.id)).toEqual(["returned-head", "returned-page"]);
    expect(result.current.recommendationLoadingMore).toBe(false);
  });
  it("distinguishes unknown watchlist from a successful empty list and retries only its read", async () => {
    const saved = deferred<Response>(); const history = deferred<Response>();
    routes.set("/api/watchlist/", () => saved.promise); routes.set("/api/track/", () => history.promise);
    const profile = viewer(); const query: AppQueryState = { profile: profile.id, view: "watchlist" };
    const { result } = renderHook(() => useCatalogController(profile, query));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.watchlistKnown).toBe(false); expect(result.current.watchlistStatus).toBe("pending");
    await act(async () => saved.resolve(reply({ detail: "Saved list offline" }, 503)));
    await waitFor(() => expect(result.current.watchlistStatus).toBe("error"));
    expect(result.current.watchlistKnown).toBe(false);
    routes.set("/api/watchlist/", () => reply([]));
    act(() => result.current.retryWatchlist());
    await waitFor(() => expect(result.current.watchlistKnown).toBe(true));
    expect(result.current.watchlist).toEqual([]);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/api/movies?")).length).toBe(1);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/api/track/")).length).toBe(1);
  });
  it("does not manufacture watchlist cache success from a movies-only cache entry", async () => {
    const pending = deferred<Response>(); routes.set("/api/watchlist/", () => pending.promise);
    const profile = viewer(); const query: AppQueryState = { profile: profile.id, view: "watchlist" };
    const first = renderHook(() => useCatalogController(profile, query));
    await waitFor(() => expect(first.result.current.loading).toBe(false)); first.unmount();
    const second = renderHook(() => useCatalogController(profile, query));
    expect(second.result.current.loading).toBe(false);
    expect(second.result.current.watchlistKnown).toBe(false);
    expect(second.result.current.watchlistStatus).toBe("pending");
    await act(async () => { await Promise.resolve(); });
    second.unmount();
  });
  it("retains known saved order on refresh failure and resolves IDs absent from the catalog without fabricating records", async () => {
    routes.set("/api/watchlist/", () => reply(["remote", "base"]));
    routes.set("/api/movies/remote", () => reply(catalogMovie("remote")));
    const profile = viewer(); const query: AppQueryState = { profile: profile.id, view: "watchlist" };
    const { result } = renderHook(() => useCatalogController(profile, query));
    await waitFor(() => expect(result.current.watchlistItems.map((item) => item.id)).toEqual(["remote", "base"]));
    routes.set("/api/watchlist/", () => reply({ detail: "Read unavailable" }, 503));
    act(() => result.current.retryWatchlist());
    await waitFor(() => expect(result.current.watchlistStatus).toBe("error"));
    expect(result.current.watchlistKnown).toBe(true);
    expect(result.current.watchlistItems.map((item) => item.id)).toEqual(["remote", "base"]);
  });
  it("fences an older watchlist GET after an authoritative toggle response", async () => {
    const saved = deferred<Response>(); routes.set("/api/watchlist/", () => saved.promise);
    const profile = viewer(); const query: AppQueryState = { profile: profile.id, view: "watchlist" };
    const { result } = renderHook(() => useCatalogController(profile, query));
    act(() => result.current.setWatchlist(["base"]));
    await act(async () => saved.resolve(reply([])));
    expect(result.current.watchlist).toEqual(["base"]);
    expect(result.current.watchlistKnown).toBe(true);
  });
  it("retires a previous profile's watchlist and ignores its late GET", async () => {
    const saved = deferred<Response>(); const firstProfile = viewer(); const other = viewer();
    routes.set(`/api/watchlist/${firstProfile.id}`, () => saved.promise);
    const { result, rerender } = renderHook(({ profile }) => useCatalogController(profile, { profile: profile.id, view: "watchlist" }), { initialProps: { profile: firstProfile } });
    rerender({ profile: other });
    await waitFor(() => expect(result.current.watchlistKnown).toBe(true));
    await act(async () => saved.resolve(reply(["old-profile-title"])));
    expect(result.current.watchlist).toEqual([]);
  });
  it("owns search errors and retries by query, never exposing old results as current", async () => {
    const old = deferred<Response>(); routes.set("/api/search", () => old.promise);
    const profile = viewer(); const initial: AppQueryState = { profile: profile.id, view: "search", q: "first" };
    const { result, rerender } = renderHook(({ query }) => useCatalogController(profile, query), { initialProps: { query: initial } });
    routes.set("/api/search", () => reply({ detail: "Search offline" }, 503));
    rerender({ query: { ...initial, q: "second" } });
    expect(result.current.results).toEqual([]);
    await waitFor(() => expect(result.current.searchStatus).toBe("error"));
    expect(result.current.searchError).toBe("Search offline"); expect(result.current.error).toBe("");
    await act(async () => old.resolve(reply([{ ...catalogMovie("m_12"), tmdbId: 12 }])));
    expect(result.current.results).toEqual([]);
    routes.set("/api/search", () => reply([])); act(() => result.current.retrySearch());
    await waitFor(() => expect(result.current.searchStatus).toBe("success"));
    expect(result.current.results).toEqual([]);
    const searches = fetchMock.mock.calls.filter(([url]) => String(url).includes("/api/search"));
    expect(searches[searches.length - 1]?.[0]).toContain("second");
  });
  it.each(["Drama", "dRaMa"])("keeps the valid %s category URL while catalog settles before feed", async (genre) => {
    const pending = deferred<Response>(); routes.set("/api/recommendations/", () => pending.promise);
    const profile = viewer(); useProfileStore.setState({ activeProfile: profile }); const changes: string[] = [];
    render(<MemoryRouter initialEntries={[`/app?profile=${profile.id}&view=movies&genre=${genre}`]}><MotionProvider><DashboardRouter /><CurrentRoute changes={changes} /></MotionProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/The catalog below is unfiltered/)).toBeTruthy());
    expect(screen.getByLabelText("Catalog route").textContent).toContain(`genre=${genre}`);
    await act(async () => pending.resolve(reply({ ...catalogFeed(["Recommendation"], "Drama"), profileId: profile.id, scope: "movies" })));
    await waitFor(() => expect(screen.getByText("Recommendations ready")).toBeTruthy());
    expect(screen.getByLabelText("Catalog route").textContent).toContain(`genre=${genre}`); expect(changes.length).toBe(1);
  });
  it("canonicalizes a truly invalid genre exactly once after successful authoritative categories", async () => {
    const pending = deferred<Response>(); routes.set("/api/recommendations/", () => pending.promise);
    const profile = viewer(); useProfileStore.setState({ activeProfile: profile }); const changes: string[] = [];
    render(<MemoryRouter initialEntries={[`/app?profile=${profile.id}&view=movies&genre=Unknown`]}><MotionProvider><DashboardRouter /><CurrentRoute changes={changes} /></MotionProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/The catalog below is unfiltered/)).toBeTruthy()); expect(changes.length).toBe(1);
    routes.set("/api/recommendations/", () => reply({ ...catalogFeed(), profileId: profile.id, scope: "movies" }));
    await act(async () => pending.resolve(reply({ ...catalogFeed(["Recommendation"], "Unknown"), profileId: profile.id, scope: "movies" })));
    await waitFor(() => expect(screen.getByLabelText("Catalog route").textContent).not.toContain("genre="));
    expect(changes.length).toBe(2);
  });

  it.each([true, false])("retires previous-profile feedback completion (success=%s) without overwriting or refreshing the current profile", async (success) => {
    const pending = deferred<Response>(); const firstProfile = viewer(); const secondProfile = viewer();
    const normal = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input: RequestInfo | URL, options?: RequestInit) => {
      const path = String(input);
      if (options?.method === "PUT" && path.includes("/preferences/")) return pending.promise;
      if (path.includes(`/api/recommendations/${secondProfile.id}/preferences`)) return Promise.resolve(reply({preferences: {base: "love"}}));
      return normal(input);
    });
    const {result, rerender} = renderHook(({profile}) => useCatalogController(profile, {profile: profile.id, view: "home"}), {initialProps: {profile: firstProfile}});
    await waitFor(() => expect(result.current.loading).toBe(false));
    let request!: Promise<void>; act(() => { request = result.current.updatePreference("base", "like"); });
    rerender({profile: secondProfile});
    await waitFor(() => expect(result.current.preferences.base).toBe("love"));
    const before = fetchMock.mock.calls.filter(([url]) => String(url).startsWith(`/api/recommendations/${secondProfile.id}?`)).length;
    await act(async () => { pending.resolve(success ? reply({ movie_id: "base", preference: "like" }) : reply({detail: "Old profile failure"}, 400)); await request; });
    expect(result.current.preferences.base).toBe("love"); expect(result.current.error).toBe("");
    expect(fetchMock.mock.calls.filter(([url]) => String(url).startsWith(`/api/recommendations/${secondProfile.id}?`)).length).toBe(before);
  });

  it.each([
    { submitted: "love" as const, succeeds: true, expected: "love" },
    { submitted: null, succeeds: true, expected: undefined },
    { submitted: "love" as const, succeeds: false, expected: "like" },
  ])("keeps a touched title authoritative after a deferred initial GET (submitted=$submitted success=$succeeds), merging untouched titles", async ({ submitted, succeeds, expected }) => {
    const profile = viewer(); const query: AppQueryState = { profile: profile.id, view: "home" };
    const normal = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input: RequestInfo | URL, options?: RequestInit) => {
      if (String(input) === `/api/recommendations/${profile.id}/preferences` && options?.method !== "PUT") return Promise.resolve(reply({ preferences: { base: "like", removedByRead: "dislike" } }));
      return normal(input);
    });
    const seed = renderHook(() => useCatalogController(profile, query));
    await waitFor(() => expect(seed.result.current.preferences.base).toBe("like")); seed.unmount();
    const oldRead = deferred<Response>(); let reads = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, options?: RequestInit) => {
      const path = String(input);
      if (path === `/api/recommendations/${profile.id}/preferences`) return ++reads === 1 ? oldRead.promise : Promise.resolve(reply({ preferences: { base: "like", untouched: "love" } }));
      if (path.includes("/preferences/base") && options?.method === "PUT") return Promise.resolve(succeeds ? reply({ movie_id: "base", preference: submitted }) : reply({ detail: "Feedback rejected" }, 400));
      return normal(input);
    });
    const { result, unmount } = renderHook(() => useCatalogController(profile, query));
    expect(result.current.preferences.base).toBe("like");
    await act(async () => { await result.current.updatePreference("base", submitted).catch(() => undefined); });
    expect(result.current.preferences.base).toBe(expected);
    await act(async () => oldRead.resolve(reply({ preferences: { base: "like", untouched: "love" } })));
    await waitFor(() => expect(result.current.preferences.untouched).toBe("love"));
    expect(result.current.preferences.base).toBe(expected);
    expect(result.current.preferences.removedByRead).toBeUndefined();
    unmount();
    fetchMock.mockImplementation((input: RequestInfo | URL) => String(input) === `/api/recommendations/${profile.id}/preferences` ? new Promise<Response>(() => undefined) : normal(input));
    const remounted = renderHook(() => useCatalogController(profile, query));
    expect(remounted.result.current.preferences).toEqual(expected ? { base: expected, untouched: "love" } : { untouched: "love" });
    await act(async () => { await Promise.resolve(); }); remounted.unmount();
  });
  it("keeps a failed mutation rollback after an older initial read merged while the mutation was pending", async () => {
    const profile = viewer(); const query: AppQueryState = { profile: profile.id, view: "home" };
    const normal = fetchMock.getMockImplementation()!;
    const oldRead = deferred<Response>(); const mutation = deferred<Response>(); let reads = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, options?: RequestInit) => {
      if (String(input) === `/api/recommendations/${profile.id}/preferences`) return ++reads === 1 ? oldRead.promise : Promise.resolve(reply({ preferences: { base: "like", untouched: "love" } }));
      if (options?.method === "PUT" && String(input).includes("/preferences/base")) return mutation.promise;
      return normal(input);
    });
    const { result, unmount } = renderHook(() => useCatalogController(profile, query));
    let saved!: Promise<void>; act(() => { saved = result.current.updatePreference("base", "love").catch(() => undefined); });
    await act(async () => oldRead.resolve(reply({ preferences: { base: "like", untouched: "love" } })));
    expect(result.current.preferences).toEqual({ base: "love", untouched: "love" });
    await act(async () => { mutation.resolve(reply({ detail: "Feedback rejected" }, 400)); await saved; });
    expect(result.current.preferences).toEqual({ base: "like", untouched: "love" });
    unmount();
    const remounted = renderHook(() => useCatalogController(profile, query));
    expect(remounted.result.current.preferences).toEqual({ base: "like", untouched: "love" });
    await act(async () => { await Promise.resolve(); }); remounted.unmount();
  });
  it("ignores a deferred initial preference GET after profile replacement and after unmount", async () => {
    const firstProfile = viewer(); const secondProfile = viewer(); const oldRead = deferred<Response>();
    const normal = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      if (String(input) === `/api/recommendations/${firstProfile.id}/preferences`) return oldRead.promise;
      if (String(input) === `/api/recommendations/${secondProfile.id}/preferences`) return Promise.resolve(reply({ preferences: { base: "love" } }));
      return normal(input);
    });
    const { result, rerender, unmount } = renderHook(({ profile }) => useCatalogController(profile, { profile: profile.id, view: "home" }), { initialProps: { profile: firstProfile } });
    rerender({ profile: secondProfile }); await waitFor(() => expect(result.current.preferences.base).toBe("love"));
    await act(async () => oldRead.resolve(reply({ preferences: { base: "dislike", retired: "like" } })));
    expect(result.current.preferences).toEqual({ base: "love" }); unmount();
    const afterUnmount = deferred<Response>();
    fetchMock.mockImplementation((input: RequestInfo | URL) => String(input) === `/api/recommendations/${secondProfile.id}/preferences` ? afterUnmount.promise : normal(input));
    const departed = renderHook(() => useCatalogController(secondProfile, { profile: secondProfile.id, view: "home" })); departed.unmount();
    await act(async () => afterUnmount.resolve(reply({ preferences: { base: "dislike" } })));
    const remounted = renderHook(() => useCatalogController(secondProfile, { profile: secondProfile.id, view: "home" }));
    expect(remounted.result.current.preferences.base).toBe("love"); remounted.unmount();
  });
  it("rejects malformed successful watchlist bodies without inventing a cold empty list or replacing known saved IDs", async () => {
    routes.set("/api/watchlist/", () => reply({}));
    const coldProfile = viewer();
    const cold = renderHook(() => useCatalogController(coldProfile, { profile: coldProfile.id, view: "watchlist" }));
    await waitFor(() => expect(cold.result.current.watchlistStatus).toBe("error"));
    expect(cold.result.current.watchlistKnown).toBe(false); expect(cold.result.current.watchlistError).toContain("invalid saved list"); cold.unmount();
    routes.set("/api/watchlist/", () => reply(["base"]));
    const profile = viewer(); const { result } = renderHook(() => useCatalogController(profile, { profile: profile.id, view: "watchlist" }));
    await waitFor(() => expect(result.current.watchlistStatus).toBe("success"));
    routes.set("/api/watchlist/", () => reply({})); act(() => result.current.retryWatchlist());
    await waitFor(() => expect(result.current.watchlistStatus).toBe("error"));
    expect(result.current.watchlistKnown).toBe(true); expect(result.current.watchlist).toEqual(["base"]);
  });
  it("treats a successful malformed search body as an error, then retries the same query with a valid actual DiscoverMovie", async () => {
    routes.set("/api/search", () => reply({})); const profile = viewer();
    const { result } = renderHook(() => useCatalogController(profile, { profile: profile.id, view: "search", q: "same query" }));
    await waitFor(() => expect(result.current.searchStatus).toBe("error"));
    expect(result.current.searchError).toContain("invalid search results"); expect(result.current.results).toEqual([]);
    routes.set("/api/search", () => reply([{ ...catalogMovie("m_12"), tmdbId: 12 }])); act(() => result.current.retrySearch());
    await waitFor(() => expect(result.current.searchStatus).toBe("success"));
    expect(result.current.results.map((item) => item.id)).toEqual(["m_12"]);
    expect(result.current.resolveMovie("m_12")?.availability).toBe("cached");
  });

  it("confirms a committed Like to Love update after its response body is lost and serializes duplicate same-title handlers", async () => {
    const profile = viewer(); const normal = fetchMock.getMockImplementation()!; let saved = "like";
    fetchMock.mockImplementation(async (input: RequestInfo | URL, options?: RequestInit) => {
      const path = String(input);
      if (path === `/api/recommendations/${profile.id}/preferences`) return reply({ preferences: { base: saved, untouched: "dislike" } });
      if (options?.method === "PUT" && path.endsWith("/preferences/base")) {
        saved = "love";
        const response = reply({ movie_id: "base", preference: saved });
        Object.defineProperty(response, "text", { value: async () => { throw new TypeError("Synthetic response body lost after commit"); } });
        return response;
      }
      return normal(input);
    });
    const { result } = renderHook(() => useCatalogController(profile, { profile: profile.id, view: "home" }));
    await waitFor(() => expect(result.current.preferences.base).toBe("like"));
    await act(async () => { await Promise.all([result.current.updatePreference("base", "love"), result.current.updatePreference("base", "dislike")]); });
    expect(result.current.preferences.base).toBe("love");
    expect(result.current.preferenceOperations?.base.status).toBe("saved");
    expect(result.current.preferenceKnownIds?.base).toBe(true);
    expect(fetchMock.mock.calls.filter(([, options]) => (options as RequestInit | undefined)?.method === "PUT").length).toBe(1);
  });
  it("keeps failed or different unknown-outcome reads blocked with a local read-only check until the desired value is confirmed", async () => {
    const profile = viewer(); const normal = fetchMock.getMockImplementation()!; let mode = "initial";
    fetchMock.mockImplementation(async (input: RequestInfo | URL, options?: RequestInit) => {
      const path = String(input);
      if (path === `/api/recommendations/${profile.id}/preferences`) return mode === "initial" ? reply({ preferences: { base: "like" } }) : mode === "unavailable" ? reply({}) : reply({ preferences: { base: mode === "different" ? "like" : "love" } });
      if (options?.method === "PUT" && path.endsWith("/preferences/base")) { mode = "unavailable"; throw new TypeError("Synthetic lost response after server commit"); }
      return normal(input);
    });
    render(<PreferenceHarness profile={profile} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Remove like for this title" }).getAttribute("aria-pressed")).toBe("true"));
    fireEvent.click(screen.getByRole("button", { name: "Set love for this title" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Check preference" })).toBeTruthy());
    screen.getAllByRole("button", { name: /for this title/ }).forEach((button) => { expect(button.hasAttribute("disabled")).toBe(true); expect(button.getAttribute("aria-pressed")).toBe("false"); });
    mode = "different"; fireEvent.click(screen.getByRole("button", { name: "Check preference" }));
    await waitFor(() => expect(screen.getByText(/still unconfirmed/)).toBeTruthy());
    expect(screen.getByRole("button", { name: "Set love for this title" }).hasAttribute("disabled")).toBe(true);
    mode = "matching"; fireEvent.click(screen.getByRole("button", { name: "Check preference" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Remove love for this title" }).getAttribute("aria-pressed")).toBe("true"));
    expect(screen.queryByRole("button", { name: "Check preference" })).toBeNull();
    expect(fetchMock.mock.calls.filter(([, options]) => (options as RequestInit | undefined)?.method === "PUT").length).toBe(1);
  });
  it("does not invent a rollback after definitive rejection when the fresh preference read fails", async () => {
    const profile = viewer(); const normal = fetchMock.getMockImplementation()!; const initial = deferred<Response>(); const mutation = deferred<Response>(); let reads = 0; let readable = false;
    fetchMock.mockImplementation(async (input: RequestInfo | URL, options?: RequestInit) => {
      const path = String(input);
      if (path === `/api/recommendations/${profile.id}/preferences`) return ++reads === 1 ? initial.promise : readable ? reply({ preferences: { base: "like" } }) : reply({ detail: "Preference read unavailable" }, 503);
      if (options?.method === "PUT" && path.endsWith("/preferences/base")) return mutation.promise;
      return normal(input);
    });
    const { result } = renderHook(() => useCatalogController(profile, { profile: profile.id, view: "home" }));
    let request!: Promise<void>; act(() => { request = result.current.updatePreference("base", "love").catch(() => undefined); });
    await act(async () => initial.resolve(reply({ preferences: { base: "like" } })));
    await act(async () => { mutation.resolve(reply({ detail: "Preference rejected" }, 400)); await request; });
    expect(result.current.preferenceOperations?.base.status).toBe("unknown");
    expect(result.current.preferenceOperations?.base.uncertain).toBe(false);
    expect(result.current.preferences.base).toBe("love");
    await act(async () => result.current.updatePreference("base", "dislike"));
    expect(fetchMock.mock.calls.filter(([, options]) => (options as RequestInit | undefined)?.method === "PUT").length).toBe(1);
    readable = true; await act(async () => result.current.checkPreference?.("base"));
    expect(result.current.preferences.base).toBe("like"); expect(result.current.preferenceOperations?.base.status).toBe("error");
  });
  it.each(["missing", "scope", "category", "profile"])("rejects a malformed or wrong-%s actual recommendation response before it can authorize categories", async (kind) => {
    const profile = viewer(); const valid = { ...catalogFeed(["record"], "Drama"), profileId: profile.id, scope: "movies" };
    const body = kind === "missing" ? {} : kind === "scope" ? { ...valid, scope: "series" } : kind === "category" ? { ...valid, category: "Comedy" } : { ...valid, profileId: "other-profile" };
    routes.set("/api/recommendations/", () => reply(body));
    const { result } = renderHook(() => useCatalogController(profile, { profile: profile.id, view: "movies", genre: "Drama" }));
    await waitFor(() => expect(result.current.recommendationError).toContain("invalid or outdated recommendation feed"));
    expect(result.current.categoriesReady).toBe(false); expect(result.current.recommendation).toBeNull(); expect(result.current.catalogItems.map((item) => item.id)).toEqual(["base"]);
  });
  it("accepts a successful explicitly empty current category list as authoritative", async () => {
    const profile = viewer(); routes.set("/api/recommendations/", () => reply({ scope: "movies", category: "Drama", categories: [] }));
    const { result } = renderHook(() => useCatalogController(profile, { profile: profile.id, view: "movies", genre: "Drama" }));
    await waitFor(() => expect(result.current.categoriesReady).toBe(true)); expect(result.current.categories).toEqual([]); expect(result.current.recommendationError).toBe("");
  });

  it("protects an acknowledged title from an older full-map reconciliation dispatched while its mutation was pending, including the remount cache", async () => {
    const profile = viewer(); const query: AppQueryState = { profile: profile.id, view: "home" };
    const normal = fetchMock.getMockImplementation()!; const firstMutation = deferred<Response>(); const secondCheck = deferred<Response>(); let reads = 0;
    fetchMock.mockImplementation(async (input: RequestInfo | URL, options?: RequestInit) => {
      const path = String(input);
      if (path === `/api/recommendations/${profile.id}/preferences`) return ++reads === 1 ? reply({ preferences: { m_101: "like", m_102: "like" } }) : secondCheck.promise;
      if (options?.method === "PUT" && path.endsWith("/preferences/m_101")) return firstMutation.promise;
      if (options?.method === "PUT" && path.endsWith("/preferences/m_102")) return reply({ detail: "Second title rejected" }, 400);
      return normal(input);
    });
    const { result, unmount } = renderHook(() => useCatalogController(profile, query));
    await waitFor(() => expect(result.current.preferences).toEqual({ m_101: "like", m_102: "like" }));
    let first!: Promise<void>; let second!: Promise<void>;
    act(() => { first = result.current.updatePreference("m_101", "love"); });
    act(() => { second = result.current.updatePreference("m_102", "love").catch(() => undefined); });
    await waitFor(() => expect(reads).toBe(2));
    expect(result.current.preferenceOperations?.m_101.status).toBe("saving");
    await act(async () => { firstMutation.resolve(reply({ movie_id: "m_101", preference: "love" })); await first; });
    expect(result.current.preferenceOperations?.m_101.status).toBe("saved");
    await act(async () => { secondCheck.resolve(reply({ preferences: { m_101: "like", m_102: "like", untouched: "love" } })); await second; });
    expect(result.current.preferences).toEqual({ m_101: "love", m_102: "like", untouched: "love" });
    expect(result.current.preferenceOperations?.m_102.status).toBe("error");
    unmount();
    fetchMock.mockImplementation((input: RequestInfo | URL) => String(input) === `/api/recommendations/${profile.id}/preferences` ? new Promise<Response>(() => undefined) : normal(input));
    const remounted = renderHook(() => useCatalogController(profile, query));
    expect(remounted.result.current.preferences).toEqual({ m_101: "love", m_102: "like", untouched: "love" });
    await act(async () => { await Promise.resolve(); }); remounted.unmount();
  });

});
