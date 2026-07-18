import { afterEach, describe, expect, it, vi } from "vitest";
import { getMovies } from "./movies";

afterEach(() => vi.unstubAllGlobals());

describe("catalog API", () => {
  it("requests backend-personalized ordering when a profile is supplied", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("[]", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(getMovies("profile one")).resolves.toEqual([]);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/movies?profile_id=profile%20one");
  });

  it("keeps independent catalog lookups unpersonalized", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("[]", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await getMovies();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/movies");
  });
});
