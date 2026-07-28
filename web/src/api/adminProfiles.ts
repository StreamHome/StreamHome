import { apiGet } from "./client";
import type {
  AdminProfileActivity,
  AdminProfileCache,
  AdminProfileData,
  AdminProfileHistory,
  AdminProfileOverview,
  AdminProfileRecommendations,
  AdminProfileSummary,
  AdminProfileWatchlist,
} from "../types/api";

export interface AdminProfileSummariesResponse {
  profiles: AdminProfileSummary[];
  storage: {
    database: string;
    durableMedia: string;
    temporaryCaches: string;
    browserPending: string;
  };
}

export const getAdminProfileSummaries = (signal?: AbortSignal) =>
  apiGet<AdminProfileSummariesResponse>("/api/admin/profiles", { signal });

export async function getAdminProfileData(profileId: string, signal?: AbortSignal): Promise<AdminProfileData> {
  const encoded = encodeURIComponent(profileId);
  const options = { signal };
  const [overview, history, watchlist, recommendations, activity, cache] = await Promise.all([
    apiGet<AdminProfileOverview>(`/api/admin/profiles/${encoded}/overview`, options),
    apiGet<AdminProfileHistory>(`/api/admin/profiles/${encoded}/history?limit=100`, options),
    apiGet<AdminProfileWatchlist>(`/api/admin/profiles/${encoded}/watchlist?limit=200`, options),
    apiGet<AdminProfileRecommendations>(`/api/admin/profiles/${encoded}/recommendations?limit=100`, options),
    apiGet<AdminProfileActivity>(`/api/admin/profiles/${encoded}/activity?limit=100`, options),
    apiGet<AdminProfileCache>(`/api/admin/profiles/${encoded}/cache?limit=200`, options),
  ]);
  return { overview, history, watchlist, recommendations, activity, cache };
}
