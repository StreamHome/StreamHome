import React, { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useLocation, useNavigate } from "react-router-dom";
import { MediaArtwork } from "../../../components/media/MediaArtwork";
import { ProgressBar } from "../../../components/ui/ProgressBar";
import { useTelemetry } from "../../../hooks/useTelemetry";
import { AnimatedState, AnimatedView, CONTENT_REVEAL, CONTENT_STAGGER, REDUCED_BILLBOARD_MOTION, THEME_MOTION, useAppMotion } from "../../../motion/motionSystem";
import { useAnimatedRail } from "../../../motion/useAnimatedRail";
import { appUrl, isCatalogView, preservedCatalogCategory, type AppQueryState, type AppView, type CatalogView } from "../../../navigation/queryState";
import { profileEditUrl } from "../../../navigation/profileEditing";
import { useAuthStore } from "../../../stores/authStore";
import { useProfileStore } from "../../../stores/profileStore";
import type { ThemeApplicationProps } from "../../../themes/application/contracts";
import type { DiscoverMovie, Movie, PlaybackSession } from "../../../types/api";
import { completionFraction, isAvailableMedia, isPlayableMovie, mediaAvailability } from "../../../utils/media";
import { DetailsRouter } from "../../details/DetailsRouter";
import { appViewMotionKey } from "../ThemeDashboard";
import { buildCatalogPresentation } from "../catalogPresentation";
import { CategoryFilterRail } from "../CategoryFilterRail";
import { GenreCategoryGallery } from "../GenreCategoryGallery";
import { AvailabilityBadge, RecommendationReason } from "../RecommendationMeta";
import { RecommendationFeedback, RecommendationFeedbackProvider, useRecommendationFeedback } from "../RecommendationFeedback";
import { ROTATION_INTERVAL, useRotatingFeature } from "../useRotatingFeature";
import { useRecommendationExposure } from "../useRecommendationExposure";
import { EmberDownloads } from "./EmberDownloads";

function EmberStatePanel({ code, title, body, loading = false, action }: { code: string; title: string; body: string; loading?: boolean; action?: React.ReactNode }) {
  return <div className="ember-state-panel">{loading && <i aria-hidden="true" />}<p>{code}</p><h2>{title}</h2><span>{body}</span>{action}</div>;
}

function EmberMediaCard({ movie, session, showReason, position, onOpen }: { movie: Movie; session?: PlaybackSession; showReason: boolean; position: number; onOpen: (movie: Movie) => void }) {
  const feedback = useRecommendationFeedback();
  const exposureRef = useRecommendationExposure({ profileId: feedback?.profileId ?? "", movie_id: movie.id, feed_generation: feedback?.feedGeneration ?? "", surface: "ember-card", scope: feedback?.scope ?? "home", category: feedback?.category ?? "recommended", position, enabled: showReason && Boolean(feedback?.feedGeneration) });
  return <div ref={exposureRef} className="ember-media-card-shell"><button type="button" className="ember-media-card" data-availability={mediaAvailability(movie)} onClick={() => onOpen(movie)}><span className="ember-media-card__tilt"><span className="ember-media-card__art"><MediaArtwork src={movie.thumbnailUrl} alt={movie.title} media={movie} className="h-full w-full object-cover" /><i aria-hidden="true" /><AvailabilityBadge movie={movie} variant="ember" /></span><span className="ember-media-card__copy"><strong>{movie.title}</strong><small>{movie.type} / {movie.releaseYear || "year n/a"}</small>{showReason && <RecommendationReason movie={movie} />}</span></span>{session && <ProgressBar className="ember-media-card__progress" progress={completionFraction(session.completionRate)} />}</button>{showReason && feedback && <RecommendationFeedback compact movieId={movie.id} preference={feedback.preferences[movie.id] ?? null} onChange={feedback.onChange} />}</div>;
}

