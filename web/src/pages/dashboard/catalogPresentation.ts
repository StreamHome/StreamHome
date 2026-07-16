import type { Movie } from "../../types/api";

export interface GenreCollection {
  genre: string;
  items: Movie[];
}

export function groupMoviesByGenre(items: Movie[], selectedGenre?: string): GenreCollection[] {
  const collections = new Map<string, Movie[]>();
  const selected = selectedGenre?.toLocaleLowerCase();

  for (const movie of items) {
    const genres = movie.genres.length ? movie.genres : ["Uncategorized"];
    for (const genre of genres) {
      if (selected && genre.toLocaleLowerCase() !== selected) continue;
      const existing = collections.get(genre) ?? [];
      existing.push(movie);
      collections.set(genre, existing);
    }
  }

  return Array.from(collections, ([genre, collectionItems]) => ({ genre, items: collectionItems }));
}
