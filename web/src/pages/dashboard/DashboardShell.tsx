import React, { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getMovies, search as searchServer } from "../../api/movies";
import { getPlaybackSessions } from "../../api/playback";
import { getWatchlist } from "../../api/watchlist";
import { MediaArtwork } from "../../components/media/MediaArtwork";
import { MediaCard } from "../../components/media/MediaCard";
import { MediaRow } from "../../components/media/MediaRow";
import { Button } from "../../components/ui/Button";
import { useAuthStore } from "../../stores/authStore";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import { AuroraBackground } from "../../themes/aurora/AuroraBackground";
import { CinemaBackground } from "../../themes/cinema/CinemaBackground";
import { EmberBackground } from "../../themes/ember/EmberBackground";
import { GeminiBackground } from "../../themes/gemini/GeminiBackground";
import type { DiscoverMovie, Movie, PlaybackSession } from "../../types/api";
import { avatarBackground, isPlayableMovie } from "../../utils/media";
import { DetailsRouter } from "../details/DetailsRouter";
import { ServerDownloads } from "./ServerDownloads";

type Tab = "home" | "movies" | "series" | "downloads";

function ThemeBackground() {
  const theme = useThemeStore((state) => state.activeTheme);
  if (theme === "aurora") return <AuroraBackground />;
  if (theme === "cinema") return <CinemaBackground />;
  if (theme === "gemini") return <GeminiBackground />;
  return <EmberBackground />;
}

