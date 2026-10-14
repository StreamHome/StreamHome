import React, { useState } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../../api/recommendations";
import { useProfileStore } from "../../../stores/profileStore";
import { RecommendationsPanel } from "./RecommendationsPanel";
vi.mock("../../../api/recommendations", () => ({ getRecommendationDiagnostics: vi.fn(), getRecommendationOnboarding: vi.fn(), saveRecommendationOnboarding: vi.fn(), rebuildRecommendations: vi.fn(), clearMediaPreferences: vi.fn() }));
const diagnostics = { profileId: "1", periodDays: 30, exposures: 0, detailsOpens: 0, playbackStarts: 0, completions: 0, playRate: 0, completionRate: 0, preferences: {like: 0, love: 0, dislike: 0}, candidatePool: 0, candidateSources: {}, catalog: {total: 0, available: 0, cached: 0}, topTastes: [] };
beforeEach(() => {
  vi.resetAllMocks();
  useProfileStore.setState({ activeProfile: {id: "1", name: "Owner", avatarColor: "blue", theme: "ember", pinEnabled: false}, profiles: [] });
  vi.mocked(api.getRecommendationDiagnostics).mockResolvedValue(diagnostics);
  vi.mocked(api.getRecommendationOnboarding).mockResolvedValue({genres: ["Action"], titleIds: []});
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}
function twoProfiles() {
  const owner = useProfileStore.getState().activeProfile!;
  useProfileStore.setState({ profiles: [owner, {...owner, id: "2", name: "Child"}] });
}

describe("starting genre editing", () => {
  it("opens profile diagnostics separately while saved-versus-draft rebuild guidance remains visible", async () => {
    render(<RecommendationsPanel />);
    fireEvent.change(await screen.findByLabelText("Starting genres"), { target: { value: "Drama" } });
    expect(screen.getByText("Uses saved genres. Your unsaved draft will not be saved.")).toBeTruthy();
    expect(screen.queryByText("TOP TASTE SIGNALS")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Recommendation signals and diagnostics" }));
    expect(screen.getByRole("dialog", { name: "Recommendation signals and diagnostics" })).toBeTruthy();
    expect(screen.getByText("No learned signals yet")).toBeTruthy();
    expect((screen.getByLabelText("Starting genres") as HTMLInputElement).value).toBe("Drama");
    expect(api.saveRecommendationOnboarding).not.toHaveBeenCalled();
    expect(api.rebuildRecommendations).not.toHaveBeenCalled();
  });
  it("does not clear a newer retained draft when an unmounted visit's save completes", async () => {
    const save = deferred<Awaited<ReturnType<typeof api.saveRecommendationOnboarding>>>();
    vi.mocked(api.saveRecommendationOnboarding).mockReturnValue(save.promise);
    function Visit() {
      const [shown, setShown] = useState(true);
      const [draft, setDraft] = useState<string>();
      return <><button onClick={() => setShown(!shown)}>Change section</button>{shown && <RecommendationsPanel draft={draft} onDraftChange={setDraft} />}</>;
    }
    render(<Visit />);
    fireEvent.change(await screen.findByLabelText("Starting genres"), {target: {value: "Drama"}});
    fireEvent.click(screen.getByRole("button", {name: "Save starting genres"}));
    await waitFor(() => expect(api.saveRecommendationOnboarding).toHaveBeenCalledWith("1", ["Drama"]));
    fireEvent.click(screen.getByRole("button", {name: "Change section"}));
    fireEvent.click(screen.getByRole("button", {name: "Change section"}));
    fireEvent.change(await screen.findByLabelText("Starting genres"), {target: {value: "Comedy"}});
    await act(async () => save.resolve({genres: ["Drama"], titleIds: []}));
    fireEvent.click(screen.getByRole("button", {name: "Change section"}));
    fireEvent.click(screen.getByRole("button", {name: "Change section"}));
    expect((await screen.findByLabelText("Starting genres") as HTMLInputElement).value).toBe("Comedy");
    expect(screen.getByText(/This profile’s draft is kept/)).toBeTruthy();
  });
  it("keeps separate profile drafts through section navigation and allows explicit removal/discard", async () => {
    twoProfiles();
    vi.mocked(api.getRecommendationOnboarding).mockImplementation(id => Promise.resolve({genres: [id === "2" ? "Horror" : "Action"], titleIds: []}));
    function Visit() {
      const [shown, setShown] = useState(true);
      const [subject, setSubject] = useState("1");
      const [drafts, setDrafts] = useState<Record<string, string | undefined>>({});
      return <><button onClick={() => setShown(!shown)}>Change section</button><button onClick={() => setSubject(subject === "1" ? "2" : "1")}>Change subject</button>{shown && <RecommendationsPanel profileId={subject} draft={drafts[subject]} onDraftChange={draft => setDrafts(previous => ({...previous, [subject]: draft}))} />}</>;
    }
    render(<Visit />);
    fireEvent.change(await screen.findByLabelText("Starting genres"), {target: {value: "Drama, Comedy"}});
    fireEvent.click(screen.getByRole("button", {name: "Change section"}));
    fireEvent.click(screen.getByRole("button", {name: "Change section"}));
    expect((await screen.findByLabelText("Starting genres") as HTMLInputElement).value).toBe("Drama, Comedy");
    fireEvent.click(screen.getByRole("button", {name: "Change subject"}));
    expect((await screen.findByLabelText("Starting genres") as HTMLInputElement).value).toBe("Horror");
    fireEvent.change(screen.getByLabelText("Starting genres"), {target: {value: "Thriller"}});
    fireEvent.click(screen.getByRole("button", {name: "Change subject"}));
    expect((await screen.findByLabelText("Starting genres") as HTMLInputElement).value).toBe("Drama, Comedy");
    fireEvent.click(screen.getByRole("button", {name: "Remove Drama"}));
    expect((screen.getByLabelText("Starting genres") as HTMLInputElement).value).toBe("Comedy");
    fireEvent.click(screen.getByRole("button", {name: "Discard genre changes"}));
    expect((screen.getByLabelText("Starting genres") as HTMLInputElement).value).toBe("Action");
    expect(api.saveRecommendationOnboarding).not.toHaveBeenCalled();
  });
  it("rejects excess genres and saves the entire corrected draft", async () => {
    render(<RecommendationsPanel />);
    const field = await screen.findByLabelText("Starting genres");
    const save = screen.getByRole("button", {name: "Save starting genres"}) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    const genres = Array.from({length: 13}, (_, i) => `Genre ${i + 1}`);
    fireEvent.change(field, {target: {value: genres.join(", ")}});
    expect(save.disabled).toBe(true);
    expect(screen.getByRole("alert").textContent).toContain("Remove 1 genre");
    expect(api.saveRecommendationOnboarding).not.toHaveBeenCalled();
    fireEvent.change(field, {target: {value: genres.slice(0,12).join(", ")}});
    fireEvent.click(save);
    await waitFor(() => expect(api.saveRecommendationOnboarding).toHaveBeenCalledWith("1", genres.slice(0,12)));
  });
  it("keeps unsaved genres when rebuilding from saved preferences", async () => {
    render(<RecommendationsPanel />);
    const field = await screen.findByLabelText("Starting genres");
    fireEvent.change(field, {target: {value: "Drama, Comedy"}});
    expect(screen.getByText("Uses saved genres. Your unsaved draft will not be saved.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", {name: "Rebuild feed"}));
    await screen.findByText("Recommendation pool rebuilt.");
    expect((field as HTMLInputElement).value).toBe("Drama, Comedy");
    expect(api.saveRecommendationOnboarding).not.toHaveBeenCalled();
    expect(api.rebuildRecommendations).toHaveBeenCalledWith("1");
    expect(screen.getByText(/No local or cached titles/)).toBeTruthy();
  });
  it("clears the old subject draft while the new subject is loading", async () => {
    twoProfiles();
    const child = deferred<Awaited<ReturnType<typeof api.getRecommendationOnboarding>>>();
    vi.mocked(api.getRecommendationOnboarding).mockImplementation(id => id === "2" ? child.promise : Promise.resolve({genres: ["Action"], titleIds: []}));
    const view = render(<RecommendationsPanel profileId="1" />);
    fireEvent.change(await screen.findByLabelText("Starting genres"), {target: {value: "Drama, Comedy"}});
    fireEvent.change(screen.getByLabelText("Type Owner to confirm"), {target: {value: "Owner"}});
    view.rerender(<RecommendationsPanel profileId="2" />);
    expect(screen.getByText(/Manage Child’s starting genres/)).toBeTruthy();
    expect(screen.queryByLabelText("Starting genres")).toBeNull();
    expect(screen.queryByRole("button", {name: "Save starting genres"})).toBeNull();
    expect(screen.queryByRole("button", {name: "Clear explicit feedback"})).toBeNull();
    await act(async () => child.resolve({genres: ["Horror"], titleIds: []}));
    const field = await screen.findByLabelText("Starting genres") as HTMLInputElement;
    expect(field.value).toBe("Horror");
    expect((screen.getByLabelText("Type Child to confirm") as HTMLInputElement).value).toBe("");
    expect((screen.getByRole("button", {name: "Save starting genres"}) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(field, {target: {value: "Thriller"}});
    fireEvent.click(screen.getByRole("button", {name: "Save starting genres"}));
    await waitFor(() => expect(api.saveRecommendationOnboarding).toHaveBeenCalledWith("2", ["Thriller"]));
    expect(api.saveRecommendationOnboarding).toHaveBeenCalledTimes(1);
  });
  it("shows a failed new-subject read without exposing the previous form", async () => {
    twoProfiles();
    const child = deferred<Awaited<ReturnType<typeof api.getRecommendationOnboarding>>>();
    vi.mocked(api.getRecommendationOnboarding).mockImplementation(id => id === "2" ? child.promise : Promise.resolve({genres: ["Action"], titleIds: []}));
    const view = render(<RecommendationsPanel profileId="1" />);
    fireEvent.change(await screen.findByLabelText("Starting genres"), {target: {value: "Drama"}});
    view.rerender(<RecommendationsPanel profileId="2" />);
    await act(async () => child.reject(new Error("Child data unavailable")));
    await screen.findByText("Recommendation data could not be loaded.");
    expect(screen.queryByLabelText("Starting genres")).toBeNull();
    expect(screen.queryByRole("button", {name: "Rebuild feed"})).toBeNull();
    expect(api.saveRecommendationOnboarding).not.toHaveBeenCalled();
  });
  it("ignores out-of-order reads through a rapid A to B to A switch", async () => {
    twoProfiles();
    const oldOwner = deferred<Awaited<ReturnType<typeof api.getRecommendationOnboarding>>>();
    vi.mocked(api.getRecommendationOnboarding)
      .mockImplementationOnce(() => oldOwner.promise)
      .mockResolvedValueOnce({genres: ["Horror"], titleIds: []})
      .mockResolvedValue({genres: ["Comedy"], titleIds: []});
    const view = render(<RecommendationsPanel profileId="1" />);
    view.rerender(<RecommendationsPanel profileId="2" />);
    expect((await screen.findByLabelText("Starting genres") as HTMLInputElement).value).toBe("Horror");
    view.rerender(<RecommendationsPanel profileId="1" />);
    await waitFor(() => expect((screen.getByLabelText("Starting genres") as HTMLInputElement).value).toBe("Comedy"));
    await act(async () => oldOwner.resolve({genres: ["Old owner draft"], titleIds: []}));
    expect((screen.getByLabelText("Starting genres") as HTMLInputElement).value).toBe("Comedy");
    expect(screen.getByText(/Manage Owner’s starting genres/)).toBeTruthy();
  });
  it("keeps a previous subject action completion out of the new form", async () => {
    twoProfiles();
    const saved = deferred<unknown>();
    vi.mocked(api.saveRecommendationOnboarding).mockImplementation(() => saved.promise);
    vi.mocked(api.getRecommendationOnboarding).mockImplementation(id => Promise.resolve({genres: [id === "2" ? "Horror" : "Action"], titleIds: []}));
    const view = render(<RecommendationsPanel profileId="1" />);
    fireEvent.change(await screen.findByLabelText("Starting genres"), {target: {value: "Drama"}});
    fireEvent.click(screen.getByRole("button", {name: "Save starting genres"}));
    await waitFor(() => expect(api.saveRecommendationOnboarding).toHaveBeenCalledWith("1", ["Drama"]));
    view.rerender(<RecommendationsPanel profileId="2" />);
    await waitFor(() => expect((screen.getByLabelText("Starting genres") as HTMLInputElement).value).toBe("Horror"));
    await act(async () => saved.resolve({}));
    expect((screen.getByLabelText("Starting genres") as HTMLInputElement).value).toBe("Horror");
    expect(screen.queryByText("Starting preferences saved.")).toBeNull();
    expect(api.getRecommendationOnboarding).toHaveBeenCalledTimes(2);
  });
});
