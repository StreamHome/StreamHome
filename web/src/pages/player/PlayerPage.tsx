import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import Hls from "hls.js";
import { useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "../../api/client";
import { getEpisodes, getMovies } from "../../api/movies";
import {
  createPlaybackRun,
  getPlaybackRun,
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
  PlaybackProgressEvent,
  PlaybackRunResponse,
} from "../../types/api";
import { formatDuration } from "../../utils/format";
import { hasSubtitleOptions, PlayerControlMenu, PlayerIcon, PlayerIconButton } from "./PlayerControls";
import {
  canUsePlayerFullscreen,
  isPlayerFullscreen,
  togglePlayerFullscreen,
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


type PlayerPhase =
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

interface ResolvedPlayback {
  asset: PlayableAsset;
  episodeSequence: Episode[];
  runResponse: PlaybackRunResponse;
}

interface PlayerPreferences {
  qualityHeight: number | "auto";
  audioLanguage: string;
  subtitleLanguage: string;
  captionScale: number;
  playbackRate: number;
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

const DEFAULT_PREFERENCES: PlayerPreferences = {
  qualityHeight: "auto",
  audioLanguage: "",
  subtitleLanguage: "off",
  captionScale: 1,
  playbackRate: 1,
};
const PREPARATION_POLL_INTERVAL = 1_000;
const PREPARATION_TIMEOUT = 120_000;
const TICKET_RENEWAL_MARGIN = 3 * 60 * 1_000;
const NEXT_EPISODE_SECONDS = 10;
const NETWORK_RETRY_LIMIT = 3;
const MEDIA_RECOVERY_LIMIT = 2;


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
    const parsed = JSON.parse(localStorage.getItem(`streamhome_player_preferences_${profileId}`) || "{}") as Partial<PlayerPreferences>;
    return {
      qualityHeight: parsed.qualityHeight === "auto" || typeof parsed.qualityHeight === "number" ? parsed.qualityHeight : "auto",
      audioLanguage: typeof parsed.audioLanguage === "string" ? parsed.audioLanguage : "",
      subtitleLanguage: typeof parsed.subtitleLanguage === "string" ? parsed.subtitleLanguage : "off",
      captionScale: typeof parsed.captionScale === "number" ? Math.min(1.5, Math.max(0.8, parsed.captionScale)) : 1,
      playbackRate: typeof parsed.playbackRate === "number" ? parsed.playbackRate : 1,
    };
  } catch {
    return DEFAULT_PREFERENCES;
  }
}

function sleep(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, milliseconds);
    signal.addEventListener("abort", () => {
      window.clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    }, { once: true });
  });
}

