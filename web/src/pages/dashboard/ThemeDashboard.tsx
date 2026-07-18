import React, { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useLocation, useNavigate } from "react-router-dom";
import { MediaArtwork } from "../../components/media/MediaArtwork";
import { ProgressBar } from "../../components/ui/ProgressBar";
import { appUrl, isCatalogView, preservedCatalogCategory, type AppQueryState, type AppView, type CatalogView } from "../../navigation/queryState";
import { profileEditUrl } from "../../navigation/profileEditing";
import { useAuthStore } from "../../stores/authStore";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import type { ThemeApplicationProps } from "../../themes/application/contracts";
import type { DiscoverMovie, Movie, PlaybackSession } from "../../types/api";
import { completionFraction, isPlayableMovie } from "../../utils/media";
import { DetailsRouter } from "../details/DetailsRouter";
import { buildCatalogPresentation } from "./catalogPresentation";
import { CategoryFilterRail } from "./CategoryFilterRail";
import { ServerDownloads } from "./ServerDownloads";
import type { CatalogController } from "./useCatalogController";
import { ROTATION_INTERVAL, useRotatingFeature } from "./useRotatingFeature";
import { AnimatedState, AnimatedView, CONTENT_REVEAL, CONTENT_STAGGER, REDUCED_BILLBOARD_MOTION, THEME_MOTION, useAppMotion } from "../../motion/motionSystem";
import { useAnimatedRail } from "../../motion/useAnimatedRail";

import { useTelemetry } from "../../hooks/useTelemetry";

function MediaCard({ movie, session, theme, onOpen }: { movie: Movie; session?: PlaybackSession; theme: string; onOpen: (movie: Movie) => void }) {
  const playable = isPlayableMovie(movie);
  const available = movie.type === "series" || playable;
  const { trackEvent } = useTelemetry();
  return <motion.button layout="position" className="catalog-card" data-card-theme={theme} onClick={() => { trackEvent({ event_type: "card_click", movie_id: movie.id }); onOpen(movie); }}><span className="catalog-card__art"><MediaArtwork src={movie.thumbnailUrl} alt={movie.title} media={movie} className="h-full w-full object-cover" /><i aria-hidden="true" /></span><span className="catalog-card__copy"><strong>{movie.title}</strong><small>{available ? `${movie.type} / ${movie.releaseYear || "catalogued"}` : "Unavailable on server"}</small></span>{session && <ProgressBar className="catalog-card__progress" progress={completionFraction(session.completionRate)} />}</motion.button>;
}

function MediaCollection({ title, label, items, sessions, theme, onOpen }: { title: string; label?: string; items: Movie[]; sessions: PlaybackSession[]; theme: string; onOpen: (movie: Movie) => void }) {
  const { rail, scroll, direction, canScrollPrevious, canScrollNext } = useAnimatedRail();
  if (!items.length) return null;
  return <motion.section variants={CONTENT_REVEAL} className="catalog-collection" data-rail-direction={direction}><header><div>{label && <p>{label}</p>}<h2>{title}</h2></div><span>{String(items.length).padStart(2, "0")} titles</span></header><div className="catalog-collection__frame"><button className="catalog-rail-blade catalog-rail-blade--previous" disabled={!canScrollPrevious} onClick={() => scroll(-1)} aria-label={`Scroll ${title} backward`}>‹</button><div ref={rail} className="catalog-collection__rail">{items.map((movie) => <MediaCard key={movie.id} movie={movie} session={sessions.find((session) => session.movieId === movie.id)} theme={theme} onOpen={onOpen} />)}</div><button className="catalog-rail-blade catalog-rail-blade--next" disabled={!canScrollNext} onClick={() => scroll(1)} aria-label={`Scroll ${title} forward`}>›</button></div></motion.section>;
}

function FeatureActions({ movie, onDetails, onPlay }: { movie: Movie; onDetails: () => void; onPlay: () => void }) {
  const unavailable = movie.type === "movie" && !isPlayableMovie(movie);
  return <div className="feature-actions"><button className="feature-action feature-action--primary" disabled={unavailable} onClick={movie.type === "series" ? onDetails : onPlay}>{movie.type === "series" ? "Select episode" : unavailable ? "Playback unavailable" : "Play now"}</button><button className="feature-action" onClick={onDetails}>View details</button></div>;
}

