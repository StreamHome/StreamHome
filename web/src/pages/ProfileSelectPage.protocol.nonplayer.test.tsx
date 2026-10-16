import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProfileSelectPage } from "./ProfileSelectPage";
import { useProfileStore } from "../stores/profileStore";
import type { Profile } from "../types/api";

const existing: Profile = { id: "2", name: "Existing viewer", avatarColor: "blue", theme: null, pinEnabled: false };
const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
let acknowledgement: unknown;
let listed: boolean;
let submitted: Profile | null;
let writes: number;
let unexpected: string[];
beforeEach(() => {
  acknowledgement = {}; listed = false; submitted = null; writes = 0; unexpected = [];
  useProfileStore.setState({ profiles: [], activeProfile: null });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const path = String(input);
    if (path === "/api/profiles" && options?.method === "GET") return json(listed && submitted ? [existing, submitted] : [existing]);
    if (path === "/api/profiles" && options?.method === "POST") {
      writes += 1; submitted = JSON.parse(String(options.body)) as Profile; return json(acknowledgement);
    }
    unexpected.push(path); throw new Error(`Unexpected profile protocol request: ${path}`);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("actual profile creation acknowledgement boundary", () => {
  it.each([null, {}, { ...existing, id: "wrong-owner" }])("retains an unknown creation %j and reconciles only the submitted ID", async (body) => {
    acknowledgement = body;
    render(<MemoryRouter><ProfileSelectPage /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: /Create profile/ }));
    const dialog = screen.getByRole("dialog", { name: "Create profile" });
    const name = within(dialog).getByLabelText("Name");
    fireEvent.change(name, { target: { value: "New viewer" } }); fireEvent.submit(name.closest("form")!);
    const check = await screen.findByRole("button", { name: "Check creation status" });
    expect(useProfileStore.getState().profiles).toEqual([existing]);
    expect(useProfileStore.getState().activeProfile).toBeNull();
    expect((screen.getByRole("button", { name: "Create" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.submit(name.closest("form")!); expect(writes).toBe(1);
    fireEvent.click(check); await screen.findByText(/Its outcome remains unconfirmed/);
    expect(screen.getByRole("dialog", { name: "Create profile" })).toBeTruthy();
    listed = true; fireEvent.click(check);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(useProfileStore.getState().profiles.map((profile) => profile.name)).toEqual(["Existing viewer", "New viewer"]);
    expect(writes).toBe(1); expect(unexpected).toEqual([]);
  });
});
