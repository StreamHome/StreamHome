import React, { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getEpisodes } from "../../api/movies";
import { toggleWatchlist } from "../../api/watchlist";
import { MediaArtwork } from "../../components/media/MediaArtwork";
import { Button } from "../../components/ui/Button";
import { GlassPane } from "../../components/ui/GlassPane";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import type { Episode, Movie } from "../../types/api";
import { isPlayableMovie, tmdbIdFromMovie } from "../../utils/media";

interface DetailsRouterProps {
  movie: Movie;
  onClose: () => void;
  isWatchlisted: boolean;
  onWatchlistChange: (ids: string[]) => void;
}

export function DetailsRouter({ movie, onClose, isWatchlisted, onWatchlistChange }: DetailsRouterProps) {
  const navigate = useNavigate();
  const activeProfile = useProfileStore((state) => state.activeProfile)!;
  const theme = useThemeStore((state) => state.activeTheme);
  const [episodes, setEpisodes] = useState<Episode[]>(movie.episodes ?? []);
  const [season, setSeason] = useState<number | null>(movie.episodes?.[0]?.seasonNumber ?? null);
  const [loadingEpisodes, setLoadingEpisodes] = useState(movie.type === "series" && !(movie.episodes?.length));
  const [error, setError] = useState("");
  const [savingWatchlist, setSavingWatchlist] = useState(false);

  useEffect(() => {
    if (movie.type !== "series") return;
    const tmdbId = tmdbIdFromMovie(movie);
    if (tmdbId === null) {
      setLoadingEpisodes(false);
      setError("This series does not have a valid server identifier.");
      return;
    }
    let active = true;
    setLoadingEpisodes(true);
    getEpisodes(tmdbId)
      .then((data) => {
        if (!active) return;
        setEpisodes(data);
        setSeason(data[0]?.seasonNumber ?? null);
      })
      .catch((requestError: unknown) => {
        if (active) setError(requestError instanceof Error ? requestError.message : "Episodes could not be loaded.");
      })
      .finally(() => { if (active) setLoadingEpisodes(false); });
    return () => { active = false; };
  }, [movie]);

  const seasons = useMemo(() => Array.from(new Set(episodes.map((episode) => episode.seasonNumber))).sort((a, b) => a - b), [episodes]);
  const visibleEpisodes = episodes.filter((episode) => episode.seasonNumber === season);
  const playable = isPlayableMovie({ ...movie, episodes });

  const updateWatchlist = async () => {
    setSavingWatchlist(true);
    setError("");
    try {
      const response = await toggleWatchlist(activeProfile.id, movie.id);
      onWatchlistChange(response.watchlist);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Watchlist could not be updated.");
    } finally {
      setSavingWatchlist(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[100] overflow-y-auto bg-black/80 p-4 backdrop-blur-xl md:p-10" data-theme={theme} role="dialog" aria-modal="true" aria-label={`${movie.title} details`}>
      <div className="mx-auto max-w-6xl">
        <div className="mb-4 flex justify-end"><Button variant="ghost" onClick={onClose}>Close</Button></div>
        <GlassPane className="overflow-hidden" spotlight={false}>
          <div className="grid md:grid-cols-[320px_1fr]">
            <MediaArtwork src={movie.thumbnailUrl} alt={movie.title} className="h-full min-h-[460px] w-full object-cover" />
            <div className="p-7 md:p-10">
              <p className="text-xs uppercase tracking-[0.2em] text-[var(--text-muted)]">{movie.type}</p>
              <h1 className="mt-3 font-[family-name:var(--font-headline)] text-4xl font-bold md:text-6xl">{movie.title}</h1>
              <div className="mt-4 flex flex-wrap gap-3 text-sm text-[var(--text-secondary)]">
                {movie.releaseYear > 0 && <span>{movie.releaseYear}</span>}
                {movie.duration && <span>{movie.duration}</span>}
                {movie.rating && <span>{movie.rating}</span>}
                {movie.quality && <span>{movie.quality}</span>}
                {movie.voteAverage > 0 && <span>{movie.voteAverage.toFixed(1)} / 10</span>}
              </div>
              {movie.description && <p className="mt-6 leading-relaxed text-[var(--text-secondary)]">{movie.description}</p>}
              {!!movie.genres.length && <p className="mt-5 text-sm text-[var(--text-muted)]">{movie.genres.join(" · ")}</p>}
              <div className="mt-8 flex flex-wrap gap-3">
                {movie.type === "movie" && <Button disabled={!movie.videoUrl} onClick={() => navigate(`/watch/${movie.id}`)}>Play</Button>}
                <Button variant="secondary" disabled={savingWatchlist} onClick={() => void updateWatchlist()}>{isWatchlisted ? "Remove from watchlist" : "Add to watchlist"}</Button>
              </div>
              {!playable && <p className="mt-5 text-sm text-[var(--text-error)]">Playback is unavailable because the server did not provide a playable media file.</p>}
              {error && <p className="mt-4 text-sm text-[var(--text-error)]">{error}</p>}
            </div>
          </div>
        </GlassPane>

        {movie.type === "series" && (
          <section className="mt-6">
            <div className="mb-5 flex flex-wrap gap-2">
              {seasons.map((item) => <button key={item} className={`rounded border px-4 py-2 ${season === item ? "border-[var(--accent-container)] bg-[var(--accent-container)] text-white" : "border-[var(--glass-border)]"}`} onClick={() => setSeason(item)}>Season {item}</button>)}
            </div>
            {loadingEpisodes ? <p className="py-8 text-center text-[var(--text-muted)]">Loading episodes from the server…</p> : !visibleEpisodes.length ? <p className="py-8 text-center text-[var(--text-muted)]">No episodes are available from the server.</p> : <div className="grid gap-4 md:grid-cols-2">{visibleEpisodes.map((episode) => <button key={episode.id} className="overflow-hidden rounded border border-[var(--glass-border)] bg-[var(--glass-fill)] text-left disabled:opacity-60" disabled={!episode.videoUrl} onClick={() => navigate(`/watch/${episode.id}`)}><div className="grid grid-cols-[140px_1fr]"><MediaArtwork src={episode.thumbnailUrl} alt={episode.title} className="h-full min-h-28 w-full object-cover" /><div className="p-4"><div className="text-xs text-[var(--text-muted)]">Episode {episode.episodeNumber}</div><h3 className="mt-1 font-semibold">{episode.title}</h3><p className="mt-2 line-clamp-2 text-xs text-[var(--text-secondary)]">{episode.description}</p><div className="mt-2 text-xs text-[var(--text-muted)]">{episode.videoUrl ? episode.duration : "Unavailable on server"}</div></div></div></button>)}</div>}
          </section>
        )}
      </div>
    </div>
  );
}
