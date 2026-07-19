import React, { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useLocation, useNavigate } from "react-router-dom";
import { MediaArtwork } from "../../components/media/MediaArtwork";
import { ProgressBar } from "../../components/ui/ProgressBar";
import { useTelemetry } from "../../hooks/useTelemetry";
import { AnimatedState, AnimatedView, CONTENT_REVEAL, CONTENT_STAGGER, REDUCED_BILLBOARD_MOTION, THEME_MOTION, useAppMotion } from "../../motion/motionSystem";
import { useAnimatedRail } from "../../motion/useAnimatedRail";
import { appUrl, isCatalogView, preservedCatalogCategory, type AppQueryState, type AppView, type CatalogView } from "../../navigation/queryState";
import { profileEditUrl } from "../../navigation/profileEditing";
import { useAuthStore } from "../../stores/authStore";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import type { ThemeApplicationProps } from "../../themes/application/contracts";
import type { DiscoverMovie, Movie, PlaybackSession } from "../../types/api";
import { completionFraction, isAvailableMedia, isPlayableMovie, mediaAvailability } from "../../utils/media";
import { DetailsRouter } from "../details/DetailsRouter";
import { buildCatalogPresentation } from "./catalogPresentation";
import { CategoryFilterRail } from "./CategoryFilterRail";
import { GenreCategoryGallery } from "./GenreCategoryGallery";
import { AvailabilityBadge, RecommendationReason } from "./RecommendationMeta";
import { RecommendationFeedback, RecommendationFeedbackProvider, useRecommendationFeedback } from "./RecommendationFeedback";
import { ServerDownloads } from "./ServerDownloads";
import type { CatalogController } from "./useCatalogController";
import { useRecommendationExposure } from "./useRecommendationExposure";
import { ROTATION_INTERVAL, useRotatingFeature } from "./useRotatingFeature";

function MediaCard({ movie, session, theme, showReason, position, onOpen }: { movie: Movie; session?: PlaybackSession; theme: string; showReason: boolean; position: number; onOpen: (movie: Movie) => void }) {
  const feedback = useRecommendationFeedback();
  const exposureRef = useRecommendationExposure({ profileId: feedback?.profileId ?? "", movie_id: movie.id, feed_generation: feedback?.feedGeneration ?? "", surface: "catalog-card", scope: feedback?.scope ?? "home", category: feedback?.category ?? "recommended", position, enabled: showReason && Boolean(feedback?.feedGeneration) });
  return <motion.div layout="position" ref={exposureRef} className="catalog-card-shell" data-card-theme={theme}><button type="button" className="catalog-card" data-card-theme={theme} data-availability={mediaAvailability(movie)} onClick={() => onOpen(movie)}>
    <span className="catalog-card__art"><MediaArtwork src={movie.thumbnailUrl} alt={movie.title} media={movie} className="h-full w-full object-cover" /><i aria-hidden="true" /><AvailabilityBadge movie={movie} /></span>
    <span className="catalog-card__copy"><strong>{movie.title}</strong><small>{movie.type} / {movie.releaseYear || "catalogued"}</small>{showReason && <RecommendationReason movie={movie} />}</span>
    {session && <ProgressBar className="catalog-card__progress" progress={completionFraction(session.completionRate)} />}
  </button>{showReason && feedback && <RecommendationFeedback compact movieId={movie.id} preference={feedback.preferences[movie.id] ?? null} onChange={feedback.onChange} />}</motion.div>;
}

function MediaCollection({ title, label, items, sessions, theme, showReasons = false, onOpen }: { title: string; label?: string; items: Movie[]; sessions: PlaybackSession[]; theme: string; showReasons?: boolean; onOpen: (movie: Movie) => void }) {
  const { rail, scroll, direction, canScrollPrevious, canScrollNext } = useAnimatedRail();
  if (!items.length) return null;
  return <motion.section variants={CONTENT_REVEAL} className="catalog-collection" data-rail-direction={direction}><header><div>{label && <p>{label}</p>}<h2>{title}</h2></div><span>{String(items.length).padStart(2, "0")} titles</span></header><div className="catalog-collection__frame"><button className="catalog-rail-blade catalog-rail-blade--previous" disabled={!canScrollPrevious} onClick={() => scroll(-1)} aria-label={`Scroll ${title} backward`}>‹</button><div ref={rail} className="catalog-collection__rail">{items.map((movie, position) => <MediaCard key={movie.id} movie={movie} session={sessions.find((session) => session.movieId === movie.id)} theme={theme} showReason={showReasons} position={position} onOpen={onOpen} />)}</div><button className="catalog-rail-blade catalog-rail-blade--next" disabled={!canScrollNext} onClick={() => scroll(1)} aria-label={`Scroll ${title} forward`}>›</button></div></motion.section>;
}

