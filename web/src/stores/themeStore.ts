import { create } from "zustand";
import type { Profile } from "../types/api";
import type { ThemeId } from "../types/theme";
import { normalizeTheme } from "../utils/media";

interface ThemeState {
  activeTheme: ThemeId;
  setTheme: (themeId: string | null | undefined) => void;
  syncFromProfile: (profile: Profile | null) => void;
}

function applyTheme(theme: string | null | undefined): ThemeId {
  const normalized = normalizeTheme(theme);
  document.documentElement.setAttribute("data-theme", normalized);
  return normalized;
}

export const useThemeStore = create<ThemeState>((set) => ({
  activeTheme: "ember",
  setTheme: (theme) => set({ activeTheme: applyTheme(theme) }),
  syncFromProfile: (profile) => set({ activeTheme: applyTheme(profile?.theme) }),
}));