function FeaturedStage({ movie, variant, context, onDetails, onPlay }: { movie: Movie; variant: string; context: "home" | "movies" | "series"; onDetails: () => void; onPlay: () => void }) {
  const art = <MediaArtwork src={movie.bannerUrl || movie.thumbnailUrl} alt={movie.title} media={movie} className="h-full w-full object-cover" />;
  const contextLabel = context === "home" ? "PROFILE LIBRARY" : context === "movies" ? "FEATURED MOVIE" : "FEATURED SERIES";
  if (variant === "terminal") return <section className="feature-stage feature-stage--terminal"><div className="terminal-intro"><span><i />CATALOG ONLINE</span><p>{contextLabel} / SERVER INDEX</p></div><div className="terminal-feature"><div className="terminal-feature__copy"><small>{contextLabel} / {movie.type.toUpperCase()}</small><h1>{movie.title}</h1><p>{movie.description || "No server description is available for this title."}</p><div className="feature-meta"><span>{movie.releaseYear || "YEAR N/A"}</span><span>{movie.quality || "SOURCE"}</span><span>{movie.genres[0] || "UNCATEGORIZED"}</span></div><FeatureActions movie={movie} onDetails={onDetails} onPlay={onPlay} /></div><div className="terminal-feature__art">{art}<span>SERVER MEDIA</span></div></div></section>;
  if (variant === "editorial") return <section className="feature-stage feature-stage--editorial"><div className="editorial-art">{art}</div><div className="editorial-copy"><small>{contextLabel}</small><h1>{movie.title}</h1><p>{movie.description || "No server description is available for this title."}</p><FeatureActions movie={movie} onDetails={onDetails} onPlay={onPlay} /></div></section>;
  if (variant === "cinematic") return <section className="feature-stage feature-stage--cinematic"><div className="cinema-art">{art}</div><div className="cinema-shade" /><div className="cinema-copy"><small>{contextLabel} / {movie.releaseYear || "server catalog"}</small><h1>{movie.title}</h1><p>{movie.description || "No server description is available for this title."}</p><FeatureActions movie={movie} onDetails={onDetails} onPlay={onPlay} /></div></section>;
  return <section className="feature-stage feature-stage--workspace"><div className="workspace-heading"><p>{contextLabel}</p><h1>{movie.title}</h1><span>{movie.description || "No server description is available for this title."}</span><FeatureActions movie={movie} onDetails={onDetails} onPlay={onPlay} /></div><div className="workspace-art">{art}</div><div className="workspace-stat"><small>AVAILABLE FORMAT</small><strong>{movie.quality || "SOURCE"}</strong><span>{movie.languages.length} audio track{movie.languages.length === 1 ? "" : "s"}</span></div></section>;
}

function RotatingBillboard({ items, variant, context, onDetails, onPlay }: { items: Movie[]; variant: string; context: "home" | "movies" | "series"; onDetails: (movie: Movie) => void; onPlay: (movie: Movie) => void }) {
  const { reduced: reduceMotion } = useAppMotion();
  const rotationItems = useMemo(() => items.slice(0, 8), [items]);
  const activeTheme = useThemeStore((state) => state.activeTheme);
  const { featured, index, setIndex, setPaused, paused, direction, source } = useRotatingFeature(rotationItems);
  if (!featured) return null;
  return <div className="billboard-rotator" style={{ "--billboard-rotation-duration": `${ROTATION_INTERVAL}ms` } as React.CSSProperties} data-motion-source={source} data-motion-direction={direction} data-rotation-paused={paused} onFocusCapture={(event) => setPaused((event.target as HTMLElement).matches(":focus-visible"))} onBlurCapture={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setPaused(false); }}><AnimatePresence mode="wait" initial={false} custom={direction}><motion.div key={featured.id} custom={direction} variants={reduceMotion ? REDUCED_BILLBOARD_MOTION : THEME_MOTION[activeTheme].billboard} initial="initial" animate="animate" exit="exit"><FeaturedStage movie={featured} variant={variant} context={context} onDetails={() => onDetails(featured)} onPlay={() => onPlay(featured)} /></motion.div></AnimatePresence>{rotationItems.length > 1 && <div className="billboard-pagination" aria-label="Featured media">{rotationItems.map((movie, itemIndex) => <button key={movie.id} data-active={itemIndex === index} aria-current={itemIndex === index ? "true" : undefined} onClick={() => setIndex(itemIndex)} aria-label={`Show ${movie.title}`} />)}</div>}</div>;
}

