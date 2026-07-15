import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { getEpisodes, getMovies } from "../../api/movies";
import { trackPlayback } from "../../api/playback";
import { Button } from "../../components/ui/Button";
import { useAuthStore } from "../../stores/authStore";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import type { Episode, Movie, SubtitleInfo } from "../../types/api";
import { formatDuration } from "../../utils/format";
import { isServerArtworkUrl } from "../../utils/media";

const QUALITIES = ["Source", "1080p", "720p", "480p", "360p", "240p"] as const;

interface PlayableAsset {
  id: string;
  movieId: string;
  episodeId?: string;
  title: string;
  subtitle: string;
  videoUrl: string;
  durationLabel: string;
  quality: string;
  languages: string[];
  subtitles: SubtitleInfo[];
  skipMarkers: Record<string, unknown>;
}

function episodeTmdbId(mediaId: string): number | null {
  const match = mediaId.match(/^ep_(\d+)_s\d+_e\d+$/);
  return match ? Number(match[1]) : null;
}

function assetFromMovie(movie: Movie): PlayableAsset {
  return {
    id: movie.id,
    movieId: movie.id,
    title: movie.title,
    subtitle: "",
    videoUrl: movie.videoUrl,
    durationLabel: movie.duration,
    quality: movie.quality,
    languages: movie.languages,
    subtitles: movie.subtitles,
    skipMarkers: movie.skipMarkers,
  };
}