function FeatureActions({ movie, onDetails, onPlay }: { movie: Movie; onDetails: () => void; onPlay: () => void }) {
  const playable = isAvailableMedia(movie) && isPlayableMovie(movie);
  if (!playable) return <div className="feature-actions"><button className="feature-action feature-action--primary" onClick={onDetails}>View details</button><button className="feature-action" disabled>{mediaAvailability(movie) === "processing" ? "Processing" : "Cached suggestion"}</button></div>;
  return <div className="feature-actions"><button className="feature-action feature-action--primary" onClick={movie.type === "series" ? onDetails : onPlay}>{movie.type === "series" ? "Select episode" : "Play now"}</button><button className="feature-action" onClick={onDetails}>View details</button></div>;
}

function FeaturedStage({ movie, variant, context, onDetails, onPlay }: { movie: Movie; variant: string; context: "home" | "movies" | "series"; onDetails: () => void; onPlay: () => void }) {
  const art = <MediaArtwork src={movie.bannerUrl || movie.thumbnailUrl} alt={movie.title} media={movie} className="h-full w-full object-cover" />;
  const contextLabel = context === "home" ? "PROFILE LIBRARY" : context === "movies" ? "FEATURED MOVIE" : "FEATURED SERIES";
  const meta = <RecommendationReason movie={movie} />;
  if (variant === "terminal") return <section className="feature-stage feature-stage--terminal"><div className="terminal-intro"><span><i />CATALOG ONLINE</span><p>{contextLabel} / PROFILE FEED</p></div><div className="terminal-feature"><div className="terminal-feature__copy"><small>{contextLabel} / {movie.type.toUpperCase()}</small>{meta}<h1>{movie.title}</h1><p>{movie.description || "No server description is available for this title."}</p><div className="feature-meta"><span>{movie.releaseYear || "YEAR N/A"}</span><span>{movie.quality || "SOURCE"}</span><span>{movie.genres[0] || "UNCATEGORIZED"}</span></div><FeatureActions movie={movie} onDetails={onDetails} onPlay={onPlay} /></div><div className="terminal-feature__art">{art}<AvailabilityBadge movie={movie} /></div></div></section>;
  if (variant === "editorial") return <section className="feature-stage feature-stage--editorial"><div className="editorial-art">{art}<AvailabilityBadge movie={movie} /></div><div className="editorial-copy"><small>{contextLabel}</small>{meta}<h1>{movie.title}</h1><p>{movie.description || "No server description is available for this title."}</p><FeatureActions movie={movie} onDetails={onDetails} onPlay={onPlay} /></div></section>;
  if (variant === "cinematic") return <section className="feature-stage feature-stage--cinematic"><div className="cinema-art">{art}</div><div className="cinema-shade" /><AvailabilityBadge movie={movie} /><div className="cinema-copy"><small>{contextLabel} / {movie.releaseYear || "catalog"}</small>{meta}<h1>{movie.title}</h1><p>{movie.description || "No server description is available for this title."}</p><FeatureActions movie={movie} onDetails={onDetails} onPlay={onPlay} /></div></section>;
  return <section className="feature-stage feature-stage--workspace"><div className="workspace-heading"><p>{contextLabel}</p>{meta}<h1>{movie.title}</h1><span>{movie.description || "No server description is available for this title."}</span><FeatureActions movie={movie} onDetails={onDetails} onPlay={onPlay} /></div><div className="workspace-art">{art}<AvailabilityBadge movie={movie} /></div><div className="workspace-stat"><small>CATALOG STATE</small><strong>{mediaAvailability(movie).toUpperCase()}</strong><span>{movie.languages.length} audio track{movie.languages.length === 1 ? "" : "s"}</span></div></section>;
}