function EmberRail({ label, title, items, sessions, showReasons = false, onOpen }: { label: string; title: string; items: Movie[]; sessions: PlaybackSession[]; showReasons?: boolean; onOpen: (movie: Movie) => void }) {
  const { rail, scroll, direction, canScrollPrevious, canScrollNext } = useAnimatedRail();
  if (!items.length) return null;
  return <motion.section variants={CONTENT_REVEAL} className="ember-rail" data-rail-direction={direction}><header><div><p>{label}</p><h2>{title}</h2></div><span>{String(items.length).padStart(2, "0")} catalog records</span></header><div className="ember-rail__frame"><button className="ember-rail__blade ember-rail__blade--previous" disabled={!canScrollPrevious} onClick={() => scroll(-1)} aria-label={`Scroll ${title} backward`}>‹</button><div ref={rail} className="ember-rail__track">{items.map((movie, position) => <EmberMediaCard key={movie.id} movie={movie} session={sessions.find((session) => session.movieId === movie.id)} showReason={showReasons} position={position} onOpen={onOpen} />)}</div><button className="ember-rail__blade ember-rail__blade--next" disabled={!canScrollNext} onClick={() => scroll(1)} aria-label={`Scroll ${title} forward`}>›</button></div></motion.section>;
}

function EmberGrid({ title, label, items, total, sessions, showReasons, onOpen }: { title: string; label: string; items: Movie[]; total: number; sessions: PlaybackSession[]; showReasons: boolean; onOpen: (movie: Movie) => void }) {
  return <motion.section layout variants={CONTENT_REVEAL} className="ember-category-grid"><header><div><p>{label}</p><h2>{title}</h2></div><span>{String(items.length).padStart(2, "0")} / {String(total).padStart(2, "0")} records</span></header><motion.div layout variants={CONTENT_STAGGER} initial="hidden" animate="shown" className="ember-grid">{items.map((movie, position) => <EmberMediaCard key={movie.id} movie={movie} session={sessions.find((session) => session.movieId === movie.id)} showReason={showReasons} position={position} onOpen={onOpen} />)}</motion.div></motion.section>;
}

function EmberActions({ movie, onDetails, onPlay }: { movie: Movie; onDetails: () => void; onPlay: () => void }) {
  const playable = isAvailableMedia(movie) && isPlayableMovie(movie);
  if (!playable) return <div className="ember-actions"><button className="ember-action ember-action--primary" onClick={onDetails}>View details</button><button className="ember-action" disabled>{mediaAvailability(movie) === "processing" ? "Processing" : "Cached suggestion"}</button></div>;
  return <div className="ember-actions"><button className="ember-action ember-action--primary" onClick={movie.type === "series" ? onDetails : onPlay}>{movie.type === "series" ? "Select episode" : "Initialize playback"}</button><button className="ember-action" onClick={onDetails}>View details</button></div>;
}

function EmberBillboard({ items, context, onDetails, onPlay }: { items: Movie[]; context: "home" | "movies" | "series"; onDetails: (movie: Movie) => void; onPlay: (movie: Movie) => void }) {
  const { reduced: reduceMotion } = useAppMotion();
  const rotationItems = useMemo(() => items.slice(0, 8), [items]);
  const { featured, index, setIndex, setPaused, paused, direction, source } = useRotatingFeature(rotationItems);
  if (!featured) return null;
  const label = context === "home" ? `PERSONALIZED TRANSMISSION / ${featured.type.toUpperCase()}` : context === "movies" ? "PERSONALIZED MOVIE" : "PERSONALIZED SERIES";
  return <section className="ember-billboard" style={{ "--billboard-rotation-duration": `${ROTATION_INTERVAL}ms` } as React.CSSProperties} data-motion-source={source} data-motion-direction={direction} data-rotation-paused={paused} onFocusCapture={(event) => setPaused((event.target as HTMLElement).matches(":focus-visible"))} onBlurCapture={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setPaused(false); }}><AnimatePresence mode="wait" initial={false} custom={direction}><motion.div className="ember-hero" key={featured.id} custom={direction} variants={reduceMotion ? REDUCED_BILLBOARD_MOTION : THEME_MOTION.ember.billboard} initial="initial" animate="animate" exit="exit"><div className="ember-hero__art"><MediaArtwork src={featured.bannerUrl || featured.thumbnailUrl} alt={featured.title} media={featured} className="h-full w-full object-cover" /></div><div className="ember-hero__shade" /><AvailabilityBadge movie={featured} variant="ember" /><div className="ember-hero__copy"><small>{label}</small><RecommendationReason movie={featured} /><h1>{featured.title}</h1><p>{featured.description || "No server description is available for this title."}</p><div className="ember-meta"><span>{featured.releaseYear || "YEAR N/A"}</span>{featured.quality && <span>{featured.quality}</span>}{featured.genres[0] && <span>{featured.genres[0]}</span>}</div><EmberActions movie={featured} onDetails={() => onDetails(featured)} onPlay={() => onPlay(featured)} /></div></motion.div></AnimatePresence>{rotationItems.length > 1 && <div className="ember-billboard__pagination" aria-label="Featured media">{rotationItems.map((movie, itemIndex) => <button key={movie.id} data-active={itemIndex === index} aria-current={itemIndex === index ? "true" : undefined} onClick={() => setIndex(itemIndex)} aria-label={`Show ${movie.title}`} />)}</div>}</section>;
}

