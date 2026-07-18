import type { CatalogView } from "../../navigation/queryState";
import type { Movie } from "../../types/api";

export const RECOMMENDED_CATEGORY = "recommended";
export const ALL_RELEASES_CATEGORY = "all";
export const TOP_PICKS_LIMIT = 12;

export interface GenreCollection {
  genre: string;
  items: Movie[];
}

export interface CategoryOption {
  value: string;
  label: string;
  kind: "virtual" | "genre";
}

export interface CatalogSection {
  id: string;
  label: string;
  title: string;
  items: Movie[];
}

export interface CatalogPresentationModel {
  mode: "recommended" | "all" | "genre";
  activeCategory: string;
  activeLabel: string;
  categories: CategoryOption[];
  billboardItems: Movie[];
  sections: CatalogSection[];
  gridItems: Movie[];
  sourceItems: Movie[];
}

const categoryKey = (value: string) => value.trim().toLocaleLowerCase();

export function groupMoviesByGenre(items: Movie[], selectedGenre?: string): GenreCollection[] {
  const collections = new Map<string, Movie[]>();
  const selected = selectedGenre ? categoryKey(selectedGenre) : undefined;

  for (const movie of items) {
    const genres = movie.genres.length ? movie.genres : ["Uncategorized"];
    for (const genre of genres) {
      if (selected && categoryKey(genre) !== selected) continue;
      const existing = collections.get(genre) ?? [];
      existing.push(movie);
      collections.set(genre, existing);
    }
  }

  return Array.from(collections, ([genre, collectionItems]) => ({ genre, items: collectionItems }));
}

export function sortByReleaseYear(items: Movie[]): Movie[] {
  return items.map((movie, index) => ({ movie, index })).sort((left, right) => {
    const yearDifference = (right.movie.releaseYear || 0) - (left.movie.releaseYear || 0);
    return yearDifference || left.index - right.index;
  }).map(({ movie }) => movie);
}

export function categoryOptions(items: Movie[]): CategoryOption[] {
  const genres = Array.from(new Set(items.flatMap((movie) => movie.genres).filter(Boolean))).sort((left, right) => left.localeCompare(right));
  return [
    { value: RECOMMENDED_CATEGORY, label: "Recommended", kind: "virtual" },
    { value: ALL_RELEASES_CATEGORY, label: "All Releases", kind: "virtual" },
    ...genres.map((genre) => ({ value: genre, label: genre, kind: "genre" as const })),
  ];
}

function sourceForView(movies: Movie[], view: CatalogView): Movie[] {
  if (view === "movies") return movies.filter((movie) => movie.type === "movie");
  if (view === "series") return movies.filter((movie) => movie.type === "series");
  return movies;
}

function section(id: string, label: string, title: string, items: Movie[]): CatalogSection | null {
  return items.length ? { id, label, title, items } : null;
}

function compactSections(items: Array<CatalogSection | null>): CatalogSection[] {
  return items.filter((item): item is CatalogSection => item !== null);
}

export function buildCatalogPresentation({
  movies,
  continueWatching,
  view,
  category,
}: {
  movies: Movie[];
  continueWatching: Movie[];
  view: CatalogView;
  category?: string;
}): CatalogPresentationModel {
  const sourceItems = sourceForView(movies, view);
  const categories = categoryOptions(sourceItems);
  const requested = category?.trim() || RECOMMENDED_CATEGORY;
  const key = categoryKey(requested);

  if (key === ALL_RELEASES_CATEGORY) {
    const gridItems = sortByReleaseYear(sourceItems);
    return { mode: "all", activeCategory: ALL_RELEASES_CATEGORY, activeLabel: "All Releases", categories, billboardItems: gridItems, sections: [], gridItems, sourceItems };
  }

  if (key !== RECOMMENDED_CATEGORY) {
    const matchingGenre = categories.find((option) => option.kind === "genre" && categoryKey(option.value) === key);
    const activeCategory = matchingGenre?.value ?? requested;
    const activeLabel = matchingGenre?.label ?? requested;
    const matching = sourceItems.filter((movie) => movie.genres.some((genre) => categoryKey(genre) === key));
    const sections = view === "home" ? compactSections([
      section("genre-movies", "GENRE / MOVIES", "Movies", matching.filter((movie) => movie.type === "movie")),
      section("genre-series", "GENRE / SERIES", "Series", matching.filter((movie) => movie.type === "series")),
    ]) : [];
    return { mode: "genre", activeCategory, activeLabel, categories, billboardItems: matching, sections, gridItems: view === "home" ? [] : matching, sourceItems };
  }

  const topPicks = sourceItems.slice(0, TOP_PICKS_LIMIT);
  const sections = view === "home" ? compactSections([
    section("continue-watching", "RESUME INDEX", "Continue watching", continueWatching),
    section("top-picks", "PERSONALIZED / PROFILE", "Top Picks For You", topPicks),
    section("recommended-movies", "FEATURE ARCHIVE", "Movies", sourceItems.filter((movie) => movie.type === "movie")),
    section("recommended-series", "EPISODIC ARCHIVE", "Series", sourceItems.filter((movie) => movie.type === "series")),
  ]) : compactSections([
    section("top-picks", "PERSONALIZED / PROFILE", "Top Picks For You", topPicks),
    ...groupMoviesByGenre(sourceItems).map((collection) => section(`genre-${categoryKey(collection.genre)}`, `${view.toUpperCase()} / GENRE`, collection.genre, collection.items)),
  ]);
  return { mode: "recommended", activeCategory: RECOMMENDED_CATEGORY, activeLabel: "Recommended", categories, billboardItems: sourceItems, sections, gridItems: [], sourceItems };
}
