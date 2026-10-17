import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useProfileStore } from "../../stores/profileStore";
import { AdminCenter } from "./AdminCenter";

const animation = vi.hoisted(() => ({ defer: false, complete: undefined as ((definition: string) => void) | undefined }));
vi.mock("framer-motion", async (importOriginal) => {
  const original = await importOriginal<typeof import("framer-motion")>();
  const { forwardRef, createElement } = await import("react");
  const Div = forwardRef<HTMLDivElement, React.ComponentProps<typeof original.motion.div>>((props, ref) => {
    if (animation.defer && props.className === "admin-content__transition") {
      animation.complete = props.onAnimationComplete as (definition: string) => void;
      const children = original.isMotionValue(props.children) ? props.children.get() : props.children;
      return createElement("div", { className: props.className, ref }, children);
    }
    return createElement(original.motion.div, { ...props, ref });
  });
  return { ...original, motion: new Proxy(original.motion, { get: (target, property) => property === "div" ? Div : Reflect.get(target, property) }) };
});

vi.mock("./panels/AccountPanel", () => ({ AccountPanel: () => <h2>Account panel</h2> }));
vi.mock("./panels/ApiKeysPanel", () => ({ ApiKeysPanel: () => <h2>API keys panel</h2> }));
vi.mock("./panels/DownloadsPanel", () => ({ DownloadsPanel: () => <h2>Downloads panel</h2> }));
vi.mock("./panels/RecommendationsPanel", () => ({ RecommendationsPanel: () => <h2>Recommendations panel</h2> }));
vi.mock("./panels/StoragePanel", () => ({ StoragePanel: ({ view, onViewChange, retainedDraft, onDraftChange }: { view: string; onViewChange: (view: string) => void; retainedDraft?: { localBudget: string }; onDraftChange: (draft: unknown) => void }) => <><h2>Storage panel</h2><p>Storage view: {view}</p><p>Draft budget: {retainedDraft?.localBudget ?? "clean"}</p><button onClick={() => onViewChange("policy")}>Open storage policy</button><button onClick={() => onViewChange("activity")}>Open storage activity</button><button onClick={() => onDraftChange({ localBudget: "90", localUnit: "GiB", cloudBudget: "1", cloudUnit: "TiB", offloadEnabled: false, prefetchNextEpisode: false, hevcCompressionMode: "auto", revision: 3 })}>Edit storage draft</button></> }));
vi.mock("./panels/UpdatesPanel", () => ({ UpdatesPanel: () => <h2>Updates panel</h2> }));
vi.mock("./panels/ProfileDataPanel", () => ({ ProfileDataPanel: () => <h2>Profile data panel</h2> }));

function HistoryControls() {
  const navigate = useNavigate();
  return <><button onClick={() => navigate(-1)}>History back</button><button onClick={() => navigate(1)}>History forward</button></>;
}