function EmberFeedStatus({ controller }: { controller: ThemeApplicationProps["controller"] }) {
  if (!controller.recommendationError && !controller.recommendationRefreshing && !controller.recommendation?.stale) return null;
  if (controller.recommendationError) return <div className="ember-feed-status" data-state="error" role="alert"><span>{controller.recommendationError}</span><button onClick={controller.retryRecommendations}>Retry signal</button></div>;
  return <div className="ember-feed-status" data-state={controller.recommendationRefreshing ? "refreshing" : "stale"} role="status"><i aria-hidden="true" /><span>{controller.recommendationRefreshing ? "Recalculating profile recommendations…" : "Cached recommendation signal active while refresh is pending."}</span></div>;
}

function EmberCatalogView({ query, controller, onOpen, onPlay, onCategory }: { query: AppQueryState; controller: ThemeApplicationProps["controller"]; onOpen: (movie: Movie) => void; onPlay: (movie: Movie) => void; onCategory: (category: string) => void }) {
  const view = query.view as CatalogView;
  const model = useMemo(() => controller.recommendation ? buildCatalogPresentation({ feed: controller.recommendation, fallbackMovies: controller.movies, continueWatching: controller.continueWatching, view }) : null, [controller.continueWatching, controller.movies, controller.recommendation, view]);
  if (!model) return <EmberStatePanel code="RECOMMENDATION SIGNAL LOST" title="Recommendations unavailable" body={controller.recommendationError || "The server returned no personalized catalog feed."} action={<button className="ember-retry" onClick={controller.retryRecommendations}>Retry signal</button>} />;
  const context = view as "home" | "movies" | "series";
  const collectionsClass = view === "home" ? "ember-collections" : "ember-genre-collections";
  const hasResults = model.gridItems.length > 0 || model.sections.length > 0;
  return <div className={`${view === "home" ? "ember-home" : "ember-discovery"} ember-category-discovery`} data-category-mode={model.mode}>
    {model.billboardItems.length > 0 && <EmberBillboard items={model.billboardItems} context={context} onDetails={onOpen} onPlay={onPlay} />}
    <EmberFeedStatus controller={controller} />
    <CategoryFilterRail options={model.categories} active={model.activeCategory} variant="ember" onSelect={onCategory} />
    <GenreCategoryGallery cards={model.genreCards} active={model.activeCategory} variant="ember" onSelect={onCategory} />
    <AnimatedState stateKey={`${model.mode}:${model.activeCategory}`}>
      {model.gridItems.length > 0 ? <EmberGrid title={model.mode === "all" ? "All Releases" : model.activeLabel} label={model.mode === "all" ? "COMPLETE SERVER CATALOG" : "CATEGORY CATALOG"} items={model.gridItems} total={model.total} sessions={controller.sessions} showReasons={model.mode !== "all"} onOpen={onOpen} /> : hasResults ? <motion.div variants={CONTENT_STAGGER} initial="hidden" animate="shown" className={collectionsClass}>{model.sections.map((collection) => <EmberRail key={collection.id} label={collection.label} title={collection.title} items={collection.items} sessions={controller.sessions} showReasons={collection.showReasons} onOpen={onOpen} />)}</motion.div> : <div className="ember-category-discovery__empty"><EmberStatePanel code="NO CATEGORY SIGNAL" title={`No ${model.activeLabel} titles`} body={`No titles match the ${model.activeLabel} category.`} /></div>}
    </AnimatedState>
    {controller.recommendationHasMore && <div className="ember-pagination"><button className="ember-load-more" disabled={controller.recommendationLoadingMore} onClick={() => void controller.loadMoreRecommendations()}>{controller.recommendationLoadingMore ? "Loading records…" : `Load more / ${model.sourceItems.length} of ${model.total}`}</button></div>}
  </div>;
}

