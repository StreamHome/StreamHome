import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { SetupStateGate } from "./SetupStateGate";
import { SetupPage } from "../../pages/SetupPage";
afterEach(() => { cleanup(); sessionStorage.clear(); vi.unstubAllGlobals(); });
const cases = ["{}", JSON.stringify({ required: false, unlocked: "yes" }), ""];
describe("setup status protocol authority", () => {
  it.each(cases)("does not redirect or mount normal setup on malformed success %s", async (body) => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body)));
    render(<MemoryRouter initialEntries={["/setup"]}><Routes><Route path="/setup" element={<SetupStateGate><p>Normal setup</p></SetupStateGate>} /><Route path="/login" element={<p>Sign-in route</p>} /></Routes></MemoryRouter>);
    expect(await screen.findByRole("heading", { name: "StreamHome is unavailable" })).toBeTruthy();
    expect(screen.queryByText("Normal setup")).toBeNull(); expect(screen.queryByText("Sign-in route")).toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });
  it.each(cases)("retains the checkpoint and exposes local recovery on malformed setup status %s", async (body) => {
    const checkpoint = JSON.stringify({version:1,step:7,email:"owner@example.test",publicUrl:"https://watch.example.test",webPort:3000,hevc:"auto",backups:true,updates:false,storage:"LOCAL",remoteName:"",audience:"external",publishingStatus:"production",driveJobId:"",drivePath:""});
    sessionStorage.setItem("streamhome.setup.checkpoint.v1", checkpoint);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body)));
    render(<SetupPage />);
    expect(await screen.findByRole("heading", { name: "Setup status is unavailable" })).toBeTruthy();
    expect(sessionStorage.getItem("streamhome.setup.checkpoint.v1")).toBe(checkpoint);
    expect(screen.getByRole("button", { name: "Retry setup status" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Unlock setup" })).toBeNull();
  });
});
