import { beforeEach, describe, expect, it } from "vitest";
import type { Profile } from "../types/api";
import { useAuthStore } from "./authStore";
import { useProfileStore } from "./profileStore";
import { useThemeStore } from "./themeStore";

const profile: Profile = { id: "1", name: "Admin", avatarColor: "", theme: "netflix", pinEnabled: false, pin: null };

beforeEach(() => {
  localStorage.clear();
  useAuthStore.setState({ token: null, email: null, isAuthenticated: false, isHydrated: false });
  useProfileStore.setState({ profiles: [], activeProfile: null, isAdmin: false });
  useThemeStore.setState({ activeTheme: "ember" });
});

describe("persisted client state", () => {
  it("hydrates token and email before protected routing", () => {
    localStorage.setItem("streamhome_token", "token");
    localStorage.setItem("streamhome_email", "admin@example.test");
    useAuthStore.getState().hydrate();
    expect(useAuthStore.getState()).toMatchObject({ token: "token", email: "admin@example.test", isAuthenticated: true, isHydrated: true });
  });

  it("restores the selected profile and maps legacy Netflix to Cinema", () => {
    localStorage.setItem("streamhome_profile", "1");
    expect(useProfileStore.getState().restoreProfile([profile])).toEqual(profile);
    useThemeStore.getState().syncFromProfile(profile);
    expect(useProfileStore.getState().isAdmin).toBe(true);
    expect(useThemeStore.getState().activeTheme).toBe("cinema");
    expect(document.documentElement.getAttribute("data-theme")).toBe("cinema");
  });
});