function CatalogGrid({ title, label, items, sessions, theme, onOpen }: { title: string; label: string; items: Movie[]; sessions: PlaybackSession[]; theme: string; onOpen: (movie: Movie) => void }) {
  return <motion.section layout variants={CONTENT_REVEAL} className="category-catalog-grid"><header><div><p>{label}</p><h2>{title}</h2></div><span>{items.length} title{items.length === 1 ? "" : "s"}</span></header><motion.div layout variants={CONTENT_STAGGER} initial="hidden" animate="shown" className="browse-grid">{items.map((movie) => <MediaCard key={movie.id} movie={movie} session={sessions.find((session) => session.movieId === movie.id)} theme={theme} onOpen={onOpen} />)}</motion.div></motion.section>;
}

function CatalogDiscoveryView({ query, controller, theme, variant, onOpen, onPlay, onCategory }: { query: AppQueryState; controller: CatalogController; theme: string; variant: string; onOpen: (movie: Movie) => void; onPlay: (movie: Movie) => void; onCategory: (category: string) => void }) {
  const view = query.view as CatalogView;
  const model = useMemo(() => buildCatalogPresentation({ movies: controller.movies, continueWatching: controller.continueWatching, view, category: query.genre }), [controller.continueWatching, controller.movies, query.genre, view]);
  const context = view as "home" | "movies" | "series";
  const collectionsClass = view === "home" ? "home-collections" : "browse-genre-collections";
  const emptyTitle = !model.sourceItems.length ? view === "home" ? "The catalog is empty" : `No ${view} found` : `No ${model.activeLabel} titles`;
  const emptyBody = !model.sourceItems.length ? "No media records were returned by the server." : `No server titles match the ${model.activeLabel} category.`;
  const hasResults = model.gridItems.length > 0 || model.sections.length > 0;

  return <div className={`${view === "home" ? "home-view" : "browse-discovery"} category-discovery`} data-category-mode={model.mode}>
    {model.billboardItems.length > 0 && <RotatingBillboard items={model.billboardItems} variant={variant} context={context} onDetails={onOpen} onPlay={onPlay} />}
    <CategoryFilterRail options={model.categories} active={model.activeCategory} variant="shared" onSelect={onCategory} />
    <AnimatedState stateKey={`${model.mode}:${model.activeCategory}`}>
      {model.gridItems.length > 0 ? <CatalogGrid title={model.mode === "all" ? "All Releases" : model.activeLabel} label={model.mode === "all" ? "COMPLETE SERVER CATALOG" : "CATEGORY CATALOG"} items={model.gridItems} sessions={controller.sessions} theme={theme} onOpen={onOpen} /> : hasResults ? <motion.div variants={CONTENT_STAGGER} initial="hidden" animate="shown" className={collectionsClass}>{model.sections.map((collection) => <MediaCollection key={collection.id} label={collection.label} title={collection.title} items={collection.items} sessions={controller.sessions} theme={theme} onOpen={onOpen} />)}</motion.div> : <div className="category-discovery__empty"><EmptyState title={emptyTitle} body={emptyBody} /></div>}
    </AnimatedState>
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
  const localResults = controller.results.map((result) => ({ result, movie: resultToMovie(result, controller.movies) }));
  return <section className="browse-view search-view"><header className="browse-heading"><div><p>SERVER SEARCH</p><h1>Results for “{query.q}”</h1></div><span>{localResults.filter((item) => item.movie).length} available locally</span></header>{form}<AnimatedState stateKey={`${query.q}:${localResults.length}`}>{!localResults.length ? <EmptyState title="No results" body="The server did not return matches for this query." /> : <motion.div layout variants={CONTENT_STAGGER} initial="hidden" animate="shown" className="search-results">{localResults.map(({ result, movie }) => <motion.button layout variants={CONTENT_REVEAL} key={`${result.type}-${result.tmdbId}`} className="search-result" data-card-theme={theme} disabled={!movie} onClick={() => { trackEvent({ event_type: "search_click", tmdb_id: result.tmdbId, metadata_json: { query: query.q, genres: result.genres, cast: result.cast, director: result.director } }); movie && onOpen(movie); }}><MediaArtwork src={result.thumbnailUrl} alt={result.title} media={movie} className="search-result__art" /><span><strong>{result.title}</strong><small>{movie ? "Available on server" : "Not available on server"}</small></span></motion.button>)}</motion.div>}</AnimatedState></section>;
}