function RotatingBillboard({ items, variant, context, onDetails, onPlay }: { items: Movie[]; variant: string; context: "home" | "movies" | "series"; onDetails: (movie: Movie) => void; onPlay: (movie: Movie) => void }) {
  const { reduced: reduceMotion } = useAppMotion();
  const rotationItems = useMemo(() => items.slice(0, 8), [items]);
  const activeTheme = useThemeStore((state) => state.activeTheme);
  const { featured, index, setIndex, setPaused, paused, direction, source } = useRotatingFeature(rotationItems);
  if (!featured) return null;
  return <div className="billboard-rotator" style={{ "--billboard-rotation-duration": `${ROTATION_INTERVAL}ms` } as React.CSSProperties} data-motion-source={source} data-motion-direction={direction} data-rotation-paused={paused} onFocusCapture={(event) => setPaused((event.target as HTMLElement).matches(":focus-visible"))} onBlurCapture={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setPaused(false); }}><AnimatePresence mode="wait" initial={false} custom={direction}><motion.div key={featured.id} custom={direction} variants={reduceMotion ? REDUCED_BILLBOARD_MOTION : THEME_MOTION[activeTheme].billboard} initial="initial" animate="animate" exit="exit"><FeaturedStage movie={featured} variant={variant} context={context} onDetails={() => onDetails(featured)} onPlay={() => onPlay(featured)} /></motion.div></AnimatePresence>{rotationItems.length > 1 && <div className="billboard-pagination" aria-label="Featured media">{rotationItems.map((movie, itemIndex) => <button key={movie.id} data-active={itemIndex === index} aria-current={itemIndex === index ? "true" : undefined} onClick={() => setIndex(itemIndex)} aria-label={`Show ${movie.title}`} />)}</div>}</div>;
}

function CatalogGrid({ title, label, items, total, sessions, theme, showReasons, onOpen }: { title: string; label: string; items: Movie[]; total: number; sessions: PlaybackSession[]; theme: string; showReasons: boolean; onOpen: (movie: Movie) => void }) {
  return <motion.section layout variants={CONTENT_REVEAL} className="category-catalog-grid"><header><div><p>{label}</p><h2>{title}</h2></div><span>{items.length} of {total} titles</span></header><motion.div layout variants={CONTENT_STAGGER} initial="hidden" animate="shown" className="browse-grid">{items.map((movie, position) => <MediaCard key={movie.id} movie={movie} session={sessions.find((session) => session.movieId === movie.id)} theme={theme} showReason={showReasons} position={position} onOpen={onOpen} />)}</motion.div></motion.section>;
}

function FeedStatus({ controller }: { controller: CatalogController }) {
  if (!controller.recommendationError && !controller.recommendationRefreshing && !controller.recommendation?.stale) return null;
  if (controller.recommendationError) return <div className="catalog-feed-status" data-state="error" role="alert"><span>{controller.recommendationError}</span><button onClick={controller.retryRecommendations}>Retry recommendations</button></div>;
  return <div className="catalog-feed-status" data-state={controller.recommendationRefreshing ? "refreshing" : "stale"} role="status"><i aria-hidden="true" /><span>{controller.recommendationRefreshing ? "Refreshing your recommendations…" : "Showing saved recommendations while the server refreshes them."}</span></div>;
}

function CatalogDiscoveryView({ query, controller, theme, variant, onOpen, onPlay, onCategory }: { query: AppQueryState; controller: CatalogController; theme: string; variant: string; onOpen: (movie: Movie) => void; onPlay: (movie: Movie) => void; onCategory: (category: string) => void }) {
  const view = query.view as CatalogView;
  const model = useMemo(() => controller.recommendation ? buildCatalogPresentation({ feed: controller.recommendation, fallbackMovies: controller.movies, continueWatching: controller.continueWatching, view }) : null, [controller.continueWatching, controller.movies, controller.recommendation, view]);
  if (!model) return <div className="catalog-feed-failure"><EmptyState title="Recommendations unavailable" body={controller.recommendationError || "The server did not return a recommendation feed."} /><button className="catalog-retry" onClick={controller.retryRecommendations}>Retry recommendations</button></div>;
  const context = view as "home" | "movies" | "series";
  const collectionsClass = view === "home" ? "home-collections" : "browse-genre-collections";
  const hasResults = model.gridItems.length > 0 || model.sections.length > 0;
  const emptyTitle = !model.sourceItems.length ? `No ${view === "home" ? "recommendations" : view} found` : `No ${model.activeLabel} titles`;
  return <div className={`${view === "home" ? "home-view" : "browse-discovery"} category-discovery`} data-category-mode={model.mode}>
    {model.billboardItems.length > 0 && <RotatingBillboard items={model.billboardItems} variant={variant} context={context} onDetails={onOpen} onPlay={onPlay} />}
    <FeedStatus controller={controller} />
    <CategoryFilterRail options={model.categories} active={model.activeCategory} variant="shared" onSelect={onCategory} />
    <GenreCategoryGallery cards={model.genreCards} active={model.activeCategory} variant="shared" onSelect={onCategory} />
    <AnimatedState stateKey={`${model.mode}:${model.activeCategory}`}>
      {model.gridItems.length > 0 ? <CatalogGrid title={model.mode === "all" ? "All Releases" : model.activeLabel} label={model.mode === "all" ? "COMPLETE SERVER CATALOG" : "CATEGORY CATALOG"} items={model.gridItems} total={model.total} sessions={controller.sessions} theme={theme} showReasons={model.mode !== "all"} onOpen={onOpen} /> : hasResults ? <motion.div variants={CONTENT_STAGGER} initial="hidden" animate="shown" className={collectionsClass}>{model.sections.map((collection) => <MediaCollection key={collection.id} label={collection.label} title={collection.title} items={collection.items} sessions={controller.sessions} theme={theme} showReasons={collection.showReasons} onOpen={onOpen} />)}</motion.div> : <div className="category-discovery__empty"><EmptyState title={emptyTitle} body={`No titles match the ${model.activeLabel} category.`} /></div>}
    </AnimatedState>
    {controller.recommendationHasMore && <div className="catalog-pagination"><button className="catalog-load-more" disabled={controller.recommendationLoadingMore} onClick={() => void controller.loadMoreRecommendations()}>{controller.recommendationLoadingMore ? "Loading more…" : `Load more (${model.sourceItems.length} of ${model.total})`}</button></div>}
  </div>;
}

