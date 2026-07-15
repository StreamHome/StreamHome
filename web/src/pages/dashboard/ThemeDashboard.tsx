import React, { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { MediaArtwork } from "../../components/media/MediaArtwork";
import { ProgressBar } from "../../components/ui/ProgressBar";
import { appUrl, type AppQueryState, type AppView } from "../../navigation/queryState";
import { useAuthStore } from "../../stores/authStore";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import type { DiscoverMovie, Movie, PlaybackSession } from "../../types/api";
import { getThemeDefinition } from "../../themes/application/themeRegistry";
import { completionFraction, isPlayableMovie } from "../../utils/media";
import { DetailsRouter } from "../details/DetailsRouter";
import { ServerDownloads } from "./ServerDownloads";
import type { CatalogController } from "./useCatalogController";

interface ThemeDashboardProps { query: AppQueryState; controller: CatalogController; }

function MediaCard({ movie, session, theme, onOpen }: { movie: Movie; session?: PlaybackSession; theme: string; onOpen: (movie: Movie) => void }) {
  const playable = isPlayableMovie(movie);
  return <button className="catalog-card" data-card-theme={theme} onClick={() => onOpen(movie)}><span className="catalog-card__art"><MediaArtwork src={movie.thumbnailUrl} alt={movie.title} className="h-full w-full object-cover" /><i aria-hidden="true" /></span><span className="catalog-card__copy"><strong>{movie.title}</strong><small>{playable ? `${movie.type} / ${movie.releaseYear || "catalogued"}` : "Unavailable on server"}</small></span>{session && <ProgressBar className="catalog-card__progress" progress={completionFraction(session.completionRate)} />}</button>;
}

function MediaCollection({ title, label, items, sessions, theme, onOpen }: { title: string; label?: string; items: Movie[]; sessions: PlaybackSession[]; theme: string; onOpen: (movie: Movie) => void }) {
  if (!items.length) return null;
  return <section className="catalog-collection"><header><div>{label && <p>{label}</p>}<h2>{title}</h2></div><span>{String(items.length).padStart(2, "0")} titles</span></header><div className="catalog-collection__rail">{items.map((movie) => <MediaCard key={movie.id} movie={movie} session={sessions.find((session) => session.movieId === movie.id)} theme={theme} onOpen={onOpen} />)}</div></section>;
}

function FeatureActions({ movie, onDetails, onPlay }: { movie: Movie; onDetails: () => void; onPlay: () => void }) {
  return <div className="feature-actions"><button className="feature-action feature-action--primary" disabled={!isPlayableMovie(movie)} onClick={movie.type === "series" ? onDetails : onPlay}>{movie.type === "series" ? "Select episode" : "Play now"}</button><button className="feature-action" onClick={onDetails}>View details</button></div>;
}

function FeaturedStage({ movie, variant, onDetails, onPlay }: { movie: Movie; variant: string; onDetails: () => void; onPlay: () => void }) {
  const art = <MediaArtwork src={movie.bannerUrl || movie.thumbnailUrl} alt={movie.title} className="h-full w-full object-cover" />;
  if (variant === "terminal") return <section className="feature-stage feature-stage--terminal"><div className="terminal-intro"><span><i />CATALOG ONLINE</span><p>PROFILE LIBRARY / SERVER INDEX</p></div><div className="terminal-feature"><div className="terminal-feature__copy"><small>FEATURED TRANSMISSION / {movie.type.toUpperCase()}</small><h1>{movie.title}</h1><p>{movie.description || "No server description is available for this title."}</p><div className="feature-meta"><span>{movie.releaseYear || "YEAR N/A"}</span><span>{movie.quality || "SOURCE"}</span><span>{movie.genres[0] || "UNCATEGORIZED"}</span></div><FeatureActions movie={movie} onDetails={onDetails} onPlay={onPlay} /></div><div className="terminal-feature__art">{art}<span>SERVER MEDIA</span></div></div></section>;
  if (variant === "editorial") return <section className="feature-stage feature-stage--editorial"><div className="editorial-art">{art}</div><div className="editorial-copy"><small>CURATED FOR THIS PROFILE</small><h1>{movie.title}</h1><p>{movie.description || "No server description is available for this title."}</p><FeatureActions movie={movie} onDetails={onDetails} onPlay={onPlay} /></div></section>;
  if (variant === "cinematic") return <section className="feature-stage feature-stage--cinematic"><div className="cinema-art">{art}</div><div className="cinema-shade" /><div className="cinema-copy"><small>{movie.type} / {movie.releaseYear || "server catalog"}</small><h1>{movie.title}</h1><p>{movie.description || "No server description is available for this title."}</p><FeatureActions movie={movie} onDetails={onDetails} onPlay={onPlay} /></div></section>;
  return <section className="feature-stage feature-stage--workspace"><div className="workspace-heading"><p>PROFILE WORKSPACE</p><h1>{movie.title}</h1><span>{movie.description || "No server description is available for this title."}</span><FeatureActions movie={movie} onDetails={onDetails} onPlay={onPlay} /></div><div className="workspace-art">{art}</div><div className="workspace-stat"><small>AVAILABLE FORMAT</small><strong>{movie.quality || "SOURCE"}</strong><span>{movie.languages.length} audio track{movie.languages.length === 1 ? "" : "s"}</span></div></section>;
}

function BrowseView({ query, controller, theme, onGenre, onOpen }: { query: AppQueryState; controller: CatalogController; theme: string; onGenre: (genre?: string) => void; onOpen: (movie: Movie) => void }) {
  return <section className="browse-view"><header className="browse-heading"><div><p>SERVER CATALOG / {query.view.toUpperCase()}</p><h1>{query.view === "series" ? "Series archive" : "Movie archive"}</h1></div><span>{controller.browseItems.length} available records</span></header><div className="genre-filter"><button data-active={!query.genre} onClick={() => onGenre()}>All</button>{controller.genres.map((genre) => <button key={genre} data-active={query.genre?.toLocaleLowerCase() === genre.toLocaleLowerCase()} onClick={() => onGenre(genre)}>{genre}</button>)}</div>{controller.browseItems.length ? <div className="browse-grid">{controller.browseItems.map((movie) => <MediaCard key={movie.id} movie={movie} session={controller.sessions.find((session) => session.movieId === movie.id)} theme={theme} onOpen={onOpen} />)}</div> : <EmptyState title={`No ${query.view} found`} body={query.genre ? "No server titles match this genre filter." : "The server has not catalogued titles for this view."} />}</section>;
}

function SearchView({ query, controller, theme, onOpen, onSearch }: { query: AppQueryState; controller: CatalogController; theme: string; onOpen: (movie: Movie) => void; onSearch: (query: string) => void }) {
  const [draft, setDraft] = useState(query.q ?? "");
  useEffect(() => setDraft(query.q ?? ""), [query.q]);
  const form = <form className="search-page-form" onSubmit={(event) => { event.preventDefault(); onSearch(draft.trim()); }}><input aria-label="Search server catalog" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="Title, film, or series" /><button type="submit">Search server</button></form>;
  if (!query.q) return <section className="browse-view search-view"><header className="browse-heading"><div><p>SERVER SEARCH</p><h1>Search the catalog</h1></div></header>{form}<EmptyState title="Awaiting a query" body="Search results are requested directly from the server." /></section>;
  if (controller.searching) return <LoadingState label={`Searching for “${query.q}”`} />;
  const localResults = controller.results.map((result) => ({ result, movie: resultToMovie(result, controller.movies) }));
  return <section className="browse-view search-view"><header className="browse-heading"><div><p>SERVER SEARCH</p><h1>Results for “{query.q}”</h1></div><span>{localResults.filter((item) => item.movie).length} available locally</span></header>{form}{!localResults.length ? <EmptyState title="No results" body="The server did not return matches for this query." /> : <div className="search-results">{localResults.map(({ result, movie }) => <button key={`${result.type}-${result.tmdbId}`} className="search-result" data-card-theme={theme} disabled={!movie} onClick={() => movie && onOpen(movie)}><MediaArtwork src={result.thumbnailUrl} alt={result.title} className="search-result__art" /><span><strong>{result.title}</strong><small>{movie ? "Available on server" : "Not available on server"}</small></span></button>)}</div>}</section>;
}

function resultToMovie(result: DiscoverMovie, movies: Movie[]) { const id = result.type === "series" ? `tv_${result.tmdbId}` : `m_${result.tmdbId}`; return movies.find((movie) => movie.id === id); }
function LoadingState({ label = "Loading server catalog" }: { label?: string }) { return <div className="catalog-state catalog-state--loading"><i /><p>{label}</p></div>; }
function EmptyState({ title, body }: { title: string; body: string }) { return <div className="catalog-state"><p>NO DATA</p><h2>{title}</h2><span>{body}</span></div>; }

export function ThemeDashboard({ query, controller }: ThemeDashboardProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const profile = useProfileStore((state) => state.activeProfile)!;
  const theme = useThemeStore((state) => state.activeTheme);
  const clearProfile = useProfileStore((state) => state.clearProfile);
  const logout = useAuthStore((state) => state.logout);
  const definition = getThemeDefinition(theme);
  const Background = definition.Background;
  const Navigation = definition.Navigation;
  const appNavigate = (view: AppView, options: Parameters<typeof appUrl>[2] = {}) => navigate(appUrl(profile.id, view, options), { state: { fromApp: true, previous: location.search } });
  const openDetails = (movie: Movie) => appNavigate("details", { media: movie.id });
  const openWatch = (movie: Movie) => appNavigate("watch", { media: movie.id });
  const selected = query.media ? controller.movies.find((movie) => movie.id === query.media) ?? null : null;

  const navigationProps = { profile, activeView: query.view, query: query.q, isAdmin: profile.id === "1", onView: (view: AppView) => appNavigate(view), onSearch: (q: string) => appNavigate("search", q ? { q } : {}), onProfiles: () => { clearProfile(); navigate("/profiles"); }, onAdmin: () => appNavigate("admin", { section: "account" }), onLogout: logout };

  return <div className={`theme-app ${definition.shellClass}`} data-theme={theme} data-view={query.view}><Background /><Navigation {...navigationProps} /><main className="theme-main">{controller.error && <div className="catalog-error" role="alert">{controller.error}</div>}{controller.loading ? <LoadingState /> : query.view === "home" ? <div className="home-view">{controller.featured ? <FeaturedStage movie={controller.featured} variant={definition.heroVariant} onDetails={() => openDetails(controller.featured!)} onPlay={() => openWatch(controller.featured!)} /> : <EmptyState title="The catalog is empty" body="No media records were returned by the server." />}<div className="home-collections"><MediaCollection label="RESUME INDEX" title="Continue watching" items={controller.continueWatching} sessions={controller.sessions} theme={definition.cardVariant} onOpen={openDetails} /><MediaCollection label="FEATURE ARCHIVE" title="Movies" items={controller.movieItems} sessions={controller.sessions} theme={definition.cardVariant} onOpen={openDetails} /><MediaCollection label="EPISODIC ARCHIVE" title="Series" items={controller.seriesItems} sessions={controller.sessions} theme={definition.cardVariant} onOpen={openDetails} /></div></div> : query.view === "movies" || query.view === "series" ? <BrowseView query={query} controller={controller} theme={definition.cardVariant} onGenre={(genre) => appNavigate(query.view, genre ? { genre } : {})} onOpen={openDetails} /> : query.view === "search" ? <SearchView query={query} controller={controller} theme={definition.cardVariant} onOpen={openDetails} onSearch={(q) => appNavigate("search", q ? { q } : {})} /> : query.view === "downloads" ? <ServerDownloads /> : query.view === "details" ? selected ? <DetailsRouter movie={selected} onClose={() => appNavigate("home")} isWatchlisted={controller.watchlist.includes(selected.id)} onWatchlistChange={controller.setWatchlist} /> : <EmptyState title="Title not found" body="That media identifier is not present in the server catalog." /> : null}</main></div>;
}
