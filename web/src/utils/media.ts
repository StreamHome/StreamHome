import type { Movie, Profile } from "../types/api";
import type { ThemeId } from "../types/theme";

export const SUPPORTED_THEMES: ThemeId[] = ["ember", "aurora", "cinema", "gemini"];

export function normalizeTheme(theme: string | null | undefined): ThemeId {
  if (theme === "netflix") return "cinema";
  return SUPPORTED_THEMES.includes(theme as ThemeId) ? (theme as ThemeId) : "ember";
}

export function isServerArtworkUrl(value: string | null | undefined): value is string {
  if (!value) return false;
  if (value.startsWith("/media/")) return true;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

export function isPlayableMovie(movie: Movie): boolean {
  if (movie.type === "series") return Boolean(movie.episodes?.some((episode) => episode.videoUrl));
  return Boolean(movie.videoUrl);
}

export function tmdbIdFromMovie(movie: Movie): number | null {
  const match = movie.id.match(/^(?:tv_|m_)(\d+)$/);
  return match ? Number(match[1]) : null;
}

const AVATAR_PALETTES = [
  "linear-gradient(135deg, #2563eb, #4f46e5)",
  "linear-gradient(135deg, #ea580c, #dc2626)",
  "linear-gradient(135deg, #059669, #0d9488)",
  "linear-gradient(135deg, #9333ea, #db2777)",
];

export function avatarBackground(profile: Pick<Profile, "id">): string {
  let hash = 0;
  for (const char of profile.id) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return AVATAR_PALETTES[hash % AVATAR_PALETTES.length];
}

export function completionFraction(completionRate: number | null | undefined): number {
  return Math.min(Math.max(completionRate ?? 0, 0), 1);
}

export function downloadFraction(progress: number): number {
  return Math.min(Math.max(progress / 100, 0), 1);
}
