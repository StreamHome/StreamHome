import React from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as profiles from "../api/profiles";
import { ApiError } from "../api/client";
import { useProfileStore } from "../stores/profileStore";
import { useThemeStore } from "../stores/themeStore";
import { ProfileSelectPage } from "./ProfileSelectPage";
import type { Profile } from "../types/api";
vi.mock("../api/profiles", () => ({ getProfiles: vi.fn(), createProfile: vi.fn(), selectProfileAccess: vi.fn(), unlockProfile: vi.fn() }));
const profile: Profile = { id: "2", name: "Viewer", theme: "ember", avatarColor: "blue", pinEnabled: true };
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (error: Error) => void; const promise = new Promise<T>((accept, fail) => { resolve = accept; reject = fail; }); return { promise, resolve, reject }; }
function mount() { return render(<MemoryRouter><ProfileSelectPage /></MemoryRouter>); }
async function openCreate() { const invoker = await screen.findByRole("button", { name: /Create profile/ }); invoker.focus(); fireEvent.click(invoker); return invoker; }
beforeEach(() => { vi.resetAllMocks(); useProfileStore.setState({ profiles: [], activeProfile: null }); vi.mocked(profiles.getProfiles).mockResolvedValue([profile]); });
describe("profile dialogs on included surfaces", () => {
  it("contains focus, inerts only its sibling, restores the exact invoker, and cleans up", async () => {
    const view = mount(); const invoker = await openCreate();
    const dialog = screen.getByRole("dialog", { name: "Create profile" }); const input = within(dialog).getByLabelText("Name"); const cancel = within(dialog).getByRole("button", { name: "Cancel" });
    expect(document.activeElement).toBe(input); expect(document.body.style.overflow).toBe("hidden");
    const content = document.querySelector<HTMLElement>(".profile-gallery__content")!; expect(content.inert).toBe(true); expect(dialog.closest("[inert]")).toBeNull();
    fireEvent.keyDown(window, { key: "Tab", shiftKey: true }); expect(document.activeElement).toBe(cancel);
    fireEvent.keyDown(window, { key: "Tab" }); expect(document.activeElement).toBe(input);
    invoker.focus(); expect(document.activeElement).toBe(input);
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull()); expect(document.activeElement).toBe(invoker); expect(content.inert).toBe(false); expect(document.body.style.overflow).toBe("");
    await openCreate(); view.unmount(); expect(document.body.style.overflow).toBe(""); expect(content.inert).toBe(false);
  });
  it("exposes exactly one pressed theme and submits the selected allowed theme", async () => {
    const created = { ...profile, id: "created", name: "New viewer", pinEnabled: false, theme: "cinema" };
    vi.mocked(profiles.createProfile).mockResolvedValue(created); mount(); await openCreate();
    const dialog = screen.getByRole("dialog"); fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "New viewer" } });
    const cinema = within(dialog).getByRole("button", { name: "Cinema" }); cinema.focus(); fireEvent.click(cinema);
    expect(Array.from(dialog.querySelectorAll("[aria-pressed]")).map((button) => button.textContent)).toEqual(["Ember", "Aurora", "Cinema"]);
    expect(cinema.getAttribute("aria-pressed")).toBe("true"); expect(within(dialog).getByRole("button", { name: "Ember" }).getAttribute("aria-pressed")).toBe("false");
    expect(dialog.querySelectorAll('[aria-pressed="true"]').length).toBe(1); fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(profiles.createProfile).toHaveBeenCalledWith(expect.objectContaining({ name: "New viewer", theme: "cinema", pinEnabled: false })));
  });
  it("guards Cancel, Escape and duplicate creation while pending then focuses the failed field", async () => {
    const request = deferred<Profile>(); vi.mocked(profiles.createProfile).mockReturnValue(request.promise); mount(); await openCreate();
    const input = screen.getByLabelText("Name"); fireEvent.change(input, { target: { value: "New viewer" } });
    const form = input.closest("form")!; fireEvent.submit(form); fireEvent.submit(form);
    expect(profiles.createProfile).toHaveBeenCalledTimes(1); expect((screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" })); fireEvent.keyDown(window, { key: "Escape" }); expect(screen.getByRole("dialog")).toBeTruthy(); expect(screen.getByRole("status").textContent).toMatch(/Creating/);
    await act(async () => request.reject(new ApiError("Fixture validation failed", 400)));
    expect(document.activeElement).toBe(input); expect(screen.getByRole("alert").textContent).toBe("Fixture validation failed");
    fireEvent.keyDown(window, { key: "Escape" }); await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
  it("reconciles an unknown create outcome before permitting another mutation", async () => {
    vi.mocked(profiles.createProfile).mockRejectedValue(new ApiError("Timeout", 0, "request_timeout")); mount(); await openCreate();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New viewer" } }); fireEvent.submit(screen.getByLabelText("Name").closest("form")!);
    await screen.findByRole("button", { name: "Check creation status" }); expect((screen.getByRole("button", { name: "Create" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Check creation status" })); await screen.findByText(/Its outcome remains unconfirmed/); expect((screen.getByRole("button", { name: "Create" }) as HTMLButtonElement).disabled).toBe(true);
    const submitted = vi.mocked(profiles.createProfile).mock.calls[0][0]; vi.mocked(profiles.getProfiles).mockResolvedValue([profile, { ...profile, ...submitted }]);
    fireEvent.click(screen.getByRole("button", { name: "Check creation status" })); await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull()); expect(profiles.createProfile).toHaveBeenCalledTimes(1);
  });
  it("blocks competing Create and Edit while unprotected profile selection awaits its server grant", async () => {
    const selectable = { ...profile, pinEnabled: false }; vi.mocked(profiles.getProfiles).mockResolvedValue([selectable]); const selection = deferred<{ selected: true }>(); vi.mocked(profiles.selectProfileAccess).mockReturnValue(selection.promise);
    function LocationProbe() { const location = useLocation(); return <p data-testid="location">{location.pathname}{location.search}</p>; }
    render(<MemoryRouter initialEntries={["/profiles"]}><ProfileSelectPage /><LocationProbe /></MemoryRouter>); fireEvent.click(await screen.findByRole("button", { name: /Viewer/ })); const create = screen.getByRole("button", { name: /Create profile/ }); const edit = screen.getByRole("button", { name: "Edit profile" });
    expect((create as HTMLButtonElement).disabled).toBe(true); expect((edit as HTMLButtonElement).disabled).toBe(true); fireEvent.click(create); fireEvent.click(edit); expect(screen.queryByRole("dialog")).toBeNull(); expect(screen.getByTestId("location").textContent).toBe("/profiles"); expect(useProfileStore.getState().activeProfile).toBeNull();
    await act(async () => selection.resolve({ selected: true })); expect(useProfileStore.getState().activeProfile?.id).toBe(profile.id); expect(screen.queryByRole("dialog")).toBeNull(); await waitFor(() => expect(screen.getByTestId("location").textContent).toContain("profile=2"));
  });
  it("requires a verified server unlock and ignores an unmounted completion", async () => {
    const request = deferred<{ verified: true }>(); vi.mocked(profiles.unlockProfile).mockReturnValue(request.promise); const view = mount();
    const invoker = await screen.findByRole("button", { name: /Viewer/ }); invoker.focus(); fireEvent.click(invoker); const pin = screen.getByLabelText("Profile PIN"); expect(document.activeElement).toBe(pin);
    fireEvent.change(pin, { target: { value: "1234" } }); fireEvent.submit(pin.closest("form")!); fireEvent.submit(pin.closest("form")!);
    expect(profiles.unlockProfile).toHaveBeenCalledTimes(1); expect(useProfileStore.getState().activeProfile).toBeNull(); fireEvent.keyDown(window, { key: "Escape" }); expect(screen.getByRole("dialog")).toBeTruthy();
    view.unmount(); await act(async () => request.resolve({ verified: true })); expect(useProfileStore.getState().activeProfile).toBeNull(); expect(document.body.style.overflow).toBe("");
  });

  it.each([false, true])("shows a retired Gemini profile as Ember while preserving server access (PIN: %s)", async (pinEnabled) => {
    const legacy = { ...profile, theme: "gemini", pinEnabled };
    vi.mocked(profiles.getProfiles).mockResolvedValue([legacy]);
    const grant = deferred<{ selected: true }>();
    const unlock = deferred<{ verified: true }>();
    vi.mocked(profiles.selectProfileAccess).mockReturnValue(grant.promise);
    vi.mocked(profiles.unlockProfile).mockReturnValue(unlock.promise);
    useThemeStore.setState({ activeTheme: "cinema" });
    const view = mount();
    const select = await screen.findByRole("button", { name: /Viewer/ });
    expect(select.querySelector(".profile-preview--ember")).not.toBeNull();
    expect(select.textContent).toContain("Ember / profile");
    fireEvent.click(select);
    if (pinEnabled) {
      const pin = screen.getByLabelText("Profile PIN");
      fireEvent.change(pin, { target: { value: "1234" } });
      fireEvent.submit(pin.closest("form")!);
      expect(profiles.selectProfileAccess).not.toHaveBeenCalled();
      expect(useProfileStore.getState().activeProfile).toBeNull();
      await act(async () => unlock.resolve({ verified: true }));
    } else {
      expect(profiles.selectProfileAccess).toHaveBeenCalledWith(legacy.id);
      expect(useProfileStore.getState().activeProfile).toBeNull();
      await act(async () => grant.resolve({ selected: true }));
    }
    expect(useThemeStore.getState().activeTheme).toBe("ember");
    expect(useProfileStore.getState().activeProfile).toEqual(legacy);
    expect(profiles.createProfile).not.toHaveBeenCalled();
    view.unmount();
  });
});