function EmberWatchlist({ controller, onOpen }: Pick<ThemeApplicationProps, "controller"> & { onOpen: (movie: Movie) => void }) {
  const movies = controller.watchlistItems.filter((movie) => movie.type === "movie");
  const series = controller.watchlistItems.filter((movie) => movie.type === "series");
  return <motion.section layout className="ember-watchlist"><header className="ember-page-heading"><div><p>SERVER WATCHLIST</p><h1>My List</h1></div><span>{controller.watchlistItems.length} saved records</span></header><AnimatedState stateKey={controller.watchlistItems.length ? "populated" : "empty"}>{controller.watchlistItems.length ? <motion.div variants={CONTENT_STAGGER} initial="hidden" animate="shown" className="ember-collections"><EmberRail label="SAVED MOVIES" title="Movies" items={movies} sessions={controller.sessions} onOpen={onOpen} /><EmberRail label="SAVED SERIES" title="Series" items={series} sessions={controller.sessions} onOpen={onOpen} /></motion.div> : <EmberStatePanel code="WATCHLIST EMPTY" title="Your list is empty" body="Add a movie or series from its details page." />}</AnimatedState></motion.section>;
}

function resultToMovie(result: DiscoverMovie, controller: ThemeApplicationProps["controller"]): Movie | null { return controller.resolveMovie(result.type === "series" ? `tv_${result.tmdbId}` : `m_${result.tmdbId}`); }

function EmberSearch({ query, controller, onOpen, onSearch }: Pick<ThemeApplicationProps, "query" | "controller"> & { onOpen: (movie: Movie) => void; onSearch: (value: string) => void }) {
  const [draft, setDraft] = useState(query.q ?? "");
  const { trackEvent } = useTelemetry();
  useEffect(() => setDraft(query.q ?? ""), [query.q]);
  const submit = (event: React.FormEvent) => { event.preventDefault(); onSearch(draft.trim()); };
  const results = controller.results.map((result) => ({ result, movie: resultToMovie(result, controller) })).filter((item): item is { result: DiscoverMovie; movie: Movie } => Boolean(item.movie));
  const stateKey = controller.searching ? "searching" : !query.q ? "ready" : !results.length ? "empty" : `${query.q}:${results.length}`;
  return <section className="ember-browse ember-search"><header className="ember-page-heading"><div><p>SERVER SEARCH</p><h1>{query.q ? `Results for “${query.q}”` : "Search the catalog"}</h1></div>{query.q && <span>{results.length} catalog records</span>}</header><form className="ember-search__form" onSubmit={submit}><input aria-label="Search server catalog" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="Title, film, or series" /><button type="submit">Search server</button></form><AnimatedState stateKey={stateKey}>{controller.searching ? <EmberStatePanel code="QUERY ACTIVE" title="Searching the server" body={`Looking for “${query.q}”.`} loading /> : !query.q ? <EmberStatePanel code="AWAITING QUERY" title="Search is ready" body="Enter a title to search the server catalog." /> : !results.length ? <EmberStatePanel code="NO MATCH" title="No results" body="The server did not return matches for this query." /> : <motion.div variants={CONTENT_STAGGER} initial="hidden" animate="shown" className="ember-search__results">{results.map(({ result, movie }) => <motion.button layout variants={CONTENT_REVEAL} key={`${result.type}-${result.tmdbId}`} data-availability={mediaAvailability(movie)} onClick={() => { trackEvent({ event_type: "search_result_select", tmdb_id: result.tmdbId }); onOpen(movie); }}><MediaArtwork src={result.thumbnailUrl} alt={result.title} media={movie} className="ember-search__art" /><span><strong>{result.title}</strong><small>{mediaAvailability(movie) === "available" ? "Available in server catalog" : "Cached suggestion / details available"}</small></span></motion.button>)}</motion.div>}</AnimatedState></section>;
}

