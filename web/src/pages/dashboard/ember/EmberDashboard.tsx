import React, { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useLocation, useNavigate } from "react-router-dom";
import { MediaArtwork } from "../../../components/media/MediaArtwork";
import { ProgressBar } from "../../../components/ui/ProgressBar";
import { appUrl, type AppView } from "../../../navigation/queryState";
import { useAuthStore } from "../../../stores/authStore";
import { useProfileStore } from "../../../stores/profileStore";
import type { ThemeApplicationProps } from "../../../themes/application/contracts";
import type { DiscoverMovie, Movie, PlaybackSession } from "../../../types/api";
import { completionFraction, isPlayableMovie } from "../../../utils/media";
import { DetailsRouter } from "../../details/DetailsRouter";
import { groupMoviesByGenre } from "../catalogPresentation";
import { useRotatingFeature } from "../useRotatingFeature";
import { EmberDownloads } from "./EmberDownloads";

function EmberStatePanel({ code, title, body, loading = false }: { code: string; title: string; body: string; loading?: boolean }) {
  return <div className="ember-state-panel">{loading && <i aria-hidden="true" />}<p>{code}</p><h2>{title}</h2><span>{body}</span></div>;
}

function EmberMediaCard({ movie, session, onOpen }: { movie: Movie; session?: PlaybackSession; onOpen: (movie: Movie) => void }) {
  const card = useRef<HTMLButtonElement>(null);
  const updateTilt = (event: React.PointerEvent<HTMLButtonElement>) => {
    if (event.pointerType !== "mouse" || window.matchMedia("(prefers-reduced-motion: reduce)").matches || !card.current) return;
    const rect = card.current.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width;
    const y = (event.clientY - rect.top) / rect.height;
    card.current.style.setProperty("--tilt-x", `${(0.5 - y) * 8}deg`);
    card.current.style.setProperty("--tilt-y", `${(x - 0.5) * 8}deg`);
    card.current.style.setProperty("--spot-x", `${x * 100}%`);
    card.current.style.setProperty("--spot-y", `${y * 100}%`);
  };
  const resetTilt = () => { card.current?.style.setProperty("--tilt-x", "0deg"); card.current?.style.setProperty("--tilt-y", "0deg"); };
  return <button ref={card} className="ember-media-card" onPointerMove={updateTilt} onPointerLeave={resetTilt} onBlur={resetTilt} onClick={() => onOpen(movie)}><span className="ember-media-card__art"><MediaArtwork src={movie.thumbnailUrl} alt={movie.title} media={movie} className="h-full w-full object-cover" /><i aria-hidden="true" /></span><span className="ember-media-card__copy"><strong>{movie.title}</strong><small>{movie.type} / {movie.releaseYear || "year n/a"}</small>{!isPlayableMovie(movie) && movie.type === "movie" && <em>Playback unavailable</em>}</span>{session && <ProgressBar className="ember-media-card__progress" progress={completionFraction(session.completionRate)} />}</button>;
}

function EmberRail({ label, title, items, sessions, onOpen }: { label: string; title: string; items: Movie[]; sessions: PlaybackSession[]; onOpen: (movie: Movie) => void }) {
  const rail = useRef<HTMLDivElement>(null);
  if (!items.length) return null;
  const scroll = (direction: number) => rail.current?.scrollBy({ left: direction * Math.min(rail.current.clientWidth * .82, 920), behavior: "smooth" });
  return <section className="ember-rail"><header><div><p>{label}</p><h2>{title}</h2></div><span>{String(items.length).padStart(2, "0")} catalog records</span></header><div className="ember-rail__frame"><button className="ember-rail__blade ember-rail__blade--previous" onClick={() => scroll(-1)} aria-label={`Scroll ${title} backward`}>‹</button><div ref={rail} className="ember-rail__track">{items.map((movie) => <EmberMediaCard key={movie.id} movie={movie} session={sessions.find((session) => session.movieId === movie.id)} onOpen={onOpen} />)}</div><button className="ember-rail__blade ember-rail__blade--next" onClick={() => scroll(1)} aria-label={`Scroll ${title} forward`}>›</button></div></section>;
}

function EmberActions({ movie, onDetails, onPlay }: { movie: Movie; onDetails: () => void; onPlay: () => void }) {
  const playable = isPlayableMovie(movie);
  if (movie.type === "series") return <div className="ember-actions"><button className="ember-action ember-action--primary" onClick={onDetails}>Select episode</button><button className="ember-action" onClick={onDetails}>View details</button></div>;
  return <div className="ember-actions">{playable ? <button className="ember-action ember-action--primary" onClick={onPlay}>Initialize playback</button> : <button className="ember-action ember-action--primary" onClick={onDetails}>View details</button>}<button className="ember-action" disabled={!playable} onClick={playable ? onDetails : undefined}>{playable ? "View details" : "Playback unavailable"}</button></div>;
}

