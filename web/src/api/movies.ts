import { apiGet } from "./client";
import type { DiscoverMovie, Episode, MediaAnalysis, Movie } from "../types/api";
import { normalizeSkipMarkers } from "./skipMarkers";

type EpisodeResponse = Omit<Partial<Episode>, "skipMarkers"> & { skipMarkers?: unknown };
type MovieResponse = Omit<Partial<Movie>, "skipMarkers" | "episodes"> & {
  skipMarkers?: unknown;
  episodes?: EpisodeResponse[] | null;
};

export function normalizeAnalysis(raw: unknown): MediaAnalysis | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const value = raw as Record<string, unknown>;
  const number = (input: unknown, allowZero = false): number | null =>
    typeof input === "number" && Number.isFinite(input) && (allowZero ? input >= 0 : input > 0) ? input : null;
  const durationProvenance = value.duration_provenance ?? value.durationProvenance;
  return {
    quality: typeof value.quality === "string" ? value.quality : "Source",
    qualityProvenance: String(value.quality_provenance ?? value.qualityProvenance ?? "unknown"),
    senderQuality: typeof (value.sender_quality ?? value.senderQuality) === "string" ? String(value.sender_quality ?? value.senderQuality) : null,
    width: number(value.width), height: number(value.height),
    videoStreamIndex: number(value.video_stream_index ?? value.videoStreamIndex, true),
    frameRate: number(value.frame_rate ?? value.frameRate),
    videoBitrate: number(value.video_bitrate ?? value.videoBitrate),
    containerBitrate: number(value.container_bitrate ?? value.containerBitrate),
    bitrateUnit: "bits_per_second",
    durationSeconds: number(value.probed_duration ?? value.durationSeconds),
    durationProvenance: typeof durationProvenance === "string" ? durationProvenance : "unknown",
  };
}

function normalizeEpisode(raw: EpisodeResponse): Episode {
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
    analysis: normalizeAnalysis(raw.analysis),
    languages: Array.isArray(raw.languages) ? raw.languages : [],
    subtitles: Array.isArray(raw.subtitles) ? raw.subtitles : [],
    skipMarkers: normalizeSkipMarkers(raw.skipMarkers),
    dialogueWpm: raw.dialogueWpm ?? null,
    dialogueConfidence: raw.dialogueConfidence ?? 0,
  };
}

export function normalizeMovie(raw: MovieResponse): Movie {
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
    analysis: normalizeAnalysis(raw.analysis),
    languages: Array.isArray(raw.languages) ? raw.languages : [],
    subtitles: Array.isArray(raw.subtitles) ? raw.subtitles : [],
    voteAverage: raw.voteAverage ?? 0,
    voteCount: raw.voteCount ?? 0,
    skipMarkers: normalizeSkipMarkers(raw.skipMarkers),
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
    crew: Array.isArray(raw.crew) ? raw.crew : [],
    tropeVectors: Array.isArray(raw.tropeVectors) ? raw.tropeVectors : [],
    dialogueWpm: raw.dialogueWpm ?? null,
    dialogueConfidence: raw.dialogueConfidence ?? 0,
    recommendationReasonDetails: Array.isArray(raw.recommendationReasonDetails) ? raw.recommendationReasonDetails : [],
  };
}

export async function getMovies(profileId?: string, signal?: AbortSignal): Promise<Movie[]> {
  const path = profileId ? `/api/movies?profile_id=${encodeURIComponent(profileId)}` : "/api/movies";
  const response = await apiGet<MovieResponse[]>(path, { signal });
  return response.map(normalizeMovie);
}

export async function getFeatured(): Promise<Movie | null> {
  const response = await apiGet<MovieResponse | null>("/api/movies/featured");
  return response ? normalizeMovie(response) : null;
}

export async function getMovie(mediaId: string, signal?: AbortSignal): Promise<Movie> {
  return normalizeMovie(await apiGet<MovieResponse>(`/api/movies/${encodeURIComponent(mediaId)}`, { signal }));
}

export const search = (query: string, signal?: AbortSignal) => apiGet<DiscoverMovie[]>(`/api/search?query=${encodeURIComponent(query)}`, { signal });
export const discover = (category: string, type: string) => apiGet<DiscoverMovie[]>(`/api/discover?category=${encodeURIComponent(category)}&type=${encodeURIComponent(type)}`);

export async function getEpisodes(tmdbId: number | string, signal?: AbortSignal): Promise<Episode[]> {
  const response = await apiGet<EpisodeResponse[]>(`/api/series/${tmdbId}/episodes`, { signal });
  return response.map(normalizeEpisode);
}

export const getTmdbDetails = (type: string, id: number | string) => apiGet<Record<string, unknown>>(`/api/tmdb/${type}/${id}`);
