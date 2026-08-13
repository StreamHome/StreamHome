import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import type Hls from "hls.js";
import { useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "../../api/client";
import { getEpisodes, getMovie } from "../../api/movies";
import {
  closePlaybackRun,
  createPlaybackRun,
  getPlaybackRun,
  prioritizePlaybackQuality,
  reportPlaybackStartupDiagnostic,
  startOverPlaybackRun,
  updatePlaybackProgress,
} from "../../api/playback";
import { Button } from "../../components/ui/Button";
import { MOTION_EASE, MOTION_TIMINGS, useAppMotion } from "../../motion/motionSystem";
import { appUrl, parseAppQuery } from "../../navigation/queryState";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import { getThemeDefinition } from "../../themes/application/themeRegistry";
import type {
  Episode,
  Movie,
  PlaybackAudioTrack,
  PlaybackProgressEvent,
  PlaybackRendition,
  PlaybackRunResponse,
} from "../../types/api";
import { formatDuration } from "../../utils/format";
import { hasSubtitleOptions, PlayerControlMenu, PlayerIcon, PlayerIconButton } from "./PlayerControls";
import { languageDisplayName, normalizeLanguageTag } from "./language";
import {
  playerFullscreenMode,
  releaseViewportPlayerFullscreen,
  togglePlayerFullscreen,
  type PlayerFullscreenMode,
} from "./fullscreen";
import {
  isForcedLandscape,
  isMobileTapCandidate,
  isPhonePlayerViewport,
  lockPlayerLandscape,
  MOBILE_TAP_CHAIN_WINDOW,
  nextMobileTap,
  readMobileViewport,
  shouldShowMobileChrome,
  unlockPlayerLandscape,
  type MobileTapChain,
  type MobileTapSide,
} from "./mobilePlayer";


export type PlayerPhase =
  | "resolving"
  | "preparing"
  | "loading"
  | "playing"
  | "paused"
  | "buffering"
  | "recovering"
  | "ended"
  | "unavailable"
  | "fatal";

type StreamMode = "hls" | "native-hls" | "progressive";

interface PlayableAsset {
  id: string;
  movieId: string;
  episodeId?: string;
  title: string;
  subtitle: string;
  durationLabel: string;
  skipMarkers: Record<string, unknown>;
}

export interface ResolvedPlayback {
  asset: PlayableAsset;
  episodeSequence: Episode[];
  runResponse: PlaybackRunResponse;
}

interface PlayerPageProps {
  visualFixture?: ResolvedPlayback;
}

interface PlayerPreferences {
  qualityHeight: number | "auto";
  audioTrackId: string;
  audioLanguage: string;
  subtitleTrackId: string;
  captionScale: number;
  subtitleOffset: number;
  playbackRate: number;
  volume: number;
  muted: boolean;
}

interface FatalState {
  title: string;
  message: string;
  retryable: boolean;
}

interface MobilePointerGesture {
  id: number;
  side: MobileTapSide | "center";
  x: number;
  y: number;
  at: number;
}

interface PlayerAudioOption {
  id: string;
  label: string;
  language: string;
  index: number;
  source: PlaybackAudioTrack["source"];
  directUrl?: string | null;
  default: boolean;
  status: PlaybackAudioTrack["status"];
}

const DEFAULT_PREFERENCES: PlayerPreferences = {
  qualityHeight: "auto",
  audioTrackId: "",
  audioLanguage: "",
  subtitleTrackId: "off",
  captionScale: 1,
  subtitleOffset: 0,
  playbackRate: 1,
  volume: 1,
  muted: false,
};
const SUBTITLE_OFFSET_OPTIONS = [-5, -3, -2, -1.5, -1, -0.5, -0.25, 0, 0.25, 0.5, 1, 1.5, 2, 3, 5].map((value) => ({
  value,
  label: value === 0 ? "Subtitle timing: synced" : `${Math.abs(value)}s ${value < 0 ? "earlier" : "later"}`,
}));
const TICKET_RENEWAL_MARGIN = 3 * 60 * 1_000;
const NEXT_EPISODE_SECONDS = 10;
const NETWORK_RETRY_LIMIT = 3;
const MEDIA_RECOVERY_LIMIT = 2;
export const PLAYER_CONTROLS_IDLE_MS = 3_000;
export const PLAYBACK_STARTUP_TIMEOUT_MS = 12_000;
export const PLAYBACK_STARTUP_MAX_PROGRESS_WAIT_MS = 30_000;
export const PLAYBACK_CLOCK_ADVANCE_TIMEOUT_MS = 2_000;
export const EXTERNAL_AUDIO_DRIFT_RECOVERY_SECONDS = 1;
export const EXTERNAL_AUDIO_DRIFT_RECOVERY_COOLDOWN_MS = 5_000;
export const FORWARD_BUFFER_TARGET_SECONDS = 30;
export const FORWARD_BUFFER_MAX_SECONDS = 60;
export const STARTUP_QUALITY_MIN_BUFFER_SECONDS = 8;
export const STARTUP_QUALITY_HEADROOM_RATIO = 1.35;

type PlaybackStartupStage =
  | "transport-initializing"
  | "media-attached"
  | "direct-metadata"
  | "manifest-parsed"
  | "level-playlist"
  | "audio-playlist"
  | "fragment-loaded"
  | "fragment-buffered"
  | "can-play"
  | "playing";

interface PlaybackStartupFault {
  type: string;
  detail: string;
  httpStatus: number | null;
}

export function playbackStartupFailureMessage(
  streamMode: StreamMode,
  stage: PlaybackStartupStage,
  fault: PlaybackStartupFault | null,
): string {
  if (streamMode === "progressive") {
    return `The protected source did not become playable during ${stage}. The source container or selected audio codec may not be supported by this browser.`;
  }
  const status = fault?.httpStatus ? ` HTTP ${fault.httpStatus}.` : "";
  const detail = fault?.detail ? ` HLS reported ${fault.detail}.` : "";
  return `Adaptive playback stopped during ${stage}.${status}${detail} Retry this title after checking the playback startup entry in backend.log.`;
}

export function shouldApplyDeferredStartupQuality(
  levelBitrate: number,
  estimatedBandwidth: number,
  bufferedAhead: number,
): boolean {
  return levelBitrate > 0
    && estimatedBandwidth >= levelBitrate * STARTUP_QUALITY_HEADROOM_RATIO
    && bufferedAhead >= STARTUP_QUALITY_MIN_BUFFER_SECONDS;
}


function episodeTmdbId(mediaId: string): number | null {
  const match = mediaId.match(/^ep_(\d+)_s\d+_e\d+$/);
  return match ? Number(match[1]) : null;
}

export function nextPlayableEpisode(episodes: Episode[], currentId: string): Episode | null {
  const ordered = [...episodes].sort((left, right) => left.seasonNumber - right.seasonNumber || left.episodeNumber - right.episodeNumber);
  const currentIndex = ordered.findIndex((episode) => episode.id === currentId);
  if (currentIndex < 0) return null;
  return ordered.slice(currentIndex + 1).find((episode) => Boolean(episode.videoUrl || episode.previewTaskId)) ?? null;
}

export function playbackQualityOptions(
  renditions: PlaybackRendition[],
  levels: Array<{ height?: number; name?: string; url?: string | string[]; attrs?: Record<string, string> }>,
): Array<{ id: string; label: string; height: number | "auto"; index: number; ready: boolean; status: PlaybackRendition["status"] | "ready" }> {
  const options = renditions
    .slice()
    .sort((left, right) => right.height - left.height)
    .map((rendition) => {
      const index = levels.findIndex((level) => {
        const urls = Array.isArray(level.url) ? level.url : [level.url || ""];
        const identityMatch = urls.some((url) => decodeURIComponent(url).includes(`/${rendition.id}/playlist.m3u8`));
        const nameMatch = level.name === rendition.label || level.attrs?.NAME === rendition.label;
        return identityMatch || nameMatch;
      });
      return {
        id: rendition.id,
        label: `${rendition.label}${rendition.original ? " \u00b7 Original" : ""}`,
        height: rendition.height,
        index,
        ready: rendition.ready && index >= 0,
        status: rendition.status,
      };
    });
  return [{ id: "auto", label: "Auto", height: "auto", index: -1, ready: true, status: "ready" }, ...options];
}

export function applySubtitleTrackSelection(video: HTMLVideoElement, selectedTrackId: string): void {
  const trackElements = Array.from(video.querySelectorAll<HTMLTrackElement>("track[data-subtitle-id]"));
  for (let index = 0; index < video.textTracks.length; index += 1) {
    video.textTracks[index].mode = "disabled";
  }
  for (const trackElement of trackElements) {
    const textTrack = trackElement.track;
    if (!textTrack) continue;
    textTrack.mode = selectedTrackId !== "off" && trackElement.dataset.subtitleId === selectedTrackId
      ? "hidden"
      : "disabled";
  }
}

export function shouldAutoHidePlayerControls(phase: PlayerPhase, menuOpen: boolean, scrubbing: boolean): boolean {
  return ["loading", "playing", "paused", "buffering", "recovering"].includes(phase) && !menuOpen && !scrubbing;
}

export function timelineValueFromPointer(
  clientX: number,
  left: number,
  width: number,
  minimum: number,
  maximum: number,
): number {
  if (!Number.isFinite(width) || width <= 0 || !Number.isFinite(maximum) || maximum <= minimum) return minimum;
  const ratio = Math.min(1, Math.max(0, (clientX - left) / width));
  return minimum + ratio * (maximum - minimum);
}

export function subtitleCueIsActive(
  cueStart: number,
  cueEnd: number,
  mediaTime: number,
  subtitleOffset: number,
): boolean {
  const adjustedTime = mediaTime - subtitleOffset;
  return cueStart <= adjustedTime && adjustedTime < cueEnd;
}

function plainSubtitleCueText(value: string): string {
  return value
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<[^>]*>/g, "")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'")
    .trim();
}

export function shouldResumePlaybackAfterTransport(playRequested: boolean, completed: boolean): boolean {
  return playRequested && !completed;
}

export function shouldRetryPlaybackStartup(completedRetries: number): boolean {
  return completedRetries < 1;
}

export function shouldRetryPlaybackStall(playRequested: boolean, readyState: number, completedRetries: number): boolean {
  return playRequested && readyState < HTMLMediaElement.HAVE_FUTURE_DATA && completedRetries < 2;
}

export function externalAudioSyncPlan(
  videoTime: number,
  _audioTime: number,
  playbackRate: number,
  forceSeek = false,
): { seekTime: number | null; playbackRate: number } {
  const target = Math.max(0, Number.isFinite(videoTime) ? videoTime : 0);
  const baseRate = Math.min(4, Math.max(0.25, Number.isFinite(playbackRate) ? playbackRate : 1));
  if (forceSeek) {
    return { seekTime: target, playbackRate: baseRate };
  }
  return { seekTime: null, playbackRate: baseRate };
}

export function isMeaningfulPointerActivity(
  previous: { x: number; y: number } | null,
  next: { x: number; y: number },
  _movementX: number,
  _movementY: number,
): boolean {
  if (!previous) return false;
  return Math.abs(previous.x - next.x) >= 1 || Math.abs(previous.y - next.y) >= 1;
}

export function authoritativePlaybackPosition(
  confirmedPosition: number,
  resumePosition: number,
  pendingTarget: number | null,
): number {
  if (pendingTarget !== null && Number.isFinite(pendingTarget)) return Math.max(0, pendingTarget);
  return Math.max(
    0,
    Number.isFinite(confirmedPosition) ? confirmedPosition : 0,
    Number.isFinite(resumePosition) ? resumePosition : 0,
  );
}

export function catalogDurationSeconds(value: string): number {
  const normalized = value.trim().toLowerCase();
  if (!normalized) return 0;
  const colon = normalized.match(/^(\d{1,3}):(\d{1,2})(?::(\d{1,2}))?$/);
  if (colon) {
    const first = Number(colon[1]);
    const second = Number(colon[2]);
    const third = colon[3] === undefined ? null : Number(colon[3]);
    return third === null ? first * 3600 + second * 60 : first * 3600 + second * 60 + third;
  }
  const hours = Number(normalized.match(/(\d+(?:\.\d+)?)\s*h/)?.[1] ?? 0);
  const minutes = Number(normalized.match(/(\d+(?:\.\d+)?)\s*m/)?.[1] ?? 0);
  const seconds = Number(normalized.match(/(\d+(?:\.\d+)?)\s*s/)?.[1] ?? 0);
  const total = hours * 3600 + minutes * 60 + seconds;
  if (total > 0) return total;
  const numeric = Number(normalized);
  return Number.isFinite(numeric) && numeric > 0 ? numeric : 0;
}

export function authoritativePlaybackDuration(
  serverDuration: number,
  catalogDuration: string,
  mode: StreamMode,
  mediaDuration: number,
): number {
  if (Number.isFinite(serverDuration) && serverDuration > 0) return serverDuration;
  const catalog = catalogDurationSeconds(catalogDuration);
  if (catalog > 0) return catalog;
  return mode === "progressive" && Number.isFinite(mediaDuration) && mediaDuration > 0 ? mediaDuration : 0;
}

function assetFromMovie(movie: Movie): PlayableAsset {
  return {
    id: movie.id,
    movieId: movie.id,
    title: movie.title,
    subtitle: "",
    durationLabel: movie.duration,
    skipMarkers: movie.skipMarkers || {},
  };
}

function assetFromEpisode(movie: Movie, episode: Episode): PlayableAsset {
  return {
    id: episode.id,
    movieId: movie.id,
    episodeId: episode.id,
    title: movie.title,
    subtitle: `S${episode.seasonNumber} E${episode.episodeNumber} · ${episode.title}`,
    durationLabel: episode.duration,
    skipMarkers: episode.skipMarkers || {},
  };
}

function activeSkipMarker(markers: Record<string, unknown>, time: number): { label: string; end: number } | null {
  for (const [name, value] of Object.entries(markers)) {
    if (!Array.isArray(value)) continue;
    for (const marker of value) {
      if (!marker || typeof marker !== "object") continue;
      const start = Number((marker as { start?: unknown }).start);
      const end = Number((marker as { end?: unknown }).end);
      if (Number.isFinite(start) && Number.isFinite(end) && time >= start && time < end) {
        return { label: `Skip ${name}`, end };
      }
    }
  }
  return null;
}

function loadPreferences(profileId: string): PlayerPreferences {
  try {
    const parsed = JSON.parse(localStorage.getItem(`streamhome_player_preferences_${profileId}`) || "{}") as Partial<PlayerPreferences> & { subtitleLanguage?: string };
    return {
      qualityHeight: parsed.qualityHeight === "auto" || typeof parsed.qualityHeight === "number" ? parsed.qualityHeight : "auto",
      audioTrackId: typeof parsed.audioTrackId === "string" ? parsed.audioTrackId : "",
      audioLanguage: typeof parsed.audioLanguage === "string" ? normalizeLanguageTag(parsed.audioLanguage, "") : "",
      subtitleTrackId: typeof parsed.subtitleTrackId === "string"
        ? parsed.subtitleTrackId
        : typeof parsed.subtitleLanguage === "string" ? parsed.subtitleLanguage : "off",
      captionScale: typeof parsed.captionScale === "number" ? Math.min(1.5, Math.max(0.8, parsed.captionScale)) : 1,
      subtitleOffset: typeof parsed.subtitleOffset === "number" && Number.isFinite(parsed.subtitleOffset)
        ? Math.min(5, Math.max(-5, parsed.subtitleOffset))
        : 0,
      playbackRate: typeof parsed.playbackRate === "number" ? parsed.playbackRate : 1,
      volume: typeof parsed.volume === "number" && Number.isFinite(parsed.volume)
        ? Math.min(1, Math.max(0, parsed.volume))
        : 1,
      muted: typeof parsed.muted === "boolean" ? parsed.muted : false,
    };
  } catch {
    return DEFAULT_PREFERENCES;
  }
}

function isInteractiveTarget(target: EventTarget | null): boolean {
  return target instanceof Element && Boolean(target.closest("input, select, textarea, a, [contenteditable='true'], [role='slider'], [role='textbox'], [role='menu'], [role='option']"));
}

type PlayerKeyboardShortcut = "play-pause" | "fullscreen" | "mute" | "pip" | "seek-back" | "seek-forward" | "volume-up" | "volume-down";

function playerKeyboardShortcut(event: Pick<KeyboardEvent, "code" | "key">): PlayerKeyboardShortcut | null {
  const key = event.key.toLowerCase();
  if (event.code === "Space" || event.code === "KeyK" || event.key === " " || key === "k") return "play-pause";
  if (event.code === "KeyF" || key === "f") return "fullscreen";
  if (event.code === "KeyM" || key === "m") return "mute";
  if (event.code === "KeyP" || key === "p") return "pip";
  if (event.code === "ArrowLeft" || event.key === "ArrowLeft") return "seek-back";
  if (event.code === "ArrowRight" || event.key === "ArrowRight") return "seek-forward";
  if (event.code === "ArrowUp" || event.key === "ArrowUp") return "volume-up";
  if (event.code === "ArrowDown" || event.key === "ArrowDown") return "volume-down";
  return null;
}

function suppressPlayerShortcutEvent(event: KeyboardEvent): void {
  event.preventDefault();
  event.stopPropagation();
  event.stopImmediatePropagation();
}

export function advancingPlaybackDelta(
  previousWallMilliseconds: number | null,
  previousMediaSeconds: number,
  nowMilliseconds: number,
  mediaSeconds: number,
  activelyPlaying: boolean,
): number {
  if (!activelyPlaying || previousWallMilliseconds === null) return 0;
  const wallDelta = Math.max(0, Math.min(2, (nowMilliseconds - previousWallMilliseconds) / 1000));
  const mediaDelta = mediaSeconds - previousMediaSeconds;
  if (mediaDelta <= 0 || mediaDelta >= 2.5) return 0;
  return Math.min(wallDelta, mediaDelta + 0.25);
}

export function clampPlaybackTime(requestedTime: number, mediaDuration: number, knownDuration: number): number {
  const upperBound = Number.isFinite(knownDuration) && knownDuration > 0
    ? knownDuration
    : Number.isFinite(mediaDuration) && mediaDuration > 0 ? mediaDuration : requestedTime;
  return Math.min(Math.max(requestedTime, 0), Math.max(0, upperBound));
}

export function isPlaybackTimeSeekable(ranges: Pick<TimeRanges, "length" | "start" | "end">, position: number, tolerance = 0.5): boolean {
  for (let index = 0; index < ranges.length; index += 1) {
    if (position >= ranges.start(index) - tolerance && position <= ranges.end(index) + tolerance) return true;
  }
  return false;
}

export function clampGrowingPlaybackTime(
  requestedTime: number,
  ranges: Pick<TimeRanges, "length" | "end">,
  seekableUntil: number,
  edgeMargin = 0.5,
): number {
  let browserEdge = 0;
  for (let index = 0; index < ranges.length; index += 1) {
    browserEdge = Math.max(browserEdge, ranges.end(index));
  }
  const publishedEdge = Number.isFinite(seekableUntil) && seekableUntil > 0 ? seekableUntil : browserEdge;
  const commonEdge = browserEdge > 0 && publishedEdge > 0
    ? Math.min(browserEdge, publishedEdge)
    : Math.max(browserEdge, publishedEdge);
  const safeEdge = Math.max(0, commonEdge - Math.max(0, edgeMargin));
  return Math.min(Math.max(0, requestedTime), safeEdge);
}

export function bufferedEndForTime(ranges: Pick<TimeRanges, "length" | "start" | "end">, position: number): number {
  let end = position;
  for (let index = 0; index < ranges.length; index += 1) {
    if (ranges.start(index) <= position + 0.25 && ranges.end(index) >= position) {
      end = Math.max(end, ranges.end(index));
    }
  }
  return end;
}

export function canUseProgressiveCompatibility(
  metadata: PlaybackRunResponse["sourceMetadata"],
  mediaElement?: Pick<HTMLMediaElement, "canPlayType"> | null,
): boolean {
  const codec = metadata.codec.trim().toLowerCase();
  const audioCodec = (metadata.audioCodec ?? "").trim().toLowerCase();
  if (!["", "aac", "mp4a", "mp3"].includes(audioCodec)) return false;
  const formats = `${metadata.container},${metadata.sourceFormat}`.toLowerCase().split(",").map((item) => item.trim());
  const mp4 = formats.some((item) => ["mp4", "mov", "mov,mp4", "m4v"].includes(item));
  const webm = formats.some((item) => item === "webm");
  if (["h264", "avc", "avc1"].includes(codec) && mp4) return true;
  if (!mediaElement) return false;
  const candidates: string[] = [];
  if (["hevc", "h265", "hvc1", "hev1"].includes(codec) && mp4) {
    candidates.push('video/mp4; codecs="hvc1"', 'video/mp4; codecs="hev1"');
  }
  if (["av1", "av01"].includes(codec)) {
    if (mp4) candidates.push('video/mp4; codecs="av01.0.05M.08"');
    if (webm) candidates.push('video/webm; codecs="av01"');
  }
  if (["vp9", "vp09"].includes(codec) && webm) candidates.push('video/webm; codecs="vp9"');
  if (["vp8", "vp08"].includes(codec) && webm) candidates.push('video/webm; codecs="vp8"');
  return candidates.some((candidate) => mediaElement.canPlayType(candidate) !== "");
}

