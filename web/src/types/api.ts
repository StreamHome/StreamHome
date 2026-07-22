export interface LoginRequest {
  email: string;
  password: string;
}

export interface LoginResponse {
  email: string;
  session?: { id: string; expiresAt: number };
  previousLogin?: LoginRecord | null;
}

export interface TwoFARequiredResponse {
  requires2fa: true;
  email: string;
  challengeToken: string;
  expiresInSeconds: number;
  message: string;
}

export type AuthResponse = LoginResponse | TwoFARequiredResponse;

export interface VerifyRequest {
  challengeToken: string;
  method: "totp" | "recovery";
  code: string;
}

export interface LoginRecord { at: number; ipAddress?: string | null; deviceLabel?: string | null }
export interface HealthResponse { status: "ready"; version: string; serverTime: number }
export interface ReauthResponse { reauthenticated: true; validForSeconds: number }
export interface SecuritySummary { email: string; twoFactorEnabled: boolean; recoveryCodesRemaining: number; sessionLifetimeDays: number; previousLogin: LoginRecord | null }
export interface AccountEmailUpdateResponse { message: string; email: string; otherSessionsRevoked: number }
export interface AccountSecurityUpdateResponse { message: string; otherSessionsRevoked: number }
export interface SessionPolicyUpdateResponse { message: string; sessionLifetimeDays: number; existingSessionsChanged: false }
export interface AuthSessionInfo { id: string; createdAt: number; lastSeenAt: number; expiresAt: number; ipAddress: string; deviceLabel: string; current: boolean }
export interface SecurityEventInfo { id: string; type: string; outcome: string; createdAt: number; ipAddress: string; deviceLabel: string; details?: Record<string, unknown> | null }
export interface SecurityEventsResponse { events: SecurityEventInfo[]; nextCursor: number | null }

export interface TwoFAStatusResponse {
  twoFactorEnabled: boolean;
  email: string;
}

export interface TwoFASetupResponse {
  secret: string;
  provisioningUri: string;
}

export interface Profile {
  id: string;
  name: string;
  avatarColor: string;
  theme: string | null;
  pinEnabled: boolean;
  pin: string | null;
}

export interface CreateProfileRequest {
  id: string;
  name: string;
  avatarColor?: string;
  theme?: string;
  pinEnabled?: boolean;
  pin?: string | null;
}

export type SaveProfileRequest = CreateProfileRequest;

export interface SubtitleInfo {
  language: string;
  extension?: string;
  url?: string;
  path?: string;
}

export interface Episode {
  id: string;
  movieId?: string;
  episodeNumber: number;
  seasonNumber: number;
  title: string;
  description: string;
  thumbnailUrl: string;
  videoUrl: string;
  duration: string;
  quality: string;
  languages: string[];
  subtitles: SubtitleInfo[];
  skipMarkers: Record<string, unknown>;
  dialogueWpm?: number | null;
  dialogueConfidence?: number;
}

export interface MediaCrewMember { name: string; roles: string[] }
export interface MediaTropeVector { id: string; label: string; railLabel: string; confidence: number; registryVersion?: number }
export interface RecommendationReasonDetail { code: string; subject?: string; strength?: number; fallbackText: string }

export interface Movie {
  id: string;
  title: string;
  description: string;
  thumbnailUrl: string;
  bannerUrl: string | null;
  videoUrl: string;
  genres: string[];
  duration: string;
  releaseYear: number;
  rating: string | null;
  cast: string[];
  director: string | null;
  type: "movie" | "series";
  quality: string;
  languages: string[];
  subtitles: SubtitleInfo[];
  voteAverage: number;
  voteCount: number;
  skipMarkers: Record<string, unknown>;
  episodes?: Episode[] | null;
  source?: MediaSource;
  availability?: MediaAvailability;
  recommendationScore?: number;
  recommendationReasons?: string[];
  remoteThumbnailUrl?: string | null;
  remoteBannerUrl?: string | null;
  localThumbnailUrl?: string | null;
  localBannerUrl?: string | null;
  cacheState?: MediaCacheState | null;
  viewerPreference?: MediaPreference;
  crew?: MediaCrewMember[];
  tropeVectors?: MediaTropeVector[];
  dialogueWpm?: number | null;
  dialogueConfidence?: number;
  recommendationReasonDetails?: RecommendationReasonDetail[];
}

export type MediaSource = "server" | "tmdb_cache" | string;
export type MediaAvailability = "available" | "processing" | "cached" | string;
export type MediaCacheState = "queued" | "caching" | "ready" | "error";
export type MediaPreference = "like" | "love" | "dislike" | null;

export interface RecommendationCategory {
  value: string;
  label: string;
  affinity: number;
  serverCount: number;
  cachedCount: number;
}

