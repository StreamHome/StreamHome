export interface LoginRequest {
  email: string;
  password: string;
}

export interface LoginResponse {
  accessToken: string;
  tokenType: string;
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
export interface SecuritySummary { email: string; twoFactorEnabled: boolean; recoveryCodesRemaining: number; previousLogin: LoginRecord | null }
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
}

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
}