export function EmberDashboard({ query, controller, presentation }: ThemeApplicationProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const profile = useProfileStore((state) => state.activeProfile)!;
  const clearProfile = useProfileStore((state) => state.clearProfile);
  const logout = useAuthStore((state) => state.logout);
  const { trackEvent } = useTelemetry();
  const Background = presentation.Background;
  const Navigation = presentation.Navigation;
  const appNavigate = (view: AppView, options: Parameters<typeof appUrl>[2] = {}) => navigate(appUrl(profile.id, view, options), { state: { fromApp: true, previous: location.search } });
  const navigateView = (view: AppView) => appNavigate(view, preservedCatalogCategory(query, view));
  const navigateDetails = (movie: Movie) => appNavigate("details", { media: movie.id });
  const openDetails = (movie: Movie) => { trackEvent({ event_type: "card_click", movie_id: movie.id }); navigateDetails(movie); };
  const openWatch = (movie: Movie) => appNavigate("watch", { media: movie.id });
  const closeDetails = () => (location.state as { fromApp?: boolean } | null)?.fromApp ? navigate(-1) : appNavigate("home");
  const selected = query.media ? controller.resolveMovie(query.media) : null;
  const navigationProps = { profile, activeView: query.view, query: query.q, isAdmin: profile.id === "1", onView: navigateView, onSearch: (value: string) => appNavigate("search", value ? { q: value } : {}), onEditProfile: () => navigate(profileEditUrl(profile.id), { state: { returnTo: `${location.pathname}${location.search}${location.hash}` } }), onProfiles: () => { clearProfile(); navigate("/profiles"); }, onAdmin: () => appNavigate("admin", { section: "account" }), onLogout: logout };
  const content = controller.loading ? <EmberStatePanel code="PROFILE FEED HANDSHAKE" title="Loading recommendations" body="Synchronizing this profile with the recommendation engine." loading /> : isCatalogView(query.view) ? <EmberCatalogView query={query} controller={controller} onOpen={openDetails} onPlay={openWatch} onCategory={(category) => appNavigate(query.view, { genre: category })} /> : query.view === "watchlist" ? <EmberWatchlist controller={controller} onOpen={openDetails} /> : query.view === "search" ? <EmberSearch query={query} controller={controller} onOpen={navigateDetails} onSearch={(value) => appNavigate("search", value ? { q: value } : {})} /> : query.view === "downloads" ? <EmberDownloads /> : query.view === "details" ? selected ? <DetailsRouter movie={selected} onClose={closeDetails} isWatchlisted={controller.watchlist.includes(selected.id)} onWatchlistChange={controller.setWatchlist} onRecommendationInvalidated={controller.refreshRecommendations} /> : controller.detailsLoading ? <EmberStatePanel code="MEDIA LOOKUP" title="Loading media details" body="Resolving the canonical server record." loading /> : controller.detailsError ? <EmberStatePanel code="MEDIA LOOKUP FAILED" title="Details unavailable" body={controller.detailsError} action={<button className="ember-retry" onClick={controller.retryDetails}>Retry lookup</button>} /> : <EmberStatePanel code="INVALID MEDIA ID" title="Title not found" body="That media identifier is not present in the server catalog." /> : null;
  const feedback = { profileId: profile.id, preferences: controller.preferences, onChange: controller.updatePreference, feedGeneration: controller.recommendation ? String(controller.recommendation.generatedAt) : "", scope: isCatalogView(query.view) ? query.view : "details", category: query.genre ?? "recommended" };
  return <RecommendationFeedbackProvider value={feedback}><div className="theme-app theme-app--ember ember-app" data-theme="ember" data-interaction={presentation.interaction.id} data-view={query.view}><Background /><Navigation {...navigationProps} /><main className="theme-main ember-main">{controller.error && <div className="ember-error" role="alert">{controller.error}</div>}<AnimatedView theme="ember" viewKey={appViewMotionKey(query)}>{content}</AnimatedView></main></div></RecommendationFeedbackProvider>;
}