function WatchlistView({ controller, theme, onOpen }: { controller: CatalogController; theme: string; onOpen: (movie: Movie) => void }) {
  const movies = controller.watchlistItems.filter((movie) => movie.type === "movie");
  const series = controller.watchlistItems.filter((movie) => movie.type === "series");
  return <motion.section layout className="watchlist-view"><header className="watchlist-heading"><p>SERVER WATCHLIST</p><h1>My List</h1><span>{controller.watchlistItems.length} saved title{controller.watchlistItems.length === 1 ? "" : "s"}</span></header><AnimatedState stateKey={controller.watchlistItems.length ? "populated" : "empty"}>{controller.watchlistItems.length ? <motion.div variants={CONTENT_STAGGER} initial="hidden" animate="shown" className="watchlist-collections"><MediaCollection label="SAVED MOVIES" title="Movies" items={movies} sessions={controller.sessions} theme={theme} onOpen={onOpen} /><MediaCollection label="SAVED SERIES" title="Series" items={series} sessions={controller.sessions} theme={theme} onOpen={onOpen} /></motion.div> : <EmptyState title="Your list is empty" body="Add a movie or series from its details page." />}</AnimatedState></motion.section>;
}

function SearchView({ query, controller, theme, onOpen, onSearch }: { query: AppQueryState; controller: CatalogController; theme: string; onOpen: (movie: Movie) => void; onSearch: (query: string) => void }) {
  const [draft, setDraft] = useState(query.q ?? "");
  const { trackEvent } = useTelemetry();
  useEffect(() => setDraft(query.q ?? ""), [query.q]);
  const form = <form className="search-page-form" onSubmit={(event) => { event.preventDefault(); onSearch(draft.trim()); }}><input aria-label="Search server catalog" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="Title, film, or series" /><button type="submit">Search server</button></form>;
  if (!query.q) return <section className="browse-view search-view"><header className="browse-heading"><div><p>SERVER SEARCH</p><h1>Search the catalog</h1></div></header>{form}<EmptyState title="Awaiting a query" body="Search results are requested directly from the server." /></section>;
  if (controller.searching) return <AnimatedState stateKey="searching"><LoadingState label={`Searching for “${query.q}”`} /></AnimatedState>;
  const normalized = controller.results.map((result) => ({ result, movie: resultToMovie(result, controller) })).filter((item): item is { result: DiscoverMovie; movie: Movie } => Boolean(item.movie));
  return <section className="browse-view search-view"><header className="browse-heading"><div><p>SERVER SEARCH</p><h1>Results for “{query.q}”</h1></div><span>{normalized.length} catalog matches</span></header>{form}<AnimatedState stateKey={`${query.q}:${normalized.length}`}>{!normalized.length ? <EmptyState title="No results" body="The server did not return matches for this query." /> : <motion.div layout variants={CONTENT_STAGGER} initial="hidden" animate="shown" className="search-results">{normalized.map(({ result, movie }) => <motion.button layout variants={CONTENT_REVEAL} key={`${result.type}-${result.tmdbId}`} className="search-result" data-card-theme={theme} data-availability={mediaAvailability(movie)} onClick={() => { trackEvent({ event_type: "search_result_select", tmdb_id: result.tmdbId }); onOpen(movie); }}><MediaArtwork src={result.thumbnailUrl} alt={result.title} media={movie} className="search-result__art" /><span><strong>{result.title}</strong><small>{mediaAvailability(movie) === "available" ? "Available on server" : "Cached suggestion"}</small></span></motion.button>)}</motion.div>}</AnimatedState></section>;
}