function isInteractiveTarget(target: EventTarget | null): boolean {
  return target instanceof Element && Boolean(target.closest("button, input, select, textarea, a, [contenteditable='true'], [role='slider']"));
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

export function PlayerPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const query = useMemo(() => parseAppQuery(location.search), [location.search]);
  const mediaId = query.media ?? "";
  const profile = useProfileStore((state) => state.activeProfile);
  const theme = useThemeStore((state) => state.activeTheme);
  const definition = getThemeDefinition(theme);
  const { reduced } = useAppMotion();

  const containerRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const timelineRef = useRef<HTMLInputElement>(null);
  const hlsRef = useRef<Hls | null>(null);
  const controlsTimerRef = useRef<number | null>(null);
  const resumePositionRef = useRef(0);
  const currentTimeRef = useRef(0);
  const resumeAppliedRef = useRef(false);
  const sequenceNumberRef = useRef(1);
  const pendingWatchedSecondsRef = useRef(0);
  const lastAdvanceWallRef = useRef<number | null>(null);
  const lastAdvanceMediaRef = useRef(0);
  const progressQueueRef = useRef<Promise<unknown>>(Promise.resolve());
  const completedRef = useRef(false);
  const networkRetriesRef = useRef(0);
  const mediaRecoveriesRef = useRef(0);
  const mobileTapChainRef = useRef<MobileTapChain | null>(null);
  const mobileSingleTapTimerRef = useRef<number | null>(null);
  const mobileTapResetTimerRef = useRef<number | null>(null);
  const mobileFullscreenAttemptedAtRef = useRef(0);
  const mobilePointerGestureRef = useRef<MobilePointerGesture | null>(null);
  const showControlsRef = useRef(true);
  const scrubbingRef = useRef(false);
  const scrubOriginRef = useRef(0);
  const timelineAnimationFrameRef = useRef<number | null>(null);
  const mountedRef = useRef(true);

  const [asset, setAsset] = useState<PlayableAsset | null>(null);
  const [episodeSequence, setEpisodeSequence] = useState<Episode[]>([]);
  const [runResponse, setRunResponse] = useState<PlaybackRunResponse | null>(null);
  const [phase, setPhase] = useState<PlayerPhase>("resolving");
  const [streamMode, setStreamMode] = useState<StreamMode>("hls");
  const [fatal, setFatal] = useState<FatalState | null>(null);
  const [retryVersion, setRetryVersion] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [bufferedEnd, setBufferedEnd] = useState(0);
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);
  const [showControls, setShowControls] = useState(true);
  const [availableQualities, setAvailableQualities] = useState<Array<{ label: string; height: number; index: number }>>([{ label: "Auto", height: 0, index: -1 }]);
  const [selectedQualityIndex, setSelectedQualityIndex] = useState(-1);
  const [availableAudioTracks, setAvailableAudioTracks] = useState<Array<{ label: string; language: string; index: number }>>([]);
  const [selectedAudioTrackIndex, setSelectedAudioTrackIndex] = useState(0);
  const [preferences, setPreferences] = useState<PlayerPreferences>(() => profile ? loadPreferences(profile.id) : DEFAULT_PREFERENCES);
  const [showStartOver, setShowStartOver] = useState(false);
  const [nextCountdown, setNextCountdown] = useState<number | null>(null);
  const [nextCancelled, setNextCancelled] = useState(false);
  const [timelinePreview, setTimelinePreview] = useState<{ x: number; time: number } | null>(null);
  const [fullscreenActive, setFullscreenActive] = useState(false);
  const [fullscreenAvailable, setFullscreenAvailable] = useState(true);
  const [fullscreenError, setFullscreenError] = useState("");
  const [mobilePlayer, setMobilePlayer] = useState(() => typeof window !== "undefined" && isPhonePlayerViewport(readMobileViewport(window)));
  const [forcedLandscape, setForcedLandscape] = useState(() => typeof window !== "undefined" && isForcedLandscape(window.innerWidth, window.innerHeight, isPhonePlayerViewport(readMobileViewport(window))));
  const [forcedLandscapeDirection, setForcedLandscapeDirection] = useState<"clockwise" | "counterclockwise">("clockwise");
  const [mobileSeekFeedback, setMobileSeekFeedback] = useState<{
    side: MobileTapSide;
    seconds: number;
  } | null>(null);

  const exitPlayer = useCallback(() => {
    unlockPlayerLandscape();
    if (!profile) {
      navigate("/profiles", { replace: true });
      return;
    }
    if ((location.state as { fromApp?: boolean } | null)?.fromApp) navigate(-1);
    else navigate(appUrl(profile.id, "home"), { replace: true });
  }, [location.state, navigate, profile]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

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
    if (!profile) return;
    localStorage.setItem(`streamhome_player_preferences_${profile.id}`, JSON.stringify(preferences));
  }, [preferences, profile]);

  useEffect(() => {
    if (!profile) return;
    setPreferences(loadPreferences(profile.id));
  }, [profile]);

  useEffect(() => {
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
    setDuration(0);
    setBufferedEnd(0);
    setShowStartOver(false);
    setNextCountdown(null);
    setNextCancelled(false);
    setStreamMode("hls");
    resumeAppliedRef.current = false;
    resumePositionRef.current = 0;
    sequenceNumberRef.current = 1;
    pendingWatchedSecondsRef.current = 0;
    completedRef.current = false;
    networkRetriesRef.current = 0;
    mediaRecoveriesRef.current = 0;

    const resolveAssetAndCreateRun = async (): Promise<ResolvedPlayback> => {
      const catalog = await getMovies(undefined, abort.signal);
      let resolvedAsset: PlayableAsset;
      let sequence: Episode[] = [];
      let response: PlaybackRunResponse;
      if (mediaId.startsWith("m_")) {
        const movie = catalog.find((item) => item.id === mediaId);
        if (!movie) throw new Error("This movie is not present in the server catalog.");
        resolvedAsset = assetFromMovie(movie);
        response = await createPlaybackRun(movie.id, profile.id, undefined, abort.signal);
      } else if (mediaId.startsWith("ep_")) {
        let matchedMovie: Movie | null = null;
        let matchedEpisode: Episode | null = null;
        for (const movie of catalog.filter((item) => item.type === "series")) {
          const embedded = movie.episodes?.find((episode) => episode.id === mediaId);
          if (embedded) {
            matchedMovie = movie;
            matchedEpisode = embedded;
            break;
          }
        }
        if (!matchedMovie || !matchedEpisode) {
          const tmdbId = episodeTmdbId(mediaId);
          if (tmdbId !== null) {
            const movie = catalog.find((item) => item.id === `tv_${tmdbId}`);
            if (movie) {
              const episodes = await getEpisodes(tmdbId, abort.signal);
              const episode = episodes.find((item) => item.id === mediaId);
              if (episode) {
                matchedMovie = movie;
                matchedEpisode = episode;
              }
            }
          }
        }
        if (!matchedMovie || !matchedEpisode) throw new Error("This episode is not present in the server catalog.");
        resolvedAsset = assetFromEpisode(matchedMovie, matchedEpisode);
        const tmdbId = episodeTmdbId(mediaId);
        sequence = tmdbId === null
          ? matchedMovie.episodes ?? [matchedEpisode]
          : await getEpisodes(tmdbId, abort.signal).catch(() => matchedMovie!.episodes ?? [matchedEpisode!]);
        response = await createPlaybackRun(matchedMovie.id, profile.id, matchedEpisode.id, abort.signal);
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
      if (response.preparationState === "preparing") {
        setPhase("preparing");
        const started = performance.now();
        while (response.preparationState === "preparing" && performance.now() - started < PREPARATION_TIMEOUT) {
          await sleep(PREPARATION_POLL_INTERVAL, abort.signal);
          response = await getPlaybackRun(response.runId, { signal: abort.signal });
          if (!active) throw new DOMException("Aborted", "AbortError");
          setRunResponse(response);
          sequenceNumberRef.current = response.nextSequenceNumber;
        }
      }
      if (response.preparationState === "error") {
        throw new ApiError(response.preparationError?.message || "The adaptive stream could not be prepared.", 503, response.preparationError?.code || "PREPARATION_FAILED");
      }
      if (response.preparationState !== "ready" || !response.manifestUrl) {
        throw new ApiError("Playback preparation timed out. You can retry without leaving this page.", 503, "PREPARATION_TIMEOUT");
      }
      return { asset: resolvedAsset, episodeSequence: sequence, runResponse: response };
    };

    resolveAssetAndCreateRun()
      .then((resolved) => {
        if (!active) return;
        setAsset(resolved.asset);
        setEpisodeSequence(resolved.episodeSequence);
        setRunResponse(resolved.runResponse);
        sequenceNumberRef.current = resolved.runResponse.nextSequenceNumber;
        setPhase("loading");
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
  }, [mediaId, profile, retryVersion]);

  const applyResume = useCallback((video: HTMLVideoElement) => {
    if (resumeAppliedRef.current) return;
    const position = resumePositionRef.current;
    if (position >= 0 && Number.isFinite(video.duration)) {
      video.currentTime = Math.min(position, Math.max(0, video.duration - 1));
      currentTimeRef.current = video.currentTime;
      setCurrentTime(video.currentTime);
    } else if (position >= 0 && video.seekable.length > 0) {
      const seekableStart = video.seekable.start(0);
      const seekableEnd = video.seekable.end(video.seekable.length - 1);
      video.currentTime = Math.min(Math.max(position, seekableStart), seekableEnd);
      currentTimeRef.current = video.currentTime;
      setCurrentTime(video.currentTime);
    }
    resumeAppliedRef.current = true;
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!runResponse || runResponse.preparationState !== "ready" || !video) return;
    const preservePosition = video.currentTime || currentTime || resumePositionRef.current;
    resumePositionRef.current = preservePosition;
    resumeAppliedRef.current = false;
    setPhase("loading");
    networkRetriesRef.current = 0;
    mediaRecoveriesRef.current = 0;

    hlsRef.current?.destroy();
    hlsRef.current = null;
    video.removeAttribute("src");
    video.load();

    const beginPlayback = () => {
      applyResume(video);
      video.playbackRate = preferences.playbackRate;
      void video.play().catch(() => setPhase("paused"));
    };

    if (streamMode === "progressive") {
      video.src = runResponse.progressiveUrl;
      video.addEventListener("loadedmetadata", beginPlayback, { once: true });
      video.load();
      return () => video.removeEventListener("loadedmetadata", beginPlayback);
    }

    if (Hls.isSupported() && runResponse.manifestUrl) {
      setStreamMode("hls");
      const hls = new Hls({
        enableWorker: true,
        capLevelToPlayerSize: true,
        startLevel: -1,
        startPosition: Math.max(0, resumePositionRef.current),
        maxBufferLength: 30,
        maxMaxBufferLength: 90,
        backBufferLength: 30,
        manifestLoadingMaxRetry: 2,
        levelLoadingMaxRetry: 2,
        fragLoadingMaxRetry: 3,
      });
      hlsRef.current = hls;
      hls.attachMedia(video);
      hls.on(Hls.Events.MEDIA_ATTACHED, () => hls.loadSource(runResponse.manifestUrl!));
      hls.on(Hls.Events.MANIFEST_PARSED, (_, data) => {
        const levels = data.levels
          .map((level, index) => ({ label: level.height ? `${level.height}p` : `Quality ${index + 1}`, height: level.height || 0, index }))
          .sort((left, right) => right.height - left.height);
        setAvailableQualities([{ label: "Auto", height: 0, index: -1 }, ...levels]);
        const preferred = preferences.qualityHeight === "auto"
          ? -1
          : levels.reduce((best, level) => Math.abs(level.height - Number(preferences.qualityHeight)) < Math.abs(best.height - Number(preferences.qualityHeight)) ? level : best, levels[0])?.index ?? -1;
        hls.currentLevel = preferred;
        setSelectedQualityIndex(preferred);
        beginPlayback();
      });
      hls.on(Hls.Events.AUDIO_TRACKS_UPDATED, (_, data) => {
        const tracks = data.audioTracks.map((track, index) => ({
          label: track.name || track.lang?.toUpperCase() || `Audio ${index + 1}`,
          language: track.lang || "und",
          index,
        }));
        setAvailableAudioTracks(tracks);
        const preferredIndex = Math.max(0, tracks.findIndex((track) => track.language === preferences.audioLanguage));
        hls.audioTrack = preferredIndex;
        setSelectedAudioTrackIndex(preferredIndex);
      });
      hls.on(Hls.Events.ERROR, (_, data) => {
        if (!data.fatal) return;
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR && networkRetriesRef.current < NETWORK_RETRY_LIMIT) {
          networkRetriesRef.current += 1;
          setPhase("recovering");
          window.setTimeout(() => hls.startLoad(video.currentTime), 500 * networkRetriesRef.current);
          return;
        }
        if (data.type === Hls.ErrorTypes.MEDIA_ERROR && mediaRecoveriesRef.current < MEDIA_RECOVERY_LIMIT) {
          mediaRecoveriesRef.current += 1;
          setPhase("recovering");
          hls.recoverMediaError();
          return;
        }
        resumePositionRef.current = video.currentTime;
        setPhase("recovering");
        setStreamMode("progressive");
      });
      return () => {
        hls.destroy();
        if (hlsRef.current === hls) hlsRef.current = null;
      };
    }

    if (runResponse.manifestUrl && video.canPlayType("application/vnd.apple.mpegurl")) {
      setStreamMode("native-hls");
      video.src = runResponse.manifestUrl;
      video.addEventListener("loadedmetadata", beginPlayback, { once: true });
      video.load();
      return () => video.removeEventListener("loadedmetadata", beginPlayback);
    }

    setStreamMode("progressive");
  }, [applyResume, runResponse, streamMode]);

  useEffect(() => {
    if (!runResponse || !profile) return;
    const renewIn = Math.max(30_000, runResponse.ticketExpiresAt * 1000 - Date.now() - TICKET_RENEWAL_MARGIN);
    const timer = window.setTimeout(() => {
      const position = videoRef.current?.currentTime ?? currentTimeRef.current;
      resumePositionRef.current = position;
      resumeAppliedRef.current = false;
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
  }, [profile, runResponse]);

  useEffect(() => {
    if (!runResponse || runResponse.preparationState !== "ready") return;
    const pendingRenditions = [...runResponse.renditions, ...runResponse.tracks].some((item) => !item.ready);
    if (!pendingRenditions) return;
    const abort = new AbortController();
    let attempts = 0;
    let timer: number | null = null;
    const knownReady = [...runResponse.renditions, ...runResponse.tracks].filter((item) => item.ready).length;

    const poll = async () => {
      attempts += 1;
      try {
        const refreshed = await getPlaybackRun(runResponse.runId, { signal: abort.signal });
        const refreshedReady = [...refreshed.renditions, ...refreshed.tracks].filter((item) => item.ready).length;
        if (refreshedReady > knownReady) {
          sequenceNumberRef.current = refreshed.nextSequenceNumber;
          resumePositionRef.current = videoRef.current?.currentTime ?? currentTimeRef.current;
          resumeAppliedRef.current = false;
          setRunResponse(refreshed);
          return;
        }
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
      }
      if (attempts < 24 && !abort.signal.aborted) timer = window.setTimeout(poll, 5_000);
    };

    timer = window.setTimeout(poll, 5_000);
    return () => {
      abort.abort();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [runResponse]);

  const applySubtitlePreference = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    video.playbackRate = preferences.playbackRate;
  }, [preferences.playbackRate]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    for (let index = 0; index < video.textTracks.length; index += 1) {
      const track = video.textTracks[index];
      track.mode = preferences.subtitleLanguage !== "off" && track.language === preferences.subtitleLanguage ? "showing" : "disabled";
    }
  }, [preferences.subtitleLanguage]);

  useEffect(() => {
    if (!runResponse || preferences.subtitleLanguage === "off") return;
    const selectedTrackStillExists = runResponse.subtitles.some(
      (subtitle) => subtitle.language === preferences.subtitleLanguage,
    );
    if (!selectedTrackStillExists) {
      setPreferences((current) => ({ ...current, subtitleLanguage: "off" }));
    }
  }, [preferences.subtitleLanguage, runResponse]);

  useEffect(() => applySubtitlePreference(), [applySubtitlePreference, runResponse]);

  const captureWatchedTime = useCallback(() => {
    const video = videoRef.current;
    if (!video || video.paused || video.seeking || video.readyState < HTMLMediaElement.HAVE_FUTURE_DATA) {
      lastAdvanceWallRef.current = null;
      lastAdvanceMediaRef.current = video?.currentTime ?? 0;
      return;
    }
    const now = performance.now();
    const mediaTime = video.currentTime;
    const previousWall = lastAdvanceWallRef.current;
    const previousMedia = lastAdvanceMediaRef.current;
    pendingWatchedSecondsRef.current += advancingPlaybackDelta(previousWall, previousMedia, now, mediaTime, true);
    lastAdvanceWallRef.current = now;
    lastAdvanceMediaRef.current = mediaTime;
  }, []);

  const reportProgress = useCallback((event: PlaybackProgressEvent, finished = false, keepalive = false) => {
    if (!runResponse) return;
    captureWatchedTime();
    const video = videoRef.current;
    const watchedSeconds = Math.floor(pendingWatchedSecondsRef.current);
    pendingWatchedSecondsRef.current -= watchedSeconds;
    const request = {
      timestamp: Math.max(0, video?.currentTime ?? currentTimeRef.current),
      durationWatched: watchedSeconds,
      isFinished: finished,
      sequenceNumber: sequenceNumberRef.current,
      event,
    } as const;
    sequenceNumberRef.current += 1;
    progressQueueRef.current = progressQueueRef.current
      .catch(() => undefined)
      .then(() => updatePlaybackProgress(runResponse.runId, request, keepalive))
      .then((response) => {
        sequenceNumberRef.current = response.nextSequenceNumber;
      })
      .catch((error: unknown) => {
        if (error instanceof ApiError && error.code === "PLAYBACK_SEQUENCE_MISMATCH") {
          void getPlaybackRun(runResponse.runId).then((fresh) => {
            sequenceNumberRef.current = fresh.nextSequenceNumber;
          }).catch(() => undefined);
        }
      });
  }, [captureWatchedTime, runResponse]);

  useEffect(() => {
    if (!runResponse) return;
    const timer = window.setInterval(() => reportProgress("heartbeat"), 10_000);
    const onVisibility = () => {
      if (document.visibilityState === "hidden" && !completedRef.current) reportProgress("visibility", false, true);
    };
    const onPageHide = () => {
      if (!completedRef.current) reportProgress("exit", false, true);
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pagehide", onPageHide);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pagehide", onPageHide);
    };
  }, [reportProgress, runResponse]);

  const safePlay = useCallback(() => {
    void videoRef.current?.play().catch(() => {
      setFatal({ title: "Playback blocked", message: "The browser could not start this stream. Try again or go back.", retryable: true });
      setPhase("fatal");
    });
  }, []);

  const seek = useCallback((nextTime: number, report = true) => {
    const video = videoRef.current;
    if (!video) return;
    captureWatchedTime();
    const bounded = Math.min(Math.max(nextTime, 0), Number.isFinite(video.duration) ? video.duration : nextTime);
    video.currentTime = bounded;
    currentTimeRef.current = bounded;
    lastAdvanceWallRef.current = null;
    lastAdvanceMediaRef.current = bounded;
    setCurrentTime(bounded);
    if (report) reportProgress("seek");
  }, [captureWatchedTime, reportProgress]);

  const revealControls = useCallback(() => {
    setShowControls(true);
    if (controlsTimerRef.current) window.clearTimeout(controlsTimerRef.current);
    if (phase === "playing") controlsTimerRef.current = window.setTimeout(() => setShowControls(false), 3_000);
  }, [phase]);

  useEffect(() => {
    if (phase !== "playing") return;
    const updateTimeline = () => {
      const video = videoRef.current;
      const timeline = timelineRef.current;
      if (video && timeline && !scrubbingRef.current) {
        const liveDuration = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : duration;
        const liveTime = Math.min(video.currentTime, liveDuration || video.currentTime);
        timeline.value = String(liveTime);
        timeline.style.setProperty("--player-progress", `${liveDuration > 0 ? Math.min(100, (liveTime / liveDuration) * 100) : 0}%`);
      }
      timelineAnimationFrameRef.current = window.requestAnimationFrame(updateTimeline);
    };
    timelineAnimationFrameRef.current = window.requestAnimationFrame(updateTimeline);
    return () => {
      if (timelineAnimationFrameRef.current !== null) window.cancelAnimationFrame(timelineAnimationFrameRef.current);
      timelineAnimationFrameRef.current = null;
    };
  }, [duration, phase]);

  useEffect(() => {
    const video = videoRef.current;
    const updateFullscreenState = () => {
      const active = isPlayerFullscreen(video);
      setFullscreenActive(active);
      setFullscreenAvailable(canUsePlayerFullscreen(containerRef.current, video));
      if (mobilePlayer) {
        if (active) void lockPlayerLandscape();
        else {
          mobileFullscreenAttemptedAtRef.current = 0;
          unlockPlayerLandscape();
        }
      } else revealControls();
    };

    updateFullscreenState();
    document.addEventListener("fullscreenchange", updateFullscreenState);
    document.addEventListener("webkitfullscreenchange", updateFullscreenState);
    video?.addEventListener("webkitbeginfullscreen", updateFullscreenState);
    video?.addEventListener("webkitendfullscreen", updateFullscreenState);
    return () => {
      document.removeEventListener("fullscreenchange", updateFullscreenState);
      document.removeEventListener("webkitfullscreenchange", updateFullscreenState);
      video?.removeEventListener("webkitbeginfullscreen", updateFullscreenState);
      video?.removeEventListener("webkitendfullscreen", updateFullscreenState);
    };
  }, [mobilePlayer, revealControls]);

  const toggleFullscreen = useCallback(() => {
    const container = containerRef.current;
    const video = videoRef.current;
    if (!container || !video) return;

    setFullscreenError("");
    void togglePlayerFullscreen(container, video, document, { allowVideoFallback: !mobilePlayer })
      .then(() => {
        setFullscreenActive(isPlayerFullscreen(video));
        if (mobilePlayer) void lockPlayerLandscape();
        revealControls();
      })
      .catch((error: unknown) => {
        setFullscreenActive(isPlayerFullscreen(video));
        setFullscreenError(error instanceof Error
          ? error.message
          : "Fullscreen could not be opened. Check this browser's fullscreen permission.");
        setShowControls(true);
      });
  }, [mobilePlayer, revealControls]);

  const ensureMobileLandscape = useCallback(() => {
    if (!mobilePlayer) return;
    if (fullscreenActive) {
      void lockPlayerLandscape();
      return;
    }
    const container = containerRef.current;
    const video = videoRef.current;
    if (!container || !video) return;
    const now = performance.now();
    if (mobileFullscreenAttemptedAtRef.current > 0 && now - mobileFullscreenAttemptedAtRef.current < 2_000) return;
    mobileFullscreenAttemptedAtRef.current = now;
    void togglePlayerFullscreen(container, video, document, { allowVideoFallback: false })
      .then(async () => {
        const active = isPlayerFullscreen(video);
        setFullscreenActive(active);
        if (active) await lockPlayerLandscape();
      })
      .catch(() => {
        mobileFullscreenAttemptedAtRef.current = Number.POSITIVE_INFINITY;
        // The CSS-rotated landscape presentation remains active when browser policy rejects fullscreen.
      });
  }, [fullscreenActive, mobilePlayer]);

  const togglePictureInPicture = useCallback(() => {
    const video = videoRef.current;
    if (!video || !document.pictureInPictureEnabled) return;
    const operation = document.pictureInPictureElement ? document.exitPictureInPicture() : video.requestPictureInPicture();
    void operation.catch(() => undefined);
  }, []);

  const startOver = useCallback(() => {
    if (!runResponse) return;
    void startOverPlaybackRun(runResponse.runId).then(() => {
      completedRef.current = false;
      resumePositionRef.current = 0;
      setShowStartOver(false);
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

  useEffect(() => {
    if (!mobilePlayer || phase !== "playing") return;
    revealControls();
  }, [mobilePlayer, phase, revealControls]);

  const handleControlMenuOpenChange = useCallback((open: boolean) => {
    if (controlsTimerRef.current) window.clearTimeout(controlsTimerRef.current);
    if (open) setShowControls(true);
    else revealControls();
  }, [revealControls]);

  useEffect(() => () => {
    if (controlsTimerRef.current) window.clearTimeout(controlsTimerRef.current);
  }, []);

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if (isInteractiveTarget(event.target)) return;
      if (event.key === " ") {
        event.preventDefault();
        videoRef.current?.paused ? safePlay() : videoRef.current?.pause();
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        seek((videoRef.current?.currentTime ?? 0) - 10);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        seek((videoRef.current?.currentTime ?? 0) + 10);
      } else if (event.key.toLowerCase() === "m" && videoRef.current) {
        videoRef.current.muted = !videoRef.current.muted;
      } else if (event.key.toLowerCase() === "f") {
        toggleFullscreen();
      } else if (event.key.toLowerCase() === "p") {
        togglePictureInPicture();
      } else if (event.key === "Escape" && !fullscreenActive) {
        exitPlayer();
      }
      revealControls();
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [exitPlayer, fullscreenActive, revealControls, safePlay, seek, toggleFullscreen, togglePictureInPicture]);

  const toggleMobileControls = useCallback(() => {
    if (showControlsRef.current) {
      if (controlsTimerRef.current) window.clearTimeout(controlsTimerRef.current);
      setShowControls(false);
      return;
    }
    revealControls();
  }, [revealControls]);

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
      reportProgress("seek");
      setMobileSeekFeedback(null);
      mobileTapChainRef.current = null;
    }
    const result = nextMobileTap(mobileTapChainRef.current, side, now);
    mobileTapChainRef.current = result.chain;

    if (mobileTapResetTimerRef.current !== null) window.clearTimeout(mobileTapResetTimerRef.current);
    mobileTapResetTimerRef.current = window.setTimeout(() => {
      if ((mobileTapChainRef.current?.seekSteps ?? 0) > 0) reportProgress("seek");
      mobileTapChainRef.current = null;
      setMobileSeekFeedback(null);
      mobileTapResetTimerRef.current = null;
    }, MOBILE_TAP_CHAIN_WINDOW + 60);

    if (result.seekDelta !== 0) {
      if (mobileSingleTapTimerRef.current !== null) window.clearTimeout(mobileSingleTapTimerRef.current);
      mobileSingleTapTimerRef.current = null;
      seek((videoRef.current?.currentTime ?? 0) + result.seekDelta, false);
      setMobileSeekFeedback({ side, seconds: result.accumulatedSeconds });
      return;
    }

    if (mobileSingleTapTimerRef.current !== null) window.clearTimeout(mobileSingleTapTimerRef.current);
    mobileSingleTapTimerRef.current = window.setTimeout(() => {
      if (mobileTapChainRef.current?.seekSteps === 0) toggleMobileControls();
      mobileTapChainRef.current = null;
      mobileSingleTapTimerRef.current = null;
    }, MOBILE_TAP_CHAIN_WINDOW);
  }, [ensureMobileLandscape, reportProgress, seek, toggleMobileControls]);

  const handleMobileCenterTap = useCallback(() => {
    ensureMobileLandscape();
    if ((mobileTapChainRef.current?.seekSteps ?? 0) > 0) reportProgress("seek");
    resetMobileTapTimers();
    mobileTapChainRef.current = null;
    setMobileSeekFeedback(null);
    toggleMobileControls();
  }, [ensureMobileLandscape, reportProgress, resetMobileTapTimers, toggleMobileControls]);

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

  const beginTimelineScrub = useCallback(() => {
    scrubbingRef.current = true;
    scrubOriginRef.current = videoRef.current?.currentTime ?? currentTimeRef.current;
    if (controlsTimerRef.current) window.clearTimeout(controlsTimerRef.current);
    setShowControls(true);
  }, []);

  const previewTimelineScrub = useCallback((value: number) => {
    currentTimeRef.current = value;
    setCurrentTime(value);
    const timeline = timelineRef.current;
    const max = Number(timeline?.max || duration || 1);
    timeline?.style.setProperty("--player-progress", `${Math.min(100, (value / max) * 100)}%`);
  }, [duration]);

  const commitTimelineScrub = useCallback((value: number) => {
    if (!scrubbingRef.current) return;
    scrubbingRef.current = false;
    seek(value);
    revealControls();
  }, [revealControls, seek]);

  const cancelTimelineScrub = useCallback(() => {
    if (!scrubbingRef.current) return;
    scrubbingRef.current = false;
    previewTimelineScrub(scrubOriginRef.current);
    revealControls();
  }, [previewTimelineScrub, revealControls]);

  const handleTimelinePreview = (event: React.PointerEvent<HTMLInputElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    setTimelinePreview({ x: ratio * rect.width, time: ratio * duration });
  };

  const changeQuality = (index: number) => {
    setSelectedQualityIndex(index);
    const selected = availableQualities.find((item) => item.index === index);
    setPreferences((current) => ({ ...current, qualityHeight: index === -1 ? "auto" : selected?.height || "auto" }));
    if (hlsRef.current) hlsRef.current.currentLevel = index;
  };

  const changeAudio = (index: number) => {
    setSelectedAudioTrackIndex(index);
    const selected = availableAudioTracks.find((item) => item.index === index);
    setPreferences((current) => ({ ...current, audioLanguage: selected?.language || "" }));
    if (hlsRef.current) hlsRef.current.audioTrack = index;
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

  if (phase === "resolving" || phase === "preparing" || (phase === "loading" && !asset)) {
    return (
      <motion.div
        className="player-view player-state-view fixed inset-0 grid min-h-screen place-items-center bg-black px-6 text-white"
        data-mobile-player={mobilePlayer ? "true" : "false"}
        data-mobile-orientation={forcedLandscape ? "forced-landscape" : "native-landscape"}
        data-mobile-landscape-direction={forcedLandscapeDirection}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
      >
        <div className="text-center" role="status" aria-live="polite">
          <motion.i className="mx-auto block h-11 w-11 rounded-full border-2 border-white/20 border-t-white" animate={reduced ? undefined : { rotate: 360 }} transition={{ duration: 0.8, repeat: Infinity, ease: "linear" }} />
          <p className="mt-5 text-sm tracking-[0.16em] text-white/70">{phaseMessage[phase]}</p>
          {phase === "preparing" && <span className="mt-2 block text-xs text-white/40">The first compatible rendition is generated once and reused.</span>}
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
        data-mobile-landscape-direction={forcedLandscapeDirection}
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
      data-mobile-player={mobilePlayer ? "true" : "false"}
      data-mobile-orientation={forcedLandscape ? "forced-landscape" : "native-landscape"}
      data-mobile-landscape-direction={forcedLandscapeDirection}
      style={{ "--caption-scale": preferences.captionScale } as React.CSSProperties}
      onMouseMove={mobilePlayer ? undefined : revealControls}
      onClick={mobilePlayer ? undefined : revealControls}
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.viewEnter }}
    >
      <video
        ref={videoRef}
        className="h-full w-full object-contain"
        crossOrigin="anonymous"
        playsInline
        onLoadedMetadata={() => {
          const video = videoRef.current;
          if (!video) return;
          setDuration(Number.isFinite(video.duration) ? video.duration : runResponse.sourceMetadata.duration);
          applyResume(video);
          window.setTimeout(applySubtitlePreference, 0);
        }}
        onPlay={() => {
          setPhase("playing");
          lastAdvanceWallRef.current = performance.now();
          lastAdvanceMediaRef.current = videoRef.current?.currentTime ?? 0;
          if (runResponse.resumePosition > 0) {
            setShowStartOver(true);
            window.setTimeout(() => mountedRef.current && setShowStartOver(false), 10_000);
          }
        }}
        onPause={() => {
          captureWatchedTime();
          if (!completedRef.current) {
            setPhase("paused");
            reportProgress("pause");
          }
        }}
        onWaiting={() => setPhase("buffering")}
        onStalled={() => setPhase("buffering")}
        onPlaying={() => setPhase("playing")}
        onCanPlay={() => setPhase(videoRef.current?.paused ? "paused" : "playing")}
        onTimeUpdate={() => {
          captureWatchedTime();
          const nextTime = videoRef.current?.currentTime ?? 0;
          currentTimeRef.current = nextTime;
          setCurrentTime(nextTime);
        }}
        onDurationChange={() => setDuration(Number.isFinite(videoRef.current?.duration) ? (videoRef.current?.duration ?? 0) : runResponse.sourceMetadata.duration)}
        onProgress={() => {
          const video = videoRef.current;
          if (!video || video.buffered.length === 0) {
            setBufferedEnd(0);
            return;
          }
          let nextBufferedEnd = 0;
          for (let index = 0; index < video.buffered.length; index += 1) {
            if (video.buffered.start(index) <= video.currentTime + 0.25) nextBufferedEnd = Math.max(nextBufferedEnd, video.buffered.end(index));
          }
          setBufferedEnd(nextBufferedEnd);
        }}
        onVolumeChange={() => {
          setVolume(videoRef.current?.volume ?? 1);
          setMuted(videoRef.current?.muted ?? false);
        }}
        onEnded={finishPlayback}
        onError={() => {
          if (streamMode !== "progressive") return;
          setFatal({ title: "Compatibility playback failed", message: "Neither adaptive HLS nor direct progressive playback could decode this media.", retryable: true });
          setPhase("fatal");
        }}
      >
        {runResponse.subtitles.map((subtitle) => (
          <track
            key={`${subtitle.id}-${runResponse.ticket}`}
            kind="subtitles"
            src={`/api/playback/subtitles/${encodeURIComponent(runResponse.mediaId)}/${encodeURIComponent(subtitle.id)}?ticket=${encodeURIComponent(runResponse.ticket)}`}
            srcLang={subtitle.language}
            label={subtitle.label}
            onLoad={applySubtitlePreference}
          />
        ))}
      </video>

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
        {showStartOver && !mobilePlayer && (
          <motion.button className="player-start-over absolute left-6 top-6 z-40 rounded-lg border border-white/20 bg-black/70 px-4 py-2 text-sm backdrop-blur-md md:left-10 md:top-10" onClick={startOver} initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }}>
            Start over
          </motion.button>
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
                {showStartOver && (
                  <PlayerIconButton icon="rewind" label="Start over" className="mobile-player-start-over" onClick={startOver} />
                )}
                {forcedLandscape && (
                  <PlayerIconButton
                    icon="rotate"
                    label="Rotate player"
                    className="mobile-player-rotate"
                    onClick={() => setForcedLandscapeDirection((direction) => direction === "clockwise" ? "counterclockwise" : "clockwise")}
                  />
                )}
              </div>
              <div className="mobile-player-title">
                <h1>{asset.title}</h1>
                {asset.subtitle && <p>{asset.subtitle}</p>}
              </div>
              <PlayerIconButton
                icon="exit"
                label="Exit player"
                className="mobile-player-exit"
                onClick={exitPlayer}
              />
            </header>

            <div className="mobile-player-transport">
              <PlayerIconButton
                icon="rewind"
                label="Rewind 10 seconds"
                className="mobile-player-transport__seek"
                onClick={() => seek((videoRef.current?.currentTime ?? currentTimeRef.current) - 10)}
              />
              <PlayerIconButton
                icon={phase === "playing" ? "pause" : "play"}
                label={phase === "playing" ? "Pause" : "Play"}
                className="mobile-player-transport__play"
                onClick={() => phase === "playing" ? videoRef.current?.pause() : safePlay()}
              />
              <PlayerIconButton
                icon="forward"
                label="Forward 10 seconds"
                className="mobile-player-transport__seek"
                onClick={() => seek((videoRef.current?.currentTime ?? currentTimeRef.current) + 10)}
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
                onInput={(event) => previewTimelineScrub(Number(event.currentTarget.value))}
                onPointerUp={(event) => commitTimelineScrub(Number(event.currentTarget.value))}
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
                  {availableAudioTracks.length > 1 && (
                    <PlayerControlMenu
                      label="Audio language"
                      icon="audio"
                      value={selectedAudioTrackIndex}
                      options={availableAudioTracks.map((track) => ({ value: track.index, label: track.label }))}
                      onSelect={changeAudio}
                      onOpenChange={handleControlMenuOpenChange}
                    />
                  )}
                  {hasSubtitles && (
                    <PlayerControlMenu
                      label="Subtitles"
                      icon="captions"
                      value={preferences.subtitleLanguage}
                      options={[{ value: "off", label: "Subtitles off" }, ...runResponse.subtitles.map((subtitle) => ({ value: subtitle.language, label: subtitle.label }))]}
                      onSelect={(value) => setPreferences((current) => ({ ...current, subtitleLanguage: value }))}
                      onOpenChange={handleControlMenuOpenChange}
                    />
                  )}
                  {hasSubtitles && preferences.subtitleLanguage !== "off" && (
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
                      value={selectedQualityIndex}
                      options={availableQualities.map((item) => ({ value: item.index, label: item.label }))}
                      onSelect={changeQuality}
                      onOpenChange={handleControlMenuOpenChange}
                    />
                  )}
                  {document.pictureInPictureEnabled && (
                    <PlayerIconButton icon="pip" label="Picture in picture" onClick={togglePictureInPicture} />
                  )}
                </div>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {!mobilePlayer && (showControls || phase !== "playing") && phase !== "ended" && (
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
                  <p className="mt-1 text-[11px] uppercase tracking-[0.14em] text-white/35">{streamMode === "progressive" ? "Compatibility stream" : "Adaptive stream"}</p>
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
                  onPointerMove={handleTimelinePreview}
                  onPointerLeave={() => setTimelinePreview(null)}
                  onChange={(event) => seek(Number(event.target.value))}
                  className="player-timeline w-full cursor-pointer"
                  style={{
                    "--player-progress": `${duration > 0 ? Math.min(100, (currentTime / duration) * 100) : 0}%`,
                    "--player-buffered": `${duration > 0 ? Math.min(100, (bufferedEnd / duration) * 100) : 0}%`,
                  } as React.CSSProperties}
                />
              </div>

              <div className="mt-3 flex flex-wrap items-center gap-2 md:gap-3">
                <PlayerIconButton icon={phase === "playing" ? "pause" : "play"} label={phase === "playing" ? "Pause" : "Play"} onClick={() => phase === "playing" ? videoRef.current?.pause() : safePlay()} />
                <PlayerIconButton icon="rewind" label="Rewind 10 seconds" onClick={() => seek(currentTime - 10)} />
                <PlayerIconButton icon="forward" label="Forward 10 seconds" onClick={() => seek(currentTime + 10)} />
                <PlayerIconButton icon={muted ? "mute" : "volume"} label={muted ? "Unmute" : "Mute"} onClick={() => { if (videoRef.current) videoRef.current.muted = !videoRef.current.muted; }} />
                <input aria-label="Volume" type="range" min={0} max={1} step={0.01} value={muted ? 0 : volume} onChange={(event) => { const next = Number(event.target.value); if (videoRef.current) { videoRef.current.muted = false; videoRef.current.volume = next; } }} className="player-volume" />
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
                  {availableAudioTracks.length > 1 && (
                    <PlayerControlMenu
                      label="Audio language"
                      icon="audio"
                      value={selectedAudioTrackIndex}
                      options={availableAudioTracks.map((track) => ({ value: track.index, label: track.label }))}
                      onSelect={changeAudio}
                      onOpenChange={handleControlMenuOpenChange}
                    />
                  )}
                  {hasSubtitles && (
                    <PlayerControlMenu
                      label="Subtitles"
                      icon="captions"
                      value={preferences.subtitleLanguage}
                      options={[{ value: "off", label: "Subtitles off" }, ...runResponse.subtitles.map((subtitle) => ({ value: subtitle.language, label: subtitle.label }))]}
                      onSelect={(value) => setPreferences((current) => ({ ...current, subtitleLanguage: value }))}
                      onOpenChange={handleControlMenuOpenChange}
                    />
                  )}
                  {hasSubtitles && preferences.subtitleLanguage !== "off" && (
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
                      value={selectedQualityIndex}
                      options={availableQualities.map((item) => ({ value: item.index, label: item.label }))}
                      onSelect={changeQuality}
                      onOpenChange={handleControlMenuOpenChange}
                    />
                  )}
                  {document.pictureInPictureEnabled && <PlayerIconButton icon="pip" label="Picture in picture" onClick={togglePictureInPicture} />}
                  <PlayerIconButton
                    icon={fullscreenActive ? "fullscreen-exit" : "fullscreen"}
                    label={fullscreenActive ? "Exit fullscreen" : "Fullscreen"}
                    aria-pressed={fullscreenActive}
                    disabled={!fullscreenAvailable}
                    onClick={toggleFullscreen}
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
