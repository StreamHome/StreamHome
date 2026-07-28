import { describe, expect, it } from "vitest";
import type { Episode } from "../../types/api";
import { advancingPlaybackDelta, nextPlayableEpisode } from "./PlayerPage";

function episode(id: string, seasonNumber: number, episodeNumber: number, videoUrl = `/media/${id}.mp4`): Episode {
  return { id, seasonNumber, episodeNumber, videoUrl, title: id, description: "", thumbnailUrl: "", duration: "", quality: "", languages: [], subtitles: [], skipMarkers: {} };
}

describe("series playback sequence", () => {
  it("sorts episodes and skips entries without playable server media", () => {
    const episodes = [episode("ep3", 1, 3), episode("ep1", 1, 1), episode("ep2", 1, 2, ""), episode("ep4", 2, 1)];
    expect(nextPlayableEpisode(episodes, "ep1")?.id).toBe("ep3");
    expect(nextPlayableEpisode(episodes, "ep3")?.id).toBe("ep4");
  });

  it("returns no episode at the end of the playable sequence", () => {
    expect(nextPlayableEpisode([episode("ep1", 1, 1)], "ep1")).toBeNull();
    expect(nextPlayableEpisode([episode("ep1", 1, 1)], "missing")).toBeNull();
  });

  it("treats an application-owned ingestion preview as playable", () => {
    const preview = { ...episode("ep2", 1, 2, ""), previewTaskId: "preview-task" };
    expect(nextPlayableEpisode([episode("ep1", 1, 1), preview], "ep1")?.id).toBe("ep2");
  });
});

describe("actual watched-time accounting", () => {
  it("counts bounded advancing playback but ignores pauses, rewinds, and forward seeks", () => {
    expect(advancingPlaybackDelta(1_000, 10, 2_000, 11, true)).toBe(1);
    expect(advancingPlaybackDelta(1_000, 10, 2_000, 11, false)).toBe(0);
    expect(advancingPlaybackDelta(1_000, 10, 2_000, 9, true)).toBe(0);
    expect(advancingPlaybackDelta(1_000, 10, 2_000, 40, true)).toBe(0);
  });

  it("never attributes more than two seconds to one delayed browser update", () => {
    expect(advancingPlaybackDelta(1_000, 10, 11_000, 12, true)).toBe(2);
  });
});
