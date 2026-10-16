import React, { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useLocation, useNavigate } from "react-router-dom";
import { getEpisodes } from "../../api/movies";
import { ApiError } from "../../api/client";
import { getWatchlist, toggleWatchlist } from "../../api/watchlist";
import { MediaActionIcon } from "../../components/media/MediaActionIcon";
import { MediaArtwork } from "../../components/media/MediaArtwork";
import { appUrl, parseAppQuery } from "../../navigation/queryState";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import { getThemeDefinition } from "../../themes/application/themeRegistry";
import type { Episode, Movie, PlaybackSession } from "../../types/api";
import { formatDuration } from "../../utils/format";
import { isAvailableMedia, isPlayableMovie, mediaAvailability, tmdbIdFromMovie } from "../../utils/media";
import { AnimatedState, CONTENT_REVEAL, CONTENT_STAGGER, MOTION_EASE, MOTION_TIMINGS, useAppMotion } from "../../motion/motionSystem";
import { AvailabilityBadge } from "../dashboard/RecommendationMeta";
import { RecommendationFeedback, feedbackPreference, useRecommendationFeedback } from "../dashboard/RecommendationFeedback";
import { detailsResumeTarget } from "./detailsPlayback";

interface DetailsRouterProps {
  movie: Movie;
  onClose: () => void;
  isWatchlisted: boolean;
  sessions: PlaybackSession[];
  onWatchlistChange: (ids: string[]) => void;
  onRecommendationInvalidated?: () => void;
}