function EmberBillboard({ items, context, onDetails, onPlay }: { items: Movie[]; context: "home" | "movies" | "series"; onDetails: (movie: Movie) => void; onPlay: (movie: Movie) => void }) {
  const reduceMotion = useReducedMotion();
  const rotationItems = useMemo(() => items.slice(0, 8), [items]);
  const { featured, index, setIndex, setPaused } = useRotatingFeature(rotationItems);
  if (!featured) return null;
  const label = context === "home" ? `FEATURED TRANSMISSION / ${featured.type.toUpperCase()}` : context === "movies" ? "FEATURED MOVIE" : "FEATURED SERIES";
  return <section className="ember-billboard" onMouseEnter={() => setPaused(true)} onMouseLeave={() => setPaused(false)} onFocusCapture={() => setPaused(true)} onBlurCapture={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setPaused(false); }}><AnimatePresence mode="wait" initial={false}><motion.div className="ember-hero" key={featured.id} initial={reduceMotion ? false : { opacity: 0, scale: 1.018, filter: "blur(8px)" }} animate={{ opacity: 1, scale: 1, filter: "blur(0px)" }} exit={reduceMotion ? undefined : { opacity: 0, scale: .992, filter: "blur(6px)" }} transition={{ duration: reduceMotion ? 0 : 1.35, ease: [0.16, 1, 0.3, 1] }}><div className="ember-hero__art"><MediaArtwork src={featured.bannerUrl || featured.thumbnailUrl} alt={featured.title} media={featured} className="h-full w-full object-cover" /></div><div className="ember-hero__shade" /><div className="ember-hero__copy"><span className="ember-status"><i />CATALOG ONLINE</span><small>{label}</small><h1>{featured.title}</h1><p>{featured.description || "No server description is available for this title."}</p><div className="ember-meta"><span>{featured.releaseYear || "YEAR N/A"}</span>{featured.quality && <span>{featured.quality}</span>}{featured.genres[0] && <span>{featured.genres[0]}</span>}</div><EmberActions movie={featured} onDetails={() => onDetails(featured)} onPlay={() => onPlay(featured)} /></div></motion.div></AnimatePresence>{rotationItems.length > 1 && <div className="ember-billboard__pagination" aria-label="Featured media">{rotationItems.map((movie, itemIndex) => <button key={movie.id} data-active={itemIndex === index} onClick={() => setIndex(itemIndex)} aria-label={`Show ${movie.title}`} />)}</div>}</section>;
}

function EmberHome({ controller, onDetails, onPlay }: Pick<ThemeApplicationProps, "controller"> & { onDetails: (movie: Movie) => void; onPlay: (movie: Movie) => void }) {
  if (!controller.movies.length) return <EmberStatePanel code="NO CATALOG SIGNAL" title="The catalog is empty" body="No media records were returned by the server." />;
  return <div className="ember-home"><EmberBillboard items={controller.movies} context="home" onDetails={onDetails} onPlay={onPlay} /><div className="ember-collections"><EmberRail label="RESUME INDEX" title="Continue watching" items={controller.continueWatching} sessions={controller.sessions} onOpen={onDetails} /><EmberRail label="FEATURE ARCHIVE" title="Movies" items={controller.movieItems} sessions={controller.sessions} onOpen={onDetails} /><EmberRail label="EPISODIC ARCHIVE" title="Series" items={controller.seriesItems} sessions={controller.sessions} onOpen={onDetails} /></div></div>;
}

function EmberBrowse({ query, controller, onOpen, onPlay }: Pick<ThemeApplicationProps, "query" | "controller"> & { onOpen: (movie: Movie) => void; onPlay: (movie: Movie) => void }) {
  const source = query.view === "series" ? controller.seriesItems : controller.movieItems;
  const collections = useMemo(() => groupMoviesByGenre(source, query.genre), [query.genre, source]);
  if (!source.length) return <section className="ember-browse"><EmberStatePanel code="NO CATALOG SIGNAL" title={`No ${query.view} found`} body="The server has not catalogued titles for this view." /></section>;
  return <div className="ember-discovery"><EmberBillboard items={source} context={query.view as "movies" | "series"} onDetails={onOpen} onPlay={onPlay} /><div className="ember-genre-collections">{collections.map((collection) => <EmberRail key={collection.genre} label={`${query.view.toUpperCase()} / GENRE`} title={collection.genre} items={collection.items} sessions={controller.sessions} onOpen={onOpen} />)}{!collections.length && <EmberStatePanel code="NO GENRE SIGNAL" title="No matching category" body="No server titles match this genre." />}</div></div>;
}

function EmberWatchlist({ controller, onOpen }: Pick<ThemeApplicationProps, "controller"> & { onOpen: (movie: Movie) => void }) {
  const movies = controller.watchlistItems.filter((movie) => movie.type === "movie");
  const series = controller.watchlistItems.filter((movie) => movie.type === "series");
  return <section className="ember-watchlist"><header className="ember-page-heading"><div><p>SERVER WATCHLIST</p><h1>My List</h1></div><span>{controller.watchlistItems.length} saved records</span></header>{controller.watchlistItems.length ? <div className="ember-collections"><EmberRail label="SAVED MOVIES" title="Movies" items={movies} sessions={controller.sessions} onOpen={onOpen} /><EmberRail label="SAVED SERIES" title="Series" items={series} sessions={controller.sessions} onOpen={onOpen} /></div> : <EmberStatePanel code="WATCHLIST EMPTY" title="Your list is empty" body="Add a movie or series from its details page." />}</section>;
}