export function progressiveAudioTrack(
  tracks: PlaybackAudioTrack[],
  preferredTrackId: string,
  preferredLanguage: string,
): PlaybackAudioTrack | null {
  const embedded = tracks.filter((track) => track.source === "embedded");
  const preferred = tracks.find((track) => track.id === preferredTrackId)
    ?? tracks.find((track) => normalizeLanguageTag(track.language) === normalizeLanguageTag(preferredLanguage, ""));
  if (preferred?.source === "external") return preferred.directUrl ? preferred : null;
  if (preferred?.source === "embedded") return preferred;
  return tracks.find((track) => track.default && (track.source === "embedded" || Boolean(track.directUrl)))
    ?? embedded[0]
    ?? tracks.find((track) => track.source === "external" && Boolean(track.directUrl))
    ?? null;
}

export function canUseProgressivePlayback(
  response: PlaybackRunResponse,
  preferredTrackId: string,
  preferredLanguage: string,
  mediaElement?: Pick<HTMLMediaElement, "canPlayType"> | null,
): boolean {
  if (!response.progressiveUrl || !canUseProgressiveCompatibility(response.sourceMetadata, mediaElement)) return false;
  if (response.tracks.length === 0) return true;
  const selected = progressiveAudioTrack(response.tracks, preferredTrackId, preferredLanguage);
  const embedded = response.tracks.filter((track) => track.source === "embedded");
  if (!selected) return false;
  if (selected.source === "external") return Boolean(selected.directUrl);
  return embedded.length === 1 || selected.default;
}

export function initialPlaybackMode(
  response: PlaybackRunResponse,
  preferredTrackId: string,
  preferredLanguage: string,
  mediaElement?: Pick<HTMLMediaElement, "canPlayType"> | null,
): StreamMode {
  const preferredTrack = progressiveAudioTrack(response.tracks, preferredTrackId, preferredLanguage);
  if (
    preferredTrack?.source === "external"
    && response.preparationState === "ready"
    && Boolean(response.manifestUrl)
  ) {
    return "hls";
  }
  return canUseProgressivePlayback(response, preferredTrackId, preferredLanguage, mediaElement)
    ? "progressive"
    : "hls";
}

export function adaptiveTransportIsGrowing(response: PlaybackRunResponse): boolean {
  return [...response.renditions, ...response.tracks].some((item) => item.status === "streamable");
}

export function playbackTransportIsReady(
  response: Pick<PlaybackRunResponse, "preparationState" | "progressiveUrl" | "manifestUrl">,
  streamMode: StreamMode,
): boolean {
  return streamMode === "progressive"
    ? Boolean(response.progressiveUrl)
    : response.preparationState === "ready" && Boolean(response.manifestUrl);
}

export function shouldExtendPlaybackStartup(
  now: number,
  startedAt: number,
  lastProgressAt: number,
): boolean {
  return now - startedAt < PLAYBACK_STARTUP_MAX_PROGRESS_WAIT_MS
    && now - lastProgressAt < PLAYBACK_STARTUP_TIMEOUT_MS;
}

export function progressSequenceWasAccepted(requestSequence: number, nextSequence: number): boolean {
  return nextSequence > requestSequence;
}

export type PlaybackProgressFailureAction = "reconcile" | "stop" | "retry-later";

export function playbackProgressFailureAction(error: unknown): PlaybackProgressFailureAction {
  if (!(error instanceof ApiError)) return "retry-later";
  if (error.status === 409 && error.code === "PLAYBACK_SEQUENCE_MISMATCH") return "reconcile";
  if (
    [403, 404, 409, 410].includes(error.status)
    || [
      "PLAYBACK_RUN_EXPIRED",
      "PLAYBACK_RUN_FORBIDDEN",
      "PLAYBACK_RUN_NOT_FOUND",
      "PLAYBACK_SOURCE_CHANGED",
    ].includes(error.code)
  ) {
    return "stop";
  }
  return "retry-later";
}

export function matchAudioTrackIndexes(
  serverTracks: Array<Pick<PlaybackAudioTrack, "id" | "language" | "label">>,
  transportTracks: Array<{ lang?: string; name?: string; url?: string | string[] }>,
): number[] {
  const used = new Set<number>();
  return serverTracks.map((serverTrack) => {
    const urlsFor = (track: (typeof transportTracks)[number]) => Array.isArray(track.url) ? track.url : [track.url || ""];
    let index = transportTracks.findIndex((track, candidate) => !used.has(candidate) && urlsFor(track).some((url) => decodeURIComponent(url).includes(`/${serverTrack.id}/playlist.m3u8`)));
    if (index < 0) {
      index = transportTracks.findIndex((track, candidate) => !used.has(candidate)
        && normalizeLanguageTag(track.lang) === normalizeLanguageTag(serverTrack.language)
        && (track.name || "").trim().toLowerCase() === serverTrack.label.trim().toLowerCase());
    }
    if (index < 0) {
      index = transportTracks.findIndex((track, candidate) => !used.has(candidate) && normalizeLanguageTag(track.lang) === normalizeLanguageTag(serverTrack.language));
    }
    if (index >= 0) used.add(index);
    return index;
  });
}

export function shouldAcceptObservedPlaybackTime(
  observedTime: number,
  stableTime: number,
  transportResetting: boolean,
  pendingSeekTarget: number | null,
  allowForwardPendingSettlement = false,
): boolean {
  if (!Number.isFinite(observedTime) || observedTime < 0) return false;
  if (
    pendingSeekTarget !== null
    && Math.abs(observedTime - pendingSeekTarget) > 1
    && (!allowForwardPendingSettlement || observedTime < pendingSeekTarget - 1)
  ) return false;
  if (pendingSeekTarget === null && stableTime > 1 && observedTime < Math.max(1, stableTime - 2)) return false;
  if (transportResetting && stableTime > 1 && observedTime < Math.max(1, stableTime - 2)) return false;
  return true;
}

export function mergePlaybackRunMetadata(
  active: PlaybackRunResponse,
  refreshed: PlaybackRunResponse,
): PlaybackRunResponse {
  if (
    active.sourceFingerprint !== refreshed.sourceFingerprint
    || (!active.manifestUrl && refreshed.manifestUrl)
  ) return refreshed;
  return {
    ...refreshed,
    ticket: active.ticket,
    ticketExpiresAt: active.ticketExpiresAt,
    manifestUrl: active.manifestUrl,
    progressiveUrl: active.progressiveUrl,
  };
}

export function preparationStatusMessage(progress: PlaybackRunResponse["preparationProgress"] | null | undefined): string {
  if (!progress) return "Scheduling the first HLS rendition.";
  if (progress.stage === "queued") {
    return progress.queuePosition > 0
      ? `Playback preparation is queued at position ${progress.queuePosition}.`
      : "Playback preparation is waiting for an HLS worker.";
  }
  if (progress.stage === "packaging") {
    return progress.readySegments > 0
      ? `Packaging the source into HLS · ${progress.readySegments} segment${progress.readySegments === 1 ? "" : "s"} ready.`
      : "Packaging the compatible source into HLS without re-encoding.";
  }
  if (progress.stage === "transcoding") {
    return progress.readySegments > 0
      ? `Generating the first compatible rendition · ${progress.readySegments} segment${progress.readySegments === 1 ? "" : "s"} ready.`
      : "Generating the first compatible HLS rendition.";
  }
  if (progress.stage === "audio") return "Preparing the default audio track for browser playback.";
  if (progress.stage === "failed") return "HLS preparation failed; recovery details are being loaded.";
  return "The first HLS rendition is ready.";
}

function errorState(error: unknown): FatalState {
  if (error instanceof ApiError) {
    if (["MEDIA_SOURCE_MISSING", "INVALID_MEDIA_PATH"].includes(error.code)) {
      return { title: "Media unavailable", message: error.message, retryable: false };
    }
    return { title: "Playback interrupted", message: error.message, retryable: error.status !== 401 && error.status !== 403 };
  }
  return {
    title: "Playback interrupted",
    message: error instanceof Error ? error.message : "The player encountered an unexpected error.",
    retryable: true,
  };
}

