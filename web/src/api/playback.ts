import { apiGet, apiPost } from "./client";
import type { PlaybackSession, TrackPlaybackRequest } from "../types/api";

export const getPlaybackSessions = (profileId: string) => apiGet<PlaybackSession[]>(`/api/track/${profileId}`);
export const trackPlayback = (data: TrackPlaybackRequest) => apiPost<{ status: string; updatedAt: string }>("/api/track", {
  movieId: data.movieId,
  profileId: data.profileId,
  timestamp: data.timestamp,
  duration_watched: data.durationWatched ?? 0,
  completion_rate: data.completionRate ?? 0,
  episodeId: data.episodeId,
  is_finished: data.isFinished ?? false,
});