function resultToMovie(result: DiscoverMovie, movies: Movie[]): Movie | null {
  const id = result.type === "series" ? `tv_${result.tmdbId}` : `m_${result.tmdbId}`;
  return movies.find((movie) => movie.id === id) ?? null;
}

function EmberSearch({ query, controller, onOpen, onSearch }: Pick<ThemeApplicationProps, "query" | "controller"> & { onOpen: (movie: Movie) => void; onSearch: (value: string) => void }) {
  const [draft, setDraft] = useState(query.q ?? "");
  useEffect(() => setDraft(query.q ?? ""), [query.q]);
  const submit = (event: React.FormEvent) => { event.preventDefault(); onSearch(draft.trim()); };
  const results = controller.results.map((result) => ({ result, movie: resultToMovie(result, controller.movies) }));
  return <section className="ember-browse ember-search"><header className="ember-page-heading"><div><p>SERVER SEARCH</p><h1>{query.q ? `Results for “${query.q}”` : "Search the catalog"}</h1></div>{query.q && <span>{results.filter((item) => item.movie).length} local records</span>}</header><form className="ember-search__form" onSubmit={submit}><input aria-label="Search server catalog" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="Title, film, or series" /><button type="submit">Search server</button></form>{controller.searching ? <EmberStatePanel code="QUERY ACTIVE" title="Searching the server" body={`Looking for “${query.q}”.`} loading /> : !query.q ? <EmberStatePanel code="AWAITING QUERY" title="Search is ready" body="Enter a title to search the server catalog." /> : !results.length ? <EmberStatePanel code="NO MATCH" title="No results" body="The server did not return matches for this query." /> : <div className="ember-search__results">{results.map(({ result, movie }) => <button key={`${result.type}-${result.tmdbId}`} disabled={!movie} onClick={() => movie && onOpen(movie)}><MediaArtwork src={result.thumbnailUrl} alt={result.title} media={movie ?? undefined} className="ember-search__art" /><span><strong>{result.title}</strong><small>{movie ? "Available in server catalog" : "Not available in local catalog"}</small></span></button>)}</div>}</section>;
}

export function EmberDashboard({ query, controller, presentation }: ThemeApplicationProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const profile = useProfileStore((state) => state.activeProfile)!;
  const clearProfile = useProfileStore((state) => state.clearProfile);
  const logout = useAuthStore((state) => state.logout);
  const Background = presentation.Background;
  const Navigation = presentation.Navigation;
  const appNavigate = (view: AppView, options: Parameters<typeof appUrl>[2] = {}) => navigate(appUrl(profile.id, view, options), { state: { fromApp: true, previous: location.search } });
  const openDetails = (movie: Movie) => appNavigate("details", { media: movie.id });
  const openWatch = (movie: Movie) => appNavigate("watch", { media: movie.id });
  const closeDetails = () => (location.state as { fromApp?: boolean } | null)?.fromApp ? navigate(-1) : appNavigate("home");
  const selected = query.media ? controller.movies.find((movie) => movie.id === query.media) ?? null : null;
  const navigationProps = { profile, activeView: query.view, query: query.q, isAdmin: profile.id === "1", onView: (view: AppView) => appNavigate(view), onSearch: (value: string) => appNavigate("search", value ? { q: value } : {}), onProfiles: () => { clearProfile(); navigate("/profiles"); }, onAdmin: () => appNavigate("admin", { section: "account" }), onLogout: logout };

  return <div className="theme-app theme-app--ember ember-app" data-theme="ember" data-view={query.view}><Background /><Navigation {...navigationProps} /><main className="theme-main ember-main">{controller.error && <div className="ember-error" role="alert">{controller.error}</div>}{controller.loading ? <EmberStatePanel code="CATALOG HANDSHAKE" title="Loading server catalog" body="Synchronizing this profile with the server index." loading /> : query.view === "home" ? <EmberHome controller={controller} onDetails={openDetails} onPlay={openWatch} /> : query.view === "movies" || query.view === "series" ? <EmberBrowse query={query} controller={controller} onOpen={openDetails} onPlay={openWatch} /> : query.view === "watchlist" ? <EmberWatchlist controller={controller} onOpen={openDetails} /> : query.view === "search" ? <EmberSearch query={query} controller={controller} onOpen={openDetails} onSearch={(value) => appNavigate("search", value ? { q: value } : {})} /> : query.view === "downloads" ? <EmberDownloads /> : query.view === "details" ? selected ? <DetailsRouter movie={selected} onClose={closeDetails} isWatchlisted={controller.watchlist.includes(selected.id)} onWatchlistChange={controller.setWatchlist} /> : <EmberStatePanel code="INVALID MEDIA ID" title="Title not found" body="That media identifier is not present in the server catalog." /> : null}</main></div>;
}
