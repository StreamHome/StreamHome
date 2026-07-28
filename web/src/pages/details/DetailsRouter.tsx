import React, { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useLocation, useNavigate } from "react-router-dom";
import { getEpisodes } from "../../api/movies";
import { toggleWatchlist } from "../../api/watchlist";
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
import { RecommendationFeedback, useRecommendationFeedback } from "../dashboard/RecommendationFeedback";
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
  const [episodes, setEpisodes] = useState<Episode[]>(movie.episodes ?? []);
  const [loadingEpisodes, setLoadingEpisodes] = useState(canLoadEpisodes && !movie.episodes?.length);
  const [error, setError] = useState("");
  const [savingWatchlist, setSavingWatchlist] = useState(false);
  const { reduced } = useAppMotion();
  const recommendationFeedback = useRecommendationFeedback();
  const available = isAvailableMedia({ ...movie, episodes });

  useEffect(() => {
    setEpisodes(movie.episodes ?? []);
    setError("");
    if (!canLoadEpisodes || movie.episodes?.length) {
      setLoadingEpisodes(false);
      return;
    }
    const tmdbId = tmdbIdFromMovie(movie);
    if (tmdbId === null) { setLoadingEpisodes(false); setError("This series does not have a valid server identifier."); return; }
    let active = true;
    setLoadingEpisodes(true);
    getEpisodes(tmdbId)
      .then((data) => { if (active) setEpisodes(data); })
      .catch((requestError: unknown) => { if (active) setError(requestError instanceof Error ? requestError.message : "Episodes could not be loaded."); })
      .finally(() => { if (active) setLoadingEpisodes(false); });
    return () => { active = false; };
  }, [canLoadEpisodes, movie]);

  const seasons = useMemo(() => Array.from(new Set(episodes.map((episode) => episode.seasonNumber))).sort((a, b) => a - b), [episodes]);
  const selectedSeason = query.season && seasons.includes(query.season) ? query.season : seasons[0];
  const visibleEpisodes = episodes.filter((episode) => episode.seasonNumber === selectedSeason);
  const playable = available && isPlayableMovie({ ...movie, episodes });
  const resumeTarget = useMemo(() => detailsResumeTarget(movie, episodes, sessions), [episodes, movie, sessions]);

  useEffect(() => {
    if (movie.type !== "series" || !available || !seasons.length || query.season === selectedSeason) return;
    navigate(appUrl(profile.id, "details", { media: movie.id, season: selectedSeason }), { replace: true, state: location.state });
  }, [available, location.state, movie.id, movie.type, navigate, profile.id, query.season, seasons.length, selectedSeason]);

  const close = () => {
    if ((location.state as { fromApp?: boolean } | null)?.fromApp) navigate(-1);
    else onClose();
  };
  const updateWatchlist = async () => {
    setSavingWatchlist(true); setError("");
    try {
      onWatchlistChange((await toggleWatchlist(profile.id, movie.id)).watchlist);
      onRecommendationInvalidated?.();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Watchlist could not be updated.");
    } finally {
      setSavingWatchlist(false);
    }
  };
  const selectSeason = (season: number) => navigate(appUrl(profile.id, "details", { media: movie.id, season }), { replace: true, state: location.state });
  const play = (media: string) => navigate(appUrl(profile.id, "watch", { media }), { state: { fromApp: true, previous: location.search } });

  const stateKey = !available ? "cached" : loadingEpisodes ? "loading" : visibleEpisodes.length ? `season-${selectedSeason}` : "empty";
  const feedback = error || (!available
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
        {recommendationFeedback && <motion.div variants={CONTENT_REVEAL} className="details-feedback"><small>Shape your recommendations</small><RecommendationFeedback movieId={movie.id} preference={recommendationFeedback.preferences[movie.id] ?? movie.viewerPreference ?? null} onChange={recommendationFeedback.onChange} /></motion.div>}
        {movie.description && <motion.p variants={CONTENT_REVEAL} className="details-description">{movie.description}</motion.p>}
        {movie.genres.length > 0 && <motion.div variants={CONTENT_REVEAL} className="details-genres">{movie.genres.map((genre) => <span key={genre}>{genre}</span>)}</motion.div>}
        <motion.div variants={CONTENT_REVEAL} className="feature-actions">
          {movie.type === "movie" && <button className="feature-action feature-action--primary details-playback-action" disabled={!playable} onClick={() => play(movie.id)}><MediaActionIcon name={availability === "processing" ? "play" : resumeTarget ? "resume" : "play"} /><span>{available ? availability === "processing" ? "Play while downloading" : resumeTarget?.label ?? "Play now" : "Playback unavailable"}</span></button>}
          {movie.type === "series" && resumeTarget && <button className="feature-action feature-action--primary details-playback-action details-playback-action--context" disabled={!playable} onClick={() => play(resumeTarget.mediaId)}><MediaActionIcon name="resume" /><span>{resumeTarget.label}</span><small>{resumeTarget.context}</small></button>}
          <motion.button layout className="feature-action details-watchlist-action" disabled={savingWatchlist} onClick={() => void updateWatchlist()}><MediaActionIcon name={isWatchlisted ? "bookmark-remove" : "bookmark-add"} /><span>{savingWatchlist ? "Updating…" : isWatchlisted ? "Remove from watchlist" : "Add to watchlist"}</span></motion.button>
        </motion.div>
        <AnimatePresence mode="wait">{feedback && <motion.p key={feedback} className="details-warning" initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: MOTION_TIMINGS.notice }}>{feedback}</motion.p>}</AnimatePresence>
      </motion.div>
    </motion.section>
    {movie.type === "series" && <motion.section variants={CONTENT_REVEAL} className="episode-browser">
      <header><div><p>EPISODE INDEX</p><h2>Seasons and episodes</h2></div>{available && <div className="season-tabs">{seasons.map((season) => <motion.button layout key={season} data-active={selectedSeason === season} onClick={() => selectSeason(season)}>Season {season}</motion.button>)}</div>}</header>
      <AnimatedState stateKey={stateKey}>{!available ? <p className="episode-state">Episode playback will appear after this cached suggestion becomes available on the server.</p> : loadingEpisodes ? <p className="episode-state">Loading episodes from the server...</p> : !visibleEpisodes.length ? <p className="episode-state">No episodes are available from the server.</p> : <motion.div variants={CONTENT_STAGGER} initial="hidden" animate="shown" className="episode-grid">{visibleEpisodes.map((episode) => {
        const resumesHere = resumeTarget?.mediaId === episode.id;
        const previewing = Boolean(episode.previewTaskId);
        const episodePlayable = Boolean(episode.videoUrl || episode.previewTaskId);
        return <motion.button layout variants={CONTENT_REVEAL} key={episode.id} className="episode-card" data-resumable={resumesHere || undefined} disabled={!episodePlayable} onClick={() => play(episode.id)}><MediaArtwork src={episode.thumbnailUrl} alt={episode.title} media={movie} episode={episode} className="episode-card__art" /><span><small>EPISODE {episode.episodeNumber}</small><strong>{episode.title}</strong><p>{episode.description || "No server description available."}</p><i>{episodePlayable ? previewing ? "Downloading in background" : episode.duration : "Unavailable on server"}</i><span className="episode-card__action"><MediaActionIcon name={previewing ? "play" : resumesHere ? "resume" : "episode"} /><b>{previewing ? "Play while downloading" : resumesHere ? `Resume from ${formatDuration(resumeTarget.session.timestamp)}` : "Play episode"}</b></span></span></motion.button>;
      })}</motion.div>}</AnimatedState>
    </motion.section>}
  </motion.article>;
}
