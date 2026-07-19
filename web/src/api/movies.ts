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

export function normalizeMovie(raw: Partial<Movie>): Movie {
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
    source: raw.source,
    availability: raw.availability,
    recommendationScore: raw.recommendationScore,
    recommendationReasons: Array.isArray(raw.recommendationReasons) ? raw.recommendationReasons : [],
    remoteThumbnailUrl: raw.remoteThumbnailUrl ?? null,
    remoteBannerUrl: raw.remoteBannerUrl ?? null,
    localThumbnailUrl: raw.localThumbnailUrl ?? null,
    localBannerUrl: raw.localBannerUrl ?? null,
    cacheState: raw.cacheState ?? null,
    viewerPreference: raw.viewerPreference ?? null,
  };
}

export async function getMovies(profileId?: string, signal?: AbortSignal): Promise<Movie[]> {
  const path = profileId ? `/api/movies?profile_id=${encodeURIComponent(profileId)}` : "/api/movies";
  const response = await apiGet<Partial<Movie>[]>(path, { signal });
  return response.map(normalizeMovie);
}

export async function getFeatured(): Promise<Movie | null> {
  const response = await apiGet<Partial<Movie> | null>("/api/movies/featured");
  return response ? normalizeMovie(response) : null;
}

export async function getMovie(mediaId: string, signal?: AbortSignal): Promise<Movie> {
  return normalizeMovie(await apiGet<Partial<Movie>>(`/api/movies/${encodeURIComponent(mediaId)}`, { signal }));
}

export const search = (query: string, signal?: AbortSignal) => apiGet<DiscoverMovie[]>(`/api/search?query=${encodeURIComponent(query)}`, { signal });
export const discover = (category: string, type: string) => apiGet<DiscoverMovie[]>(`/api/discover?category=${encodeURIComponent(category)}&type=${encodeURIComponent(type)}`);

export async function getEpisodes(tmdbId: number | string): Promise<Episode[]> {
  const response = await apiGet<Partial<Episode>[]>(`/api/series/${tmdbId}/episodes`);
  return response.map(normalizeEpisode);
}

export const getTmdbDetails = (type: string, id: number | string) => apiGet<Record<string, unknown>>(`/api/tmdb/${type}/${id}`);