export function DashboardShell() {
  const navigate = useNavigate();
  const activeProfile = useProfileStore((state) => state.activeProfile)!;
  const clearProfile = useProfileStore((state) => state.clearProfile);
  const isAdmin = useProfileStore((state) => state.isAdmin);
  const logout = useAuthStore((state) => state.logout);
  const theme = useThemeStore((state) => state.activeTheme);
  const [tab, setTab] = useState<Tab>("home");
  const [movies, setMovies] = useState<Movie[]>([]);
  const [sessions, setSessions] = useState<PlaybackSession[]>([]);
  const [watchlist, setWatchlist] = useState<string[]>([]);
  const [selected, setSelected] = useState<Movie | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<DiscoverMovie[]>([]);
  const [genre, setGenre] = useState("All");

  useEffect(() => {
    let active = true;
    setLoading(true);
    Promise.all([getMovies(), getPlaybackSessions(activeProfile.id), getWatchlist(activeProfile.id)])
      .then(([catalog, playback, saved]) => {
        if (!active) return;
        setMovies(catalog);
        setSessions(playback);
        setWatchlist(saved);
        setError("");
      })
      .catch((requestError: unknown) => {
        if (active) setError(requestError instanceof Error ? requestError.message : "The catalog could not be loaded.");
      })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [activeProfile.id]);

  const featured = movies.find(isPlayableMovie) ?? movies[0] ?? null;
  const movieItems = movies.filter((movie) => movie.type === "movie");
  const seriesItems = movies.filter((movie) => movie.type === "series");
  const genres = useMemo(() => ["All", ...Array.from(new Set(movies.flatMap((movie) => movie.genres))).sort()], [movies]);
  const tabItems = (tab === "series" ? seriesItems : movieItems).filter((movie) => genre === "All" || movie.genres.includes(genre));

  const submitSearch = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!query.trim()) { setResults([]); return; }
    setSearching(true);
    try { setResults(await searchServer(query.trim())); }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : "Search failed."); }
    finally { setSearching(false); }
  };

  const selectSearchResult = (result: DiscoverMovie) => {
    const localId = result.type === "series" ? `tv_${result.tmdbId}` : `m_${result.tmdbId}`;
    const local = movies.find((movie) => movie.id === localId);
    if (local) setSelected(local);
  };

  return (
    <div className="relative min-h-screen bg-[var(--bg-body)] text-[var(--text-primary)]" data-theme={theme}>
      <ThemeBackground />
      <header className="sticky top-0 z-40 border-b border-[var(--glass-border)] bg-[color:var(--bg-body)]/85 px-5 backdrop-blur-xl">
        <div className="mx-auto flex h-18 max-w-7xl items-center gap-5">
          <button className="font-[family-name:var(--font-headline)] text-lg font-bold tracking-wider" onClick={() => setTab("home")}>STREAMHOME</button>
          <nav className="hidden flex-1 items-center gap-2 md:flex" aria-label="Catalog">
            {(["home", "movies", "series", "downloads"] as Tab[]).map((item) => (
              <button key={item} className={`rounded px-3 py-2 text-sm capitalize ${tab === item ? "bg-[var(--accent-container)] text-white" : "text-[var(--text-secondary)]"}`} onClick={() => { setTab(item); setResults([]); }}>{item}</button>
            ))}
          </nav>
          <form className="ml-auto hidden items-center gap-2 lg:flex" onSubmit={submitSearch}>
            <input aria-label="Search server catalog" className="w-52 rounded border border-[var(--glass-border)] bg-black/20 px-3 py-2 text-sm" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search server" />
            <Button type="submit" size="sm" variant="secondary" disabled={searching}>{searching ? "…" : "Search"}</Button>
          </form>
          {isAdmin && <Button size="sm" variant="ghost" onClick={() => navigate("/admin")}>Admin</Button>}
          <button aria-label="Change profile" className="h-9 w-9 rounded-full" style={{ background: avatarBackground(activeProfile) }} onClick={() => { clearProfile(); navigate("/profiles"); }} />
          <Button size="sm" variant="ghost" onClick={logout}>Sign out</Button>
        </div>
        <nav className="flex gap-2 overflow-x-auto pb-3 md:hidden" aria-label="Catalog mobile">
          {(["home", "movies", "series", "downloads"] as Tab[]).map((item) => <button key={item} className="rounded border border-[var(--glass-border)] px-3 py-2 text-xs capitalize" onClick={() => setTab(item)}>{item}</button>)}
        </nav>
        <form className="flex gap-2 pb-3 lg:hidden" onSubmit={submitSearch}>
          <input aria-label="Search server catalog on mobile" className="min-w-0 flex-1 rounded border border-[var(--glass-border)] bg-black/20 px-3 py-2 text-sm" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search server" />
          <Button type="submit" size="sm" variant="secondary" disabled={searching}>{searching ? "…" : "Search"}</Button>
        </form>
      </header>

      <main className="relative z-10">
        {error && <div className="mx-auto max-w-5xl p-6 text-center text-[var(--text-error)]">{error}</div>}
        {loading && <div className="grid min-h-[60vh] place-items-center text-[var(--text-muted)]">Loading server catalog…</div>}

        {!loading && results.length > 0 && (
          <section className="mx-auto max-w-7xl px-6 py-10">
            <div className="mb-6 flex items-center justify-between"><h1 className="text-3xl font-semibold">Server search results</h1><Button variant="ghost" onClick={() => setResults([])}>Close</Button></div>
            <div className="grid grid-cols-2 gap-5 md:grid-cols-4 lg:grid-cols-6">
              {results.map((result) => {
                const available = movies.some((movie) => movie.id === (result.type === "series" ? `tv_${result.tmdbId}` : `m_${result.tmdbId}`));
                return <button key={`${result.type}-${result.tmdbId}`} className="overflow-hidden rounded border border-[var(--glass-border)] text-left disabled:opacity-60" disabled={!available} onClick={() => selectSearchResult(result)}><MediaArtwork src={result.thumbnailUrl} alt={result.title} className="aspect-[2/3] w-full object-cover" /><div className="p-3 text-sm">{result.title}<div className="mt-1 text-xs text-[var(--text-muted)]">{available ? "Available" : "Unavailable on server"}</div></div></button>;
              })}
            </div>
          </section>
        )}

        {!loading && results.length === 0 && tab === "home" && (
          <div className="flex flex-col gap-12 pb-16">
            {featured && <section className="relative min-h-[58vh] overflow-hidden"><MediaArtwork src={featured.bannerUrl || featured.thumbnailUrl} alt={featured.title} className="absolute inset-0 h-full w-full object-cover opacity-55" /><div className="absolute inset-0 bg-gradient-to-t from-[var(--bg-body)] via-[var(--bg-body)]/30 to-transparent" /><div className="relative mx-auto flex min-h-[58vh] max-w-7xl items-end px-6 pb-14"><div className="max-w-2xl"><p className="text-sm uppercase tracking-widest text-[var(--text-muted)]">{featured.type}</p><h1 className="mt-3 text-5xl font-bold">{featured.title}</h1>{featured.description && <p className="mt-4 line-clamp-3 text-[var(--text-secondary)]">{featured.description}</p>}<div className="mt-6 flex gap-3"><Button onClick={() => isPlayableMovie(featured) && featured.type === "movie" ? navigate(`/watch/${featured.id}`) : setSelected(featured)} disabled={!isPlayableMovie(featured)}>{featured.type === "series" ? "Choose episode" : "Play"}</Button><Button variant="secondary" onClick={() => setSelected(featured)}>Details</Button></div></div></div></section>}
            {!movies.length && <div className="py-24 text-center text-[var(--text-muted)]">The server catalog is empty.</div>}
            <MediaRow title="Continue watching" items={sessions.map((session) => movies.find((movie) => movie.id === session.movieId)).filter((movie): movie is Movie => Boolean(movie))} playbackSessions={sessions} onSelect={setSelected} />
            <MediaRow title="Movies" items={movieItems} playbackSessions={sessions} onSelect={setSelected} />
            <MediaRow title="Series" items={seriesItems} playbackSessions={sessions} onSelect={setSelected} />
          </div>
        )}

        {!loading && results.length === 0 && (tab === "movies" || tab === "series") && (
          <section className="mx-auto max-w-7xl px-6 py-10">
            <h1 className="text-4xl font-semibold capitalize">{tab}</h1>
            <div className="my-6 flex gap-2 overflow-x-auto">{genres.map((item) => <button key={item} className={`rounded-full border px-4 py-2 text-sm ${genre === item ? "border-[var(--accent-container)] bg-[var(--accent-container)] text-white" : "border-[var(--glass-border)]"}`} onClick={() => setGenre(item)}>{item}</button>)}</div>
            {!tabItems.length ? <p className="py-20 text-center text-[var(--text-muted)]">No {tab} are available from the server.</p> : <div className="flex flex-wrap gap-5">{tabItems.map((movie) => <MediaCard key={movie.id} movie={movie} playbackSession={sessions.find((session) => session.movieId === movie.id)} onSelect={setSelected} />)}</div>}
          </section>
        )}
        {!loading && results.length === 0 && tab === "downloads" && <ServerDownloads />}
      </main>

      {selected && <DetailsRouter movie={selected} onClose={() => setSelected(null)} isWatchlisted={watchlist.includes(selected.id)} onWatchlistChange={setWatchlist} />}
    </div>
  );
}
