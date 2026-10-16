import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MotionProvider } from "../../motion/motionSystem";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import type { Episode, Movie } from "../../types/api";
import { catalogMovie, catalogProfile, deferred } from "../dashboard/catalogTestFixtures.nonplayer";
import { useCatalogController } from "../dashboard/useCatalogController";
import { RecommendationFeedbackProvider } from "../dashboard/RecommendationFeedback";
import { DetailsRouter } from "./DetailsRouter";
const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const series = (id = "tv_42"): Movie => ({ ...catalogMovie(id), type: "series", availability: "available", episodes: null });
const episode = (season: number): Episode => ({ id: `synthetic-${season}`, episodeNumber: 1, seasonNumber: season, title: `Episode in season ${season}`, description: "Synthetic unavailable media", thumbnailUrl: "", videoUrl: "", duration: "", quality: "", languages: [], subtitles: [], skipMarkers: {} });
let read: () => Promise<Response> | Response;
let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) });
  useProfileStore.setState({ activeProfile: catalogProfile }); useThemeStore.setState({ activeTheme: "ember" });
  read = () => reply([]);
  fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith("/api/series/")) return Promise.resolve(read());
    if (String(input) === "/api/watchlist/toggle") return Promise.resolve(reply({ detail: "Watchlist save failed" }, 503));
    throw new Error(`Unexpected synthetic request: ${String(input)}`);
  }); vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });
function Location() { return <output aria-label="Current query">{useLocation().search}</output>; }
function Detail({ movie = series(), onWatchlistChange = vi.fn() }: { movie?: Movie; onWatchlistChange?: (ids: string[]) => void }) { return <MemoryRouter initialEntries={[`/app?profile=${catalogProfile.id}&view=details&media=${movie.id}&season=2`]}><MotionProvider><DetailsRouter movie={movie} sessions={[]} onClose={vi.fn()} isWatchlisted={false} onWatchlistChange={onWatchlistChange} /><Location /></MotionProvider></MemoryRouter>; }

