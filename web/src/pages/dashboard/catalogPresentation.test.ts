import { describe, expect, it } from "vitest";
import type { Movie } from "../../types/api";
import { ALL_RELEASES_CATEGORY, buildCatalogPresentation, categoryOptions, groupMoviesByGenre, sortByReleaseYear, TOP_PICKS_LIMIT } from "./catalogPresentation";

function movie(id: string, genres: string[], type: "movie" | "series" = "movie", releaseYear = 0): Movie {
  return {
    id, title: id, description: "", thumbnailUrl: "", bannerUrl: null, videoUrl: "",
    genres, duration: "", releaseYear, rating: null, cast: [], director: null,
    type, quality: "", languages: [], subtitles: [], voteAverage: 0,
    voteCount: 0, skipMarkers: {},
  };
}

describe("catalog presentation", () => {
  it("groups server records by genre while preserving recommendation order", () => {
    const first = movie("first", ["Action", "Drama"]);
    const second = movie("second", ["Drama"]);
    const third = movie("third", []);
    expect(groupMoviesByGenre([first, second, third])).toEqual([
      { genre: "Action", items: [first] },
      { genre: "Drama", items: [first, second] },
      { genre: "Uncategorized", items: [third] },
    ]);
  });

  it("builds virtual options before sorted server genres", () => {
    expect(categoryOptions([movie("one", ["Science Fiction", "Action"]), movie("two", ["Action"])]).map((option) => option.label)).toEqual(["Recommended", "All Releases", "Action", "Science Fiction"]);
  });

  it("sorts All Releases by year with stable ties and missing years last", () => {
    const older = movie("older", [], "movie", 2020);
    const firstTie = movie("first-tie", [], "movie", 2025);
    const missing = movie("missing", [], "movie", 0);
    const secondTie = movie("second-tie", [], "movie", 2025);
    expect(sortByReleaseYear([older, firstTie, missing, secondTie]).map((item) => item.id)).toEqual(["first-tie", "second-tie", "older", "missing"]);
  });

  it("uses backend order for Top Picks and recommended genre rails", () => {
    const ranked = Array.from({ length: TOP_PICKS_LIMIT + 3 }, (_, index) => movie(`rank-${index}`, [index % 2 ? "Drama" : "Action"]));
    const model = buildCatalogPresentation({ movies: ranked, continueWatching: [], view: "movies" });
    expect(model.mode).toBe("recommended");
    expect(model.billboardItems).toEqual(ranked);
    expect(model.sections[0].title).toBe("Top Picks For You");
    expect(model.sections[0].items).toEqual(ranked.slice(0, TOP_PICKS_LIMIT));
    expect(model.sections.find((item) => item.title === "Action")?.items.map((item) => item.id)).toEqual(ranked.filter((_, index) => index % 2 === 0).map((item) => item.id));
  });

  it("combines Home All Releases and restricts type-specific views", () => {
    const film = movie("film", ["Action"], "movie", 2024);
    const show = movie("show", ["Action"], "series", 2025);
    const home = buildCatalogPresentation({ movies: [film, show], continueWatching: [], view: "home", category: ALL_RELEASES_CATEGORY });
    const movies = buildCatalogPresentation({ movies: [film, show], continueWatching: [], view: "movies", category: ALL_RELEASES_CATEGORY });
    expect(home.gridItems).toEqual([show, film]);
    expect(movies.gridItems).toEqual([film]);
  });

  it("splits a Home genre into Movies and Series but uses a grid in archives", () => {
    const film = movie("film", ["Action"], "movie");
    const show = movie("show", ["Action"], "series");
    const drama = movie("drama", ["Drama"], "movie");
    const home = buildCatalogPresentation({ movies: [film, show, drama], continueWatching: [], view: "home", category: "action" });
    const archive = buildCatalogPresentation({ movies: [film, show, drama], continueWatching: [], view: "movies", category: "Action" });
    expect(home.sections.map((item) => [item.title, item.items])).toEqual([["Movies", [film]], ["Series", [show]]]);
    expect(archive.gridItems).toEqual([film]);
    expect(archive.billboardItems).toEqual([film]);
  });

  it("keeps unknown deep-linked genres truthful and empty", () => {
    const model = buildCatalogPresentation({ movies: [movie("film", ["Action"])], continueWatching: [], view: "home", category: "Unknown" });
    expect(model.mode).toBe("genre");
    expect(model.activeLabel).toBe("Unknown");
    expect(model.billboardItems).toEqual([]);
    expect(model.sections).toEqual([]);
  });
});
