import { apiGet } from "./client";
import type { DiscoverMovie, Episode, Movie } from "../types/api";

function normalizeEpisode(raw: Partial<Episode>): Episode {
  return {
    id: raw.id ?? "",
    movieId: raw.movieId,
    episodeNumber: raw.episodeNumber ?? 0,
    seasonNumber: raw.seasonNumber ?? 0,
    title: raw.title ?? "",
    description: raw.description ?? "",
    thumbnailUrl: raw.thumbnailUrl ?? "",
    videoUrl: raw.videoUrl ?? "",
    duration: raw.duration ?? "",
    quality: raw.quality ?? "Source",
    languages: Array.isArray(raw.languages) ? raw.languages : [],
    subtitles: Array.isArray(raw.subtitles) ? raw.subtitles : [],
    skipMarkers: raw.skipMarkers && typeof raw.skipMarkers === "object" ? raw.skipMarkers : {},
  };
}

function normalizeMovie(raw: Partial<Movie>): Movie {
  return {
    id: raw.id ?? "",
    title: raw.title ?? "",
    description: raw.description ?? "",
    thumbnailUrl: raw.thumbnailUrl ?? "",
    bannerUrl: raw.bannerUrl ?? null,
    videoUrl: raw.videoUrl ?? "",
    genres: Array.isArray(raw.genres) ? raw.genres : [],
    duration: raw.duration ?? "",
    releaseYear: raw.releaseYear ?? 0,
    rating: raw.rating ?? null,
    cast: Array.isArray(raw.cast) ? raw.cast : [],
    director: raw.director ?? null,
    type: raw.type === "series" ? "series" : "movie",
    quality: raw.quality ?? "Source",
    languages: Array.isArray(raw.languages) ? raw.languages : [],
    subtitles: Array.isArray(raw.subtitles) ? raw.subtitles : [],
    voteAverage: raw.voteAverage ?? 0,
    voteCount: raw.voteCount ?? 0,
    skipMarkers: raw.skipMarkers && typeof raw.skipMarkers === "object" ? raw.skipMarkers : {},
    episodes: Array.isArray(raw.episodes) ? raw.episodes.map(normalizeEpisode) : null,
  };
}

export async function getMovies(profileId?: string): Promise<Movie[]> {
  const path = profileId ? `/api/movies?profile_id=${encodeURIComponent(profileId)}` : "/api/movies";
  const response = await apiGet<Partial<Movie>[]>(path);
  return response.map(normalizeMovie);
}

export async function getFeatured(): Promise<Movie | null> {
  const response = await apiGet<Partial<Movie> | null>("/api/movies/featured");
  return response ? normalizeMovie(response) : null;
}

export const search = (query: string) => apiGet<DiscoverMovie[]>(`/api/search?query=${encodeURIComponent(query)}`);
export const discover = (category: string, type: string) => apiGet<DiscoverMovie[]>(`/api/discover?category=${encodeURIComponent(category)}&type=${encodeURIComponent(type)}`);

export async function getEpisodes(tmdbId: number | string): Promise<Episode[]> {
  const response = await apiGet<Partial<Episode>[]>(`/api/series/${tmdbId}/episodes`);
  return response.map(normalizeEpisode);
}

export const getTmdbDetails = (type: string, id: number | string) => apiGet<Record<string, unknown>>(`/api/tmdb/${type}/${id}`);