describe("admin section navigation", () => {
  const scrollTo = vi.fn();
  beforeEach(() => {
    animation.defer = false;
    animation.complete = undefined;
    scrollTo.mockReset();
    Object.defineProperty(window, "scrollTo", { configurable: true, value: scrollTo });
    window.history.scrollRestoration = "auto";
    window.history.replaceState(null, "", "/");
    const owner = { id: "1", name: "Owner", avatarColor: "blue", theme: "ember" as const, pinEnabled: false };
    useProfileStore.setState({ activeProfile: owner, profiles: [owner, { ...owner, id: "2", name: "Child" }] });
  });
  afterEach(() => {
    animation.defer = false;
    animation.complete = undefined;
    window.history.replaceState(null, "", "/");
  });

  it("uses the current native account anchor when the shown callback arrives after a hash change", () => {
    animation.defer = true;
    const scrollIntoView = vi.fn();
    const view = render(<MemoryRouter initialEntries={["/?profile=1&view=admin&section=account"]}><div id="root"><AdminCenter /><section id="account-credentials" /></div></MemoryRouter>);
    const root = document.getElementById("root")!;
    root.scrollTop = 631;
    Object.defineProperty(document.getElementById("account-credentials")!, "scrollIntoView", { value: scrollIntoView });
    scrollTo.mockClear();
    // A native fragment change can precede the router's next render. The saved
    // animation callback still belongs to the render without a fragment.
    window.history.replaceState(null, "", "/?profile=1&view=admin&section=account#account-credentials");
    const href = window.location.href;
    const historyLength = window.history.length;
    act(() => animation.complete!("shown"));
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "start", behavior: "auto" });
    expect(scrollTo).not.toHaveBeenCalled();
    expect(root.scrollTop).toBe(631);
    expect(window.location.href).toBe(href);
    expect(window.history.length).toBe(historyLength);
    view.unmount();
  });

  it("keeps the top reset when a previous account fragment was cleared before the shown callback", () => {
    animation.defer = true;
    const scrollIntoView = vi.fn();
    const view = render(<MemoryRouter initialEntries={["/?profile=1&view=admin&section=account#account-credentials"]}><div id="root"><AdminCenter /><section id="account-credentials" /></div></MemoryRouter>);
    const root = document.getElementById("root")!;
    root.scrollTop = 631;
    Object.defineProperty(document.getElementById("account-credentials")!, "scrollIntoView", { value: scrollIntoView });
    scrollTo.mockClear();
    act(() => animation.complete!("shown"));
    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(scrollTo).toHaveBeenCalledWith({ top: 0, left: 0, behavior: "auto" });
    expect(root.scrollTop).toBe(0);
    view.unmount();
  });

  it("restores storage subpages and visit drafts through browser history", async () => {
    render(<MemoryRouter initialEntries={["/?profile=1&view=admin&section=storage&storageView=policy"]}><AdminCenter /><HistoryControls /></MemoryRouter>);
    expect(await screen.findByText("Storage view: policy")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Edit storage draft" }));
    fireEvent.click(screen.getByRole("button", { name: "Open storage activity" }));
    expect(await screen.findByText("Storage view: activity")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Downloads" }));
    await screen.findByRole("heading", { name: "Downloads panel" });
    fireEvent.click(screen.getByRole("button", { name: "History back" }));
    expect(await screen.findByText("Storage view: activity")).toBeTruthy();
    expect(screen.getByText("Draft budget: 90")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "History back" }));
    expect(await screen.findByText("Storage view: policy")).toBeTruthy();
    expect(screen.getByText("Draft budget: 90")).toBeTruthy();
  });

  it("starts each changed section at the top through buttons, the selector and browser history", async () => {
    const view = render(<MemoryRouter initialEntries={["/?profile=1&view=admin&section=updates"]}><AdminCenter /><HistoryControls /></MemoryRouter>);
    expect(window.history.scrollRestoration).toBe("manual");
    await waitFor(() => expect(scrollTo).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Recommendations" }));
    await screen.findByRole("heading", { name: "Recommendations panel" });
    await waitFor(() => expect(scrollTo).toHaveBeenCalledTimes(3));
    fireEvent.click(screen.getByRole("combobox", { name: "Selected profile" }));
    fireEvent.click(screen.getByRole("option", { name: "Child" }));
    expect(scrollTo).toHaveBeenCalledTimes(3);
    fireEvent.click(screen.getByRole("combobox", { name: "Admin section" }));
    fireEvent.click(screen.getByRole("option", { name: "Storage & HEVC" }));
    await screen.findByRole("heading", { name: "Storage panel" });
    await waitFor(() => expect(scrollTo).toHaveBeenCalledTimes(5));
    fireEvent.click(screen.getByRole("button", { name: "History back" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Selected profile" })).toBeTruthy());
    await waitFor(() => expect(scrollTo).toHaveBeenCalledTimes(7));
    fireEvent.click(screen.getByRole("button", { name: "History forward" }));
    await screen.findByRole("heading", { name: "Storage panel" });
    await waitFor(() => expect(scrollTo).toHaveBeenCalledTimes(9));
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 0, left: 0, behavior: "auto" });
    view.unmount();
    expect(window.history.scrollRestoration).toBe("auto");
  });
});
