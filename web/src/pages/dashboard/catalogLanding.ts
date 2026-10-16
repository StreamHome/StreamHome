import type { CatalogView } from "../../navigation/queryState";
import type { Movie, RecommendationFeed } from "../../types/api";
import { buildCatalogPresentation, TOP_PICKS_LIMIT, type CatalogPresentationModel, type CatalogSection } from "./catalogPresentation";
import { mediaAvailability } from "../../utils/media";

export interface CatalogLandingModel extends CatalogPresentationModel {
  source: "catalog" | "recommendations";
  feedGeneration: string;
}

const key = (value: string) => value.trim().toLocaleLowerCase();
const inView = (movie: Movie, view: CatalogView) => view === "home" || movie.type === (view === "movies" ? "movie" : "series");
const unique = (items: Movie[]) => {
  const seen = new Set<string>();
  return items.filter((movie) => !seen.has(movie.id) && Boolean(seen.add(movie.id)));
};

/** Render server records in one landing layout; never manufacture recommendation scores or reasons. */
export function buildCatalogLanding({ feed, catalog, continueWatching, view, category = "recommended", preferences = {} }: {
  feed: RecommendationFeed | null;
  catalog: Movie[];
  continueWatching: Movie[];
  view: CatalogView;
  category?: string;
  preferences?: Record<string, string>;
}): CatalogLandingModel {
  const categoryId = key(category);
  const usableFeed = feed && (feed.items.length || feed.watchAgain.length || feed.vibeRails?.some((rail) => rail.items.length));
  // A successful explicit category response owns even an empty result.
  if (feed && (usableFeed || categoryId !== "recommended")) {
    const model = buildCatalogPresentation({ feed, fallbackMovies: catalog, continueWatching, view });
    const supplemental = [...(feed.vibeRails ?? []).flatMap((rail) => rail.items.map((item) => item.media)), ...feed.watchAgain.map((item) => item.media)];
    return { ...model, billboardItems: model.billboardItems.length || categoryId !== "recommended" ? model.billboardItems : unique(supplemental).filter((movie) => inView(movie, view)), source: "recommendations", feedGeneration: String(feed.generatedAt) };
  }
  const scoped = unique(catalog).filter((movie) => inView(movie, view));
  const eligible = categoryId === "all" ? scoped.filter((movie) => mediaAvailability(movie) === "available") : scoped.filter((movie) => preferences[movie.id] !== "dislike" && movie.viewerPreference !== "dislike");
  const items = categoryId === "recommended" || categoryId === "all" ? eligible : eligible.filter((movie) => movie.genres.some((genre) => key(genre) === categoryId));
  const genres = [...new Map(eligible.flatMap((movie) => movie.genres).map((genre) => [key(genre), genre])).values()];
  const categories = [
    { value: "recommended", label: "Recommended", kind: "virtual" as const, count: eligible.length },
    { value: "all", label: "All Releases", kind: "virtual" as const, count: scoped.filter((movie) => mediaAvailability(movie) === "available").length },
    ...genres.map((genre) => ({ value: genre, label: genre, kind: "genre" as const, count: eligible.filter((movie) => movie.genres.some((value) => key(value) === key(genre))).length })),
  ];
  const sections: CatalogSection[] = [];
  const add = (id: string, title: string, entries: Movie[], label = "LIBRARY") => {
    if (entries.length) sections.push({ id, title, label, items: entries, showReasons: false, kind: "standard" });
  };
  if (categoryId === "recommended") {
    if (view === "home") add("continue-watching", "Continue Watching", continueWatching.filter((movie) => scoped.some((item) => item.id === movie.id)), "RESUME");
    add("top-picks", view === "home" ? "Featured Titles" : view === "movies" ? "Featured Movies" : "Featured Series", items.slice(0, TOP_PICKS_LIMIT), "FEATURED");
    if (view === "home") {
      add("recommended-movies", "Movies", items.filter((movie) => movie.type === "movie").slice(0, TOP_PICKS_LIMIT));
      add("recommended-series", "Series", items.filter((movie) => movie.type === "series").slice(0, TOP_PICKS_LIMIT));
    }
    genres.slice(0, 8).forEach((genre) => add(`genre-${key(genre)}`, genre, items.filter((movie) => movie.genres.some((value) => key(value) === key(genre))).slice(0, TOP_PICKS_LIMIT), "GENRE"));
  } else if (categoryId !== "all" && view === "home") {
    add("genre-movies", "Movies", items.filter((movie) => movie.type === "movie"), "GENRE / MOVIES");
    add("genre-series", "Series", items.filter((movie) => movie.type === "series"), "GENRE / SERIES");
  }
  return {
    source: "catalog", feedGeneration: "", mode: categoryId === "recommended" ? "recommended" : categoryId === "all" ? "all" : "genre",
    activeCategory: category, activeLabel: categories.find((item) => key(item.value) === categoryId)?.label ?? category,
    categories, genreCards: [], billboardItems: items, sourceItems: items, total: items.length, sections,
    gridItems: categoryId === "all" || (categoryId !== "recommended" && view !== "home") ? items : [],
  };
}
