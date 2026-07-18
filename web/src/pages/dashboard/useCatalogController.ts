import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getMovie, getMovies, search as searchServer } from "../../api/movies";
import { getPlaybackSessions } from "../../api/playback";
import { getRecommendations } from "../../api/recommendations";
import { getWatchlist } from "../../api/watchlist";
import { isCatalogView, type AppQueryState, type CatalogView } from "../../navigation/queryState";
import type { DiscoverMovie, Movie, PlaybackSession, Profile, RecommendationFeed } from "../../types/api";

const FEED_PAGE_SIZE = 48;

export interface CatalogController {
  movies: Movie[];
  movieItems: Movie[];
  seriesItems: Movie[];
  continueWatching: Movie[];
  sessions: PlaybackSession[];
  watchlist: string[];
  watchlistItems: Movie[];
  setWatchlist: React.Dispatch<React.SetStateAction<string[]>>;
  categories: string[];
  genres: string[];
  results: DiscoverMovie[];
  recommendation: RecommendationFeed | null;
  recommendationLoading: boolean;
  recommendationRefreshing: boolean;
  recommendationLoadingMore: boolean;
  recommendationError: string;
  recommendationHasMore: boolean;
  retryRecommendations: () => void;
  refreshRecommendations: () => void;
  loadMoreRecommendations: () => Promise<void>;
  resolveMovie: (id: string) => Movie | null;
  loading: boolean;
  searching: boolean;
  error: string;
  setError: React.Dispatch<React.SetStateAction<string>>;
  detailsLoading: boolean;
  detailsError: string;
  retryDetails: () => void;
}

function discoverId(result: DiscoverMovie): string {
  return result.type === "series" ? `tv_${result.tmdbId}` : `m_${result.tmdbId}`;
}

function discoverAsMovie(result: DiscoverMovie): Movie {
  return {
    id: discoverId(result), title: result.title, description: result.description,
    thumbnailUrl: result.thumbnailUrl, bannerUrl: result.bannerUrl, videoUrl: "",
    genres: result.genres, duration: result.duration, releaseYear: result.releaseYear,
    rating: result.rating, cast: result.cast, director: result.director, type: result.type,
    quality: "Source", languages: [], subtitles: [], voteAverage: result.voteAverage,
    voteCount: result.voteCount, skipMarkers: {}, episodes: null,
    source: result.source ?? "tmdb_cache", availability: result.availability ?? "cached",
    recommendationReasons: [],
    remoteThumbnailUrl: result.remoteThumbnailUrl ?? null,
    remoteBannerUrl: result.remoteBannerUrl ?? null,
    localThumbnailUrl: result.localThumbnailUrl ?? null,
    localBannerUrl: result.localBannerUrl ?? null,
    cacheState: result.cacheState ?? null,
  };
}

function appendUnique(current: RecommendationFeed, page: RecommendationFeed): RecommendationFeed {
  const seen = new Set(current.items.map((item) => item.media.id));
  const appended = page.items.filter((item) => {
    if (seen.has(item.media.id)) return false;
    seen.add(item.media.id);
    return true;
  });
  return {
    ...page,
    offset: 0,
    items: [...current.items, ...appended],
    watchAgain: current.watchAgain,
    categories: page.categories.length ? page.categories : current.categories,
  };
}

