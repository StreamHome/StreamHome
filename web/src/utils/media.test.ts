import { describe, expect, it } from "vitest";
import type { Episode, Movie } from "../types/api";
import { completionFraction, downloadFraction, isAvailableMedia, isServerArtworkUrl, normalizeTheme, serverArtworkCandidates } from "./media";

describe("server media rules", () => {
  it("accepts only server media paths or absolute HTTP URLs", () => {
    expect(isServerArtworkUrl("/media/Movies/title/poster.jpg")).toBe(true);
    expect(isServerArtworkUrl("https://media.example/poster.jpg")).toBe(true);
    expect(isServerArtworkUrl("/poster.jpg")).toBe(false);
    expect(isServerArtworkUrl("data:image/png;base64,test")).toBe(false);
    expect(isServerArtworkUrl("")).toBe(false);
  });

  it("resolves compact server artwork references from movie identity", () => {
    expect(serverArtworkCandidates("/poster.jpg", { id: "m_1318447", title: "Apex", type: "movie", releaseYear: 2026 })).toEqual([
      "/media/Movies/Apex_2026_TMDB_1318447/poster.jpg",
      "/media/Movies/Apex_2025_TMDB_1318447/poster.jpg",
      "/media/Movies/Apex_2027_TMDB_1318447/poster.jpg",
    ]);
  });

  it("resolves episode artwork inside its server-owned series folder", () => {
    expect(serverArtworkCandidates("/thumbnail.jpg", { id: "tv_1399", title: "Example: Series", type: "series", releaseYear: 2020 }, { seasonNumber: 2, episodeNumber: 1 })).toEqual([
      "/media/Series/Example%20Series_TMDB_1399/Season_2/Episode_1/thumbnail.jpg",
      "/media/Series/Example%20Series_TMDB_1399/thumbnail.jpg",
    ]);
  });

  it("normalizes legacy and unknown themes", () => {
    expect(normalizeTheme("netflix")).toBe("ember");
    expect(normalizeTheme("aurora")).toBe("aurora");
    expect(normalizeTheme("cinema")).toBe("cinema");
    expect(normalizeTheme(null)).toBe("ember");
    expect(normalizeTheme("unknown")).toBe("ember");
  });

  it("normalizes playback and server download progress", () => {
    expect(completionFraction(1.5)).toBe(1);
    expect(completionFraction(-1)).toBe(0);
    expect(downloadFraction(45)).toBe(0.45);
    expect(downloadFraction(250)).toBe(1);
  });
});

function media(overrides: Partial<Movie>): Movie {
  return {
    id: "m_preview",
    title: "Preview",
    description: "",
    thumbnailUrl: "",
    bannerUrl: null,
    videoUrl: "",
    genres: [],
    duration: "",
    releaseYear: 2026,
    rating: null,
    cast: [],
    director: null,
    type: "movie",
    quality: "Source",
    languages: [],
    subtitles: [],
    voteAverage: 0,
    voteCount: 0,
    skipMarkers: {},
    ...overrides,
  };
}

describe("isAvailableMedia", () => {
  it("allows an active movie ingestion preview", () => {
    expect(isAvailableMedia(media({ availability: "processing", previewTaskId: "task-preview" }))).toBe(true);
  });

  it("does not expose a processing source without an application-owned preview", () => {
    expect(isAvailableMedia(media({ availability: "processing", videoUrl: "https://source.invalid/master.txt" }))).toBe(false);
  });

  it("allows a series when at least one episode has an active preview", () => {
    expect(isAvailableMedia(media({
      type: "series",
      availability: "processing",
      episodes: [{ videoUrl: "", previewTaskId: "episode-preview" } as Episode],
    }))).toBe(true);
  });
});
