import { apiDelete, apiGet, apiPost, apiPut } from "./client";
import { normalizeMovie } from "./movies";
import type { MediaPreference, RecommendationCategory, RecommendationDiagnostics, RecommendationFeed, RecommendationItem } from "../types/api";
import type { CatalogView } from "../navigation/queryState";

type RawRecommendationCategory = Partial<RecommendationCategory> & {
  server_count?: number;
  cached_count?: number;
};

type RawRecommendationItem = Partial<RecommendationItem> & {
  media?: Parameters<typeof normalizeMovie>[0];
};

type RawRecommendationFeed = Partial<Omit<RecommendationFeed, "categories" | "items" | "watchAgain">> & {
  profile_id?: string;
  generated_at?: number;
  categories?: RawRecommendationCategory[];
  items?: RawRecommendationItem[];
  watchAgain?: RawRecommendationItem[];
  watch_again?: RawRecommendationItem[];
};

export interface RecommendationRequest {
  profileId: string;
  scope: CatalogView;
  category?: string;
  limit?: number;
  offset?: number;
  signal?: AbortSignal;
}

function normalizeCategory(raw: RawRecommendationCategory): RecommendationCategory {
  return {
    value: raw.value ?? "",
    label: raw.label ?? raw.value ?? "",
    affinity: raw.affinity ?? 0,
    serverCount: raw.serverCount ?? raw.server_count ?? 0,
    cachedCount: raw.cachedCount ?? raw.cached_count ?? 0,
  };
}

function normalizeItem(raw: RawRecommendationItem): RecommendationItem {
  const source = raw.source ?? "tmdb_cache";
  const availability = raw.availability ?? "cached";
  const reasons = Array.isArray(raw.reasons) ? raw.reasons : [];
  const media = normalizeMovie(raw.media ?? {});
  media.source = source;
  media.availability = availability;
  media.recommendationScore = raw.score ?? 0;
  media.recommendationReasons = reasons;
  const viewerPreference = raw.viewerPreference ?? null;
  media.viewerPreference = viewerPreference;
  return { media, source, availability, score: raw.score ?? 0, reasons, viewerPreference, candidateSource: raw.candidateSource ?? "ranked", sourceConfidence: raw.sourceConfidence ?? 0.5 };
}

export async function getMediaPreferences(profileId: string, signal?: AbortSignal): Promise<Record<string, Exclude<MediaPreference, null>>> {
  const response = await apiGet<{ preferences: Record<string, Exclude<MediaPreference, null>> }>(`/api/recommendations/${encodeURIComponent(profileId)}/preferences`, { signal });
  return response.preferences ?? {};
}

export async function setMediaPreference(profileId: string, movieId: string, preference: MediaPreference): Promise<void> {
  await apiPut(`/api/recommendations/${encodeURIComponent(profileId)}/preferences/${encodeURIComponent(movieId)}`, { preference });
}

export interface RecommendationExposurePayload { movie_id: string; feed_generation: string; surface: string; scope: string; category: string; position: number }
export async function sendRecommendationExposures(profileId: string, exposures: RecommendationExposurePayload[]): Promise<void> {
  if (exposures.length) await apiPost(`/api/recommendations/${encodeURIComponent(profileId)}/exposures`, { exposures });
}

export async function getRecommendationDiagnostics(profileId: string): Promise<RecommendationDiagnostics> {
  const raw = await apiGet<Record<string, unknown>>(`/api/recommendations/${encodeURIComponent(profileId)}/diagnostics`);
  return {
    profileId,
    periodDays: Number(raw.periodDays ?? raw.period_days ?? 30),
    exposures: Number(raw.exposures ?? 0),
    detailsOpens: Number(raw.detailsOpens ?? raw.details_opens ?? 0),
    playbackStarts: Number(raw.playbackStarts ?? raw.playback_starts ?? 0),
    completions: Number(raw.completions ?? 0),
    playRate: Number(raw.playRate ?? raw.play_rate ?? 0),
    completionRate: Number(raw.completionRate ?? raw.completion_rate ?? 0),
    preferences: (raw.preferences ?? { like: 0, love: 0, dislike: 0 }) as RecommendationDiagnostics["preferences"],
    candidatePool: Number(raw.candidatePool ?? raw.candidate_pool ?? 0),
    candidateSources: (raw.candidateSources ?? raw.candidate_sources ?? {}) as Record<string, number>,
    catalog: (raw.catalog ?? { total: 0, available: 0, cached: 0 }) as RecommendationDiagnostics["catalog"],
    topTastes: (raw.topTastes ?? raw.top_tastes ?? []) as RecommendationDiagnostics["topTastes"],
  };
}
export const rebuildRecommendations = (profileId: string) => apiPost(`/api/recommendations/${encodeURIComponent(profileId)}/rebuild`);
export const clearMediaPreferences = (profileId: string) => apiDelete<{ cleared: number }>(`/api/recommendations/${encodeURIComponent(profileId)}/preferences`);
export async function getRecommendationOnboarding(profileId: string): Promise<{ genres: string[]; titleIds: string[] }> {
  const raw = await apiGet<{ genres?: string[]; titleIds?: string[]; title_ids?: string[] }>(`/api/recommendations/${encodeURIComponent(profileId)}/onboarding`);
  return { genres: raw.genres ?? [], titleIds: raw.titleIds ?? raw.title_ids ?? [] };
}
export const saveRecommendationOnboarding = (profileId: string, genres: string[], titleIds: string[] = []) => apiPut(`/api/recommendations/${encodeURIComponent(profileId)}/onboarding`, { genres, title_ids: titleIds });

export async function getRecommendations({
  profileId,
  scope,
  category = "recommended",
  limit = 48,
  offset = 0,
  signal,
}: RecommendationRequest): Promise<RecommendationFeed> {
  const params = new URLSearchParams({ scope, category, limit: String(limit), offset: String(offset) });
  const raw = await apiGet<RawRecommendationFeed>(`/api/recommendations/${encodeURIComponent(profileId)}?${params.toString()}`, { signal });
  const watchAgain = raw.watchAgain ?? raw.watch_again;
  return {
    profileId: raw.profileId ?? raw.profile_id ?? profileId,
    scope: raw.scope === "movies" || raw.scope === "series" ? raw.scope : "home",
    category: raw.category ?? category,
    generatedAt: raw.generatedAt ?? raw.generated_at ?? 0,
    stale: raw.stale ?? false,
    total: raw.total ?? 0,
    offset: raw.offset ?? offset,
    limit: raw.limit ?? limit,
    categories: Array.isArray(raw.categories) ? raw.categories.map(normalizeCategory).filter((item) => item.value) : [],
    items: Array.isArray(raw.items) ? raw.items.map(normalizeItem).filter((item) => item.media.id) : [],
    watchAgain: Array.isArray(watchAgain) ? watchAgain.map(normalizeItem).filter((item) => item.media.id) : [],
  };
}