export function useCatalogController(profile: Profile, query: AppQueryState): CatalogController {
  const [baseMovies, setBaseMovies] = useState<Movie[]>([]);
  const [sessions, setSessions] = useState<PlaybackSession[]>([]);
  const [watchlist, setWatchlist] = useState<string[]>([]);
  const [results, setResults] = useState<DiscoverMovie[]>([]);
  const [retainedMovies, setRetainedMovies] = useState<Record<string, Movie>>({});
  const [recommendation, setRecommendation] = useState<RecommendationFeed | null>(null);
  const [recommendationKey, setRecommendationKey] = useState("");
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [recommendationLoading, setRecommendationLoading] = useState(false);
  const [recommendationLoadingMore, setRecommendationLoadingMore] = useState(false);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState("");
  const [recommendationError, setRecommendationError] = useState("");
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [detailsError, setDetailsError] = useState("");
  const [detailsVersion, setDetailsVersion] = useState(0);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const activeFeedKey = useRef("");
  const pageAbort = useRef<AbortController | null>(null);

  const scope: CatalogView | null = isCatalogView(query.view) ? query.view : null;
  const category = query.genre?.trim() || "recommended";
  const requestedKey = scope ? `${profile.id}:${scope}:${category.toLocaleLowerCase()}` : "";

  useEffect(() => {
    const abort = new AbortController();
    setCatalogLoading(true);
    setError("");
    Promise.all([getMovies(profile.id, abort.signal), getPlaybackSessions(profile.id, abort.signal), getWatchlist(profile.id, abort.signal)])
      .then(([catalog, playback, saved]) => {
        if (abort.signal.aborted) return;
        setBaseMovies(catalog); setSessions(playback); setWatchlist(saved);
      })
      .catch((requestError: unknown) => {
        if (!abort.signal.aborted) setError(requestError instanceof Error ? requestError.message : "The catalog could not be loaded.");
      })
      .finally(() => { if (!abort.signal.aborted) setCatalogLoading(false); });
    return () => abort.abort();
  }, [profile.id]);

  useEffect(() => {
    setRetainedMovies({});
    setDetailsError("");
  }, [profile.id]);

  useEffect(() => {
    if (!scope) return;
    const abort = new AbortController();
    pageAbort.current?.abort();
    pageAbort.current = null;
    activeFeedKey.current = requestedKey;
    if (recommendationKey !== requestedKey) setRecommendation(null);
    setRecommendationLoading(true);
    setRecommendationError("");
    getRecommendations({ profileId: profile.id, scope, category, limit: FEED_PAGE_SIZE, offset: 0, signal: abort.signal })
      .then((feed) => {
        if (abort.signal.aborted || activeFeedKey.current !== requestedKey) return;
        setRecommendation(feed); setRecommendationKey(requestedKey);
      })
      .catch((requestError: unknown) => {
        if (!abort.signal.aborted && activeFeedKey.current === requestedKey) setRecommendationError(requestError instanceof Error ? requestError.message : "Recommendations could not be loaded.");
      })
      .finally(() => {
        if (!abort.signal.aborted && activeFeedKey.current === requestedKey) setRecommendationLoading(false);
      });
    return () => abort.abort();
  }, [category, profile.id, refreshVersion, requestedKey, scope]);

  useEffect(() => {
    if (query.view !== "search" || !query.q) { setSearching(false); return; }
    const abort = new AbortController();
    setSearching(true);
    setError("");
    searchServer(query.q, abort.signal)
      .then((items) => {
        if (abort.signal.aborted) return;
        setResults(items);
        setRetainedMovies((current) => {
          const next = { ...current };
          items.forEach((item) => { next[discoverId(item)] = discoverAsMovie(item); });
          return next;
        });
      })
      .catch((requestError: unknown) => { if (!abort.signal.aborted) setError(requestError instanceof Error ? requestError.message : "Search failed."); })
      .finally(() => { if (!abort.signal.aborted) setSearching(false); });
    return () => abort.abort();
  }, [query.q, query.view]);

  useEffect(() => {
    if (query.view !== "details" || !query.media) { setDetailsLoading(false); setDetailsError(""); return; }
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;
    setDetailsLoading(true);
    setDetailsError("");
    const refresh = async () => {
      try {
        const movie = await getMovie(query.media!, abort.signal);
        if (abort.signal.aborted) return;
        setRetainedMovies((current) => ({ ...current, [movie.id]: movie }));
        setDetailsLoading(false);
        if (movie.cacheState === "queued" || movie.cacheState === "caching") {
          const delays = [1000, 2000, 4000, 8000, 15000, 30000];
          timer = setTimeout(() => { attempt += 1; void refresh(); }, delays[Math.min(attempt, delays.length - 1)]);
        }
      } catch (requestError) {
        if (!abort.signal.aborted) {
          setDetailsLoading(false);
          setDetailsError(requestError instanceof Error ? requestError.message : "Media details could not be loaded.");
        }
      }
    };
    void refresh();
    return () => { abort.abort(); if (timer) clearTimeout(timer); };
  }, [detailsVersion, query.media, query.view]);

  const movies = useMemo(() => {
    const ordered: Movie[] = [];
    const byId = new Map<string, Movie>();
    const add = (movie: Movie) => {
      const existing = byId.get(movie.id);
      if (!existing) { byId.set(movie.id, movie); ordered.push(movie); return; }
      const merged = { ...movie, ...existing, source: movie.source ?? existing.source, availability: movie.availability ?? existing.availability, recommendationScore: movie.recommendationScore ?? existing.recommendationScore, recommendationReasons: movie.recommendationReasons ?? existing.recommendationReasons };
      byId.set(movie.id, merged);
      const index = ordered.findIndex((item) => item.id === movie.id);
      if (index >= 0) ordered[index] = merged;
    };
    baseMovies.forEach(add);
    recommendation?.items.forEach((item) => add(item.media));
    recommendation?.watchAgain.forEach((item) => add(item.media));
    results.forEach((result) => { if (!byId.has(discoverId(result))) add(discoverAsMovie(result)); });
    Object.values(retainedMovies).forEach((movie) => {
      const existing = byId.get(movie.id);
      if (!existing) { byId.set(movie.id, movie); ordered.push(movie); return; }
      const merged = { ...existing, ...movie };
      byId.set(movie.id, merged);
      const index = ordered.findIndex((item) => item.id === movie.id);
      if (index >= 0) ordered[index] = merged;
    });
    return ordered;
  }, [baseMovies, recommendation, results, retainedMovies]);

  const mediaById = useMemo(() => new Map(movies.map((movie) => [movie.id, movie])), [movies]);
  const resolveMovie = useCallback((id: string) => mediaById.get(id) ?? null, [mediaById]);
  const movieItems = useMemo(() => movies.filter((movie) => movie.type === "movie"), [movies]);
  const seriesItems = useMemo(() => movies.filter((movie) => movie.type === "series"), [movies]);
  const categories = useMemo(() => recommendation?.categories.map((item) => item.value) ?? [], [recommendation]);
  const genres = useMemo(() => recommendation?.categories.filter((item) => item.value !== "recommended" && item.value !== "all").map((item) => item.label) ?? [], [recommendation]);
  const continueWatching = useMemo(() => {
    const seen = new Set<string>();
    return [...sessions]
      .filter((session) => !session.isFinished && session.completionRate < 0.95)
      .sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt))
      .map((session) => mediaById.get(session.movieId))
      .filter((movie): movie is Movie => Boolean(movie))
      .filter((movie) => !seen.has(movie.id) && Boolean(seen.add(movie.id)));
  }, [mediaById, sessions]);
  const watchlistItems = useMemo(() => watchlist.map((id) => mediaById.get(id)).filter((movie): movie is Movie => Boolean(movie)), [mediaById, watchlist]);
  const recommendationHasMore = Boolean(recommendation && recommendation.items.length < recommendation.total);
  const recommendationRefreshing = recommendationLoading && Boolean(recommendation) && recommendationKey === requestedKey;

  const retryRecommendations = useCallback(() => setRefreshVersion((value) => value + 1), []);
  const retryDetails = useCallback(() => setDetailsVersion((value) => value + 1), []);
  const refreshRecommendations = retryRecommendations;
  const loadMoreRecommendations = useCallback(async () => {
    if (!scope || !recommendation || recommendationLoadingMore || recommendation.items.length >= recommendation.total) return;
    const key = requestedKey;
    const abort = new AbortController();
    pageAbort.current?.abort();
    pageAbort.current = abort;
    setRecommendationLoadingMore(true);
    setRecommendationError("");
    try {
      const page = await getRecommendations({ profileId: profile.id, scope, category, limit: FEED_PAGE_SIZE, offset: recommendation.items.length, signal: abort.signal });
      if (!abort.signal.aborted && activeFeedKey.current === key) setRecommendation((current) => current ? appendUnique(current, page) : page);
    } catch (requestError) {
      if (!abort.signal.aborted && activeFeedKey.current === key) setRecommendationError(requestError instanceof Error ? requestError.message : "More recommendations could not be loaded.");
    } finally {
      if (!abort.signal.aborted && activeFeedKey.current === key) setRecommendationLoadingMore(false);
    }
  }, [category, profile.id, recommendation, recommendationLoadingMore, requestedKey, scope]);

  const loading = catalogLoading || Boolean(scope && recommendationLoading && !recommendation);
  return {
    movies, movieItems, seriesItems, continueWatching, sessions, watchlist, watchlistItems, setWatchlist,
    categories, genres, results, recommendation, recommendationLoading, recommendationRefreshing,
    recommendationLoadingMore, recommendationError, recommendationHasMore, retryRecommendations,
    refreshRecommendations, loadMoreRecommendations, resolveMovie, loading, searching, error, setError,
    detailsLoading, detailsError, retryDetails,
  };
}
