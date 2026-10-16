import { create } from "zustand";
import { ApiError, apiGet } from "../api/client";

export type HydrationStatus = "idle" | "pending" | "ready" | "error";
interface AuthState {
  token: string | null;
  email: string | null;
  isAuthenticated: boolean;
  isHydrated: boolean;
  hydrationStatus: HydrationStatus;
  hydrationError: string | null;
  setToken: (token: string, email: string) => void;
  logout: () => void;
  hydrate: (options?: { force?: boolean }) => Promise<void>;
  cancelHydration: () => void;
}
let generation = 0;
let controller: AbortController | null = null;
let pending: Promise<void> | null = null;
function retireHydration() {
  generation += 1;
  controller?.abort();
  controller = null;
  pending = null;
}
function removeStoredIdentity(keys: string[]) {
  for (const key of keys) {
    // Browser storage can be unavailable; cookie-backed server state remains authoritative.
    try { window.localStorage.removeItem(key); } catch { /* Best-effort legacy/UI cleanup. */ }
  }
}
function clearLegacyIdentity() {
  removeStoredIdentity(["streamhome_token", "streamhome_email"]);
}

export const useAuthStore = create<AuthState>((set, get) => ({
  token: null,
  email: null,
  isAuthenticated: false,
  isHydrated: false,
  hydrationStatus: "idle",
  hydrationError: null,
  setToken: (token, email) => {
    // Login has already established the session through an HttpOnly cookie.
    void token;
    retireHydration();
    clearLegacyIdentity();
    set({ token: null, email, isAuthenticated: true, isHydrated: true, hydrationStatus: "ready", hydrationError: null });
  },
  logout: () => {
    retireHydration();
    void fetch("/api/auth/logout", { method: "POST", credentials: "same-origin", keepalive: true }).catch(() => undefined);
    clearLegacyIdentity();
    removeStoredIdentity(["streamhome_profile", "streamhome_admin_session"]);
    set({ token: null, email: null, isAuthenticated: false, isHydrated: true, hydrationStatus: "ready", hydrationError: null });
    window.location.assign("/login");
  },
  cancelHydration: () => {
    if (!pending) return;
    retireHydration();
    set({ isHydrated: false, hydrationStatus: "idle", hydrationError: null });
  },
  hydrate: (options = {}) => {
    if (!options.force && pending) return pending;
    if (!options.force && get().isHydrated && get().hydrationStatus === "ready") return Promise.resolve();
    retireHydration();
    const ownedGeneration = generation;
    const ownedController = new AbortController();
    controller = ownedController;
    clearLegacyIdentity();
    set({ token: null, email: null, isAuthenticated: false, isHydrated: false, hydrationStatus: "pending", hydrationError: null });
    pending = (async () => {
      try {
        const session = await apiGet<unknown>("/api/auth/session", { signal: ownedController.signal, redirectOnUnauthorized: false });
        if (generation !== ownedGeneration || ownedController.signal.aborted) return;
        if (!session || typeof session !== "object" || !("email" in session) || typeof session.email !== "string" || !session.email.trim()) {
          throw new Error("The server returned an invalid session response.");
        }
        set({ token: null, email: session.email, isAuthenticated: true, isHydrated: true, hydrationStatus: "ready", hydrationError: null });
      } catch (error) {
        if (generation !== ownedGeneration || ownedController.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) {
          set({ token: null, email: null, isAuthenticated: false, isHydrated: true, hydrationStatus: "ready", hydrationError: null });
        } else {
          set({ token: null, email: null, isAuthenticated: false, isHydrated: false, hydrationStatus: "error", hydrationError: error instanceof Error ? error.message : "Your session could not be checked." });
        }
      } finally {
        if (generation === ownedGeneration) { controller = null; pending = null; }
      }
    })();
    return pending;
  },
}));
