import { describe, expect, it } from "vitest";
import type { Episode, Movie, PlaybackSession } from "../../types/api";
import { detailsResumeTarget, latestResumeSession } from "./detailsPlayback";

const movie = { id: "m_1", type: "movie", title: "Movie" } as Movie;
const series = { id: "tv_1", type: "series", title: "Series" } as Movie;
const session = (overrides: Partial<PlaybackSession> = {}): PlaybackSession => ({
  movieId: "m_1",
  profileId: "1",
  timestamp: 754,
  durationWatched: 754,
  completionRate: 0.4,
  updatedAt: "2026-07-22T10:00:00Z",
  episodeId: null,
  isFinished: false,
  ...overrides,
});

describe("details playback actions", () => {
  it("shows the saved movie position in the resume action", () => {
    expect(detailsResumeTarget(movie, [], [session()])).toMatchObject({
      mediaId: "m_1",
      label: "Resume from 12:34",
      context: "Saved at 12:34",
    });
  });

  it("targets the latest unfinished series episode", () => {
    const episodes = [{ id: "ep_4", seasonNumber: 2, episodeNumber: 3, title: "The Return" }] as Episode[];
    const target = detailsResumeTarget(series, episodes, [
      session({ movieId: "tv_1", episodeId: "ep_2", updatedAt: "2026-07-20T10:00:00Z" }),
      session({ movieId: "tv_1", episodeId: "ep_4", timestamp: 65, updatedAt: "2026-07-22T10:00:00Z" }),
    ]);
    expect(target).toMatchObject({ mediaId: "ep_4", label: "Resume S2 E3 from 1:05", context: "The Return" });
  });

  it("retains the actual late movie position until the server marks it finished", () => {
    const late = session({ timestamp: 5970, completionRate: 0.968 });
    expect(detailsResumeTarget(movie, [], [late])).toMatchObject({
      session: late,
      label: "Resume from 1:39:30",
    });
    expect(latestResumeSession(movie, [{ ...late, isFinished: true }])).toBeNull();
    expect(latestResumeSession(movie, [session({ timestamp: 0 })])).toBeNull();
  });

  it("preserves the existing near-completion cutoff for series and episode sessions", () => {
    expect(latestResumeSession(series, [session({ movieId: series.id, episodeId: "ep_4", completionRate: 0.96 })])).toBeNull();
    expect(latestResumeSession(series, [session({ movieId: series.id, completionRate: 0.96 })])).toBeNull();
    expect(latestResumeSession(movie, [session({ episodeId: "ep_4", completionRate: 0.96 })])).toBeNull();
  });
});
