import { describe, expect, it } from "vitest";
import { buildCatalogLanding } from "./catalogLanding";
import { catalogFeed, catalogMovie } from "./catalogTestFixtures.nonplayer";

const film = catalogMovie("Film");
const show = { ...catalogMovie("Show"), type: "series" as const, genres: ["Comedy"] };
describe("automatic catalog landing model", () => {
  it("uses real server order for a hero and rails without inventing recommendation metadata", () => {
    const model = buildCatalogLanding({ feed: null, catalog: [show, film], continueWatching: [film], view: "home" });
    expect(model.billboardItems).toEqual([show, film]);
    expect(model.gridItems).toEqual([]);
    expect(model.sections.map((section) => section.title)).toEqual(["Continue Watching", "Featured Titles", "Movies", "Series", "Comedy", "Drama"]);
    expect(model.sections.every((section) => !section.showReasons)).toBe(true);
    expect(film.recommendationScore).toBeUndefined();
  });
  it("filters media scope, case-insensitive genres and known dislikes without reordering", () => {
    const model = buildCatalogLanding({ feed: null, catalog: [show, film, catalogMovie("Hidden")], continueWatching: [], view: "movies", category: "dRaMa", preferences: { Hidden: "dislike" } });
    expect(model.sourceItems).toEqual([film]);
    expect(model.gridItems).toEqual([film]);
    expect(buildCatalogLanding({ feed: null, catalog: [show, film], continueWatching: [], view: "series" }).billboardItems).toEqual([show]);
  });
  it("uses usable recommendations even while the base catalog is empty and preserves feed order", () => {
    const feed = catalogFeed(["Second", "First"]);
    const model = buildCatalogLanding({ feed, catalog: [], continueWatching: [], view: "movies" });
    expect(model.source).toBe("recommendations");
    expect(model.billboardItems.map((movie) => movie.id)).toEqual(["Second", "First"]);
  });
  it("uses catalog content for empty Recommended but respects successful empty explicit categories", () => {
    expect(buildCatalogLanding({ feed: catalogFeed([]), catalog: [film], continueWatching: [], view: "movies" }).billboardItems).toEqual([film]);
    const empty = buildCatalogLanding({ feed: catalogFeed([], "Drama"), catalog: [film], continueWatching: [], view: "movies", category: "Drama" });
    expect(empty.source).toBe("recommendations");
    expect(empty.sourceItems).toEqual([]);
  });
  it("can feature an actual vibe or watch-again title without generating a false top-picks row", () => {
    const feed = { ...catalogFeed([]), watchAgain: catalogFeed(["Watched"]).items };
    const model = buildCatalogLanding({ feed, catalog: [], continueWatching: [], view: "movies" });
    expect(model.billboardItems[0].id).toBe("Watched");
    expect(model.sections.map((section) => section.id)).toEqual(["watch-again"]);
  });
  it("never fills an empty genre hero with unrelated watch-again history", () => {
    const feed = { ...catalogFeed([], "Action"), watchAgain: catalogFeed(["Drama history"]).items };
    const model = buildCatalogLanding({ feed, catalog: [film], continueWatching: [], view: "movies", category: "Action" });
    expect(model.billboardItems).toEqual([]);
    expect(model.sections).toEqual([]);
  });
  it("keeps All Releases limited to available records in server order", () => {
    const available = { ...film, availability: "available" as const };
    expect(buildCatalogLanding({ feed: null, catalog: [show, available], continueWatching: [], view: "home", category: "all" }).gridItems).toEqual([available]);
  });
});
