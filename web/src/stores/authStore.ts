import { create } from "zustand";

interface AuthState {
  token: string | null;
  email: string | null;
  isAuthenticated: boolean;
  isHydrated: boolean;
  setToken: (token: string, email: string) => void;
  logout: () => void;
  hydrate: () => Promise<void>;
}

export const useAuthStore = create<AuthState>((set) => ({
  token: null,
  email: null,
  isAuthenticated: false,
  isHydrated: false,

  setToken: (token, email) => {
    // The access token is delivered through an HttpOnly cookie. Remove any
    // legacy browser-readable token left by an older StreamHome release.
    void token;
    localStorage.removeItem("streamhome_token");
    localStorage.removeItem("streamhome_email");
    set({ token: null, email, isAuthenticated: true, isHydrated: true });
  },

  logout: () => {
    void fetch("/api/auth/logout", { method: "POST", credentials: "same-origin", keepalive: true }).catch(() => undefined);
    localStorage.removeItem("streamhome_token");
    localStorage.removeItem("streamhome_email");
    localStorage.removeItem("streamhome_profile");
    localStorage.removeItem("streamhome_admin_session");
    set({ token: null, email: null, isAuthenticated: false, isHydrated: true });
    window.location.assign("/login");
  },

  hydrate: async () => {
    localStorage.removeItem("streamhome_token");
    localStorage.removeItem("streamhome_email");
    try {
      const response = await fetch("/api/auth/session", { credentials: "same-origin" });
      if (!response.ok) throw new Error("No active session");
      const session = await response.json() as { email: string };
      set({ token: null, email: session.email, isAuthenticated: true, isHydrated: true });
    } catch {
      set({ token: null, email: null, isAuthenticated: false, isHydrated: true });
    }
  },
}));
