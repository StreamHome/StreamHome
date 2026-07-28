import { afterEach, describe, expect, it, vi } from "vitest";
import { deleteProfile, saveProfile, selectProfileAccess, unlockProfile } from "./profiles";

afterEach(() => vi.unstubAllGlobals());

describe("profile management API", () => {
  it("saves canonical profile settings through the existing upsert endpoint", async () => {
    const response = { id: "2", name: "Viewer", avatarColor: "#fff", theme: "gemini", pinEnabled: false };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(saveProfile(response)).resolves.toEqual(response);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/profiles");
    const request = fetchMock.mock.calls[0][1] as RequestInit;
    expect(request.method).toBe("POST");
    expect(JSON.parse(String(request.body))).toEqual(response);
  });

  it("deletes the selected profile through its encoded server path", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "deleted" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await deleteProfile("profile 2");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/profiles/profile%202");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("DELETE");
  });

  it("verifies profile PINs through a server endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ verified: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(unlockProfile("profile 2", "4815")).resolves.toEqual({ verified: true });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/profiles/profile%202/unlock");
    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toEqual({ pin: "4815" });
  });

  it("selects profiles through the server-side access-grant endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ selected: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(selectProfileAccess("profile 2")).resolves.toEqual({ selected: true });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/profiles/profile%202/select");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("POST");
  });
});