function resultToMovie(result: DiscoverMovie, movies: Movie[]) { const id = result.type === "series" ? `tv_${result.tmdbId}` : `m_${result.tmdbId}`; return movies.find((movie) => movie.id === id); }
function LoadingState({ label = "Loading server catalog" }: { label?: string }) { return <div className="catalog-state catalog-state--loading"><i /><p>{label}</p></div>; }
function EmptyState({ title, body }: { title: string; body: string }) { return <div className="catalog-state"><p>NO DATA</p><h2>{title}</h2><span>{body}</span></div>; }

export function appViewMotionKey(query: AppQueryState): string {
  return query.view === "details" ? `${query.view}:${query.media ?? "missing"}` : query.view;
}

export function LegacyThemeAdapter({ query, controller, presentation: definition }: ThemeApplicationProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const profile = useProfileStore((state) => state.activeProfile)!;
  const theme = useThemeStore((state) => state.activeTheme);
  const clearProfile = useProfileStore((state) => state.clearProfile);
  const logout = useAuthStore((state) => state.logout);
  const Background = definition.Background;
  const Navigation = definition.Navigation;
  const appNavigate = (view: AppView, options: Parameters<typeof appUrl>[2] = {}) => navigate(appUrl(profile.id, view, options), { state: { fromApp: true, previous: location.search } });
  const navigateView = (view: AppView) => appNavigate(view, preservedCatalogCategory(query, view));
  const openDetails = (movie: Movie) => appNavigate("details", { media: movie.id });
  const openWatch = (movie: Movie) => appNavigate("watch", { media: movie.id });
  const closeDetails = () => (location.state as { fromApp?: boolean } | null)?.fromApp ? navigate(-1) : appNavigate("home");
  const selected = query.media ? controller.movies.find((movie) => movie.id === query.media) ?? null : null;
  const navigationProps = { profile, activeView: query.view, query: query.q, isAdmin: profile.id === "1", onView: navigateView, onSearch: (q: string) => appNavigate("search", q ? { q } : {}), onEditProfile: () => navigate(profileEditUrl(profile.id), { state: { returnTo: `${location.pathname}${location.search}${location.hash}` } }), onProfiles: () => { clearProfile(); navigate("/profiles"); }, onAdmin: () => appNavigate("admin", { section: "account" }), onLogout: logout };
  const content = controller.loading ? <LoadingState /> : isCatalogView(query.view) ? <CatalogDiscoveryView query={query} controller={controller} theme={definition.cardVariant} variant={definition.heroVariant} onOpen={openDetails} onPlay={openWatch} onCategory={(category) => appNavigate(query.view, { genre: category })} /> : query.view === "watchlist" ? <WatchlistView controller={controller} theme={definition.cardVariant} onOpen={openDetails} /> : query.view === "search" ? <SearchView query={query} controller={controller} theme={definition.cardVariant} onOpen={openDetails} onSearch={(q) => appNavigate("search", q ? { q } : {})} /> : query.view === "downloads" ? <ServerDownloads /> : query.view === "details" ? selected ? <DetailsRouter movie={selected} onClose={closeDetails} isWatchlisted={controller.watchlist.includes(selected.id)} onWatchlistChange={controller.setWatchlist} /> : <EmptyState title="Title not found" body="That media identifier is not present in the server catalog." /> : null;

  return <div className={`theme-app ${definition.shellClass}`} data-theme={theme} data-interaction={definition.interaction.id} data-view={query.view}><Background /><Navigation {...navigationProps} /><main className="theme-main">{controller.error && <div className="catalog-error" role="alert">{controller.error}</div>}<AnimatedView theme={theme} viewKey={appViewMotionKey(query)}>{content}</AnimatedView></main></div>;
}
