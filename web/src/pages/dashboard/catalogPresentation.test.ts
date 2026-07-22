import { describe, expect, it } from "vitest";
import type { Movie, RecommendationCategory, RecommendationFeed, RecommendationItem } from "../../types/api";
import { buildCatalogPresentation, categoryOptions, genreCategoryCards, TOP_PICKS_LIMIT } from "./catalogPresentation";

function movie(id: string, genres: string[], type: "movie" | "series" = "movie", availability = "available"): Movie {
  return { id, title: id, description: "", thumbnailUrl: "", bannerUrl: null, videoUrl: availability === "available" ? `/media/${id}` : "", genres, duration: "", releaseYear: 2025, rating: null, cast: [], director: null, type, quality: "", languages: [], subtitles: [], voteAverage: 0, voteCount: 0, skipMarkers: {}, availability, recommendationReasons: [`Because ${id}`] };
}

function item(media: Movie, score = 1): RecommendationItem {
  return { media, source: media.availability === "available" ? "server" : "tmdb_cache", availability: media.availability ?? "cached", score, reasons: media.recommendationReasons ?? [] };
}

const categories: RecommendationCategory[] = [
  { value: "recommended", label: "Recommended", affinity: 0, serverCount: 2, cachedCount: 1 },
  { value: "all", label: "All Releases", affinity: 0, serverCount: 2, cachedCount: 0 },
  { value: "Drama", label: "Drama", affinity: 3, serverCount: 1, cachedCount: 1 },
  { value: "Action", label: "Action", affinity: 2, serverCount: 1, cachedCount: 0 },
];

function feed(items: RecommendationItem[], category = "recommended", watchAgain: RecommendationItem[] = []): RecommendationFeed {
  return { profileId: "1", scope: "home", category, generatedAt: 1, stale: false, total: items.length, offset: 0, limit: 48, categories, items, watchAgain };
}

describe("server-driven catalog presentation", () => {
  it("preserves server category order and category counts", () => {
    expect(categoryOptions(categories).map((option) => [option.label, option.count])).toEqual([
      ["Recommended", 3], ["All Releases", 2], ["Drama", 2], ["Action", 1],
    ]);
  });

  it("uses the first ranked matching title as category artwork", () => {
    const firstDrama = movie("first-drama", ["Drama"]);
    const action = movie("action", ["Action"]);
    const secondDrama = movie("second-drama", ["Drama"]);
    expect(genreCategoryCards(categories, [firstDrama, action, secondDrama], []).map((card) => [card.label, card.count, card.representative.id])).toEqual([
      ["Drama", 2, "first-drama"], ["Action", 1, "action"],
    ]);
  });

  it("preserves recommendation order for billboard, Top Picks, and genre rails", () => {
    const ranked = Array.from({ length: TOP_PICKS_LIMIT + 3 }, (_, index) => movie(`rank-${index}`, [index % 2 ? "Drama" : "Action"]));
    const model = buildCatalogPresentation({ feed: feed(ranked.map((entry, index) => item(entry, 100 - index))), fallbackMovies: ranked, continueWatching: [], view: "movies" });
    expect(model.billboardItems).toEqual(ranked);
    expect(model.sections.find((section) => section.id === "top-picks")?.items).toEqual(ranked.slice(0, TOP_PICKS_LIMIT));
    expect(model.sections.find((section) => section.title === "Action")?.items).toEqual(ranked.filter((_, index) => index % 2 === 0));
  });

  it("places server-authored vibe rails after Top Picks without turning them into filters", () => {
    const ranked = [movie("ranked", ["Action"]), movie("second", ["Drama"])];
    const vibeItems = [movie("vibe-1", ["Crime"]), movie("vibe-2", ["Comedy"]), movie("vibe-3", ["Action"])];
    const recommendationFeed = { ...feed(ranked.map(item)), algorithmVersion: "v2.1", vibeRails: [{ id: "vibe-banter", label: "Witty Banter & Bullets", tropeIds: ["neo_noir_buddy_action"], reasonCode: "trope_match", items: vibeItems.map(item) }] };
    const model = buildCatalogPresentation({ feed: recommendationFeed, fallbackMovies: ranked, continueWatching: [], view: "movies" });
    const topPicksIndex = model.sections.findIndex((entry) => entry.id === "top-picks");
    const vibeIndex = model.sections.findIndex((entry) => entry.id === "vibe-banter");
    expect(vibeIndex).toBeGreaterThan(topPicksIndex);
    expect(model.sections[vibeIndex]).toMatchObject({ title: "Witty Banter & Bullets", kind: "vibe", items: vibeItems });
    expect(model.categories.some((category) => category.value === "neo_noir_buddy_action")).toBe(false);
  });

  it("places Watch Again before Top Picks without sorting or reasons", () => {
    const ranked = [movie("ranked", ["Drama"])];
    const recent = movie("recent", ["Drama"]);
    const older = movie("older", ["Action"]);
    const model = buildCatalogPresentation({ feed: feed(ranked.map(item), "recommended", [item(recent, 1), item(older, 99)]), fallbackMovies: ranked, continueWatching: [], view: "home" });
    const watchAgain = model.sections.find((section) => section.id === "watch-again")!;
    expect(model.sections.map((section) => section.id).indexOf("watch-again")).toBeLessThan(model.sections.map((section) => section.id).indexOf("top-picks"));
    expect(watchAgain.items).toEqual([recent, older]);
    expect(watchAgain.showReasons).toBe(false);
  });

  it("does not show Watch Again outside Recommended mode and never re-sorts All Releases", () => {
    const serverOrder = [movie("older", ["Drama"]), movie("newer", ["Drama"])];
    serverOrder[0].releaseYear = 1990; serverOrder[1].releaseYear = 2026;
    const all = buildCatalogPresentation({ feed: feed(serverOrder.map(item), "all", [item(serverOrder[1])]), fallbackMovies: serverOrder, continueWatching: [], view: "movies" });
    expect(all.gridItems).toEqual(serverOrder);
    expect(all.sections.some((section) => section.id === "watch-again")).toBe(false);
  });

  it("splits a Home genre by media type while archives keep one ordered grid", () => {
    const film = movie("film", ["Action"]);
    const show = movie("show", ["Action"], "series");
    const genreFeed = feed([item(film), item(show)], "Action");
    const home = buildCatalogPresentation({ feed: genreFeed, fallbackMovies: [film, show], continueWatching: [], view: "home" });
    const archive = buildCatalogPresentation({ feed: { ...genreFeed, scope: "movies", items: [item(film)], total: 1 }, fallbackMovies: [film], continueWatching: [], view: "movies" });
    expect(home.sections.map((section) => [section.title, section.items])).toEqual([["Movies", [film]], ["Series", [show]]]);
    expect(archive.gridItems).toEqual([film]);
  });
});
