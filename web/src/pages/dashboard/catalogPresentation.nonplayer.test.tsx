import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MotionProvider } from "../../motion/motionSystem";
import type { AppQueryState } from "../../navigation/queryState";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import { getThemeDefinition } from "../../themes/application/themeRegistry";
import type { ThemeId } from "../../types/theme";
import { LegacyThemeAdapter } from "./ThemeDashboard";
import { EmberDashboard } from "./ember/EmberDashboard";
import { catalogController, catalogFeed, catalogMovie, catalogProfile } from "./catalogTestFixtures.nonplayer";
const themes = ["ember", "aurora", "cinema"] as const;
beforeEach(() => {
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) });
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
  Object.defineProperty(window, "scrollTo", { configurable: true, value: vi.fn() });
  useProfileStore.setState({ activeProfile: catalogProfile });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
function App({ theme, query, controller }: { theme: ThemeId; query: AppQueryState; controller: ReturnType<typeof catalogController> }) {
  useThemeStore.setState({ activeTheme: theme });
  const Component = theme === "ember" ? EmberDashboard : LegacyThemeAdapter;
  return <MemoryRouter><MotionProvider><Component query={query} controller={controller} presentation={getThemeDefinition(theme)} /></MotionProvider></MemoryRouter>;
}
describe.each(themes)("%s truthful catalog presentation", (theme) => {
  it("opens with a real hero and rails and automatically adopts late recommendations", async () => {
    const base = catalogMovie("Base catalog title");
    const controller = catalogController({ catalogItems: [base], movies: [base], movieItems: [base] });
    const query: AppQueryState = { profile: catalogProfile.id, view: "movies" };
    const { rerender } = render(<App theme={theme} query={query} controller={controller} />);
    expect(screen.getByRole("heading", { name: base.title })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Featured Movies" })).toBeTruthy();
    expect(screen.queryByText("Loading recommendations")).toBeNull();
    expect(screen.queryByText("Your server library")).toBeNull();
    rerender(<App theme={theme} query={query} controller={{ ...controller, recommendation: catalogFeed(["Recommended title"]), recommendationLoading: false }} />);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Recommended title" })).toBeTruthy());
    expect(screen.queryByRole("button", { name: "Show recommendations" })).toBeNull();
    expect(screen.queryByText("Recommendations ready")).toBeNull();
  });
  it("keeps an active hero action stable until focus leaves, then updates without a handoff button", async () => {
    const controller = catalogController({ catalogItems: [catalogMovie("Base")] });
    const query: AppQueryState = { profile: catalogProfile.id, view: "movies" };
    const { rerender, container } = render(<App theme={theme} query={query} controller={controller} />);
    const action = screen.getByRole("button", { name: "View details" }); act(() => action.focus());
    rerender(<App theme={theme} query={query} controller={{ ...controller, recommendation: catalogFeed(["New feature"]), recommendationLoading: false }} />);
    expect(screen.getByRole("heading", { name: "Base" })).toBeTruthy();
    expect(document.activeElement).toBe(action);
    act(() => container.querySelector<HTMLElement>("[data-catalog-focus-anchor]")!.focus());
    await waitFor(() => expect(screen.getByRole("heading", { name: "New feature" })).toBeTruthy());
    expect(document.activeElement).not.toBe(document.body);
  });
  it("renders feed-first content and filters pending genres using real catalog metadata", () => {
    const query: AppQueryState = { profile: catalogProfile.id, view: "movies", genre: "Drama" };
    const first = render(<App theme={theme} query={query} controller={catalogController({ loading: true, catalogItems: [], recommendation: catalogFeed(["First ready"], "Drama"), recommendationLoading: false })} />);
    expect(screen.getByRole("heading", { name: "First ready" })).toBeTruthy(); first.unmount();
    const other = { ...catalogMovie("Other genre"), genres: ["Comedy"] };
    render(<App theme={theme} query={query} controller={catalogController({ catalogItems: [catalogMovie("Drama title"), other] })} />);
    expect(screen.getByRole("heading", { name: "Drama title" })).toBeTruthy();
    expect(screen.queryByText("Other genre")).toBeNull();
    expect(screen.queryByText(/The catalog below is unfiltered/)).toBeNull();
  });
  it("keeps the page usable on suggestion failure and separates loading, empty and failure", () => {
    const query: AppQueryState = { profile: catalogProfile.id, view: "home" };
    const retry = vi.fn();
    const { rerender } = render(<App theme={theme} query={query} controller={catalogController({ loading: true })} />);
    expect(screen.getByRole("status", { name: "Loading titles" })).toBeTruthy();
    expect(screen.queryByText("Your library is empty")).toBeNull();
    rerender(<App theme={theme} query={query} controller={catalogController({ catalogItems: [catalogMovie("Still usable")], recommendationLoading: false, recommendationError: "Failed", retryRecommendations: retry })} />);
    expect(screen.getByRole("heading", { name: "Still usable" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry suggestions" })); expect(retry).toHaveBeenCalledTimes(1);
    rerender(<App theme={theme} query={query} controller={catalogController({ recommendationLoading: false })} />);
    expect(screen.getByRole("heading", { name: "Your library is empty" })).toBeTruthy();
    rerender(<App theme={theme} query={query} controller={catalogController({ recommendationLoading: false, catalogError: "Failed", recommendationError: "Failed" })} />);
    expect(screen.getByRole("heading", { name: "Titles unavailable" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry loading titles" })).toBeTruthy();
  });
  it.each(["home", "movies", "series"] as const)("opens %s with matching real hero content while suggestions are pending", (view) => {
    const movie = catalogMovie("Film"); const series = { ...catalogMovie("Show"), type: "series" as const };
    render(<App theme={theme} query={{ profile: catalogProfile.id, view }} controller={catalogController({ catalogItems: [movie, series] })} />);
    expect(screen.getByRole("heading", { name: view === "series" ? "Show" : "Film" })).toBeTruthy();
    expect(screen.queryByText("Your server library")).toBeNull();
    expect(screen.queryByText("Loading recommendations")).toBeNull();
    expect(document.querySelector("[data-category-mode=catalog-fallback]")).toBeNull();
  });
  it("retains category escape controls in a successful empty genre", () => {
    const query: AppQueryState = { profile: catalogProfile.id, view: "movies", genre: "Drama" };
    render(<App theme={theme} query={query} controller={catalogController({ recommendation: catalogFeed([], "Drama"), recommendationLoading: false, loading: true, catalogError: "Unrelated catalog failure" })} />);
    expect(screen.getByRole("heading", { name: "No Drama titles" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "recommended" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "all" })).toBeTruthy();
  });
  it("keeps retry feedback reachable when an empty explicit feed fails to refresh", () => {
    const query: AppQueryState = { profile: catalogProfile.id, view: "movies", genre: "Drama" };
    const retryRecommendations = vi.fn();
    render(<App theme={theme} query={query} controller={catalogController({ recommendation: catalogFeed([], "Drama"), recommendationLoading: false, recommendationError: "Refresh failed", retryRecommendations })} />);
    expect(screen.getByRole("heading", { name: "No Drama titles" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry suggestions" }));
    expect(retryRecommendations).toHaveBeenCalledTimes(1);
  });
  it("offers library refresh recovery and preserves keyboard focus when retry feedback retires", async () => {
    const query: AppQueryState = { profile: catalogProfile.id, view: "movies" };
    const retryCatalog = vi.fn(); const retryRecommendations = vi.fn();
    const controller = catalogController({ catalogItems: [catalogMovie("Retained")], catalogError: "Offline", recommendationError: "Offline", recommendationLoading: false, retryCatalog, retryRecommendations });
    const { rerender, container } = render(<App theme={theme} query={query} controller={controller} />);
    const retry = screen.getByRole("button", { name: "Retry library" }); act(() => retry.focus()); fireEvent.click(retry);
    expect(retryCatalog).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(container.querySelector("[data-catalog-region]"));
    const suggestions = screen.getByRole("button", { name: "Retry suggestions" }); act(() => suggestions.focus()); fireEvent.click(suggestions);
    expect(retryRecommendations).toHaveBeenCalledTimes(1);
    rerender(<App theme={theme} query={query} controller={{ ...controller, catalogError: "", recommendationError: "", recommendationLoading: true }} />);
    expect(document.activeElement).toBe(container.querySelector("[data-catalog-region]"));
    expect(screen.getByRole("heading", { name: "Retained" })).toBeTruthy();
  });
  it("does not replace a pointer target between pointerdown and its click", async () => {
    const query: AppQueryState = { profile: catalogProfile.id, view: "movies" };
    const controller = catalogController({ catalogItems: [catalogMovie("Pressed title")] });
    const { rerender, container } = render(<App theme={theme} query={query} controller={catalogController({ loading: true })} />);
    rerender(<App theme={theme} query={query} controller={controller} />);
    const action = screen.getByRole("button", { name: "View details" });
    fireEvent.pointerDown(action);
    rerender(<App theme={theme} query={query} controller={{ ...controller, recommendation: catalogFeed(["New target"]), recommendationLoading: false }} />);
    expect(screen.getByRole("heading", { name: "Pressed title" })).toBeTruthy();
    fireEvent.pointerUp(document); act(() => container.querySelector<HTMLElement>("[data-catalog-region]")!.focus());
    await waitFor(() => expect(screen.getByRole("heading", { name: "New target" })).toBeTruthy());
  });
  it("records exposure against the painted generation while a focused hero delays refresh", async () => {
    vi.useFakeTimers();
    class VisibleObserver {
      constructor(private callback: IntersectionObserverCallback) {}
      observe(target: Element) { this.callback([{ isIntersecting: true, intersectionRatio: 1, target } as IntersectionObserverEntry], this as unknown as IntersectionObserver); }
      disconnect() {} unobserve() {} takeRecords() { return []; }
    }
    vi.stubGlobal("IntersectionObserver", VisibleObserver);
    const profileId = `painted-exposure-${theme}`;
    useProfileStore.setState({ activeProfile: { ...catalogProfile, id: profileId } });
    const controller = catalogController({ recommendation: { ...catalogFeed(["Old visible"]), generatedAt: 1 }, recommendationLoading: false });
    const query: AppQueryState = { profile: profileId, view: "movies" };
    try {
      const { rerender, container } = render(<App theme={theme} query={query} controller={controller} />);
      act(() => screen.getByRole("button", { name: "View details" }).focus());
      rerender(<App theme={theme} query={query} controller={{ ...controller, recommendation: { ...catalogFeed(["New visible"]), generatedAt: 2 } }} />);
      await act(async () => { await vi.advanceTimersByTimeAsync(1100); });
      const queued = JSON.parse(window.localStorage.getItem(`streamhome_pending_exposures:${profileId}`) || "[]");
      expect(queued.some((entry: { movie_id: string; feed_generation: string }) => entry.movie_id === "Old visible" && entry.feed_generation === "1")).toBe(true);
      expect(queued.some((entry: { feed_generation: string }) => entry.feed_generation === "2")).toBe(false);
      act(() => container.querySelector<HTMLElement>("[data-catalog-region]")!.focus());
      await act(async () => { await vi.advanceTimersByTimeAsync(1200); });
      expect(screen.getByRole("heading", { name: "New visible" })).toBeTruthy();
      cleanup();
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    } finally { vi.useRealTimers(); }
  });
  it("shows local saved-list pending, error, successful empty and unavailable record details independently", async () => {
    const retryWatchlist = vi.fn(); const query: AppQueryState = { profile: catalogProfile.id, view: "watchlist" };
    const controller = catalogController({ retryWatchlist });
    const { rerender } = render(<App theme={theme} query={query} controller={controller} />);
    expect(screen.getByRole("heading", { name: "Loading your saved list" })).toBeTruthy(); expect(screen.queryByText("Your list is empty")).toBeNull();
    rerender(<App theme={theme} query={query} controller={{ ...controller, watchlistStatus: "error", watchlistError: "Saved read failed" }} />);
    expect(screen.getByRole("heading", { name: "Saved list unavailable" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry saved list" })); expect(retryWatchlist).toHaveBeenCalledTimes(1);
    rerender(<App theme={theme} query={query} controller={{ ...controller, watchlistKnown: true, watchlistStatus: "success", watchlist: ["missing"] }} />);
    expect(screen.queryByText("Your list is empty")).toBeNull(); expect(screen.getByText(/Details are unavailable for 1 of 1 saved titles/)).toBeTruthy();
    rerender(<App theme={theme} query={query} controller={{ ...controller, watchlistKnown: true, watchlistStatus: "success" }} />);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Your list is empty" })).toBeTruthy());
  });
  it("uses local query error and retry without a false no-results or zero count", async () => {
    const retrySearch = vi.fn(); const query: AppQueryState = { profile: catalogProfile.id, view: "search", q: "retained query" };
    const controller = catalogController({ searchStatus: "error", searchError: "Search read failed", retrySearch });
    const { rerender } = render(<App theme={theme} query={query} controller={controller} />);
    expect(screen.getByRole("heading", { name: "Search unavailable" })).toBeTruthy(); expect(screen.queryByText("No results")).toBeNull();
    screen.getAllByRole("textbox", { name: "Search server catalog" }).forEach((input) => expect(input.getAttribute("value")).toBe("retained query"));
    fireEvent.click(screen.getByRole("button", { name: "Retry this search" })); expect(retrySearch).toHaveBeenCalledTimes(1);
    rerender(<App theme={theme} query={query} controller={{ ...controller, searchStatus: "success", searchError: "" }} />);
    await waitFor(() => expect(screen.getByRole("heading", { name: "No results" })).toBeTruthy());
  });
});

it("preserves Gemini's existing catalog and manual recommendation handoff", async () => {
  const query: AppQueryState = { profile: catalogProfile.id, view: "movies" };
  const controller = catalogController({ catalogItems: [catalogMovie("Gemini base")] });
  const { rerender } = render(<App theme="gemini" query={query} controller={controller} />);
  expect(screen.getByRole("heading", { name: "Your server library" })).toBeTruthy();
  rerender(<App theme="gemini" query={query} controller={{ ...controller, recommendation: catalogFeed(["Gemini recommended"]), recommendationLoading: false }} />);
  expect(screen.getByRole("button", { name: "Show recommendations" })).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "Gemini recommended" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Show recommendations" }));
  await waitFor(() => expect(screen.getByRole("heading", { name: "Gemini recommended" })).toBeTruthy());
});
