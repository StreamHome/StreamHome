import { afterEach, describe, expect, it, vi } from "vitest";
import { getAdminProfileData, getAdminProfileSummaries } from "./adminProfiles";

afterEach(() => vi.unstubAllGlobals());

describe("admin profile data API contracts", () => {
  it("loads summaries and every profile data category from protected admin routes", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ profiles: [] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
    vi.stubGlobal("fetch", fetchMock);

    await getAdminProfileSummaries();
    expect(fetchMock).toHaveBeenLastCalledWith("/api/admin/profiles", expect.objectContaining({ method: "GET" }));

    await getAdminProfileData("child profile");
    const paths = fetchMock.mock.calls.slice(1).map(([path]) => path);
    expect(paths).toEqual([
      "/api/admin/profiles/child%20profile/overview",
      "/api/admin/profiles/child%20profile/history?limit=100",
      "/api/admin/profiles/child%20profile/watchlist?limit=200",
      "/api/admin/profiles/child%20profile/recommendations?limit=100",
      "/api/admin/profiles/child%20profile/activity?limit=100",
      "/api/admin/profiles/child%20profile/cache?limit=200",
    ]);
  });
});
