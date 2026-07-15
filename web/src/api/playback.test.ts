import { afterEach, describe, expect, it, vi } from "vitest";
import { trackPlayback } from "./playback";

afterEach(() => vi.unstubAllGlobals());

describe("playback tracking", () => {
  it("sends the exact field names expected by the existing server", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "success", updatedAt: "now" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await trackPlayback({ movieId: "tv_1", profileId: "1", episodeId: "ep_1_s1_e1", timestamp: 30, durationWatched: 30, completionRate: 0.5, isFinished: false });
    const request = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(String(request.body))).toEqual({ movieId: "tv_1", profileId: "1", timestamp: 30, duration_watched: 30, completion_rate: 0.5, episodeId: "ep_1_s1_e1", is_finished: false });
  });
});