function assetFromEpisode(movie: Movie, episode: Episode): PlayableAsset {
  return {
    id: episode.id,
    movieId: movie.id,
    episodeId: episode.id,
    title: movie.title,
    subtitle: `S${episode.seasonNumber} E${episode.episodeNumber} · ${episode.title}`,
    videoUrl: episode.videoUrl,
    durationLabel: episode.duration,
    quality: episode.quality,
    languages: episode.languages,
    subtitles: episode.subtitles,
    skipMarkers: episode.skipMarkers,
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

export function PlayerPage() {
  const { mediaId = "" } = useParams();
  const navigate = useNavigate();
  const token = useAuthStore((state) => state.token);
  const profile = useProfileStore((state) => state.activeProfile)!;
  const theme = useThemeStore((state) => state.activeTheme);
  const containerRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const seekTimerRef = useRef<number | null>(null);
  const trackingTimerRef = useRef<number | null>(null);
  const [asset, setAsset] = useState<PlayableAsset | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);
  const [quality, setQuality] = useState<(typeof QUALITIES)[number]>("Source");
  const [audioTrack, setAudioTrack] = useState(0);
  const [streamStart, setStreamStart] = useState(0);
  const [showControls, setShowControls] = useState(true);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    const resolveAsset = async () => {
      const catalog = await getMovies();
      if (mediaId.startsWith("m_")) {
        const movie = catalog.find((item) => item.id === mediaId);
        if (!movie) throw new Error("This movie is not present in the server catalog.");
        return assetFromMovie(movie);
      }
      if (mediaId.startsWith("tv_")) throw new Error("Choose a playable episode before opening the player.");
      if (mediaId.startsWith("ep_")) {
        for (const movie of catalog.filter((item) => item.type === "series")) {
          const embedded = movie.episodes?.find((episode) => episode.id === mediaId);
          if (embedded) return assetFromEpisode(movie, embedded);
        }
        const tmdbId = episodeTmdbId(mediaId);
        if (tmdbId !== null) {
          const movie = catalog.find((item) => item.id === `tv_${tmdbId}`);
          if (movie) {
            const episodes = await getEpisodes(tmdbId);
            const episode = episodes.find((item) => item.id === mediaId);
            if (episode) return assetFromEpisode(movie, episode);
          }
        }
      }
      throw new Error("This media item is not present in the server catalog.");
    };

    resolveAsset()
      .then((resolved) => {
        if (!active) return;
        if (!resolved.videoUrl) throw new Error("The server did not provide a playable media file.");
        setAsset(resolved);
      })
      .catch((requestError: unknown) => { if (active) setError(requestError instanceof Error ? requestError.message : "Playback could not be initialized."); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [mediaId]);

  const videoSrc = useMemo(() => asset && token ? `/api/stream/${encodeURIComponent(asset.id)}?quality=${encodeURIComponent(quality)}&audio_track=${audioTrack}&start=${Math.max(0, streamStart)}&token=${encodeURIComponent(token)}` : "", [asset, token, quality, audioTrack, streamStart]);
  const skipMarker = asset ? activeSkipMarker(asset.skipMarkers, currentTime) : null;

  const safePlay = useCallback(() => {
    void videoRef.current?.play().catch(() => setError("The browser could not start playback."));
  }, []);

  const seek = useCallback((nextTime: number) => {
    const bounded = Math.min(Math.max(nextTime, 0), duration || nextTime);
    setCurrentTime(bounded);
    if (seekTimerRef.current) window.clearTimeout(seekTimerRef.current);
    seekTimerRef.current = window.setTimeout(() => {
      if (videoRef.current) videoRef.current.currentTime = bounded;
      seekTimerRef.current = null;
    }, 250);
  }, [duration]);

  const toggleFullscreen = useCallback(() => {
    const operation = document.fullscreenElement
      ? document.exitFullscreen()
      : containerRef.current?.requestFullscreen();
    if (operation) void operation.catch(() => undefined);
  }, []);

  const reportProgress = useCallback((finished = false) => {
    if (!asset || !profile) return;
    const watched = videoRef.current?.currentTime ?? currentTime;
    const total = videoRef.current?.duration || duration;
    void trackPlayback({
      movieId: asset.movieId,
      profileId: profile.id,
      episodeId: asset.episodeId,
      timestamp: Math.floor(watched),
      durationWatched: Math.floor(watched),
      completionRate: total > 0 ? Math.min(watched / total, 1) : 0,
      isFinished: finished,
    }).catch(() => undefined);
  }, [asset, currentTime, duration, profile]);

  useEffect(() => {
    if (!asset) return;
    trackingTimerRef.current = window.setInterval(() => reportProgress(false), 10_000);
    return () => {
      if (trackingTimerRef.current) window.clearInterval(trackingTimerRef.current);
      reportProgress(false);
    };
  }, [asset, reportProgress]);

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement) return;
      if (event.key === " ") { event.preventDefault(); isPlaying ? videoRef.current?.pause() : safePlay(); }
      if (event.key === "ArrowLeft") seek(currentTime - 10);
      if (event.key === "ArrowRight") seek(currentTime + 10);
      if (event.key.toLowerCase() === "m" && videoRef.current) videoRef.current.muted = !videoRef.current.muted;
      if (event.key.toLowerCase() === "f") toggleFullscreen();
      if (event.key === "Escape" && !document.fullscreenElement) navigate(-1);
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [currentTime, isPlaying, navigate, safePlay, seek, toggleFullscreen]);

  useEffect(() => {
    let timeout: number | undefined;
    const reveal = () => {
      setShowControls(true);
      window.clearTimeout(timeout);
      if (isPlaying) timeout = window.setTimeout(() => setShowControls(false), 3000);
    };
    window.addEventListener("mousemove", reveal);
    window.addEventListener("touchstart", reveal, { passive: true });
    return () => { window.removeEventListener("mousemove", reveal); window.removeEventListener("touchstart", reveal); window.clearTimeout(timeout); };
  }, [isPlaying]);

  const changeStream = (nextQuality: (typeof QUALITIES)[number], nextAudio = audioTrack) => {
    setStreamStart(videoRef.current?.currentTime ?? currentTime);
    setQuality(nextQuality);
    setAudioTrack(nextAudio);
  };

  if (loading) return <div className="grid min-h-screen place-items-center bg-black text-white">Loading media from the server…</div>;
  if (error || !asset) return <div className="grid min-h-screen place-items-center bg-black p-6 text-white"><div className="max-w-lg text-center"><h1 className="text-2xl font-semibold">Playback unavailable</h1><p className="mt-3 text-white/60">{error}</p><Button className="mt-6" onClick={() => navigate(-1)}>Go back</Button></div></div>;

  const usableSubtitles = asset.subtitles.filter((subtitle) => isServerArtworkUrl(subtitle.url ?? subtitle.path));

  return (
    <div ref={containerRef} className="fixed inset-0 z-[200] bg-black text-white" data-theme={theme} onClick={() => setShowControls(true)}>
      <video
        ref={videoRef}
        src={videoSrc}
        className="h-full w-full object-contain"
        autoPlay
        onPlay={() => setIsPlaying(true)}
        onPause={() => setIsPlaying(false)}
        onTimeUpdate={() => { if (!seekTimerRef.current) setCurrentTime(videoRef.current?.currentTime ?? 0); }}
        onDurationChange={() => setDuration(Number.isFinite(videoRef.current?.duration) ? (videoRef.current?.duration ?? 0) : 0)}
        onLoadedMetadata={() => { if (streamStart > 0 && videoRef.current) videoRef.current.currentTime = streamStart; safePlay(); }}
        onVolumeChange={() => { setVolume(videoRef.current?.volume ?? 1); setMuted(videoRef.current?.muted ?? false); }}
        onEnded={() => { setIsPlaying(false); reportProgress(true); }}
        onError={() => setError("The server stream could not be played.")}
      >
        {usableSubtitles.map((subtitle) => <track key={`${subtitle.language}-${subtitle.url ?? subtitle.path}`} kind="subtitles" src={subtitle.url ?? subtitle.path} srcLang={subtitle.language} label={subtitle.language.toUpperCase()} />)}
      </video>

      {skipMarker && <Button className="absolute bottom-32 right-8 z-30" onClick={() => seek(skipMarker.end)}>{skipMarker.label}</Button>}

      <div className={`absolute inset-x-0 bottom-0 bg-gradient-to-t from-black via-black/80 to-transparent px-5 pb-6 pt-24 transition-opacity md:px-10 ${showControls || !isPlaying ? "opacity-100" : "pointer-events-none opacity-0"}`}>
        <div className="mx-auto max-w-7xl">
          <div className="mb-5 flex items-start justify-between gap-5">
            <div><h1 className="text-xl font-semibold md:text-2xl">{asset.title}</h1>{asset.subtitle && <p className="mt-1 text-sm text-white/60">{asset.subtitle}</p>}</div>
            <button onClick={() => navigate(-1)} className="text-sm text-white/70">Exit</button>
          </div>
          <input aria-label="Playback position" type="range" min={0} max={duration || 0} step={0.1} value={Math.min(currentTime, duration || currentTime)} onChange={(event) => seek(Number(event.target.value))} className="w-full accent-[var(--accent-container)]" />
          <div className="mt-4 flex flex-wrap items-center gap-4">
            <button aria-label={isPlaying ? "Pause" : "Play"} onClick={() => isPlaying ? videoRef.current?.pause() : safePlay()} className="text-lg">{isPlaying ? "Pause" : "Play"}</button>
            <button onClick={() => seek(currentTime - 10)}>−10s</button><button onClick={() => seek(currentTime + 10)}>+10s</button>
            <button onClick={() => { if (videoRef.current) videoRef.current.muted = !videoRef.current.muted; }}>{muted ? "Unmute" : "Mute"}</button>
            <input aria-label="Volume" type="range" min={0} max={1} step={0.01} value={muted ? 0 : volume} onChange={(event) => { const next = Number(event.target.value); if (videoRef.current) { videoRef.current.muted = false; videoRef.current.volume = next; } }} className="w-24 accent-[var(--accent-container)]" />
            <span className="text-sm text-white/60">{formatDuration(currentTime)} / {duration ? formatDuration(duration) : asset.durationLabel}</span>
            <div className="ml-auto flex items-center gap-3">
              {asset.languages.length > 1 && <select aria-label="Audio language" value={audioTrack} onChange={(event) => changeStream(quality, Number(event.target.value))} className="rounded border border-white/20 bg-black px-3 py-2 text-sm">{asset.languages.map((language, index) => <option key={`${language}-${index}`} value={index}>{language.toUpperCase()}</option>)}</select>}
              <select aria-label="Quality" value={quality} onChange={(event) => changeStream(event.target.value as (typeof QUALITIES)[number])} className="rounded border border-white/20 bg-black px-3 py-2 text-sm">{QUALITIES.map((item) => <option key={item} value={item}>{item}</option>)}</select>
              <button onClick={toggleFullscreen}>Fullscreen</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
