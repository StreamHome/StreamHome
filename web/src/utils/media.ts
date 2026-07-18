import type { Episode, Movie } from "../types/api";
import type { ThemeId } from "../types/theme";

export const SUPPORTED_THEMES: ThemeId[] = ["ember", "aurora", "cinema", "gemini"];

export function normalizeTheme(theme: string | null | undefined): ThemeId {
  return SUPPORTED_THEMES.includes(theme as ThemeId) ? (theme as ThemeId) : "ember";
}

export function isServerArtworkUrl(value: string | null | undefined): boolean {
  if (!value) return false;
  if (value.startsWith("/media/")) return true;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

export type ArtworkMediaIdentity = Pick<Movie, "id" | "title" | "type" | "releaseYear"> & Partial<Pick<Movie, "bannerUrl" | "localThumbnailUrl" | "localBannerUrl" | "remoteThumbnailUrl" | "remoteBannerUrl" | "cacheState">>;
export type ArtworkEpisodeIdentity = Pick<Episode, "seasonNumber" | "episodeNumber">;

const COMPACT_ARTWORK_PATH = /^\/(poster|backdrop|thumbnail)\.(?:jpe?g|png|webp)$/i;

function mediaFolderTitle(title: string): string {
  return title.replace(/[^\p{L}\p{N} ._-]/gu, "");
}

function mediaPath(...segments: Array<string | number>): string {
  return `/${segments.map((segment) => encodeURIComponent(String(segment))).join("/")}`;
}

export function serverArtworkCandidates(
  value: string | null | undefined,
  media?: ArtworkMediaIdentity,
  episode?: ArtworkEpisodeIdentity,
): string[] {
  if (value && isServerArtworkUrl(value)) return [value];
  if (!value || !media || !COMPACT_ARTWORK_PATH.test(value)) return [];

  const idMatch = media.id.match(/^(?:m_|tv_)(\d+)$/);
  if (!idMatch) return [];
  const tmdbId = idMatch[1];
  const title = mediaFolderTitle(media.title);
  const filename = value.slice(1);
  if (!title) return [];

  if (media.type === "series") {
    const folder = `${title}_TMDB_${tmdbId}`;
    const candidates: string[] = [];
    if (episode) candidates.push(mediaPath("media", "Series", folder, `Season_${episode.seasonNumber}`, `Episode_${episode.episodeNumber}`, filename));
    candidates.push(mediaPath("media", "Series", folder, filename));
    return candidates;
  }

  const years = [media.releaseYear, media.releaseYear - 1, media.releaseYear + 1]
    .filter((year, index, items) => year > 0 && items.indexOf(year) === index);
  return years.map((year) => mediaPath("media", "Movies", `${title}_${year}_TMDB_${tmdbId}`, filename));
}

export function isPlayableMovie(movie: Movie): boolean {
  if (movie.type === "series") return Boolean(movie.episodes?.some((episode) => episode.videoUrl));
  return Boolean(movie.videoUrl);
}

export function mediaAvailability(movie: Movie): "available" | "processing" | "cached" {
  if (movie.availability === "available" || movie.availability === "processing" || movie.availability === "cached") return movie.availability;
  return isPlayableMovie(movie) ? "available" : "cached";
}

export function isAvailableMedia(movie: Movie): boolean {
  return mediaAvailability(movie) === "available";
}

export function tmdbIdFromMovie(movie: Movie): number | null {
  const match = movie.id.match(/^(?:tv_|m_)(\d+)$/);
  return match ? Number(match[1]) : null;
}

export function completionFraction(completionRate: number | null | undefined): number {
  return Math.min(Math.max(completionRate ?? 0, 0), 1);
}

export function downloadFraction(progress: number): number {
  return Math.min(Math.max(progress / 100, 0), 1);
}
