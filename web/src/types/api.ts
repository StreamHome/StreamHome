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
export type IntegrationScope = "ingest" | "downloads:read" | "downloads:cancel";
export interface IntegrationScopeDefinition { id: IntegrationScope; label: string; description: string }
export interface IntegrationCredentialInfo {
  id: string;
  name: string;
  tokenHint: string | null;
  scopes: IntegrationScope[];
  createdAt: number;
  expiresAt: number | null;
  revokedAt: number | null;
  lastUsedAt: number | null;
}
export interface IntegrationCredentialCreateResponse {
  credential: IntegrationCredentialInfo;
  token: string;
}

export interface TwoFAStatusResponse {
  twoFactorEnabled: boolean;
  email: string;
}

export interface TwoFASetupResponse {
  enrollmentId: string;
  manualKey: string;
  qrImageUrl: string;
  expiresAt: number;
}

export interface Profile {
  id: string;
  name: string;
  avatarColor: string;
  theme: string | null;
  pinEnabled: boolean;
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
  previewTaskId?: string | null;
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
  previewTaskId?: string | null;
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

export interface AdminProfileSummary extends Profile {
  administrator: boolean;
  historyCount: number;
  resumeCount: number;
  watchlistCount: number;
  recommendationCount: number;
  preferenceCount: number;
  watchSeconds: number;
  lastActivityAt: number | null;
}

export interface AdminMediaBrief {
  id: string;
  tmdbId: number | null;
  title: string;
  type: "movie" | "series" | string;
  releaseYear: number;
  thumbnailUrl: string | null;
  catalogSource: string;
  availability: string;
  cacheState: string | null;
}

export interface AdminProfileOverview {
  profile: AdminProfileSummary | (Profile & { administrator: boolean });
  counts: {
    history: number;
    resumeStates: number;
    watchlist: number;
    recommendations: number;
    preferences: number;
    events: number;
    exposures: number;
    playbackRuns: number;
  };
  watchSeconds: number;
  completedTitles: number;
  lastActivityAt: number | null;
  activePlaybackRuns: number;
  persistence: Array<{ label: string; location: string; durable: boolean }>;
}

export interface AdminHistoryItem {
  id: string;
  movie: AdminMediaBrief | null;
  episode: { id: string; title: string; seasonNumber: number; episodeNumber: number } | null;
  startedAt: number;
  lastSeenAt: number;
  maxCompletion: number;
  durationWatched: number;
  completedAt: number | null;
  earlyExitRecorded: boolean;
  rewatchReward: number;
}

export interface AdminProfileHistory {
  total: number;
  limit: number;
  offset: number;
  items: AdminHistoryItem[];
  resumeStates: Array<{
    movie: AdminMediaBrief | null;
    movieId: string;
    episodeId: string | null;
    timestamp: number;
    durationWatched: number;
    completionRate: number;
    updatedAt: string;
    finished: boolean;
  }>;
}

export interface AdminProfileWatchlist {
  total: number;
  limit: number;
  offset: number;
  items: Array<{ id: number; createdAt: string; movie: AdminMediaBrief | null }>;
}

export interface AdminProfileRecommendations {
  total: number;
  limit: number;
  offset: number;
  items: Array<{
    movie: AdminMediaBrief | null;
    score: number;
    reasons: string[];
    reasonDetails: RecommendationReasonDetail[];
    generatedAt: number;
    candidateSource: string;
    sourceConfidence: number;
    preference: MediaPreference;
  }>;
  preferences: Array<{ movieId: string; movie: AdminMediaBrief | null; preference: Exclude<MediaPreference, null>; updatedAt: number }>;
  onboarding: { genres: string[]; titleIds: string[] };
  tastes: Array<{ kind: string; value: string; score: number; updatedAt: number }>;
  vibe: { dialogueWpmMean: number | null; dialogueConfidence: number; sampleWeight: number; algorithmVersion: string; updatedAt: number } | null;
  refresh: { tasteVersion: number; lastRankedAt: number | null; lastTmdbRefreshAt: number | null; nextTmdbRefreshAt: number | null; refreshRequested: boolean; lastError: string | null } | null;
  runtimeMetric: { top20Overlap: number; meanDisplacement: number; generatedAt: number } | null;
}

export interface AdminProfileActivity {
  total: number;
  limit: number;
  offset: number;
  events: Array<{ id: number; type: string; movie: AdminMediaBrief | null; tmdbId: number | null; timestamp: number; metadata: Record<string, unknown> }>;
  exposures: Array<{ id: string; movie: AdminMediaBrief | null; feedGeneration: string; surface: string; scope: string; category: string; position: number; shownAt: number }>;
  playbackRuns: Array<{ id: string; movie: AdminMediaBrief | null; episodeId: string | null; state: string; createdAt: number; updatedAt: number; lastSeenAt: number; secondsPlayed: number }>;
}

export interface AdminProfileCache {
  total: number;
  sharedCacheTotal: number;
  unreferencedSharedTotal: number;
  limit: number;
  offset: number;
  items: Array<{
    movie: AdminMediaBrief;
    associationSources: string[];
    cachedAt: number | null;
    metadataRefreshedAt: number | null;
    cacheState: string | null;
    retryCount: number;
    nextRetryAt: number | null;
    lastError: string | null;
    files: {
      poster: { url: string | null; storedOnDisk: boolean; sizeBytes: number };
      backdrop: { url: string | null; storedOnDisk: boolean; sizeBytes: number };
      metadata: { storedOnDisk: boolean; sizeBytes: number };
      totalSizeBytes: number;
    };
    shared: true;
  }>;
}

export interface AdminProfileData {
  overview: AdminProfileOverview;
  history: AdminProfileHistory;
  watchlist: AdminProfileWatchlist;
  recommendations: AdminProfileRecommendations;
  activity: AdminProfileActivity;
  cache: AdminProfileCache;
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

export interface UpdatePolicy {
  automaticUpdates: boolean;
  idleMinutes: number;
  checkIntervalHours: number;
  maintenanceStart: string | null;
  maintenanceEnd: string | null;
  branch: string;
  requireSignedCommits: boolean;
}

export type UpdatePhase =
  | "idle"
  | "checking"
  | "up_to_date"
  | "update_available"
  | "queued"
  | "preflight"
  | "waiting_for_idle"
  | "stopping"
  | "installing"
  | "starting"
  | "rolling_back"
  | "succeeded"
  | "failed"
  | "rolled_back"
  | "rollback_failed";

export interface UpdateStatus {
  phase: UpdatePhase;
  message: string;
  currentCommit: string;
  targetCommit: string;
  updateAvailable: boolean;
  automatic: boolean;
  installMode: "automatic" | "when_idle" | "now";
  queuedAt: number | null;
  startedAt: number | null;
  finishedAt: number | null;
  lastCheckedAt: number | null;
  lastSuccessAt: number | null;
  failedTarget: string;
  error: string;
  blockers: string[];
  maintenanceWindowOpen: boolean;
  updateInProgress: boolean;
  logTail: string[];
  policy: UpdatePolicy;
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
  status: PlaybackRenditionStatus;
}

export interface PlaybackSourceMetadata {
  duration: number;
  container: string;
  codec: string;
  width: number;
  height: number;
  frameRate: number;
  sourceFormat: string;
}

export type PlaybackRenditionStatus = "preparing" | "streamable" | "ready" | "failed";

export interface PlaybackRendition {
  id: string;
  label: string;
  height: number;
  width: number;
  original: boolean;
  ready: boolean;
  status: PlaybackRenditionStatus;
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
