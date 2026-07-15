import { create } from "zustand";
import type { Profile } from "../types/api";

interface ProfileState {
  profiles: Profile[];
  activeProfile: Profile | null;
  isAdmin: boolean;
  setProfiles: (profiles: Profile[]) => void;
  selectProfile: (profile: Profile) => void;
  clearProfile: () => void;
  restoreProfile: (profiles: Profile[]) => Profile | null;
}

export const useProfileStore = create<ProfileState>((set) => ({
  profiles: [],
  activeProfile: null,
  isAdmin: false,

  setProfiles: (profiles) => set({ profiles }),

  selectProfile: (profile) => {
    localStorage.setItem("streamhome_profile", profile.id);
    set({ activeProfile: profile, isAdmin: profile.id === "1" });
  },

  clearProfile: () => {
    localStorage.removeItem("streamhome_profile");
    set({ activeProfile: null, isAdmin: false });
  },

  restoreProfile: (profiles) => {
    const savedId = localStorage.getItem("streamhome_profile");
    const profile = profiles.find((item) => item.id === savedId) ?? null;
    set({ profiles, activeProfile: profile, isAdmin: profile?.id === "1" });
    return profile;
  },
}));