function resultToMovie(result: DiscoverMovie, controller: CatalogController) { return controller.resolveMovie(result.type === "series" ? `tv_${result.tmdbId}` : `m_${result.tmdbId}`); }
function LoadingState({ label = "Loading recommendations" }: { label?: string }) { return <div className="catalog-state catalog-state--loading"><i /><p>{label}</p></div>; }
function EmptyState({ title, body, action }: { title: string; body: string; action?: React.ReactNode }) { return <div className="catalog-state"><p>NO DATA</p><h2>{title}</h2><span>{body}</span>{action}</div>; }

export function appViewMotionKey(query: AppQueryState): string { return query.view === "details" ? `${query.view}:${query.media ?? "missing"}` : query.view; }

export function LegacyThemeAdapter({ query, controller, presentation: definition }: ThemeApplicationProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const profile = useProfileStore((state) => state.activeProfile)!;
  const theme = useThemeStore((state) => state.activeTheme);
  const clearProfile = useProfileStore((state) => state.clearProfile);
  const logout = useAuthStore((state) => state.logout);
  const { trackEvent } = useTelemetry();
  const Background = definition.Background;
  const Navigation = definition.Navigation;
  const appNavigate = (view: AppView, options: Parameters<typeof appUrl>[2] = {}) => navigate(appUrl(profile.id, view, options), { state: { fromApp: true, previous: location.search } });
  const navigateView = (view: AppView) => appNavigate(view, preservedCatalogCategory(query, view));
  const navigateDetails = (movie: Movie) => appNavigate("details", { media: movie.id });
  const openDetails = (movie: Movie) => { trackEvent({ event_type: "card_click", movie_id: movie.id }); navigateDetails(movie); };
  const openWatch = (movie: Movie) => appNavigate("watch", { media: movie.id });
  const closeDetails = () => (location.state as { fromApp?: boolean } | null)?.fromApp ? navigate(-1) : appNavigate("home");
  const selected = query.media ? controller.resolveMovie(query.media) : null;
  const navigationProps = { profile, activeView: query.view, query: query.q, isAdmin: profile.id === "1", onView: navigateView, onSearch: (q: string) => appNavigate("search", q ? { q } : {}), onEditProfile: () => navigate(profileEditUrl(profile.id), { state: { returnTo: `${location.pathname}${location.search}${location.hash}` } }), onProfiles: () => { clearProfile(); navigate("/profiles"); }, onAdmin: () => appNavigate("admin", { section: "account" }), onLogout: logout };
  const content = controller.loading ? <LoadingState /> : isCatalogView(query.view) ? <CatalogDiscoveryView query={query} controller={controller} theme={definition.cardVariant} variant={definition.heroVariant} onOpen={openDetails} onPlay={openWatch} onCategory={(category) => appNavigate(query.view, { genre: category })} /> : query.view === "watchlist" ? <WatchlistView controller={controller} theme={definition.cardVariant} onOpen={openDetails} /> : query.view === "search" ? <SearchView query={query} controller={controller} theme={definition.cardVariant} onOpen={navigateDetails} onSearch={(q) => appNavigate("search", q ? { q } : {})} /> : query.view === "downloads" ? <ServerDownloads /> : query.view === "details" ? selected ? <DetailsRouter movie={selected} onClose={closeDetails} isWatchlisted={controller.watchlist.includes(selected.id)} onWatchlistChange={controller.setWatchlist} onRecommendationInvalidated={controller.refreshRecommendations} /> : controller.detailsLoading ? <LoadingState label="Loading media details" /> : controller.detailsError ? <EmptyState title="Details unavailable" body={controller.detailsError} action={<button className="catalog-retry" onClick={controller.retryDetails}>Retry details</button>} /> : <EmptyState title="Title not found" body="That media identifier is not present in the server catalog." /> : null;
  const feedback = { profileId: profile.id, preferences: controller.preferences, onChange: controller.updatePreference, feedGeneration: controller.recommendation ? String(controller.recommendation.generatedAt) : "", scope: isCatalogView(query.view) ? query.view : "details", category: query.genre ?? "recommended" };
  return <RecommendationFeedbackProvider value={feedback}><div className={`theme-app ${definition.shellClass}`} data-theme={theme} data-interaction={definition.interaction.id} data-view={query.view}><Background /><Navigation {...navigationProps} /><main className="theme-main">{controller.error && <div className="catalog-error" role="alert">{controller.error}</div>}<AnimatedView theme={theme} viewKey={appViewMotionKey(query)}>{content}</AnimatedView></main></div></RecommendationFeedbackProvider>;
}
