import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { useAuthStore as Store } from "./authStore";
let store: typeof Store;
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
beforeEach(async () => { vi.resetModules(); localStorage.clear(); store = (await import("./authStore")).useAuthStore; });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("server session hydration", () => {
  it("deduplicates pending hydration and grants access only after a valid server payload", async () => {
    let resolve!: (r: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((r) => { resolve = r; })));
    const first = store.getState().hydrate(), second = store.getState().hydrate();
    expect(first).toBe(second);
    expect(store.getState()).toMatchObject({ isHydrated: false, isAuthenticated: false, hydrationStatus: "pending" });
    resolve(json({ email: "owner@example.test" })); await first;
    expect(store.getState()).toMatchObject({ email: "owner@example.test", isHydrated: true, isAuthenticated: true, hydrationStatus: "ready" });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it.each([503, 403])("treats HTTP%s as unavailable rather than anonymous", async (status) => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "Unavailable" }, status)));
    await store.getState().hydrate();
    expect(store.getState()).toMatchObject({ isAuthenticated: false, isHydrated: false, hydrationStatus: "error" });
  });
  it("recognizes explicit unauthorized without a generic redirect", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "No session" }, 401)));
    await store.getState().hydrate();
    expect(store.getState()).toMatchObject({ email: null, isAuthenticated: false, isHydrated: true, hydrationStatus: "ready" });
  });
  it.each([{}, { email: 4 }, { email: " " }, null])("rejects a malformed successful session %j", async (value) => {
    vi.stubGlobal("fetch", vi.fn(async () => json(value)));
    await store.getState().hydrate();
    expect(store.getState()).toMatchObject({ isAuthenticated: false, isHydrated: false, hydrationStatus: "error" });
  });
  it("exposes a bounded unavailable state when the response body stalls", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn((_path: string, options?: RequestInit) => Promise.resolve(new Response(new ReadableStream({ start(controller) { options?.signal?.addEventListener("abort", () => controller.error(new DOMException("Aborted", "AbortError"))); } })))));
    const request = store.getState().hydrate();
    await vi.advanceTimersByTimeAsync(45_000); await request;
    expect(store.getState()).toMatchObject({ hydrationStatus: "error", isHydrated: false, isAuthenticated: false });
    expect(store.getState().hydrationError).toMatch(/too long/);
  });
  it("retries unavailable reads and retires a stale forced attempt", async () => {
    let old!: (r: Response) => void;
    const mocked = vi.fn().mockImplementationOnce(() => new Promise<Response>((r) => { old = r; })).mockResolvedValueOnce(json({ email: "new@example.test" }));
    vi.stubGlobal("fetch", mocked);
    const first = store.getState().hydrate();
    await store.getState().hydrate({ force: true });
    old(json({ email: "old@example.test" })); await first;
    expect(store.getState().email).toBe("new@example.test");
  });
  it("cannot overwrite a later completed login and removes legacy tokens", async () => {
    localStorage.setItem("streamhome_token", "legacy-not-a-token");
    let resolve!: (r: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((r) => { resolve = r; })));
    const request = store.getState().hydrate();
    store.getState().setToken("ignored", "signed-in@example.test");
    resolve(json({ email: "old@example.test" })); await request;
    expect(store.getState()).toMatchObject({ email: "signed-in@example.test", token: null, isAuthenticated: true });
    expect(localStorage.getItem("streamhome_token")).toBeNull();
  });
  it("retires a pending read when its application owner unmounts", async () => {
    let resolve!: (r: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((r) => { resolve = r; })));
    const request = store.getState().hydrate();
    store.getState().cancelHydration();
    resolve(json({ email: "late@example.test" })); await request;
    expect(store.getState()).toMatchObject({ isAuthenticated: false, isHydrated: false, hydrationStatus: "idle" });
  });
  it("does not resurrect authentication after logout", async () => {
    const originalWindow = window;
    const assign = vi.fn();
    vi.stubGlobal("window", { setTimeout: originalWindow.setTimeout.bind(originalWindow), clearTimeout: originalWindow.clearTimeout.bind(originalWindow), location: { assign } });
    let resolve!: (r: Response) => void;
    vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => new Promise<Response>((r) => { resolve = r; })).mockResolvedValue(json({})));
    const request = store.getState().hydrate(); store.getState().logout();
    resolve(json({ email: "late@example.test" })); await request;
    expect(store.getState()).toMatchObject({ isAuthenticated: false, email: null, hydrationStatus: "ready" });
    expect(assign).toHaveBeenCalledWith("/login");
  });
  it("preserves completed login and server logout when browser identity cleanup throws", () => {
    const originalWindow=window;const assign=vi.fn();
    vi.stubGlobal("window", {setTimeout:originalWindow.setTimeout.bind(originalWindow),clearTimeout:originalWindow.clearTimeout.bind(originalWindow),get localStorage(){throw new DOMException("Blocked","SecurityError");},location:{assign}});
    vi.stubGlobal("fetch",vi.fn(async()=>json({})));
    expect(()=>store.getState().setToken("ignored","owner@example.test")).not.toThrow();
    expect(store.getState()).toMatchObject({isAuthenticated:true,email:"owner@example.test"});
    expect(()=>store.getState().logout()).not.toThrow();
    expect(store.getState()).toMatchObject({isAuthenticated:false,email:null});
    expect(fetch).toHaveBeenCalledWith("/api/auth/logout",expect.objectContaining({method:"POST",credentials:"same-origin"}));
    expect(assign).toHaveBeenCalledWith("/login");
  });

});
