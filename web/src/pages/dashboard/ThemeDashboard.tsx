import React, { useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { useLocation, useNavigate } from "react-router-dom";
import { MediaActionIcon } from "../../components/media/MediaActionIcon";
import { MediaArtwork } from "../../components/media/MediaArtwork";
import { ProgressBar } from "../../components/ui/ProgressBar";
import { useTelemetry } from "../../hooks/useTelemetry";
import { AnimatedState, AnimatedView, CONTENT_REVEAL, CONTENT_STAGGER } from "../../motion/motionSystem";
import { useAnimatedRail } from "../../motion/useAnimatedRail";
import { appUrl, isCatalogView, preservedCatalogCategory, type AppQueryState, type AppView, type CatalogView } from "../../navigation/queryState";
import { profileEditUrl } from "../../navigation/profileEditing";
import { useAuthStore } from "../../stores/authStore";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import type { ThemeApplicationProps } from "../../themes/application/contracts";
import { CatalogLayout } from "../../themes/application/CatalogLayout";
import type { DiscoverMovie, Movie, PlaybackSession } from "../../types/api";
import { formatDuration } from "../../utils/format";
import { completionFraction, isAvailableMedia, isPlayableMovie, mediaAvailability } from "../../utils/media";
import { DetailsRouter } from "../details/DetailsRouter";
import { ENABLE_VISUAL_GENRE_CATEGORIES } from "./catalogFeatures";
import { CategoryFilterRail } from "./CategoryFilterRail";
import { GenreCategoryGallery } from "./GenreCategoryGallery";
import { AvailabilityBadge, RecommendationReason } from "./RecommendationMeta";
import { RecommendationFeedbackProvider, useRecommendationFeedback } from "./RecommendationFeedback";
import { ServerDownloads } from "./ServerDownloads";
import type { CatalogController } from "./useCatalogController";
import { useRecommendationExposure } from "./useRecommendationExposure";
import { BillboardStack } from "./BillboardStack";
import { useCatalogLanding } from "./useCatalogLanding";
import { CatalogLandingNotice, CatalogLandingState } from "./CatalogLandingState";

function MediaCard({ movie, session, theme, showReason, position, onOpen, onResume }: { movie: Movie; session?: PlaybackSession; theme: string; showReason: boolean; position: number; onOpen: (movie: Movie) => void; onResume?: (movie: Movie, session: PlaybackSession) => void }) {
  const feedback = useRecommendationFeedback();
  const exposureRef = useRecommendationExposure({ profileId: feedback?.profileId ?? "", movie_id: movie.id, feed_generation: feedback?.feedGeneration ?? "", surface: "catalog-card", scope: feedback?.scope ?? "home", category: feedback?.category ?? "recommended", position, enabled: showReason && Boolean(feedback?.feedGeneration) });
  return <motion.div ref={exposureRef} className="catalog-card-shell" data-card-theme={theme} data-resumable={Boolean(session && onResume) || undefined}><button type="button" className="catalog-card" data-card-theme={theme} data-availability={mediaAvailability(movie)} onClick={() => onOpen(movie)}>
    <span className="catalog-card__art"><MediaArtwork src={movie.thumbnailUrl} alt={movie.title} media={movie} className="h-full w-full object-cover" /><i aria-hidden="true" /><AvailabilityBadge movie={movie} /></span>
    <span className="catalog-card__copy"><strong>{movie.title}</strong><small>{movie.type} / {movie.releaseYear || "catalogued"}</small>{showReason && <RecommendationReason movie={movie} />}</span>
    {session && <ProgressBar className="catalog-card__progress" progress={completionFraction(session.completionRate)} />}
  </button>{session && onResume && <button type="button" className="catalog-card__resume" aria-label={`Resume ${movie.title} from ${formatDuration(session.timestamp)}`} data-tooltip={`Resume from ${formatDuration(session.timestamp)}`} onClick={() => onResume(movie, session)}><MediaActionIcon name="resume" /></button>}</motion.div>;
}

function MediaCollection({ title, label, items, sessions, theme, kind = "standard", showReasons = false, onOpen, onResume }: { title: string; label?: string; items: Movie[]; sessions: PlaybackSession[]; theme: string; kind?: "standard" | "vibe"; showReasons?: boolean; onOpen: (movie: Movie) => void; onResume?: (movie: Movie, session: PlaybackSession) => void }) {
  const { rail, scroll, direction, proximity, proximityHandlers, canScrollPrevious, canScrollNext } = useAnimatedRail();
  if (!items.length) return null;
  return <motion.section variants={CONTENT_REVEAL} className="catalog-collection" data-section-kind={kind} data-rail-direction={direction}><header><div>{label && <p>{label}</p>}<h2>{title}</h2></div><span>{String(items.length).padStart(2, "0")} titles</span></header><div className="catalog-collection__frame" data-edge-proximity={proximity} {...proximityHandlers}><button className="catalog-rail-blade catalog-rail-blade--previous" disabled={!canScrollPrevious} onClick={() => scroll(-1)} aria-label={`Scroll ${title} backward`}>‹</button><div ref={rail} className="catalog-collection__rail">{items.map((movie, position) => <MediaCard key={movie.id} movie={movie} session={sessions.find((session) => session.movieId === movie.id)} theme={theme} showReason={showReasons} position={position} onOpen={onOpen} onResume={onResume} />)}</div><button className="catalog-rail-blade catalog-rail-blade--next" disabled={!canScrollNext} onClick={() => scroll(1)} aria-label={`Scroll ${title} forward`}>›</button></div></motion.section>;
}

function FeatureActions({ movie, onDetails, onPlay, measurement = false }: { movie: Movie; onDetails: () => void; onPlay: () => void; measurement?: boolean }) {
  const playable = isAvailableMedia(movie) && isPlayableMovie(movie);
  if (!playable) return <div className="feature-actions"><button className="feature-action feature-action--primary" disabled={measurement} onClick={measurement ? undefined : onDetails}>View details</button><button className="feature-action" disabled>{mediaAvailability(movie) === "processing" ? "Processing" : "Cached suggestion"}</button></div>;
  return <div className="feature-actions"><button className="feature-action feature-action--primary" disabled={measurement} onClick={measurement ? undefined : movie.type === "series" ? onDetails : onPlay}>{movie.type === "series" ? "Select episode" : mediaAvailability(movie) === "processing" ? "Play while downloading" : "Play now"}</button><button className="feature-action" disabled={measurement} onClick={measurement ? undefined : onDetails}>View details</button></div>;
}

function FeaturedStage({ movie, variant, context, onDetails, onPlay, measurement = false, personalized = true }: { personalized?: boolean; movie: Movie; variant: string; context: "home" | "movies" | "series"; onDetails: () => void; onPlay: () => void; measurement?: boolean }) {
  const art = measurement ? null : <MediaArtwork src={movie.bannerUrl || movie.thumbnailUrl} alt={movie.title} media={movie} className="h-full w-full object-cover" />;
  const contextLabel = context === "home" ? "PROFILE LIBRARY" : context === "movies" ? "FEATURED MOVIE" : "FEATURED SERIES";
  const meta = personalized ? <RecommendationReason movie={movie} /> : null;
  if (variant === "terminal") return <section className="feature-stage feature-stage--terminal"><div className="terminal-intro"><span><i />CATALOG ONLINE</span><p>{contextLabel} / PROFILE FEED</p></div><div className="terminal-feature"><div className="terminal-feature__copy"><small>{contextLabel} / {movie.type.toUpperCase()}</small>{meta}<h1>{movie.title}</h1><p>{movie.description || "No server description is available for this title."}</p><div className="feature-meta"><span>{movie.releaseYear || "YEAR N/A"}</span><span>{movie.quality || "SOURCE"}</span><span>{movie.genres[0] || "UNCATEGORIZED"}</span></div><FeatureActions measurement={measurement} movie={movie} onDetails={onDetails} onPlay={onPlay} /></div><div className="terminal-feature__art">{art}<AvailabilityBadge movie={movie} /></div></div></section>;
  if (variant === "editorial") return <section className="feature-stage feature-stage--editorial"><div className="editorial-art">{art}<AvailabilityBadge movie={movie} /></div><div className="editorial-copy"><small>{contextLabel}</small>{meta}<h1>{movie.title}</h1><p>{movie.description || "No server description is available for this title."}</p><FeatureActions measurement={measurement} movie={movie} onDetails={onDetails} onPlay={onPlay} /></div></section>;
  return <section className="feature-stage feature-stage--cinematic"><div className="cinema-art">{art}</div><div className="cinema-shade" /><AvailabilityBadge movie={movie} /><div className="cinema-copy"><small>{contextLabel} / {movie.releaseYear || "catalog"}</small>{meta}<h1>{movie.title}</h1><p>{movie.description || "No server description is available for this title."}</p><FeatureActions measurement={measurement} movie={movie} onDetails={onDetails} onPlay={onPlay} /></div></section>;
}

function RotatingBillboard({ items, variant, context, onDetails, onPlay, personalized = true }: { personalized?: boolean; items: Movie[]; variant: string; context: "home" | "movies" | "series"; onDetails: (movie: Movie) => void; onPlay: (movie: Movie) => void }) {
  const rotationItems = useMemo(() => items.slice(0, 8), [items]);
  const activeTheme = useThemeStore((state) => state.activeTheme);
  return <BillboardStack items={rotationItems} theme={activeTheme} className="billboard-rotator" paginationClassName="billboard-pagination" render={(movie, measurement) => <FeaturedStage movie={movie} personalized={personalized} variant={variant} context={context} measurement={measurement} onDetails={() => onDetails(movie)} onPlay={() => onPlay(movie)} />} />;
}

function CatalogGrid({ title, label, items, total, sessions, theme, showReasons, onOpen }: { title: string; label: string; items: Movie[]; total: number; sessions: PlaybackSession[]; theme: string; showReasons: boolean; onOpen: (movie: Movie) => void }) {
  return <motion.section variants={CONTENT_REVEAL} className="category-catalog-grid"><header><div><p>{label}</p><h2>{title}</h2></div><span>{items.length} of {total} titles</span></header><motion.div variants={CONTENT_STAGGER} initial="hidden" animate="shown" className="browse-grid">{items.map((movie, position) => <MediaCard key={movie.id} movie={movie} session={sessions.find((session) => session.movieId === movie.id)} theme={theme} showReason={showReasons} position={position} onOpen={onOpen} />)}</motion.div></motion.section>;
}

type CatalogDiscoveryProps = { query: AppQueryState; controller: CatalogController; theme: string; variant: string; onOpen: (movie: Movie) => void; onPlay: (movie: Movie) => void; onResume: (movie: Movie, session: PlaybackSession) => void; onCategory: (category: string) => void };

function CatalogDiscoveryView({ query, controller, theme, variant, onOpen, onPlay, onResume, onCategory }: CatalogDiscoveryProps) {
  const view = query.view as CatalogView;
  const feedback = useRecommendationFeedback();
  const { region, model, hasContent, pending, error } = useCatalogLanding(query, controller);
  const regionProps = { ref: region, tabIndex: -1, "data-catalog-region": true, role: "region", "aria-label": "Discover titles" };
  if (!hasContent) return <RecommendationFeedbackProvider value={feedback ? { ...feedback, feedGeneration: model.feedGeneration } : null}><div {...regionProps} className="category-discovery">{(controller.catalogItems.length > 0 || controller.recommendation) && <CategoryFilterRail preservePageScroll options={model.categories} active={model.activeCategory} variant="shared" onSelect={onCategory} />}<CatalogLandingState pending={pending} error={error} view={view} category={query.genre} controller={controller} />{!pending && model.source === "recommendations" && <CatalogLandingNotice controller={controller} />}</div></RecommendationFeedbackProvider>;
  return <RecommendationFeedbackProvider value={feedback ? { ...feedback, feedGeneration: model.feedGeneration } : null}><div {...regionProps} className={`${view === "home" ? "home-view" : "browse-discovery"} category-discovery`} data-category-mode={model.mode}>
    <RotatingBillboard items={model.billboardItems} personalized={model.source === "recommendations"} variant={variant} context={view} onDetails={onOpen} onPlay={onPlay} />
    <CatalogLandingNotice controller={controller} />
    <CategoryFilterRail preservePageScroll options={model.categories} active={model.activeCategory} variant="shared" onSelect={onCategory} />
    {ENABLE_VISUAL_GENRE_CATEGORIES && <GenreCategoryGallery cards={model.genreCards} active={model.activeCategory} variant="shared" onSelect={onCategory} />}
    <AnimatedState stateKey={`${model.mode}:${model.activeCategory}`}>
      {model.gridItems.length ? <CatalogGrid title={model.mode === "all" ? "All Releases" : model.activeLabel} label={model.mode === "all" ? "COMPLETE SERVER CATALOG" : "CATEGORY CATALOG"} items={model.gridItems} total={model.total} sessions={controller.sessions} theme={theme} showReasons={model.source === "recommendations" && model.mode !== "all"} onOpen={onOpen} /> : <motion.div variants={CONTENT_STAGGER} initial="hidden" animate="shown" className={view === "home" ? "home-collections" : "browse-genre-collections"}>{model.sections.map((collection) => <MediaCollection key={collection.id} label={collection.label} title={collection.title} items={collection.items} sessions={controller.sessions} theme={theme} kind={collection.kind} showReasons={collection.showReasons} onOpen={onOpen} onResume={collection.id === "continue-watching" ? onResume : undefined} />)}</motion.div>}
    </AnimatedState>
    {model.source === "recommendations" && controller.recommendationHasMore && <div className="catalog-pagination"><button className="catalog-load-more" disabled={controller.recommendationLoadingMore} onClick={() => void controller.loadMoreRecommendations()}>{controller.recommendationLoadingMore ? "Loading titles…" : "Load more"}</button></div>}
  </div></RecommendationFeedbackProvider>;
}

function WatchlistView({ controller, theme, onOpen }: { controller: CatalogController; theme: string; onOpen: (movie: Movie) => void }) {
  const movies = controller.watchlistItems.filter((movie) => movie.type === "movie");
  const series = controller.watchlistItems.filter((movie) => movie.type === "series");
  return <motion.section className="watchlist-view"><header className="watchlist-heading"><p>SERVER WATCHLIST</p><h1>My List</h1><span>{controller.watchlistKnown ? `${controller.watchlist.length} saved title${controller.watchlist.length === 1 ? "" : "s"}` : "Saved count unavailable"}</span></header><WatchlistReadStatus controller={controller} /><AnimatedState stateKey={controller.watchlistItems.length ? "populated" : controller.watchlistKnown && !controller.watchlist.length ? "empty" : "unknown"}>{controller.watchlistItems.length ? <motion.div variants={CONTENT_STAGGER} initial="hidden" animate="shown" className="watchlist-collections"><MediaCollection label="SAVED MOVIES" title="Movies" items={movies} sessions={controller.sessions} theme={theme} onOpen={onOpen} /><MediaCollection label="SAVED SERIES" title="Series" items={series} sessions={controller.sessions} theme={theme} onOpen={onOpen} /></motion.div> : controller.watchlistKnown && !controller.watchlist.length ? <EmptyState title="Your list is empty" body="Add a movie or series from its details page." /> : null}</AnimatedState></motion.section>;
}

export function WatchlistReadStatus({ controller }: { controller: CatalogController }) {
  const missing = controller.watchlist.length - controller.watchlistItems.length;
  if (!controller.watchlistKnown) return <div className="catalog-resource-state"><h2>{controller.watchlistStatus === "error" ? "Saved list unavailable" : "Loading your saved list"}</h2><p role={controller.watchlistStatus === "error" ? "alert" : "status"}>{controller.watchlistError || "Requesting your saved titles from the server."}</p>{controller.watchlistStatus === "error" && <button className="catalog-retry" onClick={controller.retryWatchlist}>Retry saved list</button>}</div>;
  if (controller.watchlistStatus === "success" && !missing) return null;
  return <div className="catalog-resource-state"><p role={controller.watchlistStatus === "error" ? "alert" : "status"}>{controller.watchlistStatus === "pending" ? "Refreshing your saved list…" : controller.watchlistStatus === "error" ? `Showing the last saved list. ${controller.watchlistError}` : ""}{missing > 0 && ` Details are unavailable for ${missing} of ${controller.watchlist.length} saved titles.`}</p><button className="catalog-retry" disabled={controller.watchlistStatus === "pending"} onClick={controller.retryWatchlist}>{missing ? "Retry saved list details" : "Retry saved list"}</button></div>;
}

function SearchView({ query, controller, theme, onOpen, onSearch }: { query: AppQueryState; controller: CatalogController; theme: string; onOpen: (movie: Movie) => void; onSearch: (query: string) => void }) {
  const [draft, setDraft] = useState(query.q ?? "");
  const { trackEvent } = useTelemetry();
  useEffect(() => setDraft(query.q ?? ""), [query.q]);
  const form = <form className="search-page-form" onSubmit={(event) => { event.preventDefault(); onSearch(draft.trim()); }}><input aria-label="Search server catalog" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="Title, film, or series" /><button type="submit">Search server</button></form>;
  if (!query.q) return <section className="browse-view search-view"><header className="browse-heading"><div><p>SERVER SEARCH</p><h1>Search the catalog</h1></div></header>{form}<EmptyState title="Awaiting a query" body="Search results are requested directly from the server." /></section>;
  const normalized = controller.results.map((result) => ({ result, movie: resultToMovie(result, controller) })).filter((item): item is { result: DiscoverMovie; movie: Movie } => Boolean(item.movie));
  return <section className="browse-view search-view"><header className="browse-heading"><div><p>SERVER SEARCH</p><h1>Results for “{query.q}”</h1></div>{controller.searchStatus === "success" && <span>{normalized.length} catalog matches</span>}</header>{form}<AnimatedState stateKey={`${query.q}:${controller.searchStatus}:${normalized.length}`}>{controller.searching ? <LoadingState label={`Searching for “${query.q}”`} /> : controller.searchError ? <div className="catalog-resource-state"><h2>Search unavailable</h2><p role="alert">{controller.searchError}</p><button className="catalog-retry" onClick={controller.retrySearch}>Retry this search</button></div> : !normalized.length ? <EmptyState title="No results" body="The server did not return matches for this query." /> : <motion.div layout variants={CONTENT_STAGGER} initial="hidden" animate="shown" className="search-results">{normalized.map(({ result, movie }) => <motion.button layout variants={CONTENT_REVEAL} key={`${result.type}-${result.tmdbId}`} className="search-result" data-card-theme={theme} data-availability={mediaAvailability(movie)} onClick={() => { trackEvent({ event_type: "search_result_select", tmdb_id: result.tmdbId }); onOpen(movie); }}><MediaArtwork src={result.thumbnailUrl} alt={result.title} media={movie} className="search-result__art" /><span><strong>{result.title}</strong><small>{mediaAvailability(movie) === "available" ? "Available on server" : "Cached suggestion"}</small></span></motion.button>)}</motion.div>}</AnimatedState></section>;
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
  const appNavigate = (view: AppView, options: Parameters<typeof appUrl>[2] = {}) => navigate(appUrl(profile.id, view, options), { state: { fromApp: true, previous: location.search } });
  const navigateView = (view: AppView) => appNavigate(view, preservedCatalogCategory(query, view));
  const navigateDetails = (movie: Movie) => appNavigate("details", { media: movie.id });
  const openDetails = (movie: Movie) => { trackEvent({ event_type: "card_click", movie_id: movie.id }); navigateDetails(movie); };
  const openWatch = (movie: Movie) => appNavigate("watch", { media: movie.id });
  const resumeWatch = (movie: Movie, session: PlaybackSession) => appNavigate("watch", { media: movie.type === "series" && session.episodeId ? session.episodeId : movie.id });
  const closeDetails = () => (location.state as { fromApp?: boolean } | null)?.fromApp ? navigate(-1) : appNavigate("home");
  const selected = query.media ? controller.resolveMovie(query.media) : null;
  const navigationProps = { profile, activeView: query.view, query: query.q, isAdmin: profile.id === "1", onView: navigateView, onSearch: (q: string) => appNavigate("search", q ? { q } : {}), onEditProfile: () => navigate(profileEditUrl(profile.id), { state: { returnTo: `${location.pathname}${location.search}${location.hash}` } }), onProfiles: () => { clearProfile(); navigate("/profiles"); }, onAdmin: () => appNavigate("admin", { section: "account" }), onLogout: logout };
  const content = controller.loading && !isCatalogView(query.view) ? <LoadingState /> : isCatalogView(query.view) ? <CatalogDiscoveryView query={query} controller={controller} theme={definition.cardVariant} variant={definition.heroVariant} onOpen={openDetails} onPlay={openWatch} onResume={resumeWatch} onCategory={(category) => appNavigate(query.view, { genre: category })} /> : query.view === "watchlist" ? <WatchlistView controller={controller} theme={definition.cardVariant} onOpen={openDetails} /> : query.view === "search" ? <SearchView query={query} controller={controller} theme={definition.cardVariant} onOpen={navigateDetails} onSearch={(q) => appNavigate("search", q ? { q } : {})} /> : query.view === "downloads" ? <ServerDownloads /> : query.view === "details" ? selected ? <DetailsRouter movie={selected} onClose={closeDetails} isWatchlisted={controller.watchlist.includes(selected.id)} sessions={controller.sessions} onWatchlistChange={controller.setWatchlist} onRecommendationInvalidated={controller.refreshRecommendations} /> : controller.detailsLoading ? <LoadingState label="Loading media details" /> : controller.detailsError ? <EmptyState title="Details unavailable" body={controller.detailsError} action={<button className="catalog-retry" onClick={controller.retryDetails}>Retry details</button>} /> : <EmptyState title="Title not found" body="That media identifier is not present in the server catalog." /> : null;
  const feedback = { profileId: profile.id, preferences: controller.preferences, preferencesKnown: controller.preferencesKnown, preferenceKnownIds: controller.preferenceKnownIds, preferenceOperations: controller.preferenceOperations, onCheck: controller.checkPreference, onChange: controller.updatePreference, feedGeneration: controller.recommendation ? String(controller.recommendation.generatedAt) : "", scope: isCatalogView(query.view) ? query.view : "details", category: query.genre ?? "recommended" };
  return <RecommendationFeedbackProvider value={feedback}><CatalogLayout presentation={definition} navigationProps={navigationProps}>{controller.error && controller.error !== controller.catalogError && <div className="catalog-error" role="alert">{controller.error}</div>}<AnimatedView theme={theme} viewKey={appViewMotionKey(query)}>{content}</AnimatedView></CatalogLayout></RecommendationFeedbackProvider>;
}
