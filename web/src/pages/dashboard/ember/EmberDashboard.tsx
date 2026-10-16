import React, { useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { useLocation, useNavigate } from "react-router-dom";
import { MediaActionIcon } from "../../../components/media/MediaActionIcon";
import { MediaArtwork } from "../../../components/media/MediaArtwork";
import { ProgressBar } from "../../../components/ui/ProgressBar";
import { useTelemetry } from "../../../hooks/useTelemetry";
import { AnimatedState, AnimatedView, CONTENT_REVEAL, CONTENT_STAGGER } from "../../../motion/motionSystem";
import { useAnimatedRail } from "../../../motion/useAnimatedRail";
import { appUrl, isCatalogView, preservedCatalogCategory, type AppQueryState, type AppView, type CatalogView } from "../../../navigation/queryState";
import { profileEditUrl } from "../../../navigation/profileEditing";
import { useAuthStore } from "../../../stores/authStore";
import { useProfileStore } from "../../../stores/profileStore";
import type { ThemeApplicationProps } from "../../../themes/application/contracts";
import { CatalogLayout } from "../../../themes/application/CatalogLayout";
import type { DiscoverMovie, Movie, PlaybackSession } from "../../../types/api";
import { formatDuration } from "../../../utils/format";
import { completionFraction, isAvailableMedia, isPlayableMovie, mediaAvailability } from "../../../utils/media";
import { DetailsRouter } from "../../details/DetailsRouter";
import { appViewMotionKey, WatchlistReadStatus } from "../ThemeDashboard";
import { ENABLE_VISUAL_GENRE_CATEGORIES } from "../catalogFeatures";
import { CategoryFilterRail } from "../CategoryFilterRail";
import { GenreCategoryGallery } from "../GenreCategoryGallery";
import { AvailabilityBadge, RecommendationReason } from "../RecommendationMeta";
import { RecommendationFeedbackProvider, useRecommendationFeedback } from "../RecommendationFeedback";
import { BillboardStack } from "../BillboardStack";
import { useCatalogLanding } from "../useCatalogLanding";
import { CatalogLandingNotice, CatalogLandingState } from "../CatalogLandingState";
import { useRecommendationExposure } from "../useRecommendationExposure";
import { EmberDownloads } from "./EmberDownloads";

function EmberStatePanel({ code, title, body, loading = false, action }: { code: string; title: string; body: string; loading?: boolean; action?: React.ReactNode }) {
  return <div className="ember-state-panel">{loading && <i aria-hidden="true" />}<p>{code}</p><h2>{title}</h2><span>{body}</span>{action}</div>;
}

function EmberMediaCard({ movie, session, showReason, position, onOpen, onResume }: { movie: Movie; session?: PlaybackSession; showReason: boolean; position: number; onOpen: (movie: Movie) => void; onResume?: (movie: Movie, session: PlaybackSession) => void }) {
  const feedback = useRecommendationFeedback();
  const exposureRef = useRecommendationExposure({ profileId: feedback?.profileId ?? "", movie_id: movie.id, feed_generation: feedback?.feedGeneration ?? "", surface: "ember-card", scope: feedback?.scope ?? "home", category: feedback?.category ?? "recommended", position, enabled: showReason && Boolean(feedback?.feedGeneration) });
  return <div ref={exposureRef} className="ember-media-card-shell" data-resumable={Boolean(session && onResume) || undefined}><button type="button" className="ember-media-card" data-availability={mediaAvailability(movie)} onClick={() => onOpen(movie)}><span className="ember-media-card__tilt"><span className="ember-media-card__art"><MediaArtwork src={movie.thumbnailUrl} alt={movie.title} media={movie} className="h-full w-full object-cover" /><i aria-hidden="true" /><AvailabilityBadge movie={movie} variant="ember" /></span><span className="ember-media-card__copy"><strong>{movie.title}</strong><small>{movie.type} / {movie.releaseYear || "year n/a"}</small>{showReason && <RecommendationReason movie={movie} />}</span></span>{session && <ProgressBar className="ember-media-card__progress" progress={completionFraction(session.completionRate)} />}</button>{session && onResume && <button type="button" className="ember-media-card__resume" aria-label={`Resume ${movie.title} from ${formatDuration(session.timestamp)}`} data-tooltip={`Resume from ${formatDuration(session.timestamp)}`} onClick={() => onResume(movie, session)}><MediaActionIcon name="resume" /></button>}</div>;
}

function EmberRail({ label, title, items, sessions, kind = "standard", showReasons = false, onOpen, onResume }: { label: string; title: string; items: Movie[]; sessions: PlaybackSession[]; kind?: "standard" | "vibe"; showReasons?: boolean; onOpen: (movie: Movie) => void; onResume?: (movie: Movie, session: PlaybackSession) => void }) {
  const { rail, scroll, direction, proximity, proximityHandlers, canScrollPrevious, canScrollNext } = useAnimatedRail();
  if (!items.length) return null;
  return <motion.section variants={CONTENT_REVEAL} className="ember-rail" data-section-kind={kind} data-rail-direction={direction}><header><div><p>{label}</p><h2>{title}</h2></div><span>{String(items.length).padStart(2, "0")} catalog records</span></header><div className="ember-rail__frame" data-edge-proximity={proximity} {...proximityHandlers}><button className="ember-rail__blade ember-rail__blade--previous" disabled={!canScrollPrevious} onClick={() => scroll(-1)} aria-label={`Scroll ${title} backward`}>‹</button><div ref={rail} className="ember-rail__track">{items.map((movie, position) => <EmberMediaCard key={movie.id} movie={movie} session={sessions.find((session) => session.movieId === movie.id)} showReason={showReasons} position={position} onOpen={onOpen} onResume={onResume} />)}</div><button className="ember-rail__blade ember-rail__blade--next" disabled={!canScrollNext} onClick={() => scroll(1)} aria-label={`Scroll ${title} forward`}>›</button></div></motion.section>;
}

function EmberGrid({ title, label, items, total, sessions, showReasons, onOpen }: { title: string; label: string; items: Movie[]; total: number; sessions: PlaybackSession[]; showReasons: boolean; onOpen: (movie: Movie) => void }) {
  return <motion.section variants={CONTENT_REVEAL} className="ember-category-grid"><header><div><p>{label}</p><h2>{title}</h2></div><span>{String(items.length).padStart(2, "0")} / {String(total).padStart(2, "0")} records</span></header><motion.div variants={CONTENT_STAGGER} initial="hidden" animate="shown" className="ember-grid">{items.map((movie, position) => <EmberMediaCard key={movie.id} movie={movie} session={sessions.find((session) => session.movieId === movie.id)} showReason={showReasons} position={position} onOpen={onOpen} />)}</motion.div></motion.section>;
}

function EmberActions({ movie, onDetails, onPlay, measurement = false }: { movie: Movie; onDetails: () => void; onPlay: () => void; measurement?: boolean }) {
  const playable = isAvailableMedia(movie) && isPlayableMovie(movie);
  if (!playable) return <div className="ember-actions"><button className="ember-action ember-action--primary" disabled={measurement} onClick={measurement ? undefined : onDetails}>View details</button><button className="ember-action" disabled>{mediaAvailability(movie) === "processing" ? "Processing" : "Cached suggestion"}</button></div>;
  return <div className="ember-actions"><button className="ember-action ember-action--primary" disabled={measurement} onClick={measurement ? undefined : movie.type === "series" ? onDetails : onPlay}>{movie.type === "series" ? "Select episode" : mediaAvailability(movie) === "processing" ? "Play while downloading" : "Initialize playback"}</button><button className="ember-action" disabled={measurement} onClick={measurement ? undefined : onDetails}>View details</button></div>;
}

function EmberBillboard({ items, context, onDetails, onPlay, personalized = true }: { personalized?: boolean; items: Movie[]; context: "home" | "movies" | "series"; onDetails: (movie: Movie) => void; onPlay: (movie: Movie) => void }) {
  const rotationItems = useMemo(() => items.slice(0, 8), [items]);
  return <BillboardStack items={rotationItems} theme="ember" className="ember-billboard" paginationClassName="ember-billboard__pagination" render={(movie, measurement) => {
    const label = !personalized ? `FEATURED / ${movie.type.toUpperCase()}` : context === "home" ? `PERSONALIZED TRANSMISSION / ${movie.type.toUpperCase()}` : context === "movies" ? "PERSONALIZED MOVIE" : "PERSONALIZED SERIES";
    return <div className="ember-hero"><div className="ember-hero__art">{!measurement && <MediaArtwork src={movie.bannerUrl || movie.thumbnailUrl} alt={movie.title} media={movie} className="h-full w-full object-cover" />}</div><div className="ember-hero__shade" /><AvailabilityBadge movie={movie} variant="ember" /><div className="ember-hero__copy"><small>{label}</small>{personalized && <RecommendationReason movie={movie} />}<h1>{movie.title}</h1><p>{movie.description || "No server description is available for this title."}</p><div className="ember-meta"><span>{movie.releaseYear || "YEAR N/A"}</span>{movie.quality && <span>{movie.quality}</span>}{movie.genres[0] && <span>{movie.genres[0]}</span>}</div><EmberActions measurement={measurement} movie={movie} onDetails={() => onDetails(movie)} onPlay={() => onPlay(movie)} /></div></div>;
  }} />;
}

function EmberCatalogView({ query, controller, onOpen, onPlay, onResume, onCategory }: { query: AppQueryState; controller: ThemeApplicationProps["controller"]; onOpen: (movie: Movie) => void; onPlay: (movie: Movie) => void; onResume: (movie: Movie, session: PlaybackSession) => void; onCategory: (category: string) => void }) {
  const view = query.view as CatalogView;
  const feedback = useRecommendationFeedback();
  const { region, model, hasContent, pending, error } = useCatalogLanding(query, controller);
  const regionProps = { ref: region, tabIndex: -1, "data-catalog-region": true, role: "region", "aria-label": "Discover titles" };
  if (!hasContent) return <RecommendationFeedbackProvider value={feedback ? { ...feedback, feedGeneration: model.feedGeneration } : null}><div {...regionProps} className="ember-category-discovery">{(controller.catalogItems.length > 0 || controller.recommendation) && <CategoryFilterRail preservePageScroll options={model.categories} active={model.activeCategory} variant="ember" onSelect={onCategory} />}<CatalogLandingState pending={pending} error={error} view={view} category={query.genre} controller={controller} />{!pending && model.source === "recommendations" && <CatalogLandingNotice controller={controller} />}</div></RecommendationFeedbackProvider>;
  const context = view as "home" | "movies" | "series";
  const collectionsClass = view === "home" ? "ember-collections" : "ember-genre-collections";
  const hasResults = model.gridItems.length > 0 || model.sections.length > 0;
  return <RecommendationFeedbackProvider value={feedback ? { ...feedback, feedGeneration: model.feedGeneration } : null}><div {...regionProps} className={`${view === "home" ? "ember-home" : "ember-discovery"} ember-category-discovery`} data-category-mode={model.mode}>
    <EmberBillboard items={model.billboardItems} personalized={model.source === "recommendations"} context={context} onDetails={onOpen} onPlay={onPlay} />
    <CatalogLandingNotice controller={controller} />
    <CategoryFilterRail preservePageScroll options={model.categories} active={model.activeCategory} variant="ember" onSelect={onCategory} />
    {ENABLE_VISUAL_GENRE_CATEGORIES && <GenreCategoryGallery cards={model.genreCards} active={model.activeCategory} variant="ember" onSelect={onCategory} />}
    <AnimatedState stateKey={`${model.mode}:${model.activeCategory}`}>
      {model.gridItems.length > 0 ? <EmberGrid title={model.mode === "all" ? "All Releases" : model.activeLabel} label={model.mode === "all" ? "COMPLETE SERVER CATALOG" : "CATEGORY CATALOG"} items={model.gridItems} total={model.total} sessions={controller.sessions} showReasons={model.source === "recommendations" && model.mode !== "all"} onOpen={onOpen} /> : hasResults ? <motion.div variants={CONTENT_STAGGER} initial="hidden" animate="shown" className={collectionsClass}>{model.sections.map((collection) => <EmberRail key={collection.id} label={collection.label} title={collection.title} items={collection.items} sessions={controller.sessions} kind={collection.kind} showReasons={collection.showReasons} onOpen={onOpen} onResume={collection.id === "continue-watching" ? onResume : undefined} />)}</motion.div> : <div className="ember-category-discovery__empty"><EmberStatePanel code="NO CATEGORY SIGNAL" title={`No ${model.activeLabel} titles`} body={`No titles match the ${model.activeLabel} category.`} /></div>}
    </AnimatedState>
    {model.source === "recommendations" && controller.recommendationHasMore && <div className="ember-pagination"><button className="ember-load-more" disabled={controller.recommendationLoadingMore} onClick={() => void controller.loadMoreRecommendations()}>{controller.recommendationLoadingMore ? "Loading records…" : `Load more / ${model.sourceItems.length} of ${model.total}`}</button></div>}
  </div></RecommendationFeedbackProvider>;
}

function EmberWatchlist({ controller, onOpen }: Pick<ThemeApplicationProps, "controller"> & { onOpen: (movie: Movie) => void }) {
  const movies = controller.watchlistItems.filter((movie) => movie.type === "movie");
  const series = controller.watchlistItems.filter((movie) => movie.type === "series");
  return <motion.section className="ember-watchlist"><header className="ember-page-heading"><div><p>SERVER WATCHLIST</p><h1>My List</h1></div><span>{controller.watchlistKnown ? `${controller.watchlist.length} saved records` : "Saved count unavailable"}</span></header><WatchlistReadStatus controller={controller} /><AnimatedState stateKey={controller.watchlistItems.length ? "populated" : controller.watchlistKnown && !controller.watchlist.length ? "empty" : "unknown"}>{controller.watchlistItems.length ? <motion.div variants={CONTENT_STAGGER} initial="hidden" animate="shown" className="ember-collections"><EmberRail label="SAVED MOVIES" title="Movies" items={movies} sessions={controller.sessions} onOpen={onOpen} /><EmberRail label="SAVED SERIES" title="Series" items={series} sessions={controller.sessions} onOpen={onOpen} /></motion.div> : controller.watchlistKnown && !controller.watchlist.length ? <EmberStatePanel code="WATCHLIST EMPTY" title="Your list is empty" body="Add a movie or series from its details page." /> : null}</AnimatedState></motion.section>;
}

function resultToMovie(result: DiscoverMovie, controller: ThemeApplicationProps["controller"]): Movie | null { return controller.resolveMovie(result.type === "series" ? `tv_${result.tmdbId}` : `m_${result.tmdbId}`); }

function EmberSearch({ query, controller, onOpen, onSearch }: Pick<ThemeApplicationProps, "query" | "controller"> & { onOpen: (movie: Movie) => void; onSearch: (value: string) => void }) {
  const [draft, setDraft] = useState(query.q ?? "");
  const { trackEvent } = useTelemetry();
  useEffect(() => setDraft(query.q ?? ""), [query.q]);
  const submit = (event: React.FormEvent) => { event.preventDefault(); onSearch(draft.trim()); };
  const results = controller.results.map((result) => ({ result, movie: resultToMovie(result, controller) })).filter((item): item is { result: DiscoverMovie; movie: Movie } => Boolean(item.movie));
  const stateKey = controller.searchError ? "error" : controller.searching ? "searching" : !query.q ? "ready" : !results.length ? "empty" : `${query.q}:${results.length}`;
  return <section className="ember-browse ember-search"><header className="ember-page-heading"><div><p>SERVER SEARCH</p><h1>{query.q ? `Results for “${query.q}”` : "Search the catalog"}</h1></div>{query.q && controller.searchStatus === "success" && <span>{results.length} catalog records</span>}</header><form className="ember-search__form" onSubmit={submit}><input aria-label="Search server catalog" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="Title, film, or series" /><button type="submit">Search server</button></form><AnimatedState stateKey={stateKey}>{controller.searchError ? <div className="catalog-resource-state"><h2>Search unavailable</h2><p role="alert">{controller.searchError}</p><button className="ember-retry" onClick={controller.retrySearch}>Retry this search</button></div> : controller.searching ? <EmberStatePanel code="QUERY ACTIVE" title="Searching the server" body={`Looking for “${query.q}”.`} loading /> : !query.q ? <EmberStatePanel code="AWAITING QUERY" title="Search is ready" body="Enter a title to search the server catalog." /> : !results.length ? <EmberStatePanel code="NO MATCH" title="No results" body="The server did not return matches for this query." /> : <motion.div variants={CONTENT_STAGGER} initial="hidden" animate="shown" className="ember-search__results">{results.map(({ result, movie }) => <motion.button layout variants={CONTENT_REVEAL} key={`${result.type}-${result.tmdbId}`} data-availability={mediaAvailability(movie)} onClick={() => { trackEvent({ event_type: "search_result_select", tmdb_id: result.tmdbId }); onOpen(movie); }}><MediaArtwork src={result.thumbnailUrl} alt={result.title} media={movie} className="ember-search__art" /><span><strong>{result.title}</strong><small>{mediaAvailability(movie) === "available" ? "Available in server catalog" : "Cached suggestion / details available"}</small></span></motion.button>)}</motion.div>}</AnimatedState></section>;
}

export function EmberDashboard({ query, controller, presentation }: ThemeApplicationProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const profile = useProfileStore((state) => state.activeProfile)!;
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
  const navigationProps = { profile, activeView: query.view, query: query.q, isAdmin: profile.id === "1", onView: navigateView, onSearch: (value: string) => appNavigate("search", value ? { q: value } : {}), onEditProfile: () => navigate(profileEditUrl(profile.id), { state: { returnTo: `${location.pathname}${location.search}${location.hash}` } }), onProfiles: () => { clearProfile(); navigate("/profiles"); }, onAdmin: () => appNavigate("admin", { section: "account" }), onLogout: logout };
  const content = controller.loading && !isCatalogView(query.view) ? <EmberStatePanel code="PROFILE FEED HANDSHAKE" title="Loading recommendations" body="Synchronizing this profile with the recommendation engine." loading /> : isCatalogView(query.view) ? <EmberCatalogView query={query} controller={controller} onOpen={openDetails} onPlay={openWatch} onResume={resumeWatch} onCategory={(category) => appNavigate(query.view, { genre: category })} /> : query.view === "watchlist" ? <EmberWatchlist controller={controller} onOpen={openDetails} /> : query.view === "search" ? <EmberSearch query={query} controller={controller} onOpen={navigateDetails} onSearch={(value) => appNavigate("search", value ? { q: value } : {})} /> : query.view === "downloads" ? <EmberDownloads /> : query.view === "details" ? selected ? <DetailsRouter movie={selected} onClose={closeDetails} isWatchlisted={controller.watchlist.includes(selected.id)} sessions={controller.sessions} onWatchlistChange={controller.setWatchlist} onRecommendationInvalidated={controller.refreshRecommendations} /> : controller.detailsLoading ? <EmberStatePanel code="MEDIA LOOKUP" title="Loading media details" body="Resolving the canonical server record." loading /> : controller.detailsError ? <EmberStatePanel code="MEDIA LOOKUP FAILED" title="Details unavailable" body={controller.detailsError} action={<button className="ember-retry" onClick={controller.retryDetails}>Retry lookup</button>} /> : <EmberStatePanel code="INVALID MEDIA ID" title="Title not found" body="That media identifier is not present in the server catalog." /> : null;
  const feedback = { profileId: profile.id, preferences: controller.preferences, preferencesKnown: controller.preferencesKnown, preferenceKnownIds: controller.preferenceKnownIds, preferenceOperations: controller.preferenceOperations, onCheck: controller.checkPreference, onChange: controller.updatePreference, feedGeneration: controller.recommendation ? String(controller.recommendation.generatedAt) : "", scope: isCatalogView(query.view) ? query.view : "details", category: query.genre ?? "recommended" };
  return <RecommendationFeedbackProvider value={feedback}><CatalogLayout presentation={presentation} navigationProps={navigationProps} className="ember-app" mainClassName="ember-main">{controller.error && controller.error !== controller.catalogError && <div className="ember-error" role="alert">{controller.error}</div>}<AnimatedView theme="ember" viewKey={appViewMotionKey(query)}>{content}</AnimatedView></CatalogLayout></RecommendationFeedbackProvider>;
}