function PreferenceDetails({ movie }: { movie: Movie }) {
  const controller = useCatalogController(catalogProfile, { profile: catalogProfile.id, view: "details", media: movie.id });
  const context = { profileId: catalogProfile.id, preferences: controller.preferences, preferencesKnown: controller.preferencesKnown, preferenceKnownIds: controller.preferenceKnownIds, preferenceOperations: controller.preferenceOperations, onCheck: controller.checkPreference, onChange: controller.updatePreference, feedGeneration: "", scope: "details", category: "recommended" };
  return <RecommendationFeedbackProvider value={context}><DetailsRouter movie={movie} sessions={[]} onClose={vi.fn()} isWatchlisted={false} onWatchlistChange={vi.fn()} /></RecommendationFeedbackProvider>;
}
describe("included Details episode reads", () => {
  it("keeps pending and failed reads distinct from successful empty with a local retry and retained season", async () => {
    const pending = deferred<Response>(); read = () => pending.promise;
    render(<Detail />); expect(screen.getByText("Loading episodes from the server...")).toBeTruthy();
    expect(screen.getByLabelText("Current query").textContent).toContain("season=2");
    await act(async () => pending.resolve(reply({ detail: "Episode read failed" }, 503)));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Episodes unavailable" })).toBeTruthy());
    expect(screen.queryByText("No episodes are available from the server.")).toBeNull();
    expect(screen.getByLabelText("Current query").textContent).toContain("season=2");
    read = () => reply([episode(1), episode(2)]); fireEvent.click(screen.getByRole("button", { name: "Retry episodes" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Season 2" }).getAttribute("aria-pressed")).toBe("true"));
    expect(screen.getByRole("group", { name: "Select season" })).toBeTruthy();
    expect(screen.getByText("Episode in season 2")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Episode in season 2/ }).hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Season 1" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Season 1" }).getAttribute("aria-pressed")).toBe("true"));
  });
  it("keeps episode errors independent of failed watchlist mutations", async () => {
    read = () => reply({ detail: "Episode read failed" }, 503); render(<Detail />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry episodes" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Add to watchlist" }));
    await waitFor(() => expect(screen.getByText("Watchlist save failed")).toBeTruthy());
    expect(screen.getByText("Episode read failed")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry episodes" })).toBeTruthy();
  });
  it("renders successful empty honestly and reuses embedded empty episodes without fetching", async () => {
    render(<Detail movie={{ ...series(), episodes: [] }} />);
    await waitFor(() => expect(screen.getByText("No episodes are available from the server.")).toBeTruthy());
    expect(fetchMock).not.toHaveBeenCalled();
  });
  it("retains known episodes and the selected season when a refresh fails", async () => {
    const initial = { ...series(), episodes: [episode(1), episode(2)] };
    const { rerender } = render(<Detail movie={initial} />);
    expect(screen.getByText("Episode in season 2")).toBeTruthy();
    read = () => reply({ detail: "Refresh unavailable" }, 503);
    rerender(<Detail movie={series()} />);
    await waitFor(() => expect(screen.getByText(/Showing previously loaded episodes/)).toBeTruthy());
    expect(screen.getByText("Episode in season 2")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Season 2" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByLabelText("Current query").textContent).toContain("season=2");
  });
  it("ignores an older series response after switching identity", async () => {
    const old = deferred<Response>(); read = () => old.promise;
    const { rerender } = render(<Detail />); read = () => reply([episode(2)]);
    rerender(<Detail movie={series("tv_77")} />);
    await waitFor(() => expect(screen.getByText("Episode in season 2")).toBeTruthy());
    await act(async () => old.resolve(reply([episode(1)])));
    expect(screen.getByText("Episode in season 2")).toBeTruthy(); expect(screen.queryByText("Episode in season 1")).toBeNull();
  });
  it("does not retry an invalid series identity or canonicalize season during an error", async () => {
    render(<Detail movie={series("invalid-series")} />);
    await waitFor(() => expect(screen.getByText("This series does not have a valid server identifier.")).toBeTruthy());
    expect(screen.queryByRole("button", { name: "Retry episodes" })).toBeNull(); expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Current query").textContent).toContain("season=2");
  });
  it("reconciles a malformed toggle ACK through the existing saved-list GET without sending a second mutation", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/watchlist/toggle") return reply({});
      if (String(input) === `/api/watchlist/${catalogProfile.id}`) return reply(["tv_42"]);
      throw new Error(`Unexpected synthetic request: ${String(input)}`);
    });
    const onWatchlistChange = vi.fn(); render(<Detail movie={{ ...series(), episodes: [] }} onWatchlistChange={onWatchlistChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Add to watchlist" }));
    await waitFor(() => expect(onWatchlistChange).toHaveBeenCalledWith(["tv_42"]));
    expect(onWatchlistChange).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls.filter(([url]) => String(url) === "/api/watchlist/toggle").length).toBe(1);
    expect(fetchMock.mock.calls.filter(([url]) => String(url) === `/api/watchlist/${catalogProfile.id}`).length).toBe(1);
    expect(screen.queryByRole("button", { name: "Check watchlist" })).toBeNull();
    await waitFor(() => expect(screen.getByText("The saved list was checked. Its current saved state is shown.")).toBeTruthy());
  });
  it("keeps an acknowledged preference removal authoritative over stale embedded Like in Details without a feed", async () => {
    const movie = { ...catalogMovie("m_5"), viewerPreference: "like" as const }; const oldRead = deferred<Response>();
    fetchMock.mockImplementation(async (input: RequestInfo | URL, options?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/preferences/m_5") && options?.method === "PUT") return reply({ movie_id: "m_5", preference: null });
      if (path.endsWith("/preferences")) return oldRead.promise;
      if (path.startsWith("/api/movies?")) return reply([movie]);
      if (path === "/api/movies/m_5") return reply(movie);
      if (path.startsWith("/api/watchlist/") || path.startsWith("/api/track/")) return reply([]);
      throw new Error(`Unexpected synthetic request: ${path}`);
    });
    render(<MemoryRouter><MotionProvider><PreferenceDetails movie={movie} /></MotionProvider></MemoryRouter>);
    expect(screen.getByRole("button", { name: "Remove like for this title" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Remove like for this title" }));
    await waitFor(() => expect(screen.getByText("Preference saved.")).toBeTruthy());
    expect(screen.getByRole("button", { name: "Set like for this title" }).getAttribute("aria-pressed")).toBe("false");
    await act(async () => oldRead.resolve(reply({ preferences: { m_5: "like" } })));
    expect(screen.getByRole("button", { name: "Set like for this title" }).getAttribute("aria-pressed")).toBe("false");
    expect(fetchMock.mock.calls.some(([path]) => String(path).includes("?scope="))).toBe(false);
  });
  it("reconciles a committed toggle whose response body times out, locking repeat mutations until a read succeeds", async () => {
    vi.useFakeTimers();
    let saved: string[] = []; let readAvailable = false;
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).startsWith("/api/series/")) return reply([]);
      if (String(input) === "/api/watchlist/toggle") {
        saved = ["tv_42"]; // Server commit happened before body delivery stopped.
        const response = reply({ watchlist: saved });
        Object.defineProperty(response, "text", { value: () => new Promise((_, reject) => init?.signal?.addEventListener("abort", () => reject(new DOMException("Body cancelled", "AbortError")), { once: true })) });
        return response;
      }
      if (String(input).startsWith("/api/watchlist/")) return readAvailable ? reply(saved) : reply({ detail: "Read unavailable" }, 503);
      throw new Error(`Unexpected synthetic request: ${String(input)}`);
    });
    const onWatchlistChange = vi.fn(); render(<Detail onWatchlistChange={onWatchlistChange} />);
    await act(async () => { await Promise.resolve(); });
    fireEvent.click(screen.getByRole("button", { name: "Add to watchlist" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(45_001); });
    expect(screen.getByRole("button", { name: "Add to watchlist" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "Check watchlist" })).toBeTruthy();
    expect(onWatchlistChange).not.toHaveBeenCalled();
    readAvailable = true;
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Check watchlist" })));
    expect(onWatchlistChange).toHaveBeenCalledWith(["tv_42"]);
    expect(fetchMock.mock.calls.filter(([url]) => String(url) === "/api/watchlist/toggle").length).toBe(1);
    expect(screen.queryByRole("button", { name: "Check watchlist" })).toBeNull();
  });



});
