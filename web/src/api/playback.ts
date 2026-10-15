import { apiGet, apiPost } from "./client";
import type {
  PlaybackProgressRequest,
  PlaybackProgressResponse,
  PlaybackStartupDiagnosticRequest,
  PlaybackSession,
  PlaybackRunResponse,
} from "../types/api";

export const getPlaybackSessions = (profileId: string, signal?: AbortSignal) => apiGet<PlaybackSession[]>(`/api/track/${profileId}`, { signal });

export function normalizePlaybackRun(response: PlaybackRunResponse): PlaybackRunResponse {
  const expected = `/api/playback/streams/${encodeURIComponent(response.runId)}`;
  if (!response.streamBaseUrl) return response;
  if (response.streamBaseUrl !== expected) return { ...response, streamBaseUrl: null };
  return {
    ...response,
    manifestUrl: response.manifestUrl ? `${expected}/manifest` : null,
    progressiveUrl: response.progressiveUrl ? `${expected}/progressive` : "",
    tracks: response.tracks.map((track) => ({
      ...track,
      directUrl: track.directUrl ? `${expected}/source/${encodeURIComponent(track.id)}` : track.directUrl,
    })),
  };
}

export const createPlaybackRun = (movieId: string, profileId: string, episodeId?: string, signal?: AbortSignal) =>
  apiPost<PlaybackRunResponse>("/api/playback/runs", {
    movie_id: movieId,
    profile_id: profileId,
    episode_id: episodeId,
  }, { signal }).then(normalizePlaybackRun);

export const getPlaybackRun = (runId: string, options?: { retry?: boolean; signal?: AbortSignal }) =>
  apiGet<PlaybackRunResponse>(`/api/playback/runs/${runId}${options?.retry ? "?retry=true" : ""}`, { signal: options?.signal }).then(normalizePlaybackRun);

export const updatePlaybackProgress = (runId: string, progress: PlaybackProgressRequest, keepalive = false) =>
  apiPost<PlaybackProgressResponse>(`/api/playback/runs/${runId}/progress`, {
    timestamp: progress.timestamp,
    total_seconds_played: progress.totalSecondsPlayed,
    duration_watched: progress.durationWatched,
    is_finished: progress.isFinished,
    sequence_number: progress.sequenceNumber,
    event: progress.event,
  }, { keepalive, timeoutMs: 5_000 });

export const closePlaybackRun = (runId: string, progress: PlaybackProgressRequest, discardProgress = false) =>
  apiPost<PlaybackProgressResponse>(`/api/playback/runs/${runId}/close`, {
    timestamp: progress.timestamp,
    total_seconds_played: progress.totalSecondsPlayed,
    duration_watched: progress.durationWatched,
    is_finished: progress.isFinished,
    sequence_number: progress.sequenceNumber,
    event: "exit",
    discard_progress: discardProgress || undefined,
  }, { keepalive: true });

export const startOverPlaybackRun = (runId: string) =>
  apiPost<{ status: string }>(`/api/playback/runs/${runId}/start-over`);

export const reportPlaybackStartupDiagnostic = (runId: string, diagnostic: PlaybackStartupDiagnosticRequest) =>
  apiPost<void>(`/api/playback/runs/${runId}/diagnostics`, {
    transport: diagnostic.transport,
    stage: diagnostic.stage,
    error_type: diagnostic.errorType,
    error_detail: diagnostic.errorDetail,
    http_status: diagnostic.httpStatus,
    ready_state: diagnostic.readyState,
    network_state: diagnostic.networkState,
    current_time: diagnostic.currentTime,
    buffered_until: diagnostic.bufferedUntil,
    elapsed_ms: diagnostic.elapsedMs,
  });
