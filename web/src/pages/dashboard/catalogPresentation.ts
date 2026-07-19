import type { CatalogView } from "../../navigation/queryState";
import type { Movie, RecommendationCategory, RecommendationFeed } from "../../types/api";
import { ENABLE_VISUAL_GENRE_CATEGORIES } from "./catalogFeatures";

export const RECOMMENDED_CATEGORY = "recommended";
export const ALL_RELEASES_CATEGORY = "all";
export const TOP_PICKS_LIMIT = 12;

export interface CategoryOption {
  value: string;
  label: string;
  kind: "virtual" | "genre";
  count: number;
}

export interface GenreCategoryCard {
  value: string;
  label: string;
  count: number;
  representative: Movie;
}

export interface CatalogSection {
  id: string;
  label: string;
  title: string;
  items: Movie[];
  showReasons: boolean;
}

export interface CatalogPresentationModel {
  mode: "recommended" | "all" | "genre";
  activeCategory: string;
  activeLabel: string;
  categories: CategoryOption[];
  genreCards: GenreCategoryCard[];
  billboardItems: Movie[];
  sections: CatalogSection[];
  gridItems: Movie[];
  sourceItems: Movie[];
  total: number;
}

const categoryKey = (value: string) => value.trim().toLocaleLowerCase();
const isVirtual = (value: string) => value === RECOMMENDED_CATEGORY || value === ALL_RELEASES_CATEGORY;

export function categoryOptions(categories: RecommendationCategory[]): CategoryOption[] {
  return categories.map((category) => ({
    value: category.value,
    label: category.label,
    kind: isVirtual(categoryKey(category.value)) ? "virtual" : "genre",
    count: category.serverCount + category.cachedCount,
  }));
}

export function genreCategoryCards(categories: RecommendationCategory[], ranked: Movie[], fallback: Movie[]): GenreCategoryCard[] {
  const candidates = [...ranked, ...fallback.filter((movie) => !ranked.some((rankedMovie) => rankedMovie.id === movie.id))];
  return categoryOptions(categories)
    .filter((option) => option.kind === "genre")
    .map((option) => ({
      value: option.value,
      label: option.label,
      count: option.count,
      representative: candidates.find((movie) => movie.genres.some((genre) => categoryKey(genre) === categoryKey(option.value))),
    }))
    .filter((card): card is GenreCategoryCard => Boolean(card.representative));
}

function section(id: string, label: string, title: string, items: Movie[], showReasons: boolean): CatalogSection | null {
  return items.length ? { id, label, title, items, showReasons } : null;
}

function compact(items: Array<CatalogSection | null>): CatalogSection[] {
  return items.filter((item): item is CatalogSection => item !== null);
}

function personalizedGenreSections(categories: CategoryOption[], items: Movie[], view: CatalogView): CatalogSection[] {
  return categories.filter((category) => category.kind === "genre").map((category) => {
    const matching = items.filter((movie) => movie.genres.some((genre) => categoryKey(genre) === categoryKey(category.value)));
    return section(`genre-${categoryKey(category.value)}`, `${view.toUpperCase()} / PERSONALIZED GENRE`, category.label, matching, true);
  }).filter((item): item is CatalogSection => item !== null);
}

export function buildCatalogPresentation({
  feed,
  fallbackMovies,
  continueWatching,
  view,
}: {
  feed: RecommendationFeed;
  fallbackMovies: Movie[];
  continueWatching: Movie[];
  view: CatalogView;
}): CatalogPresentationModel {
  const sourceItems = feed.items.map((item) => item.media);
  const watchAgain = feed.watchAgain.map((item) => item.media);
  const categories = categoryOptions(feed.categories);
  const genreCards = ENABLE_VISUAL_GENRE_CATEGORIES
    ? genreCategoryCards(feed.categories, sourceItems, fallbackMovies)
    : [];
  const requested = feed.category?.trim() || RECOMMENDED_CATEGORY;
  const key = categoryKey(requested);
  const active = categories.find((category) => categoryKey(category.value) === key);
  const common = {
    activeCategory: active?.value ?? requested,
    activeLabel: active?.label ?? requested,
    categories,
    genreCards,
    billboardItems: sourceItems,
    sourceItems,
    total: feed.total,
  };

  if (key === ALL_RELEASES_CATEGORY) return { ...common, mode: "all", gridItems: sourceItems, sections: [] };

  if (key !== RECOMMENDED_CATEGORY) {
    const sections = view === "home" ? compact([
      section("genre-movies", "GENRE / MOVIES", "Movies", sourceItems.filter((movie) => movie.type === "movie"), true),
      section("genre-series", "GENRE / SERIES", "Series", sourceItems.filter((movie) => movie.type === "series"), true),
    ]) : [];
    return { ...common, mode: "genre", sections, gridItems: view === "home" ? [] : sourceItems };
  }

  const topPicks = sourceItems.slice(0, TOP_PICKS_LIMIT);
  const leading = view === "home" ? compact([
    section("continue-watching", "RESUME INDEX", "Continue Watching", continueWatching, false),
    section("watch-again", "RECENT REWATCH HISTORY", "Watch Again", watchAgain, false),
    section("top-picks", "PERSONALIZED / PROFILE", "Top Picks For You", topPicks, true),
    section("recommended-movies", "PERSONALIZED MOVIES", "Movies For You", sourceItems.filter((movie) => movie.type === "movie"), true),
    section("recommended-series", "PERSONALIZED SERIES", "Series For You", sourceItems.filter((movie) => movie.type === "series"), true),
  ]) : compact([
    section("watch-again", "RECENT REWATCH HISTORY", "Watch Again", watchAgain, false),
    section("top-picks", "PERSONALIZED / PROFILE", "Top Picks For You", topPicks, true),
  ]);
  return { ...common, mode: "recommended", sections: [...leading, ...personalizedGenreSections(categories, sourceItems, view)], gridItems: [] };
}
