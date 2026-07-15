import { create } from "zustand";

interface AuthState {
  token: string | null;
  email: string | null;
  isAuthenticated: boolean;
  isHydrated: boolean;
  setToken: (token: string, email: string) => void;
  logout: () => void;
  hydrate: () => void;
}

const TOKEN_KEY = "streamhome_token";
const EMAIL_KEY = "streamhome_email";

export const useAuthStore = create<AuthState>((set) => ({
  token: null,
  email: null,
  isAuthenticated: false,
  isHydrated: false,

  setToken: (token, email) => {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(EMAIL_KEY, email);
    set({ token, email, isAuthenticated: true, isHydrated: true });
  },

  logout: () => {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(EMAIL_KEY);
    localStorage.removeItem("streamhome_profile");
    localStorage.removeItem("streamhome_admin_session");
    set({ token: null, email: null, isAuthenticated: false, isHydrated: true });
    window.location.assign("/login");
  },

  hydrate: () => {
    const token = localStorage.getItem(TOKEN_KEY);
    const email = localStorage.getItem(EMAIL_KEY);
    set({ token, email, isAuthenticated: Boolean(token), isHydrated: true });
  },
}));
