import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { deleteProfile, getProfiles, saveProfile } from "../api/profiles";
import { PROFILE_AVATAR_PRESETS } from "../profile/profileAppearance";
import { useProfileStore } from "../stores/profileStore";
import { useThemeStore } from "../stores/themeStore";
import type { Profile } from "../types/api";
import { ProfileEditPage } from "./ProfileEditPage";

vi.mock("../api/profiles", () => ({
  deleteProfile: vi.fn(),
  getProfiles: vi.fn(),
  saveProfile: vi.fn(),
}));

vi.mock("../themes/application/themeRegistry", () => ({
  getThemeDefinition: (id: string) => ({
    id,
    shellClass: `theme-app--${id}`,
    interaction: { id },
    Background: () => <div data-testid={`background-${id}`} />,
  }),
}));

const profile: Profile = {
  id: "2",
  name: "Viewer",
  avatarColor: PROFILE_AVATAR_PRESETS[0].background,
  theme: "ember",
  pinEnabled: true,
};

function renderEditor(entry: { pathname: string; state?: { returnTo?: string } } = { pathname: "/profiles/2/edit", state: { returnTo: "/profiles" } }) {
  return render(<MemoryRouter initialEntries={[entry]}><Routes>
    <Route path="/profiles/:profileId/edit" element={<ProfileEditPage />} />
    <Route path="/profiles" element={<p>Profile gallery</p>} />
    <Route path="/" element={<p>Catalog return</p>} />
  </Routes></MemoryRouter>);
}

describe("ProfileEditPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useProfileStore.setState({ profiles: [profile], activeProfile: profile, isAdmin: false });
    useThemeStore.setState({ activeTheme: "ember" });
  });

  it("saves valid changes without returning or resending the existing PIN", async () => {
    vi.mocked(saveProfile).mockImplementation(async (payload) => ({ ...profile, ...payload }));
    renderEditor({ pathname: "/profiles/2/edit", state: { returnTo: "/?profile=2&view=movies#featured" } });

    fireEvent.change(screen.getByLabelText("Profile name"), { target: { value: "Movie Fan" } });
    fireEvent.click(screen.getByRole("button", { name: "Gemini" }));
    fireEvent.click(screen.getByRole("button", { name: "Ocean signal" }));
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(saveProfile).toHaveBeenCalledWith(expect.objectContaining({
      id: "2",
      name: "Movie Fan",
      theme: "gemini",
      avatarColor: PROFILE_AVATAR_PRESETS[1].background,
      pinEnabled: true,
    })));
    expect(vi.mocked(saveProfile).mock.calls[0][0]).not.toHaveProperty("pin");
    expect(await screen.findByText("Catalog return")).toBeTruthy();
    expect(useThemeStore.getState().activeTheme).toBe("gemini");
    expect(useProfileStore.getState().activeProfile?.name).toBe("Movie Fan");
  });

  it("protects dirty changes when leaving", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderEditor();
    fireEvent.change(screen.getByLabelText("Profile name"), { target: { value: "Changed" } });
    fireEvent.click(screen.getByRole("button", { name: /Back/ }));
    expect(confirm).toHaveBeenCalledWith("Discard your unsaved profile changes?");
    expect(screen.getByRole("heading", { name: "Edit profile" })).toBeTruthy();
    confirm.mockRestore();
  });

  it("loads a missing store profile from the server and supports typed-name deletion", async () => {
    const remote = { ...profile, id: "3", name: "Guest", pinEnabled: false };
    useProfileStore.setState({ profiles: [profile], activeProfile: profile, isAdmin: false });
    vi.mocked(getProfiles).mockResolvedValue([profile, remote]);
    vi.mocked(deleteProfile).mockResolvedValue({ status: "ok" });
    renderEditor({ pathname: "/profiles/3/edit", state: { returnTo: "/profiles" } });

    expect(await screen.findByRole("heading", { name: "Guest" })).toBeTruthy();
    const deleteButton = screen.getByRole("button", { name: "Delete profile" });
    expect(deleteButton.hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByLabelText("Confirm profile name"), { target: { value: "Guest" } });
    fireEvent.click(deleteButton);

    await waitFor(() => expect(deleteProfile).toHaveBeenCalledWith("3"));
    expect(await screen.findByText("Profile gallery")).toBeTruthy();
  });

  it("keeps the administrator profile deletion-protected", () => {
    const administrator = { ...profile, id: "1", name: "Administrator" };
    useProfileStore.setState({ profiles: [administrator], activeProfile: administrator, isAdmin: true });
    renderEditor({ pathname: "/profiles/1/edit", state: { returnTo: "/profiles" } });
    expect(screen.getByText("The administrator profile is permanent and cannot be deleted.")).toBeTruthy();
    expect(screen.queryByLabelText("Confirm profile name")).toBeNull();
  });

  it("requires matching digits before replacing a profile PIN", async () => {
    vi.mocked(saveProfile).mockImplementation(async (payload) => ({ ...profile, ...payload }));
    renderEditor();

    fireEvent.change(screen.getByLabelText("Profile PIN"), { target: { value: "1234" } });
    expect(screen.getByRole("button", { name: "Save changes" }).hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByLabelText("Confirm profile PIN"), { target: { value: "1234" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(saveProfile).toHaveBeenCalledWith(expect.objectContaining({
      id: "2",
      pinEnabled: true,
      pin: "1234",
    })));
  });
});
