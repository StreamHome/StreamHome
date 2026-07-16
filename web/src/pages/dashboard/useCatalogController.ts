import { useEffect, useMemo, useState } from "react";
import { getMovies, search as searchServer } from "../../api/movies";
import { getPlaybackSessions } from "../../api/playback";
import { getWatchlist } from "../../api/watchlist";
import type { AppQueryState } from "../../navigation/queryState";
import type { DiscoverMovie, Movie, PlaybackSession, Profile } from "../../types/api";
import { isPlayableMovie } from "../../utils/media";

export interface CatalogController {
  movies: Movie[];
  movieItems: Movie[];
  seriesItems: Movie[];
  continueWatching: Movie[];
  browseItems: Movie[];
  featured: Movie | null;
  sessions: PlaybackSession[];
  watchlist: string[];
  watchlistItems: Movie[];
  setWatchlist: React.Dispatch<React.SetStateAction<string[]>>;
  genres: string[];
  results: DiscoverMovie[];
  loading: boolean;
  searching: boolean;
  error: string;
  setError: React.Dispatch<React.SetStateAction<string>>;
}

export function useCatalogController(profile: Profile, query: AppQueryState): CatalogController {
  const [movies, setMovies] = useState<Movie[]>([]);
  const [sessions, setSessions] = useState<PlaybackSession[]>([]);
  const [watchlist, setWatchlist] = useState<string[]>([]);
  const [results, setResults] = useState<DiscoverMovie[]>([]);
  const [loading, setLoading] = useState(true);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    Promise.all([getMovies(), getPlaybackSessions(profile.id), getWatchlist(profile.id)])
      .then(([catalog, playback, saved]) => {
        if (!active) return;
        setMovies(catalog); setSessions(playback); setWatchlist(saved);
      })
      .catch((requestError: unknown) => { if (active) setError(requestError instanceof Error ? requestError.message : "The catalog could not be loaded."); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [profile.id]);

  useEffect(() => {
    if (query.view !== "search" || !query.q) { setResults([]); setSearching(false); return; }
    let active = true;
    setSearching(true);
    setError("");
    searchServer(query.q)
      .then((items) => { if (active) setResults(items); })
      .catch((requestError: unknown) => { if (active) setError(requestError instanceof Error ? requestError.message : "Search failed."); })
      .finally(() => { if (active) setSearching(false); });
    return () => { active = false; };
  }, [query.q, query.view]);

  const movieItems = useMemo(() => movies.filter((movie) => movie.type === "movie"), [movies]);
  const seriesItems = useMemo(() => movies.filter((movie) => movie.type === "series"), [movies]);
  const genres = useMemo(() => {
    const source = query.view === "movies" ? movieItems : query.view === "series" ? seriesItems : movies;
    return Array.from(new Set(source.flatMap((movie) => movie.genres))).sort();
  }, [movieItems, movies, query.view, seriesItems]);
  const browseItems = useMemo(() => {
    const source = query.view === "series" ? seriesItems : movieItems;
    return query.genre ? source.filter((movie) => movie.genres.some((genre) => genre.toLocaleLowerCase() === query.genre?.toLocaleLowerCase())) : source;
  }, [movieItems, query.genre, query.view, seriesItems]);
  const continueWatching = useMemo(() => sessions.map((session) => movies.find((movie) => movie.id === session.movieId)).filter((movie): movie is Movie => Boolean(movie)), [movies, sessions]);
  const watchlistItems = useMemo(() => watchlist.map((id) => movies.find((movie) => movie.id === id)).filter((movie): movie is Movie => Boolean(movie)), [movies, watchlist]);
  const featured = useMemo(() => movies.find(isPlayableMovie) ?? movies[0] ?? null, [movies]);

  return { movies, movieItems, seriesItems, continueWatching, browseItems, featured, sessions, watchlist, watchlistItems, setWatchlist, genres, results, loading, searching, error, setError };
}