export function DetailsRouter({ movie, onClose, isWatchlisted, sessions, onWatchlistChange, onRecommendationInvalidated }: DetailsRouterProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const profile = useProfileStore((state) => state.activeProfile)!;
  const theme = useThemeStore((state) => state.activeTheme);
  const definition = getThemeDefinition(theme);
  const query = useMemo(() => parseAppQuery(location.search), [location.search]);
  const availability = mediaAvailability(movie);
  const canLoadEpisodes = movie.type === "series" && (availability === "available" || availability === "processing");
  const [episodeRead, setEpisodeRead] = useState<{ movieId: string; episodes: Episode[]; status: "pending" | "success" | "error"; error: string; retryable: boolean }>({ movieId: movie.id, episodes: movie.episodes ?? [], status: canLoadEpisodes && movie.episodes == null ? "pending" : "success", error: "", retryable: true });
  const [episodeAttempt, setEpisodeAttempt] = useState(0);
  const [watchlistError, setWatchlistError] = useState("");
  const [watchlistUncertain, setWatchlistUncertain] = useState(false);
  const watchlistBusy = useRef(false);
  const [savingWatchlist, setSavingWatchlist] = useState(false);
  const mutationGeneration = useRef(0);
  const episodes = episodeRead.movieId === movie.id ? episodeRead.episodes : movie.episodes ?? [];
  const episodeStatus = episodeRead.movieId === movie.id ? episodeRead.status : "pending";
  const episodeError = episodeRead.movieId === movie.id ? episodeRead.error : "";
  const loadingEpisodes = episodeStatus === "pending";
  const { reduced } = useAppMotion();
  const recommendationFeedback = useRecommendationFeedback();
  const available = isAvailableMedia({ ...movie, episodes });

  useEffect(() => {
    mutationGeneration.current += 1;
    setWatchlistError("");
    setWatchlistUncertain(false);
    watchlistBusy.current = false;
    setSavingWatchlist(false);
    return () => { mutationGeneration.current += 1; };
  }, [movie.id, profile.id]);

  useEffect(() => {
    if (!canLoadEpisodes || movie.episodes != null) {
      setEpisodeRead({ movieId: movie.id, episodes: movie.episodes ?? [], status: "success", error: "", retryable: false });
      return;
    }
    const tmdbId = tmdbIdFromMovie(movie);
    if (tmdbId === null) {
      setEpisodeRead({ movieId: movie.id, episodes: [], status: "error", error: "This series does not have a valid server identifier.", retryable: false });
      return;
    }
    const abort = new AbortController();
    setEpisodeRead((current) => ({ movieId: movie.id, episodes: current.movieId === movie.id ? current.episodes : [], status: "pending", error: "", retryable: true }));
    getEpisodes(tmdbId, abort.signal).then((data) => {
      if (!abort.signal.aborted) setEpisodeRead({ movieId: movie.id, episodes: data, status: "success", error: "", retryable: true });
    }).catch((requestError: unknown) => {
      if (!abort.signal.aborted) setEpisodeRead((current) => ({ ...current, status: "error", error: requestError instanceof Error ? requestError.message : "Episodes could not be loaded." }));
    });
    return () => abort.abort();
  }, [canLoadEpisodes, episodeAttempt, movie.id, movie.episodes]);

  const seasons = useMemo(() => Array.from(new Set(episodes.map((episode) => episode.seasonNumber))).sort((a, b) => a - b), [episodes]);
  const selectedSeason = query.season && seasons.includes(query.season) ? query.season : seasons[0];
  const visibleEpisodes = episodes.filter((episode) => episode.seasonNumber === selectedSeason);
  const playable = available && isPlayableMovie({ ...movie, episodes });
  const resumeTarget = useMemo(() => detailsResumeTarget(movie, episodes, sessions), [episodes, movie, sessions]);

  useEffect(() => {
    if (episodeStatus !== "success" || movie.type !== "series" || !available || !seasons.length || query.season === selectedSeason) return;
    navigate(appUrl(profile.id, "details", { media: movie.id, season: selectedSeason }), { replace: true, state: location.state });
  }, [available, episodeStatus, location.state, movie.id, movie.type, navigate, profile.id, query.season, seasons.length, selectedSeason]);

  const close = () => {
    if ((location.state as { fromApp?: boolean } | null)?.fromApp) navigate(-1);
    else onClose();
  };
  const readConfirmedWatchlist = async (generation: number) => {
    try {
      const ids = await getWatchlist(profile.id);
      if (generation !== mutationGeneration.current) return;
      onWatchlistChange(ids);
      setWatchlistUncertain(false);
      setWatchlistError("The saved list was checked. Its current saved state is shown.");
    } catch {
      if (generation !== mutationGeneration.current) return;
      setWatchlistUncertain(true);
      setWatchlistError("The update could not be confirmed. Check the saved list before making another change.");
    }
  };
  const checkWatchlist = async () => {
    if (watchlistBusy.current) return;
    const generation = mutationGeneration.current;
    watchlistBusy.current = true;
    setSavingWatchlist(true);
    await readConfirmedWatchlist(generation);
    if (generation === mutationGeneration.current) { watchlistBusy.current = false; setSavingWatchlist(false); }
  };
  const updateWatchlist = async () => {
    if (watchlistBusy.current || watchlistUncertain) return;
    const generation = mutationGeneration.current;
    watchlistBusy.current = true;
    setSavingWatchlist(true); setWatchlistError("");
    try {
      const result = await toggleWatchlist(profile.id, movie.id);
      if (generation !== mutationGeneration.current) return;
      onWatchlistChange(result.watchlist);
      onRecommendationInvalidated?.();
    } catch (requestError) {
      if (generation !== mutationGeneration.current) return;
      if ((requestError instanceof ApiError && requestError.status === 0) || requestError instanceof SyntaxError) {
        setWatchlistUncertain(true);
        setWatchlistError("Checking whether the saved-list update completed…");
        await readConfirmedWatchlist(generation);
      } else setWatchlistError(requestError instanceof Error ? requestError.message : "Watchlist could not be updated.");
    } finally {
      if (generation === mutationGeneration.current) { watchlistBusy.current = false; setSavingWatchlist(false); }
    }
  };
  const selectSeason = (season: number) => navigate(appUrl(profile.id, "details", { media: movie.id, season }), { replace: true, state: location.state });
  const play = (media: string) => navigate(appUrl(profile.id, "watch", { media }), { state: { fromApp: true, previous: location.search } });

  const stateKey = !canLoadEpisodes ? "cached" : episodeError && !episodes.length ? "error" : loadingEpisodes && !episodes.length ? "loading" : visibleEpisodes.length ? `season-${selectedSeason}` : "empty";
  const feedback = watchlistError || (!available
    ? mediaAvailability(movie) === "processing" ? "This title is being prepared by the server." : "This cached suggestion has metadata and artwork, but no playable media is available yet."
    : !playable ? "Playback is unavailable because the server did not provide a playable media file." : "");

  return <motion.article className="details-view" data-details-theme={definition.detailsVariant} variants={CONTENT_STAGGER} initial="hidden" animate="shown">
    <motion.button variants={CONTENT_REVEAL} className="details-close" onClick={close}><MediaActionIcon name="back" /><span>Back</span></motion.button>
    <motion.section variants={CONTENT_REVEAL} className="details-hero">
      <motion.div className="details-backdrop" initial={reduced ? { opacity: 0 } : { opacity: 0, scale: 1.06 }} animate={{ opacity: 1, scale: 1 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.billboard, ease: MOTION_EASE }}><MediaArtwork src={movie.bannerUrl || movie.thumbnailUrl} alt={movie.title} media={movie} className="h-full w-full object-cover" /></motion.div>
      <AvailabilityBadge movie={movie} />
      <motion.div className="details-poster" initial={reduced ? { opacity: 0 } : { opacity: 0, y: 28, rotate: -1.5 }} animate={{ opacity: 1, y: 0, rotate: 0 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.artwork, delay: reduced ? 0 : .1, ease: MOTION_EASE }}><MediaArtwork src={movie.thumbnailUrl} alt={movie.title} media={movie} className="h-full w-full object-cover" /></motion.div>
      <motion.div variants={CONTENT_STAGGER} className="details-copy">
        <motion.p variants={CONTENT_REVEAL}>{movie.type.toUpperCase()} / CATALOG RECORD</motion.p>
        <motion.h1 variants={CONTENT_REVEAL}>{movie.title}</motion.h1>
        <motion.div variants={CONTENT_REVEAL} className="details-meta">{movie.releaseYear > 0 && <span>{movie.releaseYear}</span>}{movie.duration && <span>{movie.duration}</span>}{movie.rating && <span>{movie.rating}</span>}{movie.quality && <span>{movie.quality}</span>}{movie.voteAverage > 0 && <span>{movie.voteAverage.toFixed(1)} / 10</span>}</motion.div>
        {movie.recommendationReasons?.length ? <motion.div variants={CONTENT_REVEAL} className="details-reasons"><small>Why this was selected</small>{movie.recommendationReasons.map((reason) => <span key={reason}>{reason}</span>)}</motion.div> : null}
        {recommendationFeedback && <motion.div variants={CONTENT_REVEAL} className="details-feedback"><small>Shape your recommendations</small><RecommendationFeedback movieId={movie.id} preference={feedbackPreference(recommendationFeedback, movie.id, movie.viewerPreference ?? null)} onChange={recommendationFeedback.onChange} /></motion.div>}
        {movie.description && <motion.p variants={CONTENT_REVEAL} className="details-description">{movie.description}</motion.p>}
        {movie.genres.length > 0 && <motion.div variants={CONTENT_REVEAL} className="details-genres">{movie.genres.map((genre) => <span key={genre}>{genre}</span>)}</motion.div>}
        <motion.div variants={CONTENT_REVEAL} className="feature-actions">
          {movie.type === "movie" && <button className="feature-action feature-action--primary details-playback-action" disabled={!playable} onClick={() => play(movie.id)}><MediaActionIcon name={availability === "processing" ? "play" : resumeTarget ? "resume" : "play"} /><span>{available ? availability === "processing" ? "Play while downloading" : resumeTarget?.label ?? "Play now" : "Playback unavailable"}</span></button>}
          {movie.type === "series" && resumeTarget && <button className="feature-action feature-action--primary details-playback-action details-playback-action--context" disabled={!playable} onClick={() => play(resumeTarget.mediaId)}><MediaActionIcon name="resume" /><span>{resumeTarget.label}</span><small>{resumeTarget.context}</small></button>}
          <motion.button layout className="feature-action details-watchlist-action" disabled={savingWatchlist || watchlistUncertain} onClick={() => void updateWatchlist()}><MediaActionIcon name={isWatchlisted ? "bookmark-remove" : "bookmark-add"} /><span>{savingWatchlist ? "Updating…" : isWatchlisted ? "Remove from watchlist" : "Add to watchlist"}</span></motion.button>
          {watchlistUncertain && <button className="feature-action" disabled={savingWatchlist} onClick={() => void checkWatchlist()}>{savingWatchlist ? "Checking saved list…" : "Check watchlist"}</button>}
        </motion.div>
        <AnimatePresence mode="wait">{feedback && <motion.p key={feedback} className="details-warning" initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: MOTION_TIMINGS.notice }}>{feedback}</motion.p>}</AnimatePresence>
      </motion.div>
    </motion.section>
    {movie.type === "series" && <motion.section variants={CONTENT_REVEAL} className="episode-browser">
      <header><div><p>EPISODE INDEX</p><h2>Seasons and episodes</h2></div>{available && <div className="season-tabs" role="group" aria-label="Select season">{seasons.map((season) => <motion.button layout key={season} data-active={selectedSeason === season} aria-pressed={selectedSeason === season} onClick={() => selectSeason(season)}>Season {season}</motion.button>)}</div>}</header>
      {canLoadEpisodes && episodeError && <div className="episode-read-status"><h3>Episodes unavailable</h3><p role="alert">{episodes.length ? `Showing previously loaded episodes. ${episodeError}` : episodeError}</p>{episodeRead.retryable && <button className="catalog-retry" onClick={() => setEpisodeAttempt((value) => value + 1)}>Retry episodes</button>}</div>}
      {canLoadEpisodes && loadingEpisodes && episodes.length > 0 && <p className="episode-state" role="status">Refreshing episodes…</p>}
      <AnimatedState stateKey={stateKey}>{!canLoadEpisodes ? <p className="episode-state">Episode playback will appear after this cached suggestion becomes available on the server.</p> : loadingEpisodes && !episodes.length ? <p className="episode-state" role="status">Loading episodes from the server...</p> : episodeError && !episodes.length ? null : !visibleEpisodes.length ? <p className="episode-state">No episodes are available from the server.</p> : <motion.div variants={CONTENT_STAGGER} initial="hidden" animate="shown" className="episode-grid">{visibleEpisodes.map((episode) => {
        const resumesHere = resumeTarget?.mediaId === episode.id;
        const previewing = Boolean(episode.previewTaskId);
        const episodePlayable = Boolean(episode.videoUrl || episode.previewTaskId);
        return <motion.button layout variants={CONTENT_REVEAL} key={episode.id} className="episode-card" data-resumable={resumesHere || undefined} disabled={!episodePlayable} onClick={() => play(episode.id)}><MediaArtwork src={episode.thumbnailUrl} alt={episode.title} media={movie} episode={episode} className="episode-card__art" /><span><small>EPISODE {episode.episodeNumber}</small><strong>{episode.title}</strong><p>{episode.description || "No server description available."}</p><i>{episodePlayable ? previewing ? "Downloading in background" : episode.duration : "Unavailable on server"}</i><span className="episode-card__action"><MediaActionIcon name={previewing ? "play" : resumesHere ? "resume" : "episode"} /><b>{previewing ? "Play while downloading" : resumesHere ? `Resume from ${formatDuration(resumeTarget.session.timestamp)}` : "Play episode"}</b></span></span></motion.button>;
      })}</motion.div>}</AnimatedState>
    </motion.section>}
  </motion.article>;
}