export interface RecommendationItem {
  media: Movie;
  source: MediaSource;
  availability: MediaAvailability;
  score: number;
  reasons: string[];
  reasonDetails?: RecommendationReasonDetail[];
  viewerPreference?: MediaPreference;
  candidateSource?: string;
  sourceConfidence?: number;
}

export interface RecommendationVibeRail {
  id: string;
  label: string;
  tropeIds: string[];
  reasonCode: string;
  items: RecommendationItem[];
}

export interface RecommendationFeed {
  profileId: string;
  scope: "home" | "movies" | "series";
  category: string;
  generatedAt: number;
  stale: boolean;
  total: number;
  offset: number;
  limit: number;
  categories: RecommendationCategory[];
  items: RecommendationItem[];
  watchAgain: RecommendationItem[];
  vibeRails?: RecommendationVibeRail[];
  algorithmVersion?: string;
}

export interface RecommendationDiagnostics {
  profileId: string;
  periodDays: number;
  exposures: number;
  detailsOpens: number;
  playbackStarts: number;
  completions: number;
  playRate: number;
  completionRate: number;
  preferences: Record<"like" | "love" | "dislike", number>;
  candidatePool: number;
  candidateSources: Record<string, number>;
  catalog: { total: number; available: number; cached: number };
  topTastes: Array<{ kind: string; value: string; score: number }>;
  vibeAnalysis?: { algorithmVersion: string; analyzerVersion: number; crewCoverage: number; tropeCoverage: number; pacingCoverage: number };
}

export interface PlaybackSession {
  movieId: string;
  profileId: string;
  timestamp: number;
  durationWatched: number;
  completionRate: number;
  updatedAt: string;
  episodeId: string | null;
  isFinished: boolean;
}

export interface TrackPlaybackRequest {
  movieId: string;
  profileId: string;
  timestamp: number;
  durationWatched?: number;
  completionRate?: number;
  episodeId?: string;
  isFinished?: boolean;
}

export interface WatchlistToggleResponse {
  status: "added" | "removed";
  watchlist: string[];
}

export interface SystemSettings {
  storageEngine: "LOCAL" | "CLOUD";
  rcloneRemotePath: string;
  hevcCompressionMode: "auto" | "on" | "off";
  driveConfigured?: boolean;
  driveReachable?: boolean | null;
  driveErrorCode?: string | null;
  googleDriveAudience?: "external" | "internal";
  googleDrivePublishingStatus?: "testing" | "production";
}

export interface DownloadEvent {
  id: string;
  title: string;
  status: string;
  progress: number;
  speed: string;
  eta: string;
}

export interface DiscoverMovie {
  id: string;
  tmdbId: number;
  title: string;
  description: string;
  thumbnailUrl: string;
  bannerUrl: string | null;
  genres: string[];
  duration: string;
  releaseYear: number;
  rating: string | null;
  voteAverage: number;
  voteCount: number;
  director: string | null;
  cast: string[];
  type: "movie" | "series";
  source?: MediaSource;
  availability?: MediaAvailability;
  remoteThumbnailUrl?: string | null;
  remoteBannerUrl?: string | null;
  localThumbnailUrl?: string | null;
  localBannerUrl?: string | null;
  cacheState?: MediaCacheState | null;
}

export interface PlaybackAudioTrack {
  id: string;
  label: string;
  language: string;
  channels: number;
  default: boolean;
  ready: boolean;
}

export interface PlaybackSourceMetadata {
  duration: number;
  container: string;
  codec: string;
  width: number;
  height: number;
  frameRate: number;
}

export interface PlaybackRendition {
  id: string;
  label: string;
  height: number;
  width: number;
  original: boolean;
  ready: boolean;
}

export interface PlaybackSubtitleTrack {
  id: string;
  language: string;
  label: string;
}

export type PlaybackPreparationState = "preparing" | "ready" | "error";
export type PlaybackProgressEvent = "heartbeat" | "pause" | "seek" | "visibility" | "exit" | "ended";

export interface PlaybackProgressRequest {
  timestamp: number;
  durationWatched: number;
  isFinished: boolean;
  sequenceNumber: number;
  event: PlaybackProgressEvent;
}

export interface PlaybackProgressResponse {
  status: "ok" | "finished" | "sticky_finished";
  viewingSessionId?: string;
  acceptedSeconds?: number;
  nextSequenceNumber: number;
}

export interface PlaybackRunResponse {
  runId: string;
  mediaId: string;
  movieId: string;
  episodeId: string | null;
  resumePosition: number;
  sourceMetadata: PlaybackSourceMetadata;
  tracks: PlaybackAudioTrack[];
  renditions: PlaybackRendition[];
  subtitles: PlaybackSubtitleTrack[];
  ticket: string;
  ticketExpiresAt: number;
  manifestUrl: string | null;
  progressiveUrl: string;
  nextEpisodeId: string | null;
  preparationState: PlaybackPreparationState;
  preparationError: { code: string; message: string } | null;
  nextSequenceNumber: number;
}