export function PlayerPage({ visualFixture }: PlayerPageProps = {}) {
  const navigate = useNavigate();
  const location = useLocation();
  const query = useMemo(() => parseAppQuery(location.search), [location.search]);
  const mediaId = query.media ?? "";
  const profile = useProfileStore((state) => state.activeProfile);
  const theme = useThemeStore((state) => state.activeTheme);
  const definition = getThemeDefinition(theme);
  const { reduced } = useAppMotion();
  const initialPreferences = useMemo(
    () => profile ? loadPreferences(profile.id) : DEFAULT_PREFERENCES,
    [profile],
  );

  const containerRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const externalAudioRef = useRef<HTMLAudioElement>(null);
  const lastFrameCanvasRef = useRef<HTMLCanvasElement>(null);
  const timelineRef = useRef<HTMLInputElement>(null);
  const hlsRef = useRef<Hls | null>(null);
  const hlsAudioOptionsRef = useRef<PlayerAudioOption[]>([]);
  const hlsQualityOptionsRef = useRef<ReturnType<typeof playbackQualityOptions>>([]);
  const controlsTimerRef = useRef<number | null>(null);
  const playbackStartupTimerRef = useRef<number | null>(null);
  const playbackClockAdvanceTimerRef = useRef<number | null>(null);
  const playbackStartupRetriesRef = useRef(0);
  const playbackStartupStartedAtRef = useRef(0);
  const playbackStartupProgressAtRef = useRef(0);
  const playbackStartupStageRef = useRef<PlaybackStartupStage>("transport-initializing");
  const playbackStartupFaultRef = useRef<PlaybackStartupFault | null>(null);
  const desktopClickTimerRef = useRef<number | null>(null);
  const hlsRetryTimerRef = useRef<number | null>(null);
  const resumePositionRef = useRef(0);
  const deferredResumeTargetRef = useRef<number | null>(null);
  const currentTimeRef = useRef(0);
  const confirmedTimeRef = useRef(0);
  const pendingSeekTargetRef = useRef<number | null>(null);
  const pendingPositionKindRef = useRef<"resume" | "seek" | "recovery" | null>(null);
  const positionGenerationRef = useRef(0);
  const pendingSeekReportRef = useRef(false);
  const progressiveFailedRef = useRef(false);
  const transportGenerationRef = useRef(0);
  const runResponseRef = useRef<PlaybackRunResponse | null>(null);
  const transportResettingRef = useRef(false);
  const playbackIntentRef = useRef(true);
  const playbackLoadingSuspendedRef = useRef(false);
  const staleVideoPlayEventRef = useRef(false);
  const videoPlayRequestRef = useRef(0);
  const pendingVideoPlayRequestRef = useRef<number | null>(null);
  const externalAudioPlayRequestRef = useRef(0);
  const pendingExternalAudioPlayRequestRef = useRef<number | null>(null);
  const pendingQualitySelectionRef = useRef<string | null>(null);
  const deferredStartupQualityRef = useRef<{ id: string; index: number } | null>(null);
  const pendingAudioSelectionRef = useRef<string | null>(null);
  const selectedAudioTrackIdRef = useRef("");
  const activeExternalAudioTrackIdRef = useRef<string | null>(null);
  const externalAudioBufferingRef = useRef(false);
  const externalAudioLastDriftRecoveryAtRef = useRef(Number.NEGATIVE_INFINITY);
  const volumeRef = useRef(initialPreferences.volume);
  const mutedRef = useRef(initialPreferences.muted);
  const preferencesProfileIdRef = useRef(profile?.id ?? "");
  const stallRecoveryTimerRef = useRef<number | null>(null);
  const stallRecoveryAttemptsRef = useRef(0);
  const lastFrameCaptureAtRef = useRef(0);
  const hasLastFrameRef = useRef(false);
  const resumeAppliedRef = useRef(false);
  const sequenceNumberRef = useRef(1);
  const pendingWatchedSecondsRef = useRef(0);
  const lastAdvanceWallRef = useRef<number | null>(null);
  const lastAdvanceMediaRef = useRef(0);
  const progressQueueRef = useRef<Promise<unknown>>(Promise.resolve());
  const completedRef = useRef(false);
  const runClosedRef = useRef(false);
  const networkRetriesRef = useRef(0);
  const mediaRecoveriesRef = useRef(0);
  const mobileTapChainRef = useRef<MobileTapChain | null>(null);
  const mobileSingleTapTimerRef = useRef<number | null>(null);
  const mobileTapResetTimerRef = useRef<number | null>(null);
  const mobileFullscreenAttemptedAtRef = useRef(0);
  const mobilePointerGestureRef = useRef<MobilePointerGesture | null>(null);
  const desktopPointerPositionRef = useRef<{ x: number; y: number } | null>(null);
  const showControlsRef = useRef(true);
  const phaseRef = useRef<PlayerPhase>("resolving");
  const controlMenuOpenRef = useRef(false);
  const scrubbingRef = useRef(false);
  const scrubOriginRef = useRef(0);
  const timelineAnimationFrameRef = useRef<number | null>(null);
  const captionSignatureRef = useRef("");

  const [asset, setAsset] = useState<PlayableAsset | null>(null);
  const [episodeSequence, setEpisodeSequence] = useState<Episode[]>([]);
  const [runResponse, setRunResponse] = useState<PlaybackRunResponse | null>(null);
  const [phase, setPhase] = useState<PlayerPhase>("resolving");
  const [streamMode, setStreamMode] = useState<StreamMode>("hls");
  const [hlsEngine, setHlsEngine] = useState<typeof import("hls.js").default | null>(null);
  const [transportRevision, setTransportRevision] = useState(0);
  const [fatal, setFatal] = useState<FatalState | null>(null);
  const [retryVersion, setRetryVersion] = useState(0);
  const [preparationElapsedSeconds, setPreparationElapsedSeconds] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [bufferedEnd, setBufferedEnd] = useState(0);
  const [preferences, setPreferences] = useState<PlayerPreferences>(initialPreferences);
  const [volume, setVolume] = useState(initialPreferences.volume);
  const [muted, setMuted] = useState(initialPreferences.muted);
  const [showControls, setShowControls] = useState(true);
  const [controlMenuOpen, setControlMenuOpen] = useState(false);
  const [timelineScrubbing, setTimelineScrubbing] = useState(false);
  const [availableQualities, setAvailableQualities] = useState<ReturnType<typeof playbackQualityOptions>>([{ id: "auto", label: "Auto", height: "auto", index: -1, ready: true, status: "ready" }]);
  const [selectedQualityId, setSelectedQualityId] = useState("auto");
  const [availableAudioTracks, setAvailableAudioTracks] = useState<PlayerAudioOption[]>([]);
  const [selectedAudioTrackId, setSelectedAudioTrackId] = useState("");
  const [nextCountdown, setNextCountdown] = useState<number | null>(null);
  const [nextCancelled, setNextCancelled] = useState(false);
  const [timelinePreview, setTimelinePreview] = useState<{ x: number; time: number } | null>(null);
  const [captionLines, setCaptionLines] = useState<string[]>([]);
  const [fullscreenActive, setFullscreenActive] = useState(false);
  const [fullscreenMode, setFullscreenMode] = useState<PlayerFullscreenMode>(null);
  const [fullscreenError, setFullscreenError] = useState("");
  const [playerNotice, setPlayerNotice] = useState("");
  const [hasLastFrame, setHasLastFrame] = useState(false);
  const [playbackLoadingSuspended, setPlaybackLoadingSuspended] = useState(false);
  const [mobilePlayer, setMobilePlayer] = useState(() => typeof window !== "undefined" && isPhonePlayerViewport(readMobileViewport(window)));
  const [forcedLandscape, setForcedLandscape] = useState(() => typeof window !== "undefined" && isForcedLandscape(window.innerWidth, window.innerHeight, isPhonePlayerViewport(readMobileViewport(window))));
  const [mobileSeekFeedback, setMobileSeekFeedback] = useState<{
    side: MobileTapSide;
    seconds: number;
  } | null>(null);

  useEffect(() => {
    runResponseRef.current = runResponse;
  }, [runResponse]);

  useEffect(() => {
    volumeRef.current = volume;
  }, [volume]);

  useEffect(() => {
    mutedRef.current = muted;
  }, [muted]);

  useLayoutEffect(() => {
    const video = videoRef.current;
    const audio = externalAudioRef.current;
    if (video) {
      video.volume = volume;
      video.muted = activeExternalAudioTrackIdRef.current ? true : muted;
    }
    if (audio) {
      audio.volume = volume;
      audio.muted = muted;
    }
  }, [muted, volume]);

  useEffect(() => {
    if (phase !== "preparing") {
      setPreparationElapsedSeconds(0);
      return;
    }
    const startedAt = Date.now();
    const update = () => setPreparationElapsedSeconds(Math.floor((Date.now() - startedAt) / 1_000));
    update();
    const timer = window.setInterval(update, 1_000);
    return () => window.clearInterval(timer);
  }, [phase]);

  useEffect(() => {
    if (!playerNotice) return;
    const timer = window.setTimeout(() => setPlayerNotice(""), 4_000);
    return () => window.clearTimeout(timer);
  }, [playerNotice]);

  useEffect(() => {
    if (hlsEngine) return;
    let active = true;
    void import("hls.js").then((module) => {
      if (active) setHlsEngine(() => module.default);
    }).catch(() => {
      if (active) setPlayerNotice("The adaptive playback engine could not be loaded.");
    });
    return () => {
      active = false;
    };
  }, [hlsEngine]);

  const closeActiveRun = useCallback(() => {
    const activeRun = runResponseRef.current;
    if (!activeRun || visualFixture || completedRef.current || runClosedRef.current) return;
    runClosedRef.current = true;
    const watchedSeconds = Math.floor(pendingWatchedSecondsRef.current);
    pendingWatchedSecondsRef.current -= watchedSeconds;
    void closePlaybackRun(activeRun.runId, {
      timestamp: Math.max(0, confirmedTimeRef.current),
      durationWatched: watchedSeconds,
      isFinished: false,
      sequenceNumber: sequenceNumberRef.current,
      event: "exit",
    }).then((response) => {
      sequenceNumberRef.current = Math.max(sequenceNumberRef.current, response.nextSequenceNumber);
    }).catch(() => undefined);
  }, [visualFixture]);

  const exitPlayer = useCallback(() => {
    unlockPlayerLandscape();
    closeActiveRun();
    if (!profile) {
      navigate("/profiles", { replace: true });
      return;
    }
    if ((location.state as { fromApp?: boolean } | null)?.fromApp) navigate(-1);
    else navigate(appUrl(profile.id, "home"), { replace: true });
  }, [closeActiveRun, location.state, navigate, profile]);

  useEffect(() => {
    const updateMobileEnvironment = () => {
      const metrics = readMobileViewport();
      const nextMobilePlayer = isPhonePlayerViewport(metrics);
      setMobilePlayer(nextMobilePlayer);
      setForcedLandscape(isForcedLandscape(metrics.width, metrics.height, nextMobilePlayer));
      if (!nextMobilePlayer) {
        mobileFullscreenAttemptedAtRef.current = 0;
        unlockPlayerLandscape();
      }
    };

    updateMobileEnvironment();
    window.addEventListener("resize", updateMobileEnvironment);
    window.addEventListener("orientationchange", updateMobileEnvironment);
    screen.orientation?.addEventListener?.("change", updateMobileEnvironment);
    return () => {
      window.removeEventListener("resize", updateMobileEnvironment);
      window.removeEventListener("orientationchange", updateMobileEnvironment);
      screen.orientation?.removeEventListener?.("change", updateMobileEnvironment);
      unlockPlayerLandscape();
    };
  }, []);

  useEffect(() => {
    showControlsRef.current = showControls;
  }, [showControls]);

  useEffect(() => {
    if (!profile || preferencesProfileIdRef.current !== profile.id) return;
    localStorage.setItem(`streamhome_player_preferences_${profile.id}`, JSON.stringify(preferences));
  }, [preferences, profile]);

  useEffect(() => {
    if (!profile || preferencesProfileIdRef.current === profile.id) return;
    const restored = loadPreferences(profile.id);
    preferencesProfileIdRef.current = profile.id;
    volumeRef.current = restored.volume;
    mutedRef.current = restored.muted;
    setPreferences(restored);
    setVolume(restored.volume);
    setMuted(restored.muted);
  }, [profile]);

  useEffect(() => {
    if (!asset || !runResponse) return;
    const authoritative = authoritativePlaybackDuration(
      runResponse.sourceMetadata.duration,
      asset.durationLabel,
      streamMode,
      videoRef.current?.duration ?? 0,
    );
    if (authoritative > 0) setDuration(authoritative);
  }, [asset, runResponse?.sourceMetadata.duration, streamMode]);

  useEffect(() => {
    const playOnLoad = true;
    if (visualFixture) {
      setFatal(null);
      setAsset(visualFixture.asset);
      setEpisodeSequence(visualFixture.episodeSequence);
      setRunResponse(visualFixture.runResponse);
      sequenceNumberRef.current = visualFixture.runResponse.nextSequenceNumber;
      resumePositionRef.current = visualFixture.runResponse.resumePosition;
      currentTimeRef.current = visualFixture.runResponse.resumePosition;
      confirmedTimeRef.current = visualFixture.runResponse.resumePosition;
      setCurrentTime(visualFixture.runResponse.resumePosition);
      setDuration(authoritativePlaybackDuration(
        visualFixture.runResponse.sourceMetadata.duration,
        visualFixture.asset.durationLabel,
        "progressive",
        0,
      ));
      playbackIntentRef.current = playOnLoad;
      playbackLoadingSuspendedRef.current = false;
      staleVideoPlayEventRef.current = false;
      setPlaybackLoadingSuspended(false);
      const fixtureMode = initialPlaybackMode(
        visualFixture.runResponse,
        preferences.audioTrackId,
        preferences.audioLanguage,
        videoRef.current,
      );
      setStreamMode(fixtureMode);
      setPhase(playbackTransportIsReady(visualFixture.runResponse, fixtureMode) ? "loading" : "preparing");
      return;
    }
    if (!profile || !mediaId) {
      setFatal({ title: "Playback unavailable", message: "Choose a profile and a playable title first.", retryable: false });
      setPhase("unavailable");
      return;
    }
    const abort = new AbortController();
    let active = true;
    setPhase("resolving");
    setFatal(null);
    setAsset(null);
    setEpisodeSequence([]);
    setRunResponse(null);
    setCurrentTime(0);
    currentTimeRef.current = 0;
    confirmedTimeRef.current = 0;
    setDuration(0);
    setBufferedEnd(0);
    setAvailableQualities([{ id: "auto", label: "Auto", height: "auto", index: -1, ready: true, status: "ready" }]);
    setSelectedQualityId("auto");
    setAvailableAudioTracks([]);
    setSelectedAudioTrackId("");
    selectedAudioTrackIdRef.current = "";
    activeExternalAudioTrackIdRef.current = null;
    externalAudioBufferingRef.current = false;
    externalAudioPlayRequestRef.current += 1;
    videoPlayRequestRef.current += 1;
    const externalAudio = externalAudioRef.current;
    if (externalAudio) {
      externalAudio.pause();
      externalAudio.removeAttribute("src");
      externalAudio.load();
    }
    setPlayerNotice("");
    hlsAudioOptionsRef.current = [];
    hlsQualityOptionsRef.current = [];
    setControlMenuOpen(false);
    setTimelineScrubbing(false);
    showControlsRef.current = true;
    setShowControls(true);
    setNextCountdown(null);
    setNextCancelled(false);
    setStreamMode("hls");
    setTransportRevision(0);
    resumeAppliedRef.current = false;
    resumePositionRef.current = 0;
    pendingSeekTargetRef.current = null;
    pendingPositionKindRef.current = null;
    positionGenerationRef.current += 1;
    pendingSeekReportRef.current = false;
    progressiveFailedRef.current = false;
    transportGenerationRef.current += 1;
    transportResettingRef.current = false;
    playbackIntentRef.current = playOnLoad;
    playbackLoadingSuspendedRef.current = false;
    staleVideoPlayEventRef.current = false;
    setPlaybackLoadingSuspended(false);
    if (playbackStartupTimerRef.current !== null) window.clearTimeout(playbackStartupTimerRef.current);
    playbackStartupTimerRef.current = null;
    if (playbackClockAdvanceTimerRef.current !== null) window.clearTimeout(playbackClockAdvanceTimerRef.current);
    playbackClockAdvanceTimerRef.current = null;
    if (hlsRetryTimerRef.current !== null) window.clearTimeout(hlsRetryTimerRef.current);
    hlsRetryTimerRef.current = null;
    if (stallRecoveryTimerRef.current !== null) window.clearTimeout(stallRecoveryTimerRef.current);
    stallRecoveryTimerRef.current = null;
    stallRecoveryAttemptsRef.current = 0;
    playbackStartupRetriesRef.current = 0;
    playbackStartupStartedAtRef.current = 0;
    playbackStartupProgressAtRef.current = 0;
    playbackStartupStageRef.current = "transport-initializing";
    playbackStartupFaultRef.current = null;
    deferredResumeTargetRef.current = null;
    pendingQualitySelectionRef.current = null;
    deferredStartupQualityRef.current = null;
    pendingAudioSelectionRef.current = null;
    lastFrameCaptureAtRef.current = 0;
    hasLastFrameRef.current = false;
    setHasLastFrame(false);
    const frameCanvas = lastFrameCanvasRef.current;
    frameCanvas?.getContext("2d")?.clearRect(0, 0, frameCanvas.width, frameCanvas.height);
    sequenceNumberRef.current = 1;
    pendingWatchedSecondsRef.current = 0;
    completedRef.current = false;
    runClosedRef.current = false;
    networkRetriesRef.current = 0;
    mediaRecoveriesRef.current = 0;

    const resolveAssetAndCreateRun = async (): Promise<ResolvedPlayback> => {
      let resolvedAsset: PlayableAsset;
      let sequence: Episode[] = [];
      let response: PlaybackRunResponse;
      if (mediaId.startsWith("m_")) {
        const [movie, createdRun] = await Promise.all([
          getMovie(mediaId, abort.signal),
          createPlaybackRun(mediaId, profile.id, undefined, abort.signal),
        ]);
        resolvedAsset = assetFromMovie(movie);
        response = createdRun;
      } else if (mediaId.startsWith("ep_")) {
        const tmdbId = episodeTmdbId(mediaId);
        if (tmdbId === null) throw new Error("This episode has an invalid catalog identity.");
        const movieId = `tv_${tmdbId}`;
        const [matchedMovie, createdRun] = await Promise.all([
          getMovie(movieId, abort.signal),
          createPlaybackRun(movieId, profile.id, mediaId, abort.signal),
        ]);
        const sequenceResult = matchedMovie.episodes?.length
          ? matchedMovie.episodes
          : await getEpisodes(tmdbId, abort.signal);
        const matchedEpisode = sequenceResult.find((item) => item.id === mediaId) ?? null;
        if (!matchedMovie || !matchedEpisode) throw new Error("This episode is not present in the server catalog.");
        resolvedAsset = assetFromEpisode(matchedMovie, matchedEpisode);
        sequence = sequenceResult;
        response = createdRun;
      } else {
        throw new Error("Choose a playable item before opening the player.");
      }

      setAsset(resolvedAsset);
      setEpisodeSequence(sequence);
      setRunResponse(response);
      sequenceNumberRef.current = response.nextSequenceNumber;
      resumePositionRef.current = response.resumePosition;
      setCurrentTime(response.resumePosition);
      currentTimeRef.current = response.resumePosition;
      confirmedTimeRef.current = response.resumePosition;
      if (response.preparationState === "error") {
        throw new ApiError(response.preparationError?.message || "The adaptive stream could not be prepared.", 503, response.preparationError?.code || "PREPARATION_FAILED");
      }
      if (response.preparationState === "preparing") setPhase("preparing");
      return { asset: resolvedAsset, episodeSequence: sequence, runResponse: response };
    };

    resolveAssetAndCreateRun()
      .then((resolved) => {
        if (!active) return;
        setAsset(resolved.asset);
        setEpisodeSequence(resolved.episodeSequence);
        setRunResponse(resolved.runResponse);
        sequenceNumberRef.current = resolved.runResponse.nextSequenceNumber;
        const initialMode = initialPlaybackMode(
          resolved.runResponse,
          preferences.audioTrackId,
          preferences.audioLanguage,
          videoRef.current,
        );
        setStreamMode(initialMode);
        setPhase(playbackTransportIsReady(resolved.runResponse, initialMode) ? "loading" : "preparing");
      })
      .catch((error: unknown) => {
        if (!active || (error instanceof DOMException && error.name === "AbortError")) return;
        const nextFatal = errorState(error);
        setFatal(nextFatal);
        setPhase(nextFatal.retryable ? "fatal" : "unavailable");
      });

    return () => {
      active = false;
      abort.abort();
    };
  }, [mediaId, profile, retryVersion, visualFixture]);

  const captureLastFrame = useCallback((force = false) => {
    const video = videoRef.current;
    const canvas = lastFrameCanvasRef.current;
    if (!video || !canvas || video.videoWidth <= 0 || video.videoHeight <= 0 || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return false;
    if (hasLastFrameRef.current && video.readyState < HTMLMediaElement.HAVE_FUTURE_DATA) return true;
    const now = performance.now();
    if (!force && now - lastFrameCaptureAtRef.current < 500) return hasLastFrameRef.current;
    const scale = Math.min(1, 1280 / video.videoWidth);
    const width = Math.max(1, Math.round(video.videoWidth * scale));
    const height = Math.max(1, Math.round(video.videoHeight * scale));
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
    const context = canvas.getContext("2d");
    if (!context) return false;
    try {
      context.drawImage(video, 0, 0, canvas.width, canvas.height);
    } catch {
      return false;
    }
    lastFrameCaptureAtRef.current = now;
    if (!hasLastFrameRef.current) {
      hasLastFrameRef.current = true;
      setHasLastFrame(true);
    }
    return true;
  }, []);

  const syncExternalAudio = useCallback((force = false, targetOverride: number | null = null) => {
    const video = videoRef.current;
    const audio = externalAudioRef.current;
    if (!video || !audio || !activeExternalAudioTrackIdRef.current || audio.readyState < HTMLMediaElement.HAVE_METADATA) return false;
    const target = Math.max(0, targetOverride ?? (
      pendingSeekTargetRef.current
      ?? (transportResettingRef.current ? resumePositionRef.current : video.currentTime)
    ));
    if (!force && (externalAudioBufferingRef.current || audio.seeking || audio.readyState < HTMLMediaElement.HAVE_FUTURE_DATA)) {
      return false;
    }
    const drift = Math.abs(audio.currentTime - target);
    const now = performance.now();
    const recoverSevereDrift = !force
      && drift >= EXTERNAL_AUDIO_DRIFT_RECOVERY_SECONDS
      && now - externalAudioLastDriftRecoveryAtRef.current >= EXTERNAL_AUDIO_DRIFT_RECOVERY_COOLDOWN_MS;
    const plan = externalAudioSyncPlan(target, audio.currentTime, video.playbackRate, force || recoverSevereDrift);
    if (plan.seekTime !== null) {
      try {
        audio.currentTime = plan.seekTime;
        if (recoverSevereDrift) externalAudioLastDriftRecoveryAtRef.current = now;
      } catch {
        return false;
      }
    }
    if (Math.abs(audio.playbackRate - plan.playbackRate) > 0.001) audio.playbackRate = plan.playbackRate;
    return true;
  }, []);

  const pauseExternalAudio = useCallback(() => {
    externalAudioPlayRequestRef.current += 1;
    externalAudioRef.current?.pause();
  }, []);

  const requestExternalAudioPlay = useCallback((blockedNotice: string) => {
    const audio = externalAudioRef.current;
    if (!audio || !activeExternalAudioTrackIdRef.current || !playbackIntentRef.current || completedRef.current) return;
    if (pendingExternalAudioPlayRequestRef.current === externalAudioPlayRequestRef.current) return;
    syncExternalAudio();
    const request = ++externalAudioPlayRequestRef.current;
    pendingExternalAudioPlayRequestRef.current = request;
    void audio.play()
      .then(() => {
        if (request !== externalAudioPlayRequestRef.current || !playbackIntentRef.current) return;
        window.requestAnimationFrame(() => {
          if (request === externalAudioPlayRequestRef.current && playbackIntentRef.current) syncExternalAudio();
        });
      })
      .catch((error: unknown) => {
        if (request !== externalAudioPlayRequestRef.current || !playbackIntentRef.current) return;
        if ((error instanceof DOMException || error instanceof Error) && error.name === "AbortError") return;
        setPlayerNotice(blockedNotice);
      })
      .finally(() => {
        if (pendingExternalAudioPlayRequestRef.current === request) pendingExternalAudioPlayRequestRef.current = null;
      });
  }, [syncExternalAudio]);

  const releaseExternalAudio = useCallback(() => {
    const audio = externalAudioRef.current;
    if (audio) {
      pauseExternalAudio();
      audio.removeAttribute("src");
      audio.load();
    }
    activeExternalAudioTrackIdRef.current = null;
    externalAudioBufferingRef.current = false;
    externalAudioLastDriftRecoveryAtRef.current = Number.NEGATIVE_INFINITY;
    const video = videoRef.current;
    if (video) {
      video.volume = volumeRef.current;
      video.muted = mutedRef.current;
    }
  }, [pauseExternalAudio]);

  const activateExternalAudio = useCallback((track: Pick<PlayerAudioOption, "id" | "directUrl">) => {
    const video = videoRef.current;
    const audio = externalAudioRef.current;
    if (!video || !audio || !track.directUrl) return false;
    activeExternalAudioTrackIdRef.current = track.id;
    externalAudioBufferingRef.current = false;
    externalAudioLastDriftRecoveryAtRef.current = Number.NEGATIVE_INFINITY;
    if (audio.getAttribute("src") !== track.directUrl) {
      audio.src = track.directUrl;
      audio.load();
    }
    audio.volume = volumeRef.current;
    audio.muted = mutedRef.current;
    audio.playbackRate = video.playbackRate;
    audio.preservesPitch = true;
    video.preservesPitch = true;
    video.volume = volumeRef.current;
    video.muted = true;
    syncExternalAudio(true, authoritativePlaybackPosition(
      confirmedTimeRef.current,
      resumePositionRef.current,
      pendingSeekTargetRef.current,
    ));
    return true;
  }, [syncExternalAudio]);

  const rememberOutputVolume = useCallback((nextVolume: number, nextMuted: boolean) => {
    const boundedVolume = Math.min(1, Math.max(0, nextVolume));
    volumeRef.current = boundedVolume;
    mutedRef.current = nextMuted;
    setVolume(boundedVolume);
    setMuted(nextMuted);
    setPreferences((current) => current.volume === boundedVolume && current.muted === nextMuted
      ? current
      : { ...current, volume: boundedVolume, muted: nextMuted });
    return boundedVolume;
  }, []);

  const setOutputVolume = useCallback((nextVolume: number, nextMuted: boolean) => {
    const boundedVolume = rememberOutputVolume(nextVolume, nextMuted);
    const video = videoRef.current;
    const audio = externalAudioRef.current;
    if (video) {
      video.volume = boundedVolume;
      video.muted = activeExternalAudioTrackIdRef.current ? true : nextMuted;
    }
    if (audio) {
      audio.volume = boundedVolume;
      audio.muted = nextMuted;
    }
  }, [rememberOutputVolume]);

  const toggleOutputMute = useCallback(() => {
    setOutputVolume(volumeRef.current, !mutedRef.current);
  }, [setOutputVolume]);

  const applyResume = useCallback((video: HTMLVideoElement) => {
    if (resumeAppliedRef.current && pendingSeekTargetRef.current === null) return true;
    const position = authoritativePlaybackPosition(
      confirmedTimeRef.current,
      resumePositionRef.current,
      pendingSeekTargetRef.current,
    );
    resumePositionRef.current = position;
    if (position <= 0) {
      currentTimeRef.current = 0;
      setCurrentTime(0);
    } else if (streamMode === "progressive" && Number.isFinite(video.duration) && video.duration > 0 && position < video.duration) {
      pendingSeekTargetRef.current = position;
      pendingPositionKindRef.current = "resume";
      transportResettingRef.current = true;
      video.currentTime = position;
      currentTimeRef.current = position;
      setCurrentTime(position);
    } else if (isPlaybackTimeSeekable(video.seekable, position)) {
      pendingSeekTargetRef.current = position;
      pendingPositionKindRef.current = "resume";
      transportResettingRef.current = true;
      video.currentTime = position;
      currentTimeRef.current = position;
      setCurrentTime(position);
    } else {
      return false;
    }
    resumeAppliedRef.current = true;
    return true;
  }, [streamMode]);

  const preservePlaybackPosition = useCallback((settling = true) => {
    const target = authoritativePlaybackPosition(
      confirmedTimeRef.current,
      resumePositionRef.current,
      pendingSeekTargetRef.current,
    );
    resumePositionRef.current = target;
    currentTimeRef.current = target;
    if (settling) {
      const hadPendingTarget = pendingSeekTargetRef.current !== null;
      pendingSeekTargetRef.current = target > 0 ? target : null;
      if (!hadPendingTarget) pendingPositionKindRef.current = target > 0 ? "recovery" : null;
      positionGenerationRef.current += 1;
    }
    return target;
  }, []);

  const clearPlaybackStartupWatchdog = useCallback(() => {
    if (playbackStartupTimerRef.current !== null) {
      window.clearTimeout(playbackStartupTimerRef.current);
      playbackStartupTimerRef.current = null;
    }
  }, []);

  const markPlaybackStartupReady = useCallback(() => {
    clearPlaybackStartupWatchdog();
    playbackStartupRetriesRef.current = 0;
    playbackStartupStartedAtRef.current = 0;
    playbackStartupProgressAtRef.current = 0;
  }, [clearPlaybackStartupWatchdog]);

  const clearPlaybackClockAdvanceWatchdog = useCallback(() => {
    if (playbackClockAdvanceTimerRef.current !== null) {
      window.clearTimeout(playbackClockAdvanceTimerRef.current);
      playbackClockAdvanceTimerRef.current = null;
    }
  }, []);

  const cancelPendingVideoPlay = useCallback(() => {
    if (pendingVideoPlayRequestRef.current !== null) staleVideoPlayEventRef.current = true;
    videoPlayRequestRef.current += 1;
    pendingVideoPlayRequestRef.current = null;
  }, []);

  const requestVideoPlay = useCallback((blockedNotice = "The browser blocked playback. Press play again to continue.") => {
    const video = videoRef.current;
    if (!video || !playbackIntentRef.current || completedRef.current) return;
    if (pendingVideoPlayRequestRef.current === videoPlayRequestRef.current) return;
    staleVideoPlayEventRef.current = false;
    const request = ++videoPlayRequestRef.current;
    pendingVideoPlayRequestRef.current = request;
    void video.play()
      .catch((error: unknown) => {
        if (request !== videoPlayRequestRef.current || !playbackIntentRef.current) return;
        if ((error instanceof DOMException || error instanceof Error) && error.name === "AbortError") return;
        playbackIntentRef.current = false;
        markPlaybackStartupReady();
        setPlayerNotice(blockedNotice);
        setPhase("paused");
      })
      .finally(() => {
        if (pendingVideoPlayRequestRef.current === request) pendingVideoPlayRequestRef.current = null;
      });
  }, [markPlaybackStartupReady]);

  const markPlaybackStartupProgress = useCallback((stage: PlaybackStartupStage) => {
    playbackStartupStageRef.current = stage;
    playbackStartupProgressAtRef.current = performance.now();
  }, []);

  const submitPlaybackStartupDiagnostic = useCallback((transport: StreamMode, fault: PlaybackStartupFault | null = playbackStartupFaultRef.current) => {
    const activeRun = runResponseRef.current;
    const video = videoRef.current;
    if (!activeRun || !video) return;
    let bufferedUntil = 0;
    for (let index = 0; index < video.buffered.length; index += 1) {
      if (video.buffered.start(index) <= video.currentTime + 0.25) {
        bufferedUntil = Math.max(bufferedUntil, video.buffered.end(index));
      }
    }
    void reportPlaybackStartupDiagnostic(activeRun.runId, {
      transport,
      stage: playbackStartupStageRef.current,
      errorType: fault?.type || null,
      errorDetail: fault?.detail || null,
      httpStatus: fault?.httpStatus || null,
      readyState: video.readyState,
      networkState: video.networkState,
      currentTime: Math.max(0, video.currentTime || currentTimeRef.current),
      bufferedUntil,
      elapsedMs: Math.max(0, Math.round(performance.now() - playbackStartupStartedAtRef.current)),
    }).catch(() => undefined);
  }, []);

  const applyDeferredResume = useCallback((video: HTMLVideoElement) => {
    const target = deferredResumeTargetRef.current;
    if (target === null || !isPlaybackTimeSeekable(video.seekable, target)) return false;
    video.currentTime = target;
    currentTimeRef.current = target;
    resumePositionRef.current = target;
    pendingSeekTargetRef.current = target;
    pendingPositionKindRef.current = "resume";
    resumeAppliedRef.current = true;
    deferredResumeTargetRef.current = null;
    setCurrentTime(target);
    setPlayerNotice("Resume position reached.");
    return true;
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!runResponse || !video) return;
    const transportReady = playbackTransportIsReady(runResponse, streamMode);
    if (!transportReady) return;
    if (!playbackIntentRef.current || playbackLoadingSuspendedRef.current) {
      markPlaybackStartupReady();
      setPhase("paused");
      return;
    }
    const transportGeneration = ++transportGenerationRef.current;
    preservePlaybackPosition();
    resumeAppliedRef.current = false;
    transportResettingRef.current = true;
    setPhase("loading");
    networkRetriesRef.current = 0;
    mediaRecoveriesRef.current = 0;
    cancelPendingVideoPlay();
    clearPlaybackClockAdvanceWatchdog();
    clearPlaybackStartupWatchdog();
    const startupStartedAt = performance.now();
    playbackStartupStartedAtRef.current = startupStartedAt;
    playbackStartupProgressAtRef.current = startupStartedAt;
    playbackStartupStageRef.current = streamMode === "progressive" ? "direct-metadata" : "transport-initializing";
    const armStartupWatchdog = () => {
      clearPlaybackStartupWatchdog();
      playbackStartupTimerRef.current = window.setTimeout(() => {
        playbackStartupTimerRef.current = null;
        if (!playbackIntentRef.current || playbackLoadingSuspendedRef.current) {
          setPhase("paused");
          return;
        }
        const now = performance.now();
        if (
          streamMode !== "progressive"
          && !progressiveFailedRef.current
          && canUseProgressivePlayback(
            runResponse,
            preferences.audioTrackId,
            preferences.audioLanguage,
            video,
          )
        ) {
          captureLastFrame(true);
          preservePlaybackPosition();
          resumeAppliedRef.current = false;
          transportResettingRef.current = true;
          setPlayerNotice("Adaptive startup stalled. Switching to the protected source stream…");
          setPhase("recovering");
          setStreamMode("progressive");
          return;
        }
        if (
          streamMode !== "progressive"
          && shouldExtendPlaybackStartup(
            now,
            playbackStartupStartedAtRef.current,
            playbackStartupProgressAtRef.current,
          )
        ) {
          setPlayerNotice("The adaptive stream is still loading…");
          armStartupWatchdog();
          return;
        }
        if (shouldRetryPlaybackStartup(playbackStartupRetriesRef.current)) {
          playbackStartupRetriesRef.current += 1;
          captureLastFrame(true);
          preservePlaybackPosition();
          resumeAppliedRef.current = false;
          transportResettingRef.current = true;
          setPhase("recovering");
          setPlayerNotice("Playback startup stalled. Refreshing the stream once…");
          void getPlaybackRun(runResponse.runId, { retry: true })
            .then((refreshed) => {
              if (transportGenerationRef.current !== transportGeneration) return;
              sequenceNumberRef.current = refreshed.nextSequenceNumber;
              setRunResponse((current) => current ? mergePlaybackRunMetadata(current, refreshed) : refreshed);
              setTransportRevision((value) => value + 1);
            })
            .catch((error: unknown) => {
              if (transportGenerationRef.current !== transportGeneration) return;
              setFatal({
                title: "Playback did not start",
                message: error instanceof Error
                  ? error.message
                  : "The stream could not be refreshed. Retry this title.",
                retryable: true,
              });
              setPhase("fatal");
            });
          return;
        }
        setFatal({
          title: "Playback did not start",
          message: playbackStartupFailureMessage(
            streamMode,
            playbackStartupStageRef.current,
            playbackStartupFaultRef.current,
          ),
          retryable: true,
        });
        submitPlaybackStartupDiagnostic(streamMode);
        setPhase("fatal");
      }, PLAYBACK_STARTUP_TIMEOUT_MS);
    };
    armStartupWatchdog();

    hlsRef.current?.destroy();
    hlsRef.current = null;
    releaseExternalAudio();
    video.removeAttribute("src");
    video.load();

    const serverAudioTracks = runResponse.tracks
      .filter((track) => ["streamable", "ready"].includes(track.status))
      .map((track) => ({
        id: track.id,
        label: languageDisplayName(track.language, track.label),
        language: normalizeLanguageTag(track.language),
        index: -1,
        source: track.source,
        directUrl: track.directUrl,
        default: track.default,
        status: track.status,
      }));
    setAvailableAudioTracks(serverAudioTracks);
    const serverPreferredAudio = serverAudioTracks.find((track) => track.id === preferences.audioTrackId)
      ?? serverAudioTracks.find((track) => track.language === preferences.audioLanguage)
      ?? serverAudioTracks.find((track) => track.default)
      ?? serverAudioTracks[0];
    const requestedProgressiveTrack = progressiveAudioTrack(runResponse.tracks, preferences.audioTrackId, preferences.audioLanguage);
    const embeddedTracks = runResponse.tracks.filter((track) => track.source === "embedded");
    const progressiveTrack = requestedProgressiveTrack && (
      requestedProgressiveTrack.source === "external"
      || requestedProgressiveTrack.default
      || embeddedTracks.length <= 1
    )
      ? requestedProgressiveTrack
      : embeddedTracks.find((track) => track.default) ?? embeddedTracks[0] ?? requestedProgressiveTrack;
    const selectedTransportAudioId = (
      streamMode === "progressive"
        ? progressiveTrack?.id ?? ""
        : serverAudioTracks.find((track) => track.default)?.id ?? serverAudioTracks[0]?.id ?? ""
    );
    selectedAudioTrackIdRef.current = selectedTransportAudioId;
    setSelectedAudioTrackId(selectedTransportAudioId);
    if (serverPreferredAudio && serverPreferredAudio.id !== selectedAudioTrackIdRef.current) {
      pendingAudioSelectionRef.current = serverPreferredAudio.id;
    }
    const beginPlayback = () => {
      video.playbackRate = preferences.playbackRate;
      if (streamMode === "progressive") {
        if (!applyResume(video)) {
          setPhase("recovering");
          return;
        }
      } else if (resumePositionRef.current > 0 && (!runResponse.resumeReady || !applyResume(video))) {
        deferredResumeTargetRef.current = resumePositionRef.current;
        if (!playbackIntentRef.current || playbackLoadingSuspendedRef.current) {
          markPlaybackStartupReady();
          setPhase("paused");
          return;
        }
        setPhase("recovering");
        setPlayerNotice("Preparing your saved position before playback starts.");
        return;
      } else if (!applyResume(video)) {
        setPhase("recovering");
        return;
      }
      const shouldResumePlayback = shouldResumePlaybackAfterTransport(
        playbackIntentRef.current,
        completedRef.current,
      );
      if (!shouldResumePlayback) {
        markPlaybackStartupReady();
        setPhase("paused");
        return;
      }
      requestVideoPlay("Playback is ready. Press play to begin.");
    };

    if (streamMode === "progressive") {
      if (progressiveTrack?.source === "external" && progressiveTrack.directUrl) {
        activateExternalAudio({ id: progressiveTrack.id, directUrl: progressiveTrack.directUrl });
      }
      video.src = runResponse.progressiveUrl;
      video.addEventListener("loadedmetadata", beginPlayback, { once: true });
      video.load();
      return () => {
        clearPlaybackStartupWatchdog();
        clearPlaybackClockAdvanceWatchdog();
        captureLastFrame(true);
        transportResettingRef.current = true;
        video.removeEventListener("loadedmetadata", beginPlayback);
      };
    }

    const HlsRuntime = hlsEngine;
    if (HlsRuntime?.isSupported() && runResponse.manifestUrl) {
      setStreamMode("hls");
      const hls = new HlsRuntime({
        enableWorker: true,
        capLevelToPlayerSize: true,
        startLevel: -1,
        startPosition: runResponse.resumeReady ? Math.max(0, resumePositionRef.current) : 0,
        maxBufferLength: FORWARD_BUFFER_TARGET_SECONDS,
        maxMaxBufferLength: FORWARD_BUFFER_MAX_SECONDS,
        backBufferLength: 30,
        manifestLoadingMaxRetry: 2,
        levelLoadingMaxRetry: 2,
        fragLoadingMaxRetry: 3,
        abrEwmaDefaultEstimate: 5_000_000,
        maxStarvationDelay: 4,
        maxLoadingDelay: 4,
      });
      hlsRef.current = hls;
      const applyDeferredStartupQuality = () => {
        const deferred = deferredStartupQualityRef.current;
        if (!deferred) return;
        const level = hls.levels[deferred.index];
        const levelBitrate = Math.max(0, Number(level?.bitrate || level?.averageBitrate || 0));
        const bufferedAhead = Math.max(0, bufferedEndForTime(video.buffered, video.currentTime) - video.currentTime);
        if (!shouldApplyDeferredStartupQuality(levelBitrate, hls.bandwidthEstimate, bufferedAhead)) return;
        deferredStartupQualityRef.current = null;
        pendingQualitySelectionRef.current = deferred.id;
        hls.currentLevel = deferred.index;
        hls.nextLevel = deferred.index;
      };
      hls.attachMedia(video);
      hls.on(HlsRuntime.Events.MEDIA_ATTACHED, () => {
        markPlaybackStartupProgress("media-attached");
        hls.loadSource(runResponse.manifestUrl!);
      });
      hls.on(HlsRuntime.Events.MANIFEST_PARSED, (_, data) => {
        markPlaybackStartupProgress("manifest-parsed");
        const options = playbackQualityOptions(runResponse.renditions, data.levels);
        hlsQualityOptionsRef.current = options;
        setAvailableQualities(options);
        const requestedQuality = pendingQualitySelectionRef.current
          ? options.find((item) => item.id === pendingQualitySelectionRef.current && item.ready)
          : undefined;
        const readyOptions = options.filter((item) => item.ready && item.height !== "auto");
        const preferred = requestedQuality || (preferences.qualityHeight === "auto" || readyOptions.length === 0
          ? options[0]
          : readyOptions.reduce((best, item) => Math.abs(Number(item.height) - Number(preferences.qualityHeight)) < Math.abs(Number(best.height) - Number(preferences.qualityHeight)) ? item : best));
        if (requestedQuality && requestedQuality.index >= 0) {
          deferredStartupQualityRef.current = null;
          hls.currentLevel = requestedQuality.index;
        } else {
          hls.currentLevel = -1;
          deferredStartupQualityRef.current = preferred && preferred.index >= 0
            ? { id: preferred.id, index: preferred.index }
            : null;
          setSelectedQualityId("auto");
        }
        beginPlayback();
      });
      hls.on(HlsRuntime.Events.LEVEL_SWITCHING, () => {
        setPlayerNotice("Switching video quality…");
      });
      hls.on(HlsRuntime.Events.LEVEL_SWITCHED, (_, data) => {
        const selected = hlsQualityOptionsRef.current.find((item) => item.index === data.level);
        const deferred = deferredStartupQualityRef.current;
        if (deferred && data.level !== deferred.index) {
          setSelectedQualityId("auto");
          setPlayerNotice("Automatic startup quality is protecting the playback buffer.");
          return;
        }
        const requestedQuality = pendingQualitySelectionRef.current;
        const automatic = requestedQuality === "auto"
          || (requestedQuality === null && preferences.qualityHeight === "auto");
        const selectedId = automatic ? "auto" : selected?.id ?? "auto";
        setSelectedQualityId(selectedId);
        pendingQualitySelectionRef.current = null;
        setPreferences((current) => ({ ...current, qualityHeight: automatic ? "auto" : selected?.height ?? "auto" }));
        setPlayerNotice(automatic ? "Automatic quality active" : selected ? `${selected.label} quality active` : "Automatic quality active");
      });
      hls.on(HlsRuntime.Events.LEVEL_UPDATED, () => {
        markPlaybackStartupProgress("level-playlist");
        const deferredApplied = applyDeferredResume(video);
        if (!deferredApplied && !resumeAppliedRef.current && !applyResume(video)) return;
        if (playbackIntentRef.current) {
          requestVideoPlay();
        } else {
          setPhase("paused");
        }
      });
      hls.on(HlsRuntime.Events.AUDIO_TRACKS_UPDATED, (_, data) => {
        markPlaybackStartupProgress("audio-playlist");
        const matchedIndexes = matchAudioTrackIndexes(runResponse.tracks, data.audioTracks);
        const tracks = runResponse.tracks
          .map((serverTrack, serverIndex) => {
            const language = normalizeLanguageTag(serverTrack.language);
            return {
              id: serverTrack.id,
              label: languageDisplayName(serverTrack.language, serverTrack.label),
              language,
              index: matchedIndexes[serverIndex],
              source: serverTrack.source,
              directUrl: serverTrack.directUrl,
              default: serverTrack.default,
              status: serverTrack.status,
            };
          })
        hlsAudioOptionsRef.current = tracks;
        setAvailableAudioTracks(tracks);
        const transportTrack = pendingAudioSelectionRef.current
          ? tracks.find((track) => track.id === pendingAudioSelectionRef.current && track.index >= 0)
          : tracks.find((track) => track.id === preferences.audioTrackId && track.index >= 0)
            ?? tracks.find((track) => track.language === preferences.audioLanguage && track.index >= 0);
        if (transportTrack) {
          releaseExternalAudio();
          hls.audioTrack = transportTrack.index;
          if (transportTrack.id !== selectedAudioTrackIdRef.current) {
            setPlayerNotice(`Switching audio to ${transportTrack.label}...`);
          }
          return;
        }
        const directTrack = pendingAudioSelectionRef.current
          ? tracks.find((track) => track.id === pendingAudioSelectionRef.current && Boolean(track.directUrl))
          : tracks.find((track) => track.id === preferences.audioTrackId && Boolean(track.directUrl))
            ?? tracks.find((track) => track.language === preferences.audioLanguage && Boolean(track.directUrl));
        if (directTrack?.directUrl && activateExternalAudio(directTrack)) {
          selectedAudioTrackIdRef.current = directTrack.id;
          setSelectedAudioTrackId(directTrack.id);
          pendingAudioSelectionRef.current = null;
          setPreferences((current) => ({
            ...current,
            audioTrackId: directTrack.id,
            audioLanguage: normalizeLanguageTag(directTrack.language, ""),
          }));
          setPlayerNotice(`${directTrack.label} audio active`);
          return;
        }
        const requestedTrack = pendingAudioSelectionRef.current
          ? tracks.find((track) => track.id === pendingAudioSelectionRef.current && track.index >= 0)
          : undefined;
        const preferredTrack = requestedTrack
          || tracks.find((track) => track.id === preferences.audioTrackId && track.index >= 0)
          || tracks.find((track) => track.language === preferences.audioLanguage && track.index >= 0)
          || tracks.find((track) => track.index >= 0);
        if (preferredTrack) {
          hls.audioTrack = preferredTrack.index;
          if (preferredTrack.id !== selectedAudioTrackIdRef.current) setPlayerNotice(`Switching audio to ${preferredTrack.label}…`);
        }
      });
      hls.on(HlsRuntime.Events.AUDIO_TRACK_SWITCHING, (_, data) => {
        const selected = hlsAudioOptionsRef.current.find((track) => track.index === data.id);
        if (selected) setPlayerNotice(`Switching audio to ${selected.label}…`);
      });
      hls.on(HlsRuntime.Events.AUDIO_TRACK_SWITCHED, (_, data) => {
        markPlaybackStartupProgress("audio-playlist");
        const selected = hlsAudioOptionsRef.current.find((track) => track.index === data.id);
        if (!selected) return;
        releaseExternalAudio();
        selectedAudioTrackIdRef.current = selected.id;
        setSelectedAudioTrackId(selected.id);
        pendingAudioSelectionRef.current = null;
        setPreferences((current) => ({
          ...current,
          audioTrackId: selected.id,
          audioLanguage: normalizeLanguageTag(selected.language, ""),
        }));
        setPlayerNotice(`${selected.label} audio active`);
      });
      hls.on(HlsRuntime.Events.LEVEL_LOADED, () => markPlaybackStartupProgress("level-playlist"));
      hls.on(HlsRuntime.Events.FRAG_LOADED, () => markPlaybackStartupProgress("fragment-loaded"));
      hls.on(HlsRuntime.Events.FRAG_BUFFERED, () => {
        markPlaybackStartupProgress("fragment-buffered");
        applyDeferredStartupQuality();
      });
      hls.on(HlsRuntime.Events.ERROR, (_, data) => {
        const responseCode = typeof data.response?.code === "number" ? data.response.code : null;
        playbackStartupFaultRef.current = {
          type: String(data.type || "hls-error").replace(/[^a-zA-Z0-9_.-]/g, "-").slice(0, 80),
          detail: String(data.details || "unknown").replace(/[^a-zA-Z0-9_.-]/g, "-").slice(0, 120),
          httpStatus: responseCode,
        };
        if (!playbackIntentRef.current || playbackLoadingSuspendedRef.current) return;
        if (!data.fatal) return;
        if (data.type === HlsRuntime.ErrorTypes.NETWORK_ERROR && networkRetriesRef.current < NETWORK_RETRY_LIMIT) {
          networkRetriesRef.current += 1;
          setPhase("recovering");
          captureLastFrame(true);
          if (hlsRetryTimerRef.current !== null) window.clearTimeout(hlsRetryTimerRef.current);
          hlsRetryTimerRef.current = window.setTimeout(() => {
            hlsRetryTimerRef.current = null;
            if (
              hlsRef.current === hls
              && playbackIntentRef.current
              && !playbackLoadingSuspendedRef.current
            ) hls.startLoad(currentTimeRef.current);
          }, 500 * networkRetriesRef.current);
          return;
        }
        if (data.type === HlsRuntime.ErrorTypes.MEDIA_ERROR && mediaRecoveriesRef.current < MEDIA_RECOVERY_LIMIT) {
          mediaRecoveriesRef.current += 1;
          setPhase("recovering");
          hls.recoverMediaError();
          return;
        }
        captureLastFrame(true);
        preservePlaybackPosition();
        const selectedTrack = runResponse.tracks.find((track) => track.id === selectedAudioTrackIdRef.current);
        if (selectedTrack?.source === "embedded" && !selectedTrack.default) {
          clearPlaybackStartupWatchdog();
          setFatal({
            title: "Selected audio interrupted",
            message: `${selectedTrack.label} requires adaptive playback, which could not recover. Retry to keep this audio track selected.`,
            retryable: true,
          });
          submitPlaybackStartupDiagnostic(streamMode, playbackStartupFaultRef.current);
          setPhase("fatal");
          return;
        }
        if (!progressiveFailedRef.current && canUseProgressivePlayback(
          runResponse,
          preferences.audioTrackId,
          preferences.audioLanguage,
          video,
        )) {
          setPhase("recovering");
          setPlayerNotice("Adaptive playback failed; using the protected source stream.");
          setStreamMode("progressive");
          return;
        }
        clearPlaybackStartupWatchdog();
        setFatal({
          title: "Adaptive playback failed",
          message: `The browser could not load the prepared stream (${String(data.details || "unknown HLS error")}). Retry this title.`,
          retryable: true,
        });
        submitPlaybackStartupDiagnostic(streamMode, playbackStartupFaultRef.current);
        setPhase("fatal");
      });
      return () => {
        clearPlaybackStartupWatchdog();
        clearPlaybackClockAdvanceWatchdog();
        if (hlsRetryTimerRef.current !== null) window.clearTimeout(hlsRetryTimerRef.current);
        hlsRetryTimerRef.current = null;
        captureLastFrame(true);
        transportResettingRef.current = true;
        hls.destroy();
        if (hlsRef.current === hls) hlsRef.current = null;
      };
    }

    if (runResponse.manifestUrl && video.canPlayType("application/vnd.apple.mpegurl")) {
      setStreamMode("native-hls");
      const syncNativeAudioTracks = () => {
        const nativeTracks = (video as HTMLVideoElement & {
          audioTracks?: ArrayLike<{ enabled: boolean; language?: string; label?: string }>;
        }).audioTracks;
        if (!nativeTracks?.length) return;
        const usedIndexes = new Set<number>();
        const options = runResponse.tracks
          .filter((track) => ["streamable", "ready"].includes(track.status))
          .map((track, serverIndex) => {
            const language = normalizeLanguageTag(track.language);
            let index = Array.from(nativeTracks).findIndex((nativeTrack, nativeIndex) => (
              !usedIndexes.has(nativeIndex)
              && normalizeLanguageTag(nativeTrack.language ?? "", "") === normalizeLanguageTag(track.language, "")
            ));
            if (index < 0 && serverIndex < nativeTracks.length && !usedIndexes.has(serverIndex)) index = serverIndex;
            if (index >= 0) usedIndexes.add(index);
            return {
              id: track.id,
              label: languageDisplayName(track.language, track.label),
              language,
              index,
              source: track.source,
              directUrl: track.directUrl,
              default: track.default,
              status: track.status,
            };
          })
          .filter((track) => track.index >= 0);
        setAvailableAudioTracks(options);
        const active = options.find((option) => nativeTracks[option.index]?.enabled)
          ?? options.find((option) => option.id === preferences.audioTrackId)
          ?? options.find((option) => option.default)
          ?? options[0];
        if (active) {
          selectedAudioTrackIdRef.current = active.id;
          setSelectedAudioTrackId(active.id);
        }
      };
      video.src = runResponse.manifestUrl;
      const onLoadedMetadata = () => {
        markPlaybackStartupProgress("manifest-parsed");
        syncNativeAudioTracks();
        beginPlayback();
      };
      video.addEventListener("loadedmetadata", onLoadedMetadata, { once: true });
      video.load();
      return () => {
        clearPlaybackStartupWatchdog();
        clearPlaybackClockAdvanceWatchdog();
        captureLastFrame(true);
        transportResettingRef.current = true;
        video.removeEventListener("loadedmetadata", onLoadedMetadata);
      };
    }

    if (runResponse.manifestUrl && !hlsEngine) return;
    if (!progressiveFailedRef.current && canUseProgressivePlayback(
      runResponse,
      preferences.audioTrackId,
      preferences.audioLanguage,
      video,
    )) {
      setStreamMode("progressive");
    }
    return clearPlaybackStartupWatchdog;
  }, [
    applyDeferredResume,
    applyResume,
    activateExternalAudio,
    cancelPendingVideoPlay,
    captureLastFrame,
    clearPlaybackStartupWatchdog,
    clearPlaybackClockAdvanceWatchdog,
    hlsEngine,
    markPlaybackStartupProgress,
    preservePlaybackPosition,
    runResponse?.manifestUrl,
    runResponse?.progressiveUrl,
    runResponse?.runId,
    runResponse?.sourceFingerprint,
    runResponse?.ticket,
    releaseExternalAudio,
    requestVideoPlay,
    streamMode,
    submitPlaybackStartupDiagnostic,
    transportRevision,
  ]);

  useEffect(() => {
    if (!runResponse || !profile || visualFixture) return;
    const renewIn = Math.max(30_000, runResponse.ticketExpiresAt * 1000 - Date.now() - TICKET_RENEWAL_MARGIN);
    const timer = window.setTimeout(() => {
      captureLastFrame(true);
      preservePlaybackPosition();
      resumeAppliedRef.current = false;
      transportResettingRef.current = true;
      void getPlaybackRun(runResponse.runId)
        .then((renewed) => {
          sequenceNumberRef.current = renewed.nextSequenceNumber;
          setRunResponse(renewed);
        })
        .catch((error: unknown) => {
          const nextFatal = errorState(error);
          setFatal(nextFatal);
          setPhase("fatal");
        });
    }, renewIn);
    return () => window.clearTimeout(timer);
  }, [captureLastFrame, preservePlaybackPosition, profile, runResponse?.runId, runResponse?.ticketExpiresAt, visualFixture]);

  useEffect(() => {
    if (!runResponse) return;
    setAvailableQualities((current) => {
      const auto = current.find((item) => item.id === "auto") ?? { id: "auto", label: "Auto", height: "auto" as const, index: -1, ready: true, status: "ready" as const };
      const currentById = new Map(current.map((item) => [item.id, item]));
      const renditions = runResponse.renditions
        .slice()
        .sort((left, right) => right.height - left.height)
        .map((rendition) => {
          const existing = currentById.get(rendition.id);
          const index = existing?.index ?? -1;
          return {
            id: rendition.id,
            label: `${rendition.label}${rendition.original ? " \u00b7 Original" : ""}`,
            height: rendition.height,
            index,
            ready: rendition.ready,
            status: rendition.status,
          };
        });
      return [auto, ...renditions];
    });
    setAvailableAudioTracks((current) => {
      const currentById = new Map(current.map((item) => [item.id, item]));
      return runResponse.tracks.map((track) => ({
        id: track.id,
        label: languageDisplayName(track.language, track.label),
        language: normalizeLanguageTag(track.language),
        index: currentById.get(track.id)?.index ?? -1,
        source: track.source,
        directUrl: track.directUrl,
        default: track.default,
        status: track.status,
      }));
    });
  }, [runResponse?.renditions, runResponse?.tracks]);

  useEffect(() => {
    selectedAudioTrackIdRef.current = selectedAudioTrackId;
  }, [selectedAudioTrackId]);

  useEffect(() => {
    if (!runResponse || visualFixture) return;
    if (runResponse.fullyPrepared) return;
    const abort = new AbortController();
    let attempts = 0;
    let consecutiveFailures = 0;
    let timer: number | null = null;
    let lastPolledResponse = runResponse;

    const poll = async () => {
      attempts += 1;
      try {
        const refreshed = await getPlaybackRun(runResponse.runId, { signal: abort.signal });
        if (abort.signal.aborted) return;
        consecutiveFailures = 0;
        const previous = lastPolledResponse;
        sequenceNumberRef.current = Math.max(sequenceNumberRef.current, refreshed.nextSequenceNumber);
        const requestedQuality = pendingQualitySelectionRef.current
          ? refreshed.renditions.find((item) => item.id === pendingQualitySelectionRef.current)
          : undefined;
        const requestedAudio = pendingAudioSelectionRef.current
          ? refreshed.tracks.find((item) => item.id === pendingAudioSelectionRef.current)
          : undefined;
        const readyStatus = (status: PlaybackRendition["status"] | PlaybackAudioTrack["status"]) => ["streamable", "ready"].includes(status);
        const qualityBecameReady = refreshed.renditions.some((item) => (
          readyStatus(item.status)
          && !previous.renditions.some((candidate) => candidate.id === item.id && readyStatus(candidate.status))
        ));
        const audioBecameReady = refreshed.tracks.some((item) => (
          readyStatus(item.status)
          && !previous.tracks.some((candidate) => candidate.id === item.id && readyStatus(candidate.status))
        ));
        const pendingQualityBecameReady = Boolean(
          pendingQualitySelectionRef.current
          && refreshed.renditions.some((item) => item.id === pendingQualitySelectionRef.current && readyStatus(item.status))
          && !previous.renditions.some((item) => item.id === pendingQualitySelectionRef.current && readyStatus(item.status)),
        );
        const pendingAudioBecameReady = Boolean(
          pendingAudioSelectionRef.current
          && refreshed.tracks.some((item) => item.id === pendingAudioSelectionRef.current && readyStatus(item.status))
          && !previous.tracks.some((item) => item.id === pendingAudioSelectionRef.current && readyStatus(item.status)),
        );
        const selectedQualityReady = Boolean(
          pendingQualitySelectionRef.current
          && refreshed.renditions.some((item) => item.id === pendingQualitySelectionRef.current && item.ready),
        );
        const selectedAudioReady = Boolean(
          pendingAudioSelectionRef.current
          && refreshed.tracks.some((item) => item.id === pendingAudioSelectionRef.current && readyStatus(item.status)),
        );
        const adaptiveTransportRequired = streamMode !== "progressive" || progressiveFailedRef.current;
        const manifestChanged = refreshed.manifestUrl !== previous.manifestUrl && adaptiveTransportRequired;
        const fingerprintChanged = refreshed.sourceFingerprint !== previous.sourceFingerprint;
        const preparationCompleted = refreshed.fullyPrepared && !previous.fullyPrepared && adaptiveTransportRequired;
        lastPolledResponse = refreshed;
        setRunResponse((active) => active?.runId === refreshed.runId ? mergePlaybackRunMetadata(active, refreshed) : active);
        if (
          manifestChanged
          || fingerprintChanged
          || preparationCompleted
          || (
            streamMode !== "progressive"
            && (pendingQualityBecameReady || pendingAudioBecameReady || qualityBecameReady || audioBecameReady)
          )
        ) {
          setTransportRevision((value) => value + 1);
        }
        if (refreshed.preparationState === "error") {
          const nextFatal = errorState(new ApiError(
            refreshed.preparationError?.message || "The adaptive stream could not be prepared.",
            503,
            refreshed.preparationError?.code || "PREPARATION_FAILED",
          ));
          setFatal(nextFatal);
          setPhase("fatal");
          return;
        }
        if (
          refreshed.manifestUrl
          && (
            progressiveFailedRef.current
            || (streamMode === "progressive" && (selectedQualityReady || selectedAudioReady))
          )
        ) {
          captureLastFrame(true);
          preservePlaybackPosition();
          resumeAppliedRef.current = false;
          transportResettingRef.current = true;
          setFatal(null);
          setPhase("loading");
          setStreamMode("hls");
        } else if (refreshed.manifestUrl && phaseRef.current === "preparing") {
          setFatal(null);
          setPhase("loading");
        }
        if (requestedQuality?.status === "failed") pendingQualitySelectionRef.current = null;
        if (requestedAudio?.status === "failed") {
          pendingAudioSelectionRef.current = null;
          setPlayerNotice(`${requestedAudio.label} audio could not be prepared.`);
        }
        const stillPending = !refreshed.fullyPrepared;
        if (!stillPending) return;
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        consecutiveFailures += 1;
        if (consecutiveFailures >= 4) setPlayerNotice("Stream preparation status is temporarily unavailable; retrying.");
      }
      if (attempts < 720 && !abort.signal.aborted) {
        const latest = runResponseRef.current;
        const delay = consecutiveFailures > 0
          ? Math.min(15_000, 1_000 * (2 ** Math.min(consecutiveFailures, 4)))
          : !latest?.fullyPrepared ? attempts < 8 ? 500 : 1_500 : attempts < 24 ? 5_000 : 15_000;
        timer = window.setTimeout(poll, delay);
      }
    };

    timer = window.setTimeout(poll, 250);
    return () => {
      abort.abort();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [captureLastFrame, preservePlaybackPosition, runResponse?.runId, streamMode, visualFixture]);

  const applyPlaybackRatePreference = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    video.playbackRate = preferences.playbackRate;
  }, [preferences.playbackRate]);

  const updateCaptionOverlay = useCallback((mediaTime?: number) => {
    const video = videoRef.current;
    const clearCaptions = () => {
      if (!captionSignatureRef.current) return;
      captionSignatureRef.current = "";
      setCaptionLines([]);
    };
    if (!video || preferences.subtitleTrackId === "off") {
      clearCaptions();
      return;
    }
    const trackElement = Array.from(video.querySelectorAll<HTMLTrackElement>("track[data-subtitle-id]"))
      .find((candidate) => candidate.dataset.subtitleId === preferences.subtitleTrackId);
    const cues = trackElement?.track?.cues;
    if (!cues) {
      clearCaptions();
      return;
    }
    const clock = Number.isFinite(mediaTime) ? Number(mediaTime) : video.currentTime;
    const lines: string[] = [];
    for (let index = 0; index < cues.length; index += 1) {
      const cue = cues[index];
      if (!cue || !subtitleCueIsActive(cue.startTime, cue.endTime, clock, preferences.subtitleOffset)) continue;
      const text = plainSubtitleCueText((cue as VTTCue).text || "");
      if (text) lines.push(text);
    }
    const signature = lines.join("\u0000");
    if (signature === captionSignatureRef.current) return;
    captionSignatureRef.current = signature;
    setCaptionLines(lines);
  }, [preferences.subtitleOffset, preferences.subtitleTrackId]);

  const applySubtitlePreference = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    applySubtitleTrackSelection(video, preferences.subtitleTrackId);
    updateCaptionOverlay();
  }, [preferences.subtitleTrackId, updateCaptionOverlay]);

  useEffect(() => applySubtitlePreference(), [applySubtitlePreference, runResponse]);

  useEffect(() => updateCaptionOverlay(), [updateCaptionOverlay]);

  useEffect(() => {
    if (!runResponse || preferences.subtitleTrackId === "off") return;
    if (runResponse.subtitles.some((subtitle) => subtitle.id === preferences.subtitleTrackId)) return;
    const legacyLanguage = normalizeLanguageTag(preferences.subtitleTrackId, "");
    const migrated = runResponse.subtitles.find((subtitle) => normalizeLanguageTag(subtitle.language) === legacyLanguage);
    setPreferences((current) => ({ ...current, subtitleTrackId: migrated?.id ?? "off" }));
  }, [preferences.subtitleTrackId, runResponse]);

  useEffect(() => applyPlaybackRatePreference(), [applyPlaybackRatePreference, runResponse]);

  const captureWatchedTime = useCallback(() => {
    const video = videoRef.current;
    if (!video || video.paused || video.seeking || transportResettingRef.current || pendingSeekTargetRef.current !== null || video.readyState < HTMLMediaElement.HAVE_FUTURE_DATA) {
      lastAdvanceWallRef.current = null;
      lastAdvanceMediaRef.current = currentTimeRef.current;
      return;
    }
    const now = performance.now();
    const mediaTime = currentTimeRef.current;
    const previousWall = lastAdvanceWallRef.current;
    const previousMedia = lastAdvanceMediaRef.current;
    pendingWatchedSecondsRef.current += advancingPlaybackDelta(previousWall, previousMedia, now, mediaTime, true);
    lastAdvanceWallRef.current = now;
    lastAdvanceMediaRef.current = mediaTime;
  }, []);

  const reportProgress = useCallback((event: PlaybackProgressEvent, finished = false, keepalive = false) => {
    const activeRun = runResponseRef.current;
    if (!activeRun || visualFixture || runClosedRef.current) return;
    captureWatchedTime();
    const watchedSeconds = Math.floor(pendingWatchedSecondsRef.current);
    pendingWatchedSecondsRef.current -= watchedSeconds;
    const timestamp = Math.max(0, confirmedTimeRef.current);
    progressQueueRef.current = progressQueueRef.current
      .catch(() => undefined)
      .then(async () => {
        if (runClosedRef.current) {
          pendingWatchedSecondsRef.current += watchedSeconds;
          return;
        }
        const sequenceNumber = sequenceNumberRef.current;
        const request = {
          timestamp,
          durationWatched: watchedSeconds,
          isFinished: finished,
          sequenceNumber,
          event,
        } as const;
        try {
          const response = await updatePlaybackProgress(activeRun.runId, request, keepalive);
          if (runResponseRef.current?.runId !== activeRun.runId) return;
          sequenceNumberRef.current = response.nextSequenceNumber;
          return;
        } catch (error: unknown) {
          if (runResponseRef.current?.runId !== activeRun.runId) return;
          const action = playbackProgressFailureAction(error);
          if (action === "stop") {
            runClosedRef.current = true;
            return;
          }
          if (action !== "reconcile") {
            pendingWatchedSecondsRef.current += watchedSeconds;
            return;
          }
          try {
            const fresh = await getPlaybackRun(activeRun.runId);
            if (runResponseRef.current?.runId !== activeRun.runId) return;
            sequenceNumberRef.current = fresh.nextSequenceNumber;
            if (progressSequenceWasAccepted(sequenceNumber, fresh.nextSequenceNumber)) return;
            const retryResponse = await updatePlaybackProgress(
              activeRun.runId,
              { ...request, sequenceNumber: fresh.nextSequenceNumber },
              keepalive,
            );
            if (runResponseRef.current?.runId !== activeRun.runId) return;
            sequenceNumberRef.current = retryResponse.nextSequenceNumber;
          } catch (retryError: unknown) {
            if (playbackProgressFailureAction(retryError) === "stop") {
              runClosedRef.current = true;
              return;
            }
            pendingWatchedSecondsRef.current += watchedSeconds;
          }
        }
      });
  }, [captureWatchedTime, visualFixture]);

  useEffect(() => {
    if (!runResponse || visualFixture) return;
    const timer = window.setInterval(() => reportProgress("heartbeat"), 10_000);
    const onVisibility = () => {
      if (document.visibilityState === "hidden" && !completedRef.current) reportProgress("visibility", false, true);
    };
    const onPageHide = () => {
      closeActiveRun();
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pagehide", onPageHide);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pagehide", onPageHide);
    };
  }, [closeActiveRun, reportProgress, runResponse?.runId, visualFixture]);

  const clearStallRecovery = useCallback(() => {
    if (stallRecoveryTimerRef.current !== null) {
      window.clearTimeout(stallRecoveryTimerRef.current);
      stallRecoveryTimerRef.current = null;
    }
  }, []);

  const suspendPlaybackLoading = useCallback(() => {
    if (playbackLoadingSuspendedRef.current) return;
    playbackLoadingSuspendedRef.current = true;
    setPlaybackLoadingSuspended(true);
    markPlaybackStartupReady();
    clearPlaybackClockAdvanceWatchdog();
    clearStallRecovery();
    if (hlsRetryTimerRef.current !== null) {
      window.clearTimeout(hlsRetryTimerRef.current);
      hlsRetryTimerRef.current = null;
    }
    hlsRef.current?.stopLoad();
    const video = videoRef.current;
    if (video) {
      const stablePosition = pendingSeekTargetRef.current ?? Math.max(
        0,
        confirmedTimeRef.current,
        currentTimeRef.current,
        video.currentTime || 0,
      );
      resumePositionRef.current = stablePosition;
      currentTimeRef.current = stablePosition;
      pendingSeekTargetRef.current = stablePosition > 0 ? stablePosition : null;
      if (pendingPositionKindRef.current === null) {
        pendingPositionKindRef.current = stablePosition > 0 ? "recovery" : null;
      }
      positionGenerationRef.current += 1;
      resumeAppliedRef.current = false;
      video.preload = "none";
      if (!hlsRef.current && video.getAttribute("src")) {
        transportResettingRef.current = true;
        video.load();
      }
    }
    const audio = externalAudioRef.current;
    if (audio) {
      audio.preload = "none";
      if (audio.getAttribute("src")) audio.load();
    }
    if (!completedRef.current) setPhase("paused");
  }, [clearPlaybackClockAdvanceWatchdog, clearStallRecovery, markPlaybackStartupReady]);

  const safePlay = useCallback(() => {
    const wasSuspended = playbackLoadingSuspendedRef.current;
    playbackIntentRef.current = true;
    playbackLoadingSuspendedRef.current = false;
    staleVideoPlayEventRef.current = false;
    setPlaybackLoadingSuspended(false);
    const video = videoRef.current;
    const audio = externalAudioRef.current;
    if (video) video.preload = "auto";
    if (audio) audio.preload = "auto";
    if (wasSuspended) setPhase("loading");
    if (wasSuspended && hlsRef.current) hlsRef.current.startLoad(Math.max(0, currentTimeRef.current));
    if (wasSuspended && video && !hlsRef.current) {
      if (video.getAttribute("src")) {
        transportResettingRef.current = true;
        video.load();
        return;
      } else {
        setPhase("loading");
        setTransportRevision((value) => value + 1);
        return;
      }
    }
    requestVideoPlay();
  }, [requestVideoPlay]);

  const pausePlayback = useCallback(() => {
    playbackIntentRef.current = false;
    cancelPendingVideoPlay();
    pauseExternalAudio();
    videoRef.current?.pause();
    suspendPlaybackLoading();
  }, [cancelPendingVideoPlay, pauseExternalAudio, suspendPlaybackLoading]);

  const scheduleStallRecovery = useCallback((clockBaseline: number | null = null) => {
    if (!playbackIntentRef.current || stallRecoveryTimerRef.current !== null) return;
    const positionGeneration = positionGenerationRef.current;
    stallRecoveryTimerRef.current = window.setTimeout(() => {
      stallRecoveryTimerRef.current = null;
      const video = videoRef.current;
      if (!video || !playbackIntentRef.current || positionGenerationRef.current !== positionGeneration) return;
      const clockStillFrozen = clockBaseline !== null && video.currentTime <= clockBaseline + 0.05;
      if (!clockStillFrozen && video.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) return;
      const recoveryReadyState = clockStillFrozen ? HTMLMediaElement.HAVE_METADATA : video.readyState;
      if (!shouldRetryPlaybackStall(playbackIntentRef.current, recoveryReadyState, stallRecoveryAttemptsRef.current)) {
        setFatal({
          title: "Playback connection stalled",
          message: "The media source stopped responding after automatic recovery attempts. Retry this title.",
          retryable: true,
        });
        setPhase("fatal");
        return;
      }
      stallRecoveryAttemptsRef.current += 1;
      captureLastFrame(true);
      preservePlaybackPosition();
      resumeAppliedRef.current = false;
      transportResettingRef.current = true;
      setPhase("recovering");
      setPlayerNotice("The connection stalled. Recovering from the current position.");
      setTransportRevision((value) => value + 1);
    }, 4_000);
  }, [captureLastFrame, preservePlaybackPosition]);

  const armPlaybackClockAdvanceWatchdog = useCallback(() => {
    clearPlaybackClockAdvanceWatchdog();
    const video = videoRef.current;
    if (!video || !playbackIntentRef.current || video.paused || completedRef.current) return;
    const baseline = video.currentTime;
    playbackClockAdvanceTimerRef.current = window.setTimeout(() => {
      playbackClockAdvanceTimerRef.current = null;
      const activeVideo = videoRef.current;
      if (
        !activeVideo
        || !playbackIntentRef.current
        || activeVideo.paused
        || completedRef.current
        || activeVideo.currentTime > baseline + 0.05
      ) return;
      setPhase("buffering");
      scheduleStallRecovery(baseline);
    }, PLAYBACK_CLOCK_ADVANCE_TIMEOUT_MS);
  }, [clearPlaybackClockAdvanceWatchdog, scheduleStallRecovery]);

  const seek = useCallback((nextTime: number, report = true) => {
    const video = videoRef.current;
    if (!video) return;
    captureWatchedTime();
    clearPlaybackClockAdvanceWatchdog();
    clearStallRecovery();
    pauseExternalAudio();
    const requested = clampPlaybackTime(nextTime, video.duration, duration);
    const growingAdaptive = streamMode !== "progressive" && Boolean(runResponse && !runResponse.fullyPrepared);
    const bounded = growingAdaptive && runResponse
      ? clampGrowingPlaybackTime(requested, video.seekable, runResponse.seekableUntil)
      : requested;
    if (bounded + 0.1 < requested) {
      setPlayerNotice("The download is still expanding. Jumped to the latest available point.");
    }
    pendingSeekTargetRef.current = bounded;
    pendingPositionKindRef.current = "seek";
    resumePositionRef.current = bounded;
    currentTimeRef.current = bounded;
    positionGenerationRef.current += 1;
    transportResettingRef.current = true;
    deferredResumeTargetRef.current = null;
    pendingSeekReportRef.current = pendingSeekReportRef.current || report;
    lastAdvanceWallRef.current = null;
    lastAdvanceMediaRef.current = bounded;
    setCurrentTime(bounded);
    resumeAppliedRef.current = false;
    const adaptiveTargetReady = streamMode === "progressive" || isPlaybackTimeSeekable(video.seekable, bounded);
    if (adaptiveTargetReady) {
      video.currentTime = bounded;
      resumeAppliedRef.current = true;
    } else {
      captureLastFrame(true);
      cancelPendingVideoPlay();
      video.pause();
      setPhase("recovering");
      if (runResponse && !progressiveFailedRef.current && canUseProgressivePlayback(
        runResponse,
        selectedAudioTrackIdRef.current || preferences.audioTrackId,
        preferences.audioLanguage,
        video,
      )) {
        setStreamMode("progressive");
      } else {
        hlsRef.current?.startLoad(bounded);
      }
    }
  }, [cancelPendingVideoPlay, captureLastFrame, captureWatchedTime, clearPlaybackClockAdvanceWatchdog, clearStallRecovery, duration, pauseExternalAudio, preferences.audioLanguage, preferences.audioTrackId, runResponse, streamMode]);

  const reportConfirmedSeek = useCallback(() => {
    if (pendingSeekTargetRef.current !== null) {
      pendingSeekReportRef.current = true;
      return;
    }
    reportProgress("seek");
  }, [reportProgress]);

  const setControlsVisibility = useCallback((visible: boolean) => {
    showControlsRef.current = visible;
    setShowControls(visible);
  }, []);

  const scheduleControlsHide = useCallback(() => {
    if (controlsTimerRef.current !== null) window.clearTimeout(controlsTimerRef.current);
    controlsTimerRef.current = null;
    if (shouldAutoHidePlayerControls(phaseRef.current, controlMenuOpenRef.current, scrubbingRef.current)) {
      controlsTimerRef.current = window.setTimeout(() => {
        setControlsVisibility(false);
        controlsTimerRef.current = null;
      }, PLAYER_CONTROLS_IDLE_MS);
    }
  }, [setControlsVisibility]);

  const revealControls = useCallback(() => {
    setControlsVisibility(true);
    scheduleControlsHide();
  }, [scheduleControlsHide, setControlsVisibility]);

  const handleDesktopPointerActivity = useCallback((event: React.MouseEvent<HTMLDivElement>) => {
    const previous = desktopPointerPositionRef.current;
    const next = { x: event.clientX, y: event.clientY };
    desktopPointerPositionRef.current = next;
    if (!previous && !showControlsRef.current) {
      revealControls();
      return;
    }
    if (!isMeaningfulPointerActivity(previous, next, event.movementX, event.movementY)) return;
    revealControls();
  }, [revealControls]);

  useEffect(() => {
    phaseRef.current = phase;
    controlMenuOpenRef.current = controlMenuOpen;
    if (!shouldAutoHidePlayerControls(phase, controlMenuOpenRef.current, scrubbingRef.current)) {
      if (controlsTimerRef.current !== null) window.clearTimeout(controlsTimerRef.current);
      controlsTimerRef.current = null;
      return;
    }
    if (showControlsRef.current && controlsTimerRef.current === null) {
      scheduleControlsHide();
    }
  }, [controlMenuOpen, phase, scheduleControlsHide, timelineScrubbing]);

  useEffect(() => {
    if (phase !== "playing") return;
    const updateTimeline = () => {
      const video = videoRef.current;
      const timeline = timelineRef.current;
      if (video && timeline && !scrubbingRef.current) {
        const liveDuration = duration > 0 ? duration : Number.isFinite(video.duration) && video.duration > 0 ? video.duration : 0;
        const liveTime = Math.min(currentTimeRef.current, liveDuration || currentTimeRef.current);
        timeline.value = String(liveTime);
        timeline.style.setProperty("--player-progress", `${liveDuration > 0 ? Math.min(100, (liveTime / liveDuration) * 100) : 0}%`);
      }
      updateCaptionOverlay(video?.currentTime);
      syncExternalAudio();
      timelineAnimationFrameRef.current = window.requestAnimationFrame(updateTimeline);
    };
    timelineAnimationFrameRef.current = window.requestAnimationFrame(updateTimeline);
    return () => {
      if (timelineAnimationFrameRef.current !== null) window.cancelAnimationFrame(timelineAnimationFrameRef.current);
      timelineAnimationFrameRef.current = null;
    };
  }, [duration, phase, syncExternalAudio, updateCaptionOverlay]);

  const resolvePlayerContainer = useCallback((interactionTarget?: HTMLElement | null) => {
    const interactionContainer = interactionTarget?.matches("[data-player-root='true']")
      ? interactionTarget
      : interactionTarget?.closest<HTMLElement>("[data-player-root='true']");
    return containerRef.current
      ?? interactionContainer
      ?? document.querySelector<HTMLElement>("[data-player-root='true']");
  }, []);

  useEffect(() => {
    const container = resolvePlayerContainer();
    const video = videoRef.current;
    const updateFullscreenState = () => {
      const nextMode = playerFullscreenMode(container, video);
      const active = nextMode !== null;
      setFullscreenMode(nextMode);
      setFullscreenActive(active);
      if (active) setFullscreenError("");
      if (mobilePlayer) {
        if (active) void lockPlayerLandscape();
        else {
          mobileFullscreenAttemptedAtRef.current = 0;
          unlockPlayerLandscape();
        }
      }
    };
    const reportFullscreenError = () => {
      const nextMode = playerFullscreenMode(container, video);
      setFullscreenMode(nextMode);
      setFullscreenActive(nextMode !== null);
      if (nextMode !== null) {
        setFullscreenError("");
        return;
      }
      setFullscreenError("The browser rejected the fullscreen request. Check the fullscreen permission for this site.");
      setControlsVisibility(true);
    };

    updateFullscreenState();
    document.addEventListener("fullscreenchange", updateFullscreenState);
    document.addEventListener("fullscreenerror", reportFullscreenError);
    document.addEventListener("webkitfullscreenchange", updateFullscreenState);
    document.addEventListener("webkitfullscreenerror", reportFullscreenError);
    video?.addEventListener("webkitbeginfullscreen", updateFullscreenState);
    video?.addEventListener("webkitendfullscreen", updateFullscreenState);
    return () => {
      document.removeEventListener("fullscreenchange", updateFullscreenState);
      document.removeEventListener("fullscreenerror", reportFullscreenError);
      document.removeEventListener("webkitfullscreenchange", updateFullscreenState);
      document.removeEventListener("webkitfullscreenerror", reportFullscreenError);
      video?.removeEventListener("webkitbeginfullscreen", updateFullscreenState);
      video?.removeEventListener("webkitendfullscreen", updateFullscreenState);
      releaseViewportPlayerFullscreen(container);
    };
  }, [mobilePlayer, resolvePlayerContainer, runResponse?.runId, setControlsVisibility]);

  const toggleFullscreen = useCallback((interactionTarget?: HTMLElement | null) => {
    const container = resolvePlayerContainer(interactionTarget);
    const video = videoRef.current;
    if (!container || !video) return;

    setFullscreenError("");
    const operation = togglePlayerFullscreen(container, video, document, { allowVideoFallback: true });
    interactionTarget?.blur();
    container.focus({ preventScroll: true });
    void operation
      .then((result) => {
        const nextMode = playerFullscreenMode(container, video);
        const active = result === "entered" && nextMode !== null;
        setFullscreenMode(nextMode);
        setFullscreenActive(active);
        setFullscreenError("");
        if (active && nextMode === "viewport") {
          setPlayerNotice("This browser uses app fullscreen because native fullscreen is unavailable.");
        }
        if (mobilePlayer && active) void lockPlayerLandscape();
        if (mobilePlayer && !active) unlockPlayerLandscape();
        revealControls();
      })
      .catch((error: unknown) => {
        const nextMode = playerFullscreenMode(container, video);
        setFullscreenMode(nextMode);
        setFullscreenActive(nextMode !== null);
        setFullscreenError(error instanceof Error
          ? error.message
          : "Fullscreen could not be opened. Check this browser's fullscreen permission.");
        setControlsVisibility(true);
      })
      .finally(() => {
        window.requestAnimationFrame(() => container.focus({ preventScroll: true }));
      });
  }, [mobilePlayer, resolvePlayerContainer, revealControls, setControlsVisibility]);

  const ensureMobileLandscape = useCallback(() => {
    if (!mobilePlayer) return;
    if (fullscreenActive) {
      void lockPlayerLandscape();
      return;
    }
    const container = resolvePlayerContainer();
    const video = videoRef.current;
    if (!container || !video) return;
    const now = performance.now();
    if (mobileFullscreenAttemptedAtRef.current > 0 && now - mobileFullscreenAttemptedAtRef.current < 2_000) return;
    mobileFullscreenAttemptedAtRef.current = now;
    void togglePlayerFullscreen(container, video, document, { allowVideoFallback: true })
      .then(async (result) => {
        const nextMode = playerFullscreenMode(container, video);
        const active = result === "entered" && nextMode !== null;
        setFullscreenMode(nextMode);
        setFullscreenActive(active);
        if (active) await lockPlayerLandscape();
      })
      .catch(() => {
        mobileFullscreenAttemptedAtRef.current = 0;
        // The CSS-rotated landscape presentation remains active when browser policy rejects fullscreen.
      });
  }, [fullscreenActive, mobilePlayer, resolvePlayerContainer]);

  const startMobilePlayback = useCallback(() => {
    ensureMobileLandscape();
    safePlay();
  }, [ensureMobileLandscape, safePlay]);

  const handleDesktopSurfaceClick = useCallback((event: React.MouseEvent<HTMLDivElement>) => {
    if (mobilePlayer || (event.target !== event.currentTarget && event.target !== videoRef.current)) return;
    revealControls();
    if (event.detail !== 1) return;
    if (desktopClickTimerRef.current !== null) window.clearTimeout(desktopClickTimerRef.current);
    desktopClickTimerRef.current = window.setTimeout(() => {
      desktopClickTimerRef.current = null;
      const video = videoRef.current;
      if (!video) return;
      if (video.paused) safePlay();
      else pausePlayback();
    }, 220);
  }, [mobilePlayer, pausePlayback, revealControls, safePlay]);

  const handleDesktopSurfaceDoubleClick = useCallback((event: React.MouseEvent<HTMLDivElement>) => {
    if (mobilePlayer || (event.target !== event.currentTarget && event.target !== videoRef.current)) return;
    event.preventDefault();
    if (desktopClickTimerRef.current !== null) window.clearTimeout(desktopClickTimerRef.current);
    desktopClickTimerRef.current = null;
    toggleFullscreen(event.currentTarget);
    revealControls();
  }, [mobilePlayer, revealControls, toggleFullscreen]);

  const togglePictureInPicture = useCallback(() => {
    const video = videoRef.current;
    if (!video || !document.pictureInPictureEnabled) {
      setPlayerNotice("Picture-in-picture is not available in this browser.");
      return;
    }
    const operation = document.pictureInPictureElement ? document.exitPictureInPicture() : video.requestPictureInPicture();
    void operation
      .then(() => setPlayerNotice(document.pictureInPictureElement ? "Picture-in-picture active" : "Picture-in-picture closed"))
      .catch((error: unknown) => {
        setPlayerNotice(error instanceof Error ? error.message : "Picture-in-picture could not be opened.");
        setControlsVisibility(true);
      });
  }, [setControlsVisibility]);

  const startOver = useCallback(() => {
    if (!runResponse) return;
    void startOverPlaybackRun(runResponse.runId).then(() => {
      completedRef.current = false;
      resumePositionRef.current = 0;
      setNextCountdown(null);
      seek(0);
      safePlay();
    }).catch((error: unknown) => {
      setFatal(errorState(error));
      setPhase("fatal");
    });
  }, [runResponse, safePlay, seek]);

  const playNextEpisode = useCallback(() => {
    if (!runResponse?.nextEpisodeId || !profile) return;
    setNextCountdown(null);
    currentTimeRef.current = 0;
    setCurrentTime(0);
    navigate(appUrl(profile.id, "watch", { media: runResponse.nextEpisodeId }), { replace: true, state: location.state });
  }, [location.state, navigate, profile, runResponse?.nextEpisodeId]);

  const finishPlayback = useCallback(() => {
    playbackIntentRef.current = false;
    completedRef.current = true;
    setPhase("ended");
    reportProgress("ended", true, true);
    if (runResponse?.nextEpisodeId && !nextCancelled) setNextCountdown(NEXT_EPISODE_SECONDS);
  }, [nextCancelled, reportProgress, runResponse?.nextEpisodeId]);

  useEffect(() => {
    if (nextCountdown === null) return;
    if (nextCountdown <= 0) {
      playNextEpisode();
      return;
    }
    const timer = window.setTimeout(() => setNextCountdown((value) => value === null ? null : value - 1), 1_000);
    return () => window.clearTimeout(timer);
  }, [nextCountdown, playNextEpisode]);

  const handleControlMenuOpenChange = useCallback((open: boolean) => {
    controlMenuOpenRef.current = open;
    setControlMenuOpen(open);
    revealControls();
  }, [revealControls]);

  useEffect(() => () => {
    if (controlsTimerRef.current !== null) window.clearTimeout(controlsTimerRef.current);
    if (playbackStartupTimerRef.current !== null) window.clearTimeout(playbackStartupTimerRef.current);
    if (desktopClickTimerRef.current !== null) window.clearTimeout(desktopClickTimerRef.current);
    if (hlsRetryTimerRef.current !== null) window.clearTimeout(hlsRetryTimerRef.current);
    clearStallRecovery();
    releaseExternalAudio();
  }, [clearStallRecovery, releaseExternalAudio]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (fullscreenMode === "viewport") {
          suppressPlayerShortcutEvent(event);
          toggleFullscreen();
        } else if (!fullscreenActive && !isInteractiveTarget(event.target)) {
          exitPlayer();
        }
        revealControls();
        return;
      }
      if (isInteractiveTarget(event.target)) return;
      const shortcut = playerKeyboardShortcut(event);
      if (!shortcut) return;
      suppressPlayerShortcutEvent(event);
      if (shortcut === "fullscreen") {
        if (event.repeat) return;
        toggleFullscreen();
        revealControls();
        return;
      }
      if (shortcut === "play-pause") {
        if (event.repeat) return;
        const video = videoRef.current;
        if (playbackIntentRef.current && video && !video.paused) pausePlayback();
        else safePlay();
      } else if (shortcut === "seek-back") {
        seek(currentTimeRef.current - 10);
      } else if (shortcut === "seek-forward") {
        seek(currentTimeRef.current + 10);
      } else if (shortcut === "volume-up") {
        setOutputVolume(volumeRef.current + 0.05, false);
      } else if (shortcut === "volume-down") {
        setOutputVolume(volumeRef.current - 0.05, false);
      } else if (shortcut === "mute" && videoRef.current) {
        if (event.repeat) return;
        toggleOutputMute();
      } else if (shortcut === "pip") {
        if (event.repeat) return;
        togglePictureInPicture();
      }
      revealControls();
    };
    const handleKeyUp = (event: KeyboardEvent) => {
      if (isInteractiveTarget(event.target) || !playerKeyboardShortcut(event)) return;
      suppressPlayerShortcutEvent(event);
    };
    document.addEventListener("keydown", handleKeyDown, true);
    document.addEventListener("keyup", handleKeyUp, true);
    return () => {
      document.removeEventListener("keydown", handleKeyDown, true);
      document.removeEventListener("keyup", handleKeyUp, true);
    };
  }, [exitPlayer, fullscreenActive, fullscreenMode, pausePlayback, revealControls, safePlay, seek, setOutputVolume, toggleFullscreen, toggleOutputMute, togglePictureInPicture]);

  const toggleMobileControls = useCallback(() => {
    if (showControlsRef.current) {
      if (controlsTimerRef.current) window.clearTimeout(controlsTimerRef.current);
      setControlsVisibility(false);
      return;
    }
    revealControls();
  }, [revealControls, setControlsVisibility]);

  const resetMobileTapTimers = useCallback(() => {
    if (mobileSingleTapTimerRef.current !== null) window.clearTimeout(mobileSingleTapTimerRef.current);
    if (mobileTapResetTimerRef.current !== null) window.clearTimeout(mobileTapResetTimerRef.current);
    mobileSingleTapTimerRef.current = null;
    mobileTapResetTimerRef.current = null;
  }, []);

  const handleMobileSurfaceTap = useCallback((side: MobileTapSide) => {
    ensureMobileLandscape();
    const now = performance.now();
    const previous = mobileTapChainRef.current;
    if (previous && previous.seekSteps > 0 && (previous.side !== side || now - previous.lastTapAt > MOBILE_TAP_CHAIN_WINDOW)) {
      reportConfirmedSeek();
      setMobileSeekFeedback(null);
      mobileTapChainRef.current = null;
    }
    const result = nextMobileTap(mobileTapChainRef.current, side, now);
    mobileTapChainRef.current = result.chain;

    if (mobileTapResetTimerRef.current !== null) window.clearTimeout(mobileTapResetTimerRef.current);
    mobileTapResetTimerRef.current = window.setTimeout(() => {
      if ((mobileTapChainRef.current?.seekSteps ?? 0) > 0) reportConfirmedSeek();
      mobileTapChainRef.current = null;
      setMobileSeekFeedback(null);
      mobileTapResetTimerRef.current = null;
    }, MOBILE_TAP_CHAIN_WINDOW + 60);

    if (result.seekDelta !== 0) {
      if (mobileSingleTapTimerRef.current !== null) window.clearTimeout(mobileSingleTapTimerRef.current);
      mobileSingleTapTimerRef.current = null;
      seek(currentTimeRef.current + result.seekDelta, false);
      setMobileSeekFeedback({ side, seconds: result.accumulatedSeconds });
      return;
    }

    if (mobileSingleTapTimerRef.current !== null) window.clearTimeout(mobileSingleTapTimerRef.current);
    mobileSingleTapTimerRef.current = window.setTimeout(() => {
      if (mobileTapChainRef.current?.seekSteps === 0) toggleMobileControls();
      mobileTapChainRef.current = null;
      mobileSingleTapTimerRef.current = null;
    }, MOBILE_TAP_CHAIN_WINDOW);
  }, [ensureMobileLandscape, reportConfirmedSeek, seek, toggleMobileControls]);

  const handleMobileCenterTap = useCallback(() => {
    ensureMobileLandscape();
    if ((mobileTapChainRef.current?.seekSteps ?? 0) > 0) reportConfirmedSeek();
    resetMobileTapTimers();
    mobileTapChainRef.current = null;
    setMobileSeekFeedback(null);
    toggleMobileControls();
  }, [ensureMobileLandscape, reportConfirmedSeek, resetMobileTapTimers, toggleMobileControls]);

  useEffect(() => () => {
    resetMobileTapTimers();
  }, [resetMobileTapTimers]);

  const handleMobilePointerDown = useCallback((side: MobileTapSide | "center", event: React.PointerEvent<HTMLDivElement>) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    mobilePointerGestureRef.current = { id: event.pointerId, side, x: event.clientX, y: event.clientY, at: performance.now() };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }, []);

  const handleMobilePointerEnd = useCallback((side: MobileTapSide | "center", event: React.PointerEvent<HTMLDivElement>, cancelled = false) => {
    const start = mobilePointerGestureRef.current;
    mobilePointerGestureRef.current = null;
    if (!start || start.id !== event.pointerId || start.side !== side || cancelled) return;
    if (!isMobileTapCandidate(
      { x: start.x, y: start.y, at: start.at },
      { x: event.clientX, y: event.clientY, at: performance.now() },
    )) return;
    event.preventDefault();
    event.stopPropagation();
    if (side === "center") handleMobileCenterTap();
    else handleMobileSurfaceTap(side);
  }, [handleMobileCenterTap, handleMobileSurfaceTap]);

  const timelinePointerValue = useCallback((event: React.PointerEvent<HTMLInputElement>) => {
    const timeline = event.currentTarget;
    const rect = timeline.getBoundingClientRect();
    return timelineValueFromPointer(
      event.clientX,
      rect.left,
      rect.width,
      Number(timeline.min || 0),
      Number(timeline.max || duration || 0),
    );
  }, [duration]);

  const previewTimelineScrub = useCallback((value: number) => {
    currentTimeRef.current = value;
    setCurrentTime(value);
    updateCaptionOverlay(value);
    const timeline = timelineRef.current;
    const max = Number(timeline?.max || duration || 1);
    if (timeline) timeline.value = String(value);
    timeline?.style.setProperty("--player-progress", `${Math.min(100, (value / max) * 100)}%`);
  }, [duration, updateCaptionOverlay]);

  const updateTimelinePointerPreview = useCallback((event: React.PointerEvent<HTMLInputElement>, value: number) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const ratio = rect.width > 0 ? Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)) : 0;
    setTimelinePreview({ x: ratio * rect.width, time: value });
  }, []);

  const beginTimelineScrub = useCallback((event: React.PointerEvent<HTMLInputElement>) => {
    event.preventDefault();
    event.currentTarget.focus({ preventScroll: true });
    event.currentTarget.setPointerCapture?.(event.pointerId);
    scrubbingRef.current = true;
    setTimelineScrubbing(true);
    scrubOriginRef.current = currentTimeRef.current;
    if (controlsTimerRef.current) window.clearTimeout(controlsTimerRef.current);
    setControlsVisibility(true);
    const value = timelinePointerValue(event);
    previewTimelineScrub(value);
    updateTimelinePointerPreview(event, value);
  }, [previewTimelineScrub, setControlsVisibility, timelinePointerValue, updateTimelinePointerPreview]);

  const moveTimelinePointer = useCallback((event: React.PointerEvent<HTMLInputElement>) => {
    const value = timelinePointerValue(event);
    updateTimelinePointerPreview(event, value);
    if (scrubbingRef.current) previewTimelineScrub(value);
  }, [previewTimelineScrub, timelinePointerValue, updateTimelinePointerPreview]);

  const commitTimelineScrub = useCallback((value: number) => {
    if (!scrubbingRef.current) return;
    scrubbingRef.current = false;
    setTimelineScrubbing(false);
    seek(value);
    revealControls();
  }, [revealControls, seek]);

  const endTimelineScrub = useCallback((event: React.PointerEvent<HTMLInputElement>) => {
    event.preventDefault();
    const value = timelinePointerValue(event);
    updateTimelinePointerPreview(event, value);
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    commitTimelineScrub(value);
  }, [commitTimelineScrub, timelinePointerValue, updateTimelinePointerPreview]);

  const cancelTimelineScrub = useCallback(() => {
    if (!scrubbingRef.current) return;
    scrubbingRef.current = false;
    setTimelineScrubbing(false);
    previewTimelineScrub(scrubOriginRef.current);
    revealControls();
  }, [previewTimelineScrub, revealControls]);

  const changeQuality = (renditionId: string) => {
    const selected = availableQualities.find((item) => item.id === renditionId);
    if (!selected) return;
    deferredStartupQualityRef.current = null;
    pendingQualitySelectionRef.current = renditionId;
    setPlayerNotice(renditionId === "auto" ? "Enabling automatic quality…" : `Switching quality to ${selected.label}…`);
    if (renditionId === "auto") {
      if (hlsRef.current) {
        hlsRef.current.currentLevel = -1;
        hlsRef.current.nextLevel = -1;
      }
      setSelectedQualityId("auto");
      setPreferences((current) => ({ ...current, qualityHeight: "auto" }));
      pendingQualitySelectionRef.current = null;
      setPlayerNotice("Automatic quality active");
      return;
    }
    if (selected.status === "failed") {
      pendingQualitySelectionRef.current = null;
      setPlayerNotice(`${selected.label} is unavailable for this source.`);
      return;
    }
    if (!selected.ready) {
      if (!runResponse) {
        pendingQualitySelectionRef.current = null;
        setPlayerNotice("Playback quality preparation is unavailable without an active run.");
        return;
      }
      setPlayerNotice(`Preparing ${selected.label} quality without interrupting playback…`);
      void prioritizePlaybackQuality(runResponse.runId, selected.id)
        .then(() => {
          if (pendingQualitySelectionRef.current === selected.id) {
            setPlayerNotice(`${selected.label} preparation prioritized.`);
          }
        })
        .catch((error: unknown) => {
          if (pendingQualitySelectionRef.current === selected.id) pendingQualitySelectionRef.current = null;
          setPlayerNotice(error instanceof Error ? error.message : `${selected.label} could not be prepared.`);
        });
      return;
    }
    if (hlsRef.current && selected.index >= 0) {
      hlsRef.current.currentLevel = selected.index;
      hlsRef.current.nextLevel = selected.index;
      return;
    }
    if (runResponse?.manifestUrl) {
      captureLastFrame(true);
      preservePlaybackPosition();
      resumeAppliedRef.current = false;
      transportResettingRef.current = true;
      setStreamMode("hls");
      setTransportRevision((value) => value + 1);
      return;
    }
    pendingQualitySelectionRef.current = null;
    setPlayerNotice("Adaptive quality switching is unavailable for the current transport.");
  };

  const changeAudio = (trackId: string) => {
    const selected = availableAudioTracks.find((item) => item.id === trackId);
    if (!selected || selected.id === selectedAudioTrackId) return;
    if (selected.status === "failed") {
      pendingAudioSelectionRef.current = null;
      setPlayerNotice(`${selected.label} audio is unavailable for this source.`);
      return;
    }
    pendingAudioSelectionRef.current = trackId;
    setPlayerNotice(`Switching audio to ${selected.label}…`);
    if (hlsRef.current && selected.index >= 0) {
      hlsRef.current.audioTrack = selected.index;
      return;
    }
    const nativeTracks = (videoRef.current as HTMLVideoElement & { audioTracks?: ArrayLike<{ enabled: boolean }> } | null)?.audioTracks;
    if (streamMode === "native-hls" && nativeTracks && selected.index >= 0) {
      for (let trackIndex = 0; trackIndex < nativeTracks.length; trackIndex += 1) {
        nativeTracks[trackIndex].enabled = trackIndex === selected.index;
      }
      setSelectedAudioTrackId(selected.id);
      pendingAudioSelectionRef.current = null;
      setPreferences((current) => ({
        ...current,
        audioTrackId: selected.id,
        audioLanguage: normalizeLanguageTag(selected.language, ""),
      }));
      setPlayerNotice(`${selected.label} audio active`);
      return;
    }
    if (
      selected.source === "external"
      && runResponse?.preparationState === "ready"
      && runResponse.manifestUrl
    ) {
      captureLastFrame(true);
      preservePlaybackPosition();
      resumeAppliedRef.current = false;
      transportResettingRef.current = true;
      setStreamMode("hls");
      setTransportRevision((value) => value + 1);
      return;
    }
    if (selected.source === "external" && selected.directUrl && activateExternalAudio(selected)) {
      selectedAudioTrackIdRef.current = selected.id;
      setSelectedAudioTrackId(selected.id);
      pendingAudioSelectionRef.current = null;
      setPreferences((current) => ({
        ...current,
        audioTrackId: selected.id,
        audioLanguage: normalizeLanguageTag(selected.language, ""),
      }));
      const video = videoRef.current;
      const audio = externalAudioRef.current;
      if (video && audio && !video.paused && playbackIntentRef.current) {
        syncExternalAudio(true);
        requestExternalAudioPlay(`${selected.label} is ready. Press play once to allow audio playback.`);
      }
      setPlayerNotice(`${selected.label} audio active`);
      return;
    }
    if (selected.source === "embedded" && selected.default && streamMode === "progressive") {
      releaseExternalAudio();
      selectedAudioTrackIdRef.current = selected.id;
      setSelectedAudioTrackId(selected.id);
      pendingAudioSelectionRef.current = null;
      setPreferences((current) => ({
        ...current,
        audioTrackId: selected.id,
        audioLanguage: normalizeLanguageTag(selected.language, ""),
      }));
      setPlayerNotice(`${selected.label} audio active`);
      return;
    }
    if (runResponse?.manifestUrl) {
      captureLastFrame(true);
      preservePlaybackPosition();
      resumeAppliedRef.current = false;
      transportResettingRef.current = true;
      setStreamMode("hls");
      setTransportRevision((value) => value + 1);
      return;
    }
    pendingAudioSelectionRef.current = null;
    setPlayerNotice(`${selected.label} is not available in the active playback transport.`);
  };

  const retryPlayback = () => {
    if (runResponse?.preparationState === "error") {
      setFatal(null);
      setPhase("preparing");
      void getPlaybackRun(runResponse.runId, { retry: true })
        .then((response) => {
          sequenceNumberRef.current = response.nextSequenceNumber;
          setRunResponse(response);
          setRetryVersion((value) => value + 1);
        })
        .catch((error: unknown) => {
          setFatal(errorState(error));
          setPhase("fatal");
        });
      return;
    }
    setRetryVersion((value) => value + 1);
  };

  const skipMarker = asset ? activeSkipMarker(asset.skipMarkers, currentTime) : null;
  const hasSubtitles = hasSubtitleOptions(runResponse?.subtitles ?? []);
  const playbackControlShowsPause = playbackIntentRef.current
    && ["loading", "recovering", "buffering", "playing"].includes(phase);
  const phaseMessage: Record<PlayerPhase, string> = {
    resolving: "Resolving secure playback",
    preparing: "Preparing adaptive stream",
    loading: "Loading stream",
    playing: "Playing",
    paused: "Paused",
    buffering: "Buffering",
    recovering: streamMode === "progressive" ? "Switching to compatibility playback" : "Recovering stream",
    ended: "Playback complete",
    unavailable: "Playback unavailable",
    fatal: "Playback interrupted",
  };
  const preparationDetail = preparationStatusMessage(runResponse?.preparationProgress);
  const holdLastFrame = hasLastFrame && ["loading", "buffering", "recovering"].includes(phase);

  if (phase === "resolving" || phase === "preparing" || (phase === "loading" && !asset)) {
    return (
      <motion.div
        className="player-view player-state-view fixed inset-0 grid min-h-screen place-items-center bg-black px-6 text-white"
        data-mobile-player={mobilePlayer ? "true" : "false"}
        data-mobile-orientation={forcedLandscape ? "forced-landscape" : "native-landscape"}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
      >
        <div className="max-w-lg text-center">
          <div role="status" aria-live="polite">
            <motion.i className="mx-auto block h-11 w-11 rounded-full border-2 border-white/20 border-t-white" animate={reduced ? undefined : { rotate: 360 }} transition={{ duration: 0.8, repeat: Infinity, ease: "linear" }} />
            <p className="mt-5 text-sm tracking-[0.16em] text-white/70">{phaseMessage[phase]}</p>
            {phase === "preparing" && (
              <>
                <span className="mt-2 block text-xs text-white/50">{preparationDetail}</span>
                <span className="mt-2 block text-[0.7rem] tabular-nums text-white/35">
                  {formatDuration(preparationElapsedSeconds)} elapsed
                  {runResponse?.preparationProgress ? ` · ${runResponse.preparationProgress.activeWorkers} active worker${runResponse.preparationProgress.activeWorkers === 1 ? "" : "s"}` : ""}
                </span>
              </>
            )}
          </div>
          <Button className="mt-6" variant="ghost" onClick={exitPlayer}>Go back</Button>
        </div>
      </motion.div>
    );
  }

  if (fatal || !asset || !runResponse) {
    return (
      <motion.div
        className="player-view player-state-view fixed inset-0 grid min-h-screen place-items-center bg-black p-6 text-white"
        data-mobile-player={mobilePlayer ? "true" : "false"}
        data-mobile-orientation={forcedLandscape ? "forced-landscape" : "native-landscape"}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
      >
        <motion.div className="max-w-lg text-center" initial={reduced ? { opacity: 0 } : { opacity: 0, y: 18 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialogEnter, ease: MOTION_EASE }}>
          <p className="text-xs uppercase tracking-[0.2em] text-white/40">{phase === "unavailable" ? "Unavailable" : "Recovery required"}</p>
          <h1 className="mt-3 text-2xl font-semibold">{fatal?.title || "Playback unavailable"}</h1>
          <p className="mt-3 text-white/60">{fatal?.message || "The requested media could not be loaded."}</p>
          <div className="mt-6 flex justify-center gap-3">
            {fatal?.retryable && <Button onClick={retryPlayback}>Retry</Button>}
            <Button onClick={exitPlayer}>Go back</Button>
          </div>
        </motion.div>
      </motion.div>
    );
  }

  return (
    <motion.div
      ref={containerRef}
      className="player-view fixed inset-0 z-[200] overflow-hidden bg-black text-white"
      data-theme={theme}
      data-interaction={definition.interaction.id}
      data-player-theme={definition.playerVariant}
      data-player-phase={phase}
      data-player-root="true"
      data-frame-hold={holdLastFrame ? "true" : "false"}
      data-controls-visible={showControls ? "true" : "false"}
      data-mobile-player={mobilePlayer ? "true" : "false"}
      data-mobile-orientation={forcedLandscape ? "forced-landscape" : "native-landscape"}
      data-player-viewport-fullscreen={fullscreenMode === "viewport" ? "true" : undefined}
      tabIndex={-1}
      style={{ "--caption-scale": preferences.captionScale } as React.CSSProperties}
      onMouseMove={mobilePlayer ? undefined : handleDesktopPointerActivity}
      onClick={mobilePlayer ? undefined : handleDesktopSurfaceClick}
      onDoubleClick={mobilePlayer ? undefined : handleDesktopSurfaceDoubleClick}
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.viewEnter }}
    >
      <canvas
        ref={lastFrameCanvasRef}
        className="player-last-frame"
        aria-hidden="true"
      />
      <video
        ref={videoRef}
        className="player-video"
        crossOrigin="anonymous"
        preload={playbackLoadingSuspended ? "none" : "auto"}
        playsInline
        onLoadedMetadata={() => {
          const video = videoRef.current;
          if (!video) return;
          markPlaybackStartupProgress(streamMode === "progressive" ? "direct-metadata" : "manifest-parsed");
          const authoritativeDuration = authoritativePlaybackDuration(
            runResponse.sourceMetadata.duration,
            asset.durationLabel,
            streamMode,
            video.duration,
          );
          if (authoritativeDuration > 0) setDuration(authoritativeDuration);
          applyResume(video);
          syncExternalAudio(true);
          window.setTimeout(() => {
            applySubtitlePreference();
            updateCaptionOverlay(video.currentTime);
          }, 0);
        }}
        onPlay={() => {
          if (!playbackIntentRef.current) {
            const video = videoRef.current;
            if (!video || video.paused || staleVideoPlayEventRef.current) {
              staleVideoPlayEventRef.current = false;
              cancelPendingVideoPlay();
              video?.pause();
              setPhase("paused");
              return;
            }
            playbackIntentRef.current = true;
            playbackLoadingSuspendedRef.current = false;
            setPlaybackLoadingSuspended(false);
            video.preload = "auto";
            if (externalAudioRef.current) externalAudioRef.current.preload = "auto";
            hlsRef.current?.startLoad(Math.max(0, currentTimeRef.current));
          }
          if (!transportResettingRef.current) {
            setPhase("loading");
            lastAdvanceWallRef.current = performance.now();
            lastAdvanceMediaRef.current = currentTimeRef.current;
          }
        }}
        onPause={() => {
          pauseExternalAudio();
          captureWatchedTime();
          if (transportResettingRef.current && playbackIntentRef.current) return;
          if (playbackIntentRef.current) {
            playbackIntentRef.current = false;
            cancelPendingVideoPlay();
          }
          suspendPlaybackLoading();
          if (!completedRef.current) {
            setPhase("paused");
            reportProgress("pause");
          }
        }}
        onWaiting={() => {
          if (!playbackIntentRef.current) {
            setPhase("paused");
            return;
          }
          clearPlaybackClockAdvanceWatchdog();
          if (!hasLastFrameRef.current) captureLastFrame(true);
          pauseExternalAudio();
          setPhase("buffering");
          scheduleStallRecovery();
        }}
        onStalled={() => {
          if (!playbackIntentRef.current) {
            setPhase("paused");
            return;
          }
          clearPlaybackClockAdvanceWatchdog();
          if (!hasLastFrameRef.current) captureLastFrame(true);
          pauseExternalAudio();
          setPhase("buffering");
          scheduleStallRecovery();
        }}
        onPlaying={() => {
          if (!playbackIntentRef.current) {
            cancelPendingVideoPlay();
            videoRef.current?.pause();
            setPhase("paused");
            return;
          }
          clearStallRecovery();
          const observedTime = videoRef.current?.currentTime ?? currentTimeRef.current;
          const accepted = shouldAcceptObservedPlaybackTime(
            observedTime,
            currentTimeRef.current,
            transportResettingRef.current,
            pendingSeekTargetRef.current,
            pendingPositionKindRef.current !== "seek",
          );
          if (!accepted) {
            if (videoRef.current) applyResume(videoRef.current);
            setPhase("recovering");
            return;
          }
          if (pendingSeekTargetRef.current === null) transportResettingRef.current = false;
          const externalAudio = externalAudioRef.current;
          if (pendingSeekTargetRef.current === null && activeExternalAudioTrackIdRef.current && externalAudio) {
            if (externalAudio.readyState < HTMLMediaElement.HAVE_FUTURE_DATA) {
              externalAudioBufferingRef.current = true;
            } else {
              externalAudioBufferingRef.current = false;
              syncExternalAudio();
              requestExternalAudioPlay("Press play once to allow the selected dubbing track.");
            }
          }
          markPlaybackStartupProgress("playing");
          phaseRef.current = "loading";
          setPhase("loading");
          armPlaybackClockAdvanceWatchdog();
          window.requestAnimationFrame(() => captureLastFrame(true));
        }}
        onCanPlay={() => {
          const video = videoRef.current;
          if (!video) return;
          markPlaybackStartupProgress("can-play");
          if (!playbackIntentRef.current) {
            cancelPendingVideoPlay();
            video.pause();
            transportResettingRef.current = false;
            markPlaybackStartupReady();
            setPhase("paused");
            return;
          }
          const resumeReady = applyResume(video);
          if (transportResettingRef.current && !resumeReady) {
            setPhase("recovering");
            return;
          }
          if (video.paused && resumeReady) {
            setPhase("loading");
            window.setTimeout(() => {
              if (playbackIntentRef.current && video.paused && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
                requestVideoPlay();
              }
            }, 0);
            return;
          }
          setPhase(video.paused ? "paused" : transportResettingRef.current ? "recovering" : "loading");
        }}
        onTimeUpdate={() => {
          captureWatchedTime();
          const video = videoRef.current;
          if (!video) return;
          const nextTime = video.currentTime;
          updateCaptionOverlay(nextTime);
          const clockAdvanced = nextTime > confirmedTimeRef.current + 0.05;
          if (clockAdvanced) {
            clearPlaybackClockAdvanceWatchdog();
            clearStallRecovery();
            stallRecoveryAttemptsRef.current = 0;
          }
          if (!shouldAcceptObservedPlaybackTime(
            nextTime,
            currentTimeRef.current,
            transportResettingRef.current,
            pendingSeekTargetRef.current,
            pendingPositionKindRef.current !== "seek",
          )) return;
          const confirmedPendingSeek = pendingSeekTargetRef.current !== null && (
            Math.abs(nextTime - pendingSeekTargetRef.current) <= 1
            || (pendingPositionKindRef.current !== "seek" && nextTime >= pendingSeekTargetRef.current - 1)
          );
          if (confirmedPendingSeek) {
            pendingSeekTargetRef.current = null;
            pendingPositionKindRef.current = null;
          }
          currentTimeRef.current = nextTime;
          confirmedTimeRef.current = nextTime;
          resumePositionRef.current = nextTime;
          setCurrentTime(nextTime);
          if (confirmedPendingSeek) syncExternalAudio(true, nextTime);
          else syncExternalAudio();
          if (confirmedPendingSeek && activeExternalAudioTrackIdRef.current && !video.paused) {
            requestExternalAudioPlay("Press play once to allow the selected dubbing track.");
          }
          if ((clockAdvanced || confirmedPendingSeek) && playbackIntentRef.current && !video.paused) {
            transportResettingRef.current = false;
            markPlaybackStartupReady();
            phaseRef.current = "playing";
            setPhase("playing");
            scheduleControlsHide();
          }
          if (confirmedPendingSeek && playbackIntentRef.current && video.paused) requestVideoPlay();
          if (confirmedPendingSeek && pendingSeekReportRef.current) {
            pendingSeekReportRef.current = false;
            reportProgress("seek");
          }
          captureLastFrame();
        }}
        onSeeked={() => {
          const video = videoRef.current;
          if (!video) return;
          const nextTime = video.currentTime;
          updateCaptionOverlay(nextTime);
          const stableTime = currentTimeRef.current;
          const pendingTarget = pendingSeekTargetRef.current;
          if (!shouldAcceptObservedPlaybackTime(
            nextTime,
            stableTime,
            transportResettingRef.current,
            pendingTarget,
            pendingPositionKindRef.current !== "seek",
          )) return;
          pendingSeekTargetRef.current = null;
          pendingPositionKindRef.current = null;
          currentTimeRef.current = nextTime;
          confirmedTimeRef.current = nextTime;
          resumePositionRef.current = nextTime;
          setCurrentTime(nextTime);
          transportResettingRef.current = false;
          markPlaybackStartupReady();
          syncExternalAudio(true, nextTime);
          if (activeExternalAudioTrackIdRef.current && !video.paused) {
            requestExternalAudioPlay("Press play once to allow the selected dubbing track.");
          }
          if (pendingTarget !== null && pendingSeekReportRef.current) {
            pendingSeekReportRef.current = false;
            reportProgress("seek");
          }
          if (playbackIntentRef.current) {
            if (video.paused) requestVideoPlay();
            else {
              phaseRef.current = "playing";
              setPhase("playing");
              scheduleControlsHide();
            }
          }
        }}
        onSeeking={() => {
          pauseExternalAudio();
        }}
        onRateChange={() => {
          const video = videoRef.current;
          const audio = externalAudioRef.current;
          if (video && audio) audio.playbackRate = video.playbackRate;
        }}
        onDurationChange={() => {
          const mediaDuration = videoRef.current?.duration ?? 0;
          const authoritativeDuration = authoritativePlaybackDuration(
            runResponse.sourceMetadata.duration,
            asset.durationLabel,
            streamMode,
            mediaDuration,
          );
          if (authoritativeDuration > 0) setDuration(authoritativeDuration);
        }}
        onProgress={() => {
          const video = videoRef.current;
          if (!video) return;
          markPlaybackStartupProgress("fragment-buffered");
          applyDeferredResume(video);
          const pendingTarget = pendingSeekTargetRef.current;
          if (pendingTarget !== null && isPlaybackTimeSeekable(video.seekable, pendingTarget)) {
            video.currentTime = pendingTarget;
            resumeAppliedRef.current = true;
          }
          if (video.buffered.length === 0) {
            return;
          }
          let nextBufferedEnd = 0;
          for (let index = 0; index < video.buffered.length; index += 1) {
            if (video.buffered.start(index) <= video.currentTime + 0.25) nextBufferedEnd = Math.max(nextBufferedEnd, video.buffered.end(index));
          }
          setBufferedEnd(nextBufferedEnd);
        }}
        onVolumeChange={() => {
          if (activeExternalAudioTrackIdRef.current) {
            rememberOutputVolume(
              externalAudioRef.current?.volume ?? volumeRef.current,
              externalAudioRef.current?.muted ?? mutedRef.current,
            );
            return;
          }
          const nextVolume = videoRef.current?.volume ?? 1;
          const nextMuted = videoRef.current?.muted ?? false;
          rememberOutputVolume(nextVolume, nextMuted);
        }}
        onEnded={() => {
          clearPlaybackClockAdvanceWatchdog();
          pauseExternalAudio();
          finishPlayback();
        }}
        onError={() => {
          if (!videoRef.current?.getAttribute("src")) return;
          clearPlaybackClockAdvanceWatchdog();
          if (streamMode === "native-hls") {
            captureLastFrame(true);
            preservePlaybackPosition();
            resumeAppliedRef.current = false;
            transportResettingRef.current = true;
            if (!progressiveFailedRef.current && canUseProgressivePlayback(
              runResponse,
              selectedAudioTrackIdRef.current || preferences.audioTrackId,
              preferences.audioLanguage,
              videoRef.current,
            )) {
              setPhase("recovering");
              setStreamMode("progressive");
            } else {
              clearPlaybackStartupWatchdog();
              setFatal({ title: "Adaptive playback failed", message: "The browser could not decode the prepared adaptive stream.", retryable: true });
              submitPlaybackStartupDiagnostic("native-hls");
              setPhase("fatal");
            }
            return;
          }
          if (streamMode !== "progressive") return;
          progressiveFailedRef.current = true;
          clearPlaybackStartupWatchdog();
          if (runResponse.manifestUrl && runResponse.preparationState === "ready") {
            setFatal(null);
            setPhase("loading");
            setPlayerNotice("The source stream was not playable. Switching to the prepared adaptive stream.");
            setStreamMode("hls");
            setTransportRevision((value) => value + 1);
            return;
          }
          if (runResponse.preparationState === "preparing") {
            setFatal(null);
            setPhase("preparing");
            setPlayerNotice("The source stream was not playable. Finishing the adaptive stream now.");
            return;
          }
          setFatal({ title: "Compatibility playback failed", message: "Neither adaptive HLS nor direct progressive playback could decode this media.", retryable: true });
          submitPlaybackStartupDiagnostic("progressive");
          setPhase("fatal");
        }}
      >
        {runResponse.subtitles.map((subtitle) => (
          <track
            key={`${subtitle.id}-${runResponse.ticket}`}
            kind="subtitles"
            src={`/api/playback/subtitles/${encodeURIComponent(runResponse.mediaId)}/${encodeURIComponent(subtitle.id)}?ticket=${encodeURIComponent(runResponse.ticket)}`}
            srcLang={normalizeLanguageTag(subtitle.language)}
            label={languageDisplayName(subtitle.language, subtitle.label)}
            data-subtitle-id={subtitle.id}
            onLoad={() => {
              applySubtitlePreference();
              updateCaptionOverlay();
            }}
          />
        ))}
      </video>
      <audio
        ref={externalAudioRef}
        preload={playbackLoadingSuspended ? "none" : "auto"}
        aria-hidden="true"
        onLoadedMetadata={() => syncExternalAudio(true)}
        onCanPlay={() => {
          if (!activeExternalAudioTrackIdRef.current || !playbackIntentRef.current) return;
          const video = videoRef.current;
          const audio = externalAudioRef.current;
          externalAudioBufferingRef.current = false;
          if (!video || !audio || transportResettingRef.current || pendingSeekTargetRef.current !== null) return;
          syncExternalAudio();
          if (!video.paused && !completedRef.current) {
            requestExternalAudioPlay("Press play once to allow the selected dubbing track.");
          }
        }}
        onWaiting={() => {
          if (!activeExternalAudioTrackIdRef.current || !playbackIntentRef.current) return;
          externalAudioBufferingRef.current = true;
        }}
        onStalled={() => {
          if (!activeExternalAudioTrackIdRef.current || !playbackIntentRef.current) return;
          externalAudioBufferingRef.current = true;
        }}
        onError={() => {
          if (!activeExternalAudioTrackIdRef.current) return;
          const fallback = runResponse.tracks.find((track) => track.source === "embedded" && track.default)
            ?? runResponse.tracks.find((track) => track.source === "embedded");
          releaseExternalAudio();
          if (fallback) {
            selectedAudioTrackIdRef.current = fallback.id;
            setSelectedAudioTrackId(fallback.id);
            setPreferences((current) => ({
              ...current,
              audioTrackId: fallback.id,
              audioLanguage: normalizeLanguageTag(fallback.language, ""),
            }));
          }
          setPlayerNotice("The selected dubbing file could not be played. Restored the original audio.");
          if (playbackIntentRef.current) requestVideoPlay();
        }}
      />

      {captionLines.length > 0 && (
        <div className="player-caption-layer" role="region" aria-label="Subtitles" aria-live="off">
          {captionLines.map((line, index) => <span key={`${index}-${line}`}>{line}</span>)}
        </div>
      )}

      {mobilePlayer && phase !== "ended" && (
        <div className="mobile-player-gesture-layer" aria-hidden="true">
          <div
            className="mobile-player-gesture-zone mobile-player-gesture-zone--left"
            onPointerDown={(event) => handleMobilePointerDown("left", event)}
            onPointerUp={(event) => handleMobilePointerEnd("left", event)}
            onPointerCancel={(event) => handleMobilePointerEnd("left", event, true)}
          />
          <div
            className="mobile-player-gesture-zone mobile-player-gesture-zone--center"
            onPointerDown={(event) => handleMobilePointerDown("center", event)}
            onPointerUp={(event) => handleMobilePointerEnd("center", event)}
            onPointerCancel={(event) => handleMobilePointerEnd("center", event, true)}
          />
          <div
            className="mobile-player-gesture-zone mobile-player-gesture-zone--right"
            onPointerDown={(event) => handleMobilePointerDown("right", event)}
            onPointerUp={(event) => handleMobilePointerEnd("right", event)}
            onPointerCancel={(event) => handleMobilePointerEnd("right", event, true)}
          />
        </div>
      )}

      <AnimatePresence>
        {mobilePlayer && mobileSeekFeedback && (
          <motion.div
            className={`mobile-player-seek-feedback mobile-player-seek-feedback--${mobileSeekFeedback.side}`}
            initial={reduced ? { opacity: 0 } : { opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={reduced ? { opacity: 0 } : { opacity: 0, y: 2 }}
            transition={{ duration: reduced ? MOTION_TIMINGS.reduced : 0.13, ease: MOTION_EASE }}
            aria-hidden="true"
          >
            <i>
              <PlayerIcon name={mobileSeekFeedback.side === "left" ? "rewind" : "forward"} />
            </i>
            <strong>{mobileSeekFeedback.side === "left" ? "−" : "+"}{mobileSeekFeedback.seconds}</strong>
            <span>seconds</span>
          </motion.div>
        )}
      </AnimatePresence>
      <span className="sr-only" role="status" aria-live="polite">
        {mobileSeekFeedback
          ? `${mobileSeekFeedback.side === "left" ? "Rewind" : "Forward"} ${mobileSeekFeedback.seconds} seconds`
          : ""}
      </span>

      <div className="sr-only" role="status" aria-live="polite">{phaseMessage[phase]}</div>

      <AnimatePresence>
        {fullscreenError && (
          <motion.div
            className="absolute left-1/2 top-[max(1rem,env(safe-area-inset-top))] z-[60] w-[min(92vw,34rem)] -translate-x-1/2 rounded-lg border border-red-300/25 bg-black/90 px-4 py-3 text-center text-sm text-red-100 shadow-2xl backdrop-blur-md"
            role="alert"
            initial={reduced ? { opacity: 0 } : { opacity: 0, y: -10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={reduced ? { opacity: 0 } : { opacity: 0, y: -8 }}
          >
            {fullscreenError}
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {playerNotice && !fullscreenError && (
          <motion.div
            className="pointer-events-none absolute left-1/2 top-[max(1rem,env(safe-area-inset-top))] z-[55] w-[min(92vw,34rem)] -translate-x-1/2 rounded-lg border border-white/15 bg-black/85 px-4 py-3 text-center text-sm text-white/85 shadow-2xl backdrop-blur-md"
            role="status"
            initial={reduced ? { opacity: 0 } : { opacity: 0, y: -10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={reduced ? { opacity: 0 } : { opacity: 0, y: -8 }}
          >
            {playerNotice}
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {["buffering", "recovering"].includes(phase) && (
          <motion.div className="pointer-events-none absolute inset-0 z-20 grid place-items-center bg-black/10" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            <div className="rounded-xl bg-black/65 px-5 py-4 text-center backdrop-blur-md">
              <motion.i className="mx-auto block h-10 w-10 rounded-full border-2 border-white/20 border-t-white" animate={reduced ? undefined : { rotate: 360 }} transition={{ duration: 0.8, repeat: Infinity, ease: "linear" }} />
              <span className="mt-3 block text-xs tracking-[0.12em] text-white/70">{phaseMessage[phase]}</span>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {skipMarker && (
          <motion.div className="player-skip-control absolute bottom-36 right-6 z-40 md:right-10" initial={reduced ? { opacity: 0 } : { opacity: 0, x: 18 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: 12 }}>
            <Button onClick={() => seek(skipMarker.end)}>{skipMarker.label}</Button>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {phase === "ended" && (
          <motion.div className="absolute inset-0 z-50 grid place-items-center bg-black/70 p-6 backdrop-blur-sm" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            <motion.div className="w-full max-w-md rounded-2xl border border-white/15 bg-black/80 p-7 text-center" initial={reduced ? { opacity: 0 } : { opacity: 0, y: 18, scale: 0.96 }} animate={{ opacity: 1, y: 0, scale: 1 }}>
              <p className="text-xs uppercase tracking-[0.2em] text-white/45">Playback complete</p>
              <h2 className="mt-3 text-2xl font-semibold">{asset.title}</h2>
              {runResponse.nextEpisodeId && nextCountdown !== null ? (
                <>
                  <p className="mt-3 text-white/60">Next episode starts in {nextCountdown} seconds.</p>
                  <div className="mt-6 flex justify-center gap-3">
                    <Button onClick={playNextEpisode}>Play now</Button>
                    <Button onClick={() => { setNextCancelled(true); setNextCountdown(null); }}>Cancel</Button>
                  </div>
                </>
              ) : (
                <div className="mt-6 flex justify-center gap-3">
                  <Button onClick={startOver}>Replay</Button>
                  <Button onClick={exitPlayer}>Back</Button>
                </div>
              )}
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {mobilePlayer && shouldShowMobileChrome(phase, showControls) && phase !== "ended" && (
          <motion.div
            className="mobile-player-chrome"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: reduced ? MOTION_TIMINGS.reduced : 0.16, ease: MOTION_EASE }}
          >
            <header className="mobile-player-topbar">
              <div className="mobile-player-topbar__actions">
                <PlayerIconButton
                  icon="exit"
                  label="Exit player"
                  className="mobile-player-exit"
                  onClick={exitPlayer}
                />
              </div>
              <div className="mobile-player-title">
                <h1>{asset.title}</h1>
                {asset.subtitle && <p>{asset.subtitle}</p>}
                <small>{runResponse.sourceMetadata.sourceFormat} source</small>
              </div>
            </header>

            <div className="mobile-player-transport">
              <PlayerIconButton
                icon="rewind"
                label="Rewind 10 seconds"
                className="mobile-player-transport__seek"
                onClick={() => seek(currentTimeRef.current - 10)}
              />
              <PlayerIconButton
                icon={playbackControlShowsPause ? "pause" : "play"}
                label={playbackControlShowsPause ? "Pause" : "Play"}
                className="mobile-player-transport__play"
                onClick={() => playbackControlShowsPause ? pausePlayback() : startMobilePlayback()}
              />
              <PlayerIconButton
                icon="forward"
                label="Forward 10 seconds"
                className="mobile-player-transport__seek"
                onClick={() => seek(currentTimeRef.current + 10)}
              />
            </div>

            <div className="mobile-player-bottom">
              <input
                ref={timelineRef}
                aria-label="Playback position"
                type="range"
                min={0}
                max={duration > 0 ? duration : Math.max(runResponse.sourceMetadata.duration, currentTime, 1)}
                step={0.1}
                value={Math.min(currentTime, duration || currentTime)}
                onPointerDown={beginTimelineScrub}
                onPointerMove={moveTimelinePointer}
                onInput={(event) => previewTimelineScrub(Number(event.currentTarget.value))}
                onPointerUp={endTimelineScrub}
                onPointerCancel={cancelTimelineScrub}
                onChange={(event) => {
                  if (!scrubbingRef.current) seek(Number(event.currentTarget.value));
                }}
                className="player-timeline mobile-player-timeline"
                style={{
                  "--player-progress": `${duration > 0 ? Math.min(100, (currentTime / duration) * 100) : 0}%`,
                  "--player-buffered": `${duration > 0 ? Math.min(100, (bufferedEnd / duration) * 100) : 0}%`,
                } as React.CSSProperties}
              />
              <div className="mobile-player-bottom__row">
                <span className="mobile-player-time">
                  {formatDuration(currentTime)} <i>/</i> {duration ? formatDuration(duration) : asset.durationLabel}
                </span>
                <div className="mobile-player-settings">
                  <PlayerControlMenu
                    label="Playback speed"
                    icon="speed"
                    value={preferences.playbackRate}
                    options={[0.5, 0.75, 1, 1.25, 1.5, 2].map((rate) => ({ value: rate, label: `${rate}×` }))}
                    onSelect={(value) => setPreferences((current) => ({ ...current, playbackRate: value }))}
                    onOpenChange={handleControlMenuOpenChange}
                  />
                  {availableAudioTracks.length > 0 && (
                    <PlayerControlMenu
                      label="Audio language"
                      icon="audio"
                      value={selectedAudioTrackId}
                      options={availableAudioTracks.map((track) => ({ value: track.id, label: track.label, disabled: track.status === "failed", status: track.status === "failed" ? "Unavailable" : undefined }))}
                      onSelect={changeAudio}
                      onOpenChange={handleControlMenuOpenChange}
                    />
                  )}
                  {hasSubtitles && (
                    <PlayerControlMenu
                      label="Subtitles"
                      icon="captions"
                      value={preferences.subtitleTrackId}
                      options={[{ value: "off", label: "Subtitles off" }, ...runResponse.subtitles.map((subtitle) => ({ value: subtitle.id, label: languageDisplayName(subtitle.language, subtitle.label) }))]}
                      onSelect={(value) => setPreferences((current) => ({ ...current, subtitleTrackId: value }))}
                      onOpenChange={handleControlMenuOpenChange}
                    />
                  )}
                  {hasSubtitles && preferences.subtitleTrackId !== "off" && (
                    <PlayerControlMenu
                      label="Subtitle timing"
                      icon="captions"
                      value={preferences.subtitleOffset}
                      options={SUBTITLE_OFFSET_OPTIONS}
                      onSelect={(value) => setPreferences((current) => ({ ...current, subtitleOffset: value }))}
                      onOpenChange={handleControlMenuOpenChange}
                    />
                  )}
                  {hasSubtitles && preferences.subtitleTrackId !== "off" && (
                    <PlayerControlMenu
                      label="Caption size"
                      icon="captions"
                      value={preferences.captionScale}
                      options={[{ value: 0.8, label: "Captions S" }, { value: 1, label: "Captions M" }, { value: 1.25, label: "Captions L" }, { value: 1.5, label: "Captions XL" }]}
                      onSelect={(value) => setPreferences((current) => ({ ...current, captionScale: value }))}
                      onOpenChange={handleControlMenuOpenChange}
                    />
                  )}
                  {availableQualities.length > 1 && (
                    <PlayerControlMenu
                      label="Quality"
                      icon="quality"
                      value={selectedQualityId}
                      options={availableQualities.map((item) => ({ value: item.id, label: item.label, disabled: item.status === "failed", status: item.ready ? undefined : item.status === "failed" ? "Unavailable" : item.status === "idle" ? "Prepare on request" : "Preparing" }))}
                      onSelect={changeQuality}
                      onOpenChange={handleControlMenuOpenChange}
                    />
                  )}
                  {document.pictureInPictureEnabled && (
                    <PlayerIconButton icon="pip" label="Picture in picture" onClick={togglePictureInPicture} />
                  )}
                  <PlayerIconButton
                    icon={fullscreenActive ? "fullscreen-exit" : "fullscreen"}
                    label={fullscreenActive ? "Exit fullscreen" : "Fullscreen"}
                    aria-pressed={fullscreenActive}
                    onClick={(event) => toggleFullscreen(event.currentTarget)}
                  />
                </div>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {!mobilePlayer && showControls && phase !== "ended" && (
          <motion.div
            className="player-controls absolute inset-x-0 bottom-0 z-30 bg-gradient-to-t from-black via-black/85 to-transparent px-4 pb-[max(1rem,env(safe-area-inset-bottom))] pt-24 md:px-10 md:pb-7"
            initial={reduced ? { opacity: 0 } : { opacity: 0, y: 18 }}
            animate={{ opacity: 1, y: 0 }}
            exit={reduced ? { opacity: 0 } : { opacity: 0, y: 12 }}
          >
            <div className="mx-auto max-w-7xl">
              <div className="mb-4 flex items-start justify-between gap-5">
                <div>
                  <h1 className="text-lg font-semibold md:text-2xl">{asset.title}</h1>
                  {asset.subtitle && <p className="mt-1 text-xs text-white/60 md:text-sm">{asset.subtitle}</p>}
                  <p className="mt-1 text-[11px] uppercase tracking-[0.14em] text-white/35">{runResponse.sourceMetadata.sourceFormat} source · {streamMode === "progressive" ? "Compatibility delivery" : "Adaptive delivery"}</p>
                </div>
                <button onClick={exitPlayer} className="player-exit-button" aria-label="Exit player">
                  <PlayerIcon name="exit" />
                  <span>Exit</span>
                </button>
              </div>

              <div className="relative">
                {timelinePreview && (
                  <span className="pointer-events-none absolute -top-9 -translate-x-1/2 rounded bg-black/90 px-2 py-1 text-xs" style={{ left: timelinePreview.x }}>
                    {formatDuration(timelinePreview.time)}
                  </span>
                )}
                <input
                  ref={timelineRef}
                  aria-label="Playback position"
                  type="range"
                  min={0}
                  max={duration || 0}
                  step={0.1}
                  value={Math.min(currentTime, duration || currentTime)}
                  onPointerMove={moveTimelinePointer}
                  onPointerLeave={() => { if (!scrubbingRef.current) setTimelinePreview(null); }}
                  onPointerDown={beginTimelineScrub}
                  onPointerUp={endTimelineScrub}
                  onPointerCancel={cancelTimelineScrub}
                  onBlur={(event) => {
                    if (scrubbingRef.current) commitTimelineScrub(Number(event.currentTarget.value));
                  }}
                  onChange={(event) => {
                    const value = Number(event.target.value);
                    if (scrubbingRef.current) previewTimelineScrub(value);
                    else seek(value);
                  }}
                  className="player-timeline w-full cursor-pointer"
                  style={{
                    "--player-progress": `${duration > 0 ? Math.min(100, (currentTime / duration) * 100) : 0}%`,
                    "--player-buffered": `${duration > 0 ? Math.min(100, (bufferedEnd / duration) * 100) : 0}%`,
                  } as React.CSSProperties}
                />
              </div>

              <div className="mt-3 flex flex-wrap items-center gap-2 md:gap-3">
                <PlayerIconButton icon={playbackControlShowsPause ? "pause" : "play"} label={playbackControlShowsPause ? "Pause" : "Play"} onClick={() => playbackControlShowsPause ? pausePlayback() : safePlay()} />
                <PlayerIconButton icon="rewind" label="Rewind 10 seconds" onClick={() => seek(currentTimeRef.current - 10)} />
                <PlayerIconButton icon="forward" label="Forward 10 seconds" onClick={() => seek(currentTimeRef.current + 10)} />
                <PlayerIconButton icon={muted ? "mute" : "volume"} label={muted ? "Unmute" : "Mute"} onClick={toggleOutputMute} />
                <input aria-label="Volume" type="range" min={0} max={1} step={0.01} value={muted ? 0 : volume} onChange={(event) => setOutputVolume(Number(event.target.value), false)} className="player-volume" />
                <span className="min-w-[8.5rem] text-xs tabular-nums text-white/65 md:text-sm">{formatDuration(currentTime)} / {duration ? formatDuration(duration) : asset.durationLabel}</span>

                <div className="player-control-menus ml-auto flex flex-wrap items-center justify-end gap-2">
                  <PlayerControlMenu
                    label="Playback speed"
                    icon="speed"
                    value={preferences.playbackRate}
                    options={[0.5, 0.75, 1, 1.25, 1.5, 2].map((rate) => ({ value: rate, label: `${rate}×` }))}
                    onSelect={(value) => setPreferences((current) => ({ ...current, playbackRate: value }))}
                    onOpenChange={handleControlMenuOpenChange}
                  />
                  {availableAudioTracks.length > 0 && (
                    <PlayerControlMenu
                      label="Audio language"
                      icon="audio"
                      value={selectedAudioTrackId}
                      options={availableAudioTracks.map((track) => ({ value: track.id, label: track.label, disabled: track.status === "failed", status: track.status === "failed" ? "Unavailable" : undefined }))}
                      onSelect={changeAudio}
                      onOpenChange={handleControlMenuOpenChange}
                    />
                  )}
                  {hasSubtitles && (
                    <PlayerControlMenu
                      label="Subtitles"
                      icon="captions"
                      value={preferences.subtitleTrackId}
                      options={[{ value: "off", label: "Subtitles off" }, ...runResponse.subtitles.map((subtitle) => ({ value: subtitle.id, label: languageDisplayName(subtitle.language, subtitle.label) }))]}
                      onSelect={(value) => setPreferences((current) => ({ ...current, subtitleTrackId: value }))}
                      onOpenChange={handleControlMenuOpenChange}
                    />
                  )}
                  {hasSubtitles && preferences.subtitleTrackId !== "off" && (
                    <PlayerControlMenu
                      label="Subtitle timing"
                      icon="captions"
                      value={preferences.subtitleOffset}
                      options={SUBTITLE_OFFSET_OPTIONS}
                      onSelect={(value) => setPreferences((current) => ({ ...current, subtitleOffset: value }))}
                      onOpenChange={handleControlMenuOpenChange}
                    />
                  )}
                  {hasSubtitles && preferences.subtitleTrackId !== "off" && (
                    <PlayerControlMenu
                      label="Caption size"
                      icon="captions"
                      value={preferences.captionScale}
                      options={[{ value: 0.8, label: "Captions S" }, { value: 1, label: "Captions M" }, { value: 1.25, label: "Captions L" }, { value: 1.5, label: "Captions XL" }]}
                      onSelect={(value) => setPreferences((current) => ({ ...current, captionScale: value }))}
                      onOpenChange={handleControlMenuOpenChange}
                    />
                  )}
                  {availableQualities.length > 1 && (
                    <PlayerControlMenu
                      label="Quality"
                      icon="quality"
                      value={selectedQualityId}
                      options={availableQualities.map((item) => ({ value: item.id, label: item.label, disabled: item.status === "failed", status: item.ready ? undefined : item.status === "failed" ? "Unavailable" : item.status === "idle" ? "Prepare on request" : "Preparing" }))}
                      onSelect={changeQuality}
                      onOpenChange={handleControlMenuOpenChange}
                    />
                  )}
                  {document.pictureInPictureEnabled && <PlayerIconButton icon="pip" label="Picture in picture" onClick={togglePictureInPicture} />}
                  <PlayerIconButton
                    icon={fullscreenActive ? "fullscreen-exit" : "fullscreen"}
                    label={fullscreenActive ? "Exit fullscreen" : "Fullscreen"}
                    aria-pressed={fullscreenActive}
                    onClick={(event) => toggleFullscreen(event.currentTarget)}
                  />
                </div>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
