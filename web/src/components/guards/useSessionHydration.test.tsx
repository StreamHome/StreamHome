import React, { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { useSessionHydration } from "./useSessionHydration";
import { useAuthStore } from "../../stores/authStore";
import { AuthGuard } from "./AuthGuard";
import { MotionProvider } from "../../motion/motionSystem";
function Owner() { useSessionHydration(); return <MotionProvider><MemoryRouter><AuthGuard><h1>Authorized content</h1></AuthGuard></MemoryRouter></MotionProvider>; }
beforeEach(() => { useAuthStore.getState().cancelHydration(); useAuthStore.setState({ isAuthenticated:false,isHydrated:false,hydrationStatus:"idle",hydrationError:null,email:null }); });
afterEach(() => { cleanup(); useAuthStore.getState().cancelHydration(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
describe("actual application session owner", () => {
  it("retires the first StrictMode effect and accepts only the remounted read", async () => {
    let latest!: (r:Response)=>void;
    const signals: AbortSignal[] = [];
    vi.stubGlobal("fetch",vi.fn((_path:string,options?:RequestInit)=>new Promise<Response>((resolve,reject)=>{ signals.push(options!.signal!); latest=resolve;options?.signal?.addEventListener("abort",()=>reject(new DOMException("Aborted","AbortError"))); })));
    render(<StrictMode><Owner/></StrictMode>);
    expect(signals).toHaveLength(2);expect(signals[0].aborted).toBe(true);expect(signals[1].aborted).toBe(false);
    expect(screen.getByRole("status").textContent).toMatch(/Checking/);expect(screen.queryByText("Authorized content")).toBeNull();
    await act(async()=>latest(new Response(JSON.stringify({email:"owner@example.test"}))));
    expect(screen.getByRole("heading",{name:"Authorized content"})).toBeTruthy();
    expect(useAuthStore.getState().email).toBe("owner@example.test");
  });
  it("retires a pending owner on unmount before a late success", async () => {
    let resolve!: (r:Response)=>void;let signal!:AbortSignal;
    vi.stubGlobal("fetch",vi.fn((_path:string,options?:RequestInit)=>{signal=options!.signal!;return new Promise<Response>(r=>{resolve=r;});}));
    const mounted=render(<Owner/>);mounted.unmount();expect(signal.aborted).toBe(true);
    await act(async()=>resolve(new Response(JSON.stringify({email:"late@example.test"}))));
    expect(useAuthStore.getState()).toMatchObject({isAuthenticated:false,isHydrated:false,hydrationStatus:"idle"});
  });
  it("recovers the mounted protected guard through its real Retry action", async () => {
    let resolve!: (r:Response)=>void;
    vi.stubGlobal("fetch",vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({detail:"Synthetic unavailable"}),{status:503})).mockImplementationOnce(()=>new Promise<Response>(r=>{resolve=r;})));
    render(<Owner/>);
    expect(await screen.findByRole("heading",{name:"Unable to check your session"})).toBeTruthy();
    fireEvent.click(screen.getByRole("button",{name:"Retry session check"}));
    expect(screen.getByRole("status").textContent).toMatch(/Checking/);expect(screen.queryByText("Authorized content")).toBeNull();
    await act(async()=>resolve(new Response(JSON.stringify({email:"owner@example.test"}))));
    expect(screen.getByRole("heading",{name:"Authorized content"})).toBeTruthy();
  });
  it.each(["method", "getter"])("keeps server authentication authoritative when storage %s is blocked", async (failure) => {
    if (failure === "method") vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => { throw new DOMException("Blocked", "SecurityError"); });
    else vi.spyOn(window, "localStorage", "get").mockImplementation(() => { throw new DOMException("Blocked", "SecurityError"); });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({email:"owner@example.test"}))));
    render(<Owner/>);
    expect(await screen.findByRole("heading", {name:"Authorized content"})).toBeTruthy();
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
  });
  it("shows recoverable server failure rather than crashing when legacy storage cleanup fails", async () => {
    vi.spyOn(window, "localStorage", "get").mockImplementation(() => { throw new DOMException("Blocked", "SecurityError"); });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({detail:"Synthetic unavailable"}),{status:503})));
    render(<Owner/>);
    expect(await screen.findByRole("heading", {name:"Unable to check your session"})).toBeTruthy();
    expect(screen.getByRole("button", {name:"Retry session check"})).toBeTruthy();
    expect(screen.queryByText("Authorized content")).toBeNull();
  });

});
