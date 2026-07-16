import { describe, expect, it } from "vitest";
import type { Movie } from "../../types/api";
import { groupMoviesByGenre } from "./catalogPresentation";

function movie(id: string, genres: string[]): Movie {
  return {
    id, title: id, description: "", thumbnailUrl: "", bannerUrl: null, videoUrl: "",
    genres, duration: "", releaseYear: 0, rating: null, cast: [], director: null,
    type: "movie", quality: "", languages: [], subtitles: [], voteAverage: 0,
    voteCount: 0, skipMarkers: {},
  };
}

describe("catalog presentation", () => {
  it("groups server records by genre while preserving catalog order", () => {
    const first = movie("first", ["Action", "Drama"]);
    const second = movie("second", ["Drama"]);
    const third = movie("third", []);
    expect(groupMoviesByGenre([first, second, third])).toEqual([
      { genre: "Action", items: [first] },
      { genre: "Drama", items: [first, second] },
      { genre: "Uncategorized", items: [third] },
    ]);
  });

  it("limits deep-linked genre views without inventing categories", () => {
    const action = movie("action", ["Action"]);
    const drama = movie("drama", ["Drama"]);
    expect(groupMoviesByGenre([action, drama], "action")).toEqual([{ genre: "Action", items: [action] }]);
  });
});
