import { ApiError, apiGet, apiPost } from "./client";
import type { WatchlistToggleResponse } from "../types/api";

const validIds = (value: unknown): value is string[] => Array.isArray(value) && value.every((id) => typeof id === "string" && id.trim().length > 0);
export async function getWatchlist(profileId: string, signal?: AbortSignal): Promise<string[]> {
  const value = await apiGet<string[]>(`/api/watchlist/${profileId}`, { signal });
  if (!validIds(value)) throw new ApiError("The server returned an invalid saved list. Try loading it again.", 0, "invalid_response");
  return value;
}
export async function toggleWatchlist(profileId: string, movieId: string): Promise<WatchlistToggleResponse> {
  const value = await apiPost<WatchlistToggleResponse>("/api/watchlist/toggle", { profile_id: profileId, movie_id: movieId });
  if (!value || !validIds(value.watchlist) || !["added", "removed"].includes(value.status)) {
    throw new ApiError("The saved-list update returned an incomplete response. Check your list before trying again.", 0, "invalid_response");
  }
  return value;
}
