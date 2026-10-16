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
  it("retains painted base catalog DOM and focused record until explicit recommendation handoff", async () => {
    const base = catalogMovie("Base catalog title"); const remote = catalogMovie("Recommended title");
    const controller = catalogController({ catalogItems: [base], movies: [base], movieItems: [base], resolveMovie: (id) => id === base.id ? base : remote });
    const query: AppQueryState = { profile: catalogProfile.id, view: "movies" };
    const { rerender, container } = render(<App theme={theme} query={query} controller={controller} />);
    expect(screen.getByText("Loading recommendations")).toBeTruthy();
    const record = screen.getByRole("button", { name: /Base catalog title/ }); act(() => record.focus());
    const updated = { ...controller, movies: [base, remote], recommendation: catalogFeed([remote.id]), recommendationLoading: false };
    rerender(<App theme={theme} query={query} controller={updated} />);
    expect(screen.getByRole("button", { name: /Base catalog title/ })).toBe(record);
    expect(document.activeElement).toBe(record); expect(screen.queryByRole("heading", { name: remote.title })).toBeNull();
    expect(screen.getByText("Recommendations ready")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Show recommendations" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: remote.title })).toBeTruthy());
    expect(document.activeElement).toBe(container.querySelector("[data-catalog-region]"));
    expect(container.querySelector("[data-catalog-region]")?.getAttribute("aria-label")).toBe("Recommendations");
  });
  it("returns focused hero controls to the persistent shell before a parent fallback retires the hero", async () => {
    const controller = catalogController({ recommendation: catalogFeed(["Feature one", "Feature two"]), recommendationLoading: false, catalogItems: [catalogMovie("Base")] });
    const query: AppQueryState = { profile: catalogProfile.id, view: "movies" };
    const { rerender, container } = render(<App theme={theme} query={query} controller={controller} />);
    act(() => screen.getByRole("button", { name: "Pause automatic rotation" }).focus());
    rerender(<App theme={theme} query={{ ...query, genre: "Drama" }} controller={{ ...controller, recommendation: null, recommendationLoading: true }} />);
    expect(screen.queryByRole("button", { name: "Pause automatic rotation" })).toBeNull();
    expect(document.activeElement).toBe(container.querySelector("[data-catalog-focus-anchor]"));
    rerender(<App theme={theme} query={query} controller={controller} />);
    const action = screen.getByRole("button", { name: "View details" }); act(() => action.focus());
    rerender(<App theme={theme} query={query} controller={{ ...controller, recommendation: catalogFeed([]), recommendationLoading: false }} />);
    expect(document.activeElement).not.toBe(document.body);
    expect(document.activeElement).toBe(container.querySelector("[data-catalog-region]"));
  });
  it("renders feed-first discovery and never labels unfiltered fallback as the pending genre", () => {
    const query: AppQueryState = { profile: catalogProfile.id, view: "movies", genre: "Drama" };
    const first = render(<App theme={theme} query={query} controller={catalogController({ loading: true, catalogItems: [], recommendation: catalogFeed(["First ready"], "Drama"), recommendationLoading: false })} />);
    expect(screen.getByRole("heading", { name: "First ready" })).toBeTruthy(); first.unmount();
    render(<App theme={theme} query={query} controller={catalogController({ catalogItems: [catalogMovie("Base")], movies: [catalogMovie("Base")] })} />);
    expect(screen.getByText(/The catalog below is unfiltered/)).toBeTruthy();
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
