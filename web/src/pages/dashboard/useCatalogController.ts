import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import { getMovie, getMovies, search as searchServer } from "../../api/movies";
import { getPlaybackSessions } from "../../api/playback";
import { getPlaybackSessionRevision, subscribePlaybackSessions } from "../../api/playbackSessionEvents";
import { getMediaPreferences, getRecommendations, setMediaPreference } from "../../api/recommendations";
import { getWatchlist } from "../../api/watchlist";
import { isCatalogView, type AppQueryState, type CatalogView } from "../../navigation/queryState";
import type { DiscoverMovie, MediaPreference, Movie, PlaybackSession, Profile, RecommendationFeed } from "../../types/api";
import { landingCacheGeneration, readLandingFeed, saveLandingFeed } from "./catalogLandingCache";

const FEED_PAGE_SIZE = 48;

export interface PreferenceOperation {
  status: "saving" | "checking" | "unknown" | "saved" | "error";
  desired: MediaPreference;
  uncertain: boolean;
  message: string;
}

interface CatalogBootstrapState {
  movies?: Movie[];
  sessions: PlaybackSession[];
  watchlist?: string[];
  preferences: Record<string, Exclude<MediaPreference, null>>;
  preferencesKnown?: boolean;
  preferenceKnownIds?: Record<string, true>;
  preferenceOperations?: Record<string, PreferenceOperation>;
}

const catalogBootstrapCache = new Map<string, CatalogBootstrapState>();

function updateCatalogBootstrap(profileId: string, update: Partial<CatalogBootstrapState>): void {
  const current = catalogBootstrapCache.get(profileId) ?? { sessions: [], preferences: {} };
  catalogBootstrapCache.set(profileId, { ...current, ...update });
}

function retirePreferenceOperation(operation: PreferenceOperation): PreferenceOperation {
  if (operation.status !== "saving" && operation.status !== "checking") return operation;
  return { ...operation, status: "unknown", uncertain: operation.status === "saving" || operation.uncertain, message: "The saved preference is unconfirmed. Check preference before making another change." };
}
function createPreferenceOwner(profileId: string) {
  const cached = catalogBootstrapCache.get(profileId);
  const operations = Object.fromEntries(Object.entries(cached?.preferenceOperations ?? {}).map(([id, operation]) => [id, retirePreferenceOperation(operation)]));
  return { profileId, active: true, requests: new Map<string, number>(), busy: new Set<string>(), reads: new Set<AbortController>(), known: cached?.preferencesKnown ?? false, knownIds: cached?.preferenceKnownIds ?? {}, operations };
}
type PreferenceOwner = ReturnType<typeof createPreferenceOwner>;

export interface CatalogController {
  catalogItems: Movie[];
  categoriesReady: boolean;
  watchlistKnown: boolean;
  watchlistStatus: "pending" | "success" | "error";
  watchlistError: string;
  retryWatchlist: () => void;
  searchStatus: "idle" | "pending" | "success" | "error";
  searchError: string;
  retrySearch: () => void;
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
  catalogError?: string;
  retryCatalog?: () => void;
  searching: boolean;
  error: string;
  setError: React.Dispatch<React.SetStateAction<string>>;
  detailsLoading: boolean;
  detailsError: string;
  retryDetails: () => void;
  preferences: Record<string, Exclude<MediaPreference, null>>;
  updatePreference: (movieId: string, preference: MediaPreference) => Promise<void>;
  preferencesKnown?: boolean;
  preferenceKnownIds?: Record<string, true>;
  preferenceOperations?: Record<string, PreferenceOperation>;
  checkPreference?: (movieId: string) => Promise<void>;
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

export function useCatalogController(profile: Profile, query: AppQueryState, automaticLanding = false): CatalogController {
  const initialFeedKey = isCatalogView(query.view) ? `${profile.id}:${query.view}:${(query.genre?.trim() || "recommended").toLocaleLowerCase()}` : "";
  const initialFeed = automaticLanding ? readLandingFeed(initialFeedKey) : null;
  const initialBootstrap = catalogBootstrapCache.get(profile.id);
  const [baseMovies, setBaseMovies] = useState<Movie[]>(() => initialBootstrap?.movies ?? []);
  const [sessions, setSessions] = useState<PlaybackSession[]>(() => initialBootstrap?.sessions ?? []);
  const [watchlistRead, setWatchlistRead] = useState(() => ({ profileId: profile.id, ids: initialBootstrap?.watchlist ?? [], known: initialBootstrap?.watchlist !== undefined, status: "pending" as "pending" | "success" | "error", error: "" }));
  const [watchlistVersion, setWatchlistVersion] = useState(0);
  const watchlistGeneration = useRef(0);
  const watchlistAbort = useRef<AbortController | null>(null);
  const [preferences, setPreferences] = useState<Record<string, Exclude<MediaPreference, null>>>(() => initialBootstrap?.preferences ?? {});
  const preferenceOwner = useRef(createPreferenceOwner(profile.id));
  if (preferenceOwner.current.profileId !== profile.id) preferenceOwner.current = createPreferenceOwner(profile.id);
  const [preferenceUi, setPreferenceUi] = useState(() => ({ profileId: profile.id, known: preferenceOwner.current.known, knownIds: preferenceOwner.current.knownIds, operations: preferenceOwner.current.operations }));
  const publishPreferenceUi = (owner: PreferenceOwner) => {
    if (!owner.active || preferenceOwner.current !== owner) return;
    setPreferenceUi({ profileId: owner.profileId, known: owner.known, knownIds: owner.knownIds, operations: owner.operations });
    updateCatalogBootstrap(owner.profileId, { preferencesKnown: owner.known, preferenceKnownIds: owner.knownIds, preferenceOperations: owner.operations });
  };
  const mergePreferenceRead = (owner: PreferenceOwner, incoming: Record<string, Exclude<MediaPreference, null>>, snapshot: Map<string, number>, pendingAtDispatch: ReadonlySet<string>, resolvedId?: string) => {
    if (!owner.active || preferenceOwner.current !== owner) return;
    owner.known = true;
    publishPreferenceUi(owner);
    setPreferences((current) => {
      if (!owner.active || preferenceOwner.current !== owner) return current;
      const next = { ...incoming };
      const guardedIds = new Set([...owner.requests.keys(), ...Object.keys(owner.operations)]);
      guardedIds.forEach((id) => {
        const operation = owner.operations[id];
        const protectedRead = id !== resolvedId && ((owner.requests.get(id) ?? 0) !== (snapshot.get(id) ?? 0) || pendingAtDispatch.has(id) || owner.busy.has(id) || operation?.status === "unknown");
        if (!protectedRead) return;
        if (current[id]) next[id] = current[id]; else delete next[id];
      });
      updateCatalogBootstrap(owner.profileId, { preferences: next });
      return next;
    });
  };
  useEffect(() => {
    const owner = preferenceOwner.current;
    owner.active = true;
    publishPreferenceUi(owner);
    return () => {
      owner.active = false;
      owner.reads.forEach((read) => read.abort());
      const operations = { ...owner.operations };
      owner.busy.forEach((id) => { const operation = operations[id]; if (operation) operations[id] = retirePreferenceOperation(operation); });
      updateCatalogBootstrap(owner.profileId, { preferenceOperations: operations });
    };
  }, [profile.id]);
  const [results, setResults] = useState<DiscoverMovie[]>([]);
  const [retainedMovies, setRetainedMovies] = useState<Record<string, Movie>>({});
  const [recommendation, setRecommendation] = useState<RecommendationFeed | null>(initialFeed);
  const [recommendationKey, setRecommendationKey] = useState(initialFeed ? initialFeedKey : "");
  const [validatedFeedKey, setValidatedFeedKey] = useState("");
  const [catalogLoading, setCatalogLoading] = useState(initialBootstrap?.movies === undefined);
  const [catalogError, setCatalogError] = useState("");
  const [catalogVersion, setCatalogVersion] = useState(0);
  const [recommendationLoading, setRecommendationLoading] = useState(automaticLanding && Boolean(initialFeedKey));
  const [recommendationLoadingMore, setRecommendationLoadingMore] = useState(false);
  const [searchRead, setSearchRead] = useState({ key: "", status: "idle" as "idle" | "pending" | "success" | "error", error: "" });
  const [searchVersion, setSearchVersion] = useState(0);
  const [catalogProfile, setCatalogProfile] = useState(profile.id);
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
  const enteringRequest = automaticLanding && activeFeedKey.current !== requestedKey;
  const displayedRecommendation = scope && enteringRequest ? readLandingFeed(requestedKey) : scope && recommendationKey !== requestedKey ? null : recommendation;

  useEffect(() => {
    const abort = new AbortController();
    const cached = catalogBootstrapCache.get(profile.id);
    setCatalogProfile(profile.id);
    if (cached) {
      setBaseMovies(cached.movies ?? []);
      setSessions(cached.sessions);
      setPreferences(cached.preferences);
      setCatalogLoading(cached.movies === undefined);
    } else {
      setBaseMovies([]);
      setSessions([]);
      setPreferences({});
      setCatalogLoading(true);
    }
    setError("");
    setCatalogError("");
    void getMovies(profile.id, abort.signal)
      .then((catalog) => {
        if (abort.signal.aborted) return;
        setBaseMovies(catalog);
        updateCatalogBootstrap(profile.id, { movies: catalog });
      })
      .catch((requestError: unknown) => {
        if (!abort.signal.aborted) {
          const message = requestError instanceof Error ? requestError.message : "The catalog could not be loaded.";
          setCatalogError(message); setError(message);
        }
      })
      .finally(() => { if (!abort.signal.aborted) setCatalogLoading(false); });
    const preferenceReadOwner = preferenceOwner.current;
    const preferenceRequestsAtDispatch = new Map(preferenceReadOwner.requests);
    const preferencePendingAtDispatch = new Set(preferenceReadOwner.busy);
    void getMediaPreferences(profile.id, abort.signal).then((preferenceMap) => {
      if (abort.signal.aborted || !preferenceReadOwner.active || preferenceOwner.current !== preferenceReadOwner) return;
      mergePreferenceRead(preferenceReadOwner, preferenceMap, preferenceRequestsAtDispatch, preferencePendingAtDispatch);
    }).catch(() => { /* A preference read cannot delay independent catalog data. */ });
    return () => abort.abort();
  }, [profile.id, catalogVersion]);

  useEffect(() => {
    const abort = new AbortController();
    watchlistAbort.current = abort;
    const generation = ++watchlistGeneration.current;
    const cached = catalogBootstrapCache.get(profile.id)?.watchlist;
    setWatchlistRead((current) => ({ profileId: profile.id, ids: current.profileId === profile.id ? current.ids : cached ?? [], known: current.profileId === profile.id ? current.known : cached !== undefined, status: "pending", error: "" }));
    void getWatchlist(profile.id, abort.signal).then((ids) => {
      if (abort.signal.aborted || generation !== watchlistGeneration.current) return;
      setWatchlistRead({ profileId: profile.id, ids, known: true, status: "success", error: "" });
      updateCatalogBootstrap(profile.id, { watchlist: ids });
    }).catch((requestError: unknown) => {
      if (abort.signal.aborted || generation !== watchlistGeneration.current) return;
      setWatchlistRead((current) => ({ ...current, status: "error", error: requestError instanceof Error ? requestError.message : "Your saved list could not be loaded." }));
    });
    return () => abort.abort();
  }, [profile.id, watchlistVersion]);

  const setWatchlist = useCallback<React.Dispatch<React.SetStateAction<string[]>>>((update) => {
    // The toggle response is newer authority than any already-started GET.
    watchlistGeneration.current += 1;
    watchlistAbort.current?.abort();
    setWatchlistRead((current) => {
      const ids = typeof update === "function" ? update(current.profileId === profile.id ? current.ids : []) : update;
      updateCatalogBootstrap(profile.id, { watchlist: ids });
      return { profileId: profile.id, ids, known: true, status: "success", error: "" };
    });
  }, [profile.id]);
  const retryWatchlist = useCallback(() => setWatchlistVersion((value) => value + 1), []);

  useEffect(() => {
    let disposed = false;
    let request: AbortController | null = null;
    const refresh = () => {
      request?.abort();
      const abort = new AbortController();
      request = abort;
      const revision = getPlaybackSessionRevision(profile.id);
      void getPlaybackSessions(profile.id, abort.signal).then((playback) => {
        if (disposed || abort.signal.aborted || revision !== getPlaybackSessionRevision(profile.id)) return;
        setSessions(playback);
        updateCatalogBootstrap(profile.id, { sessions: playback });
      }).catch(() => { /* Keep the last server-confirmed sessions when refresh fails. */ });
    };
    const unsubscribe = subscribePlaybackSessions(profile.id, refresh);
    refresh();
    return () => { disposed = true; unsubscribe(); request?.abort(); };
  }, [profile.id]);

  useEffect(() => {
    setRetainedMovies({});
    setDetailsError("");
  }, [profile.id]);

  useEffect(() => {
    pageAbort.current?.abort();
    pageAbort.current = null;
    setRecommendationLoadingMore(false);
    const previousKey = activeFeedKey.current;
    activeFeedKey.current = requestedKey;
    if (!scope) { setRecommendationLoading(false); setValidatedFeedKey(""); return; }
    const abort = new AbortController();
    const cacheOwner = landingCacheGeneration();
    if (recommendationKey !== requestedKey || (automaticLanding && previousKey !== requestedKey)) {
      const cached = automaticLanding ? readLandingFeed(requestedKey) : null;
      setRecommendation(cached);
      if (cached || automaticLanding) setRecommendationKey(requestedKey);
    }
    setValidatedFeedKey("");
    setRecommendationLoading(true);
    setRecommendationError("");
    getRecommendations({ profileId: profile.id, scope, category, limit: FEED_PAGE_SIZE, offset: 0, signal: abort.signal })
      .then((feed) => {
        if (abort.signal.aborted || activeFeedKey.current !== requestedKey) return;
        setRecommendation(feed); setRecommendationKey(requestedKey);
        setValidatedFeedKey(requestedKey);
        if (automaticLanding) saveLandingFeed(requestedKey, feed, cacheOwner);
      })
      .catch((requestError: unknown) => {
        if (!abort.signal.aborted && activeFeedKey.current === requestedKey) setRecommendationError(requestError instanceof Error ? requestError.message : "Recommendations could not be loaded.");
      })
      .finally(() => {
        if (!abort.signal.aborted && activeFeedKey.current === requestedKey) setRecommendationLoading(false);
      });
    return () => abort.abort();
  }, [automaticLanding, category, profile.id, refreshVersion, requestedKey, scope]);

  const searchKey = query.view === "search" && query.q ? `${profile.id}:${query.q}` : "";
  useEffect(() => {
    if (!searchKey || !query.q) { setSearchRead({ key: "", status: "idle", error: "" }); setResults([]); return; }
    const abort = new AbortController();
    setSearchRead({ key: searchKey, status: "pending", error: "" });
    setResults([]);
    searchServer(query.q, abort.signal).then((items) => {
      if (abort.signal.aborted) return;
      setResults(items);
      setSearchRead({ key: searchKey, status: "success", error: "" });
      setRetainedMovies((current) => {
        const next = { ...current };
        items.forEach((item) => { next[discoverId(item)] = discoverAsMovie(item); });
        return next;
      });
    }).catch((requestError: unknown) => {
      if (!abort.signal.aborted) setSearchRead({ key: searchKey, status: "error", error: requestError instanceof Error ? requestError.message : "Search could not be completed." });
    });
    return () => abort.abort();
  }, [query.q, searchKey, searchVersion]);
  const retrySearch = useCallback(() => setSearchVersion((value) => value + 1), []);
  const searchStatus = !searchKey ? "idle" : searchRead.key === searchKey ? searchRead.status : "pending";
  const searchError = searchRead.key === searchKey ? searchRead.error : "";
  const searching = searchStatus === "pending";
  const currentResults = searchRead.key === searchKey && searchStatus === "success" ? results : [];

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
    if (catalogProfile === profile.id) baseMovies.forEach(add);
    const ownedRecommendation = recommendationKey.startsWith(`${profile.id}:`) ? recommendation : null;
    ownedRecommendation?.items.forEach((item) => add(item.media));
    ownedRecommendation?.watchAgain.forEach((item) => add(item.media));
    ownedRecommendation?.vibeRails?.forEach((rail) => rail.items.forEach((item) => add(item.media)));
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
  }, [baseMovies, catalogProfile, profile.id, recommendation, recommendationKey, results, retainedMovies]);

  const mediaById = useMemo(() => new Map(movies.map((movie) => [movie.id, movie])), [movies]);
  const resolveMovie = useCallback((id: string) => mediaById.get(id) ?? null, [mediaById]);
  const movieItems = useMemo(() => movies.filter((movie) => movie.type === "movie"), [movies]);
  const seriesItems = useMemo(() => movies.filter((movie) => movie.type === "series"), [movies]);
  const categoriesReady = Boolean(scope && recommendation && recommendationKey === requestedKey && (!automaticLanding || (validatedFeedKey === requestedKey && !enteringRequest)));
  const categories = useMemo(() => categoriesReady ? recommendation!.categories.map((item) => item.value) : [], [categoriesReady, recommendation]);
  const genres = useMemo(() => recommendation?.categories.filter((item) => item.value !== "recommended" && item.value !== "all").map((item) => item.label) ?? [], [recommendation]);
  const continueWatching = useMemo(() => {
    const seen = new Set<string>();
    return [...sessions]
      .filter((session) => !session.isFinished && (session.completionRate < 0.95 || (mediaById.get(session.movieId)?.type === "movie" && !session.episodeId)))
      .sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt))
      .map((session) => mediaById.get(session.movieId))
      .filter((movie): movie is Movie => Boolean(movie))
      .filter((movie) => !seen.has(movie.id) && Boolean(seen.add(movie.id)));
  }, [mediaById, sessions]);
  const watchlist = watchlistRead.profileId === profile.id ? watchlistRead.ids : [];
  const watchlistKnown = watchlistRead.profileId === profile.id && watchlistRead.known;
  const watchlistStatus = watchlistRead.profileId === profile.id ? watchlistRead.status : "pending";
  const watchlistError = watchlistRead.profileId === profile.id ? watchlistRead.error : "";
  const watchlistItems = useMemo(() => watchlist.map((id) => mediaById.get(id)).filter((movie): movie is Movie => Boolean(movie)), [mediaById, watchlist]);
  useEffect(() => {
    if (!watchlistKnown || catalogLoading) return;
    const missing = watchlist.filter((id) => !mediaById.has(id));
    if (!missing.length) return;
    const abort = new AbortController();
    void Promise.allSettled(missing.map(async (id) => {
      const movie = await getMovie(id, abort.signal);
      if (!abort.signal.aborted) setRetainedMovies((current) => ({ ...current, [movie.id]: movie }));
    }));
    return () => abort.abort();
    // Lookup only when the saved roster/catalog settles or the user retries;
    // a failed lookup must not create a render-driven retry loop.
  }, [catalogLoading, profile.id, watchlistRead.ids, watchlistKnown, watchlistVersion]);

  const recommendationHasMore = Boolean(recommendation && recommendation.items.length < recommendation.total);
  const recommendationRefreshing = recommendationLoading && Boolean(recommendation) && recommendationKey === requestedKey;

  const retryRecommendations = useCallback(() => setRefreshVersion((value) => value + 1), []);
  const retryDetails = useCallback(() => setDetailsVersion((value) => value + 1), []);
  const retryCatalog = useCallback(() => setCatalogVersion((value) => value + 1), []);
  const refreshRecommendations = retryRecommendations;
  const reconcilePreference = async (owner: PreferenceOwner, movieId: string, request: number, uncertain: boolean, failureMessage: string) => {
    const owned = () => owner.active && preferenceOwner.current === owner && owner.requests.get(movieId) === request;
    if (!owned()) return;
    const desired = owner.operations[movieId].desired;
    owner.operations = { ...owner.operations, [movieId]: { status: "checking", desired, uncertain, message: "Checking the saved preference…" } };
    publishPreferenceUi(owner);
    const abort = new AbortController();
    owner.reads.add(abort);
    const snapshot = new Map(owner.requests);
    const pendingAtDispatch = new Set(owner.busy);
    try {
      const incoming = await getMediaPreferences(owner.profileId, abort.signal);
      if (!owned() || abort.signal.aborted) return;
      const observed = incoming[movieId] ?? null;
      if (uncertain && observed !== desired) {
        owner.operations = { ...owner.operations, [movieId]: { status: "unknown", desired, uncertain, message: "The update is still unconfirmed. Check preference before making another change." } };
        mergePreferenceRead(owner, incoming, snapshot, pendingAtDispatch);
        return;
      }
      owner.knownIds = { ...owner.knownIds, [movieId]: true };
      owner.operations = { ...owner.operations, [movieId]: { status: uncertain ? "saved" : "error", desired, uncertain, message: uncertain ? "Preference checked. The saved preference is confirmed." : `${failureMessage} The current saved preference was checked.` } };
      mergePreferenceRead(owner, incoming, snapshot, pendingAtDispatch, movieId);
      setRefreshVersion((value) => value + 1);
    } catch {
      if (!owned() || abort.signal.aborted) return;
      owner.operations = { ...owner.operations, [movieId]: { status: "unknown", desired, uncertain, message: "The saved preference is unavailable. Check preference before making another change." } };
      publishPreferenceUi(owner);
    } finally { owner.reads.delete(abort); }
  };
  const checkPreference = async (movieId: string) => {
    const owner = preferenceOwner.current;
    const operation = owner.operations[movieId];
    if (!owner.active || owner.profileId !== profile.id || owner.busy.has(movieId) || operation?.status !== "unknown") return;
    const request = (owner.requests.get(movieId) ?? 0) + 1;
    owner.requests.set(movieId, request); owner.busy.add(movieId);
    try { await reconcilePreference(owner, movieId, request, operation.uncertain, "The preference change was not saved."); }
    finally { owner.busy.delete(movieId); }
  };
  const updatePreference = useCallback(async (movieId: string, preference: MediaPreference) => {
    const owner = preferenceOwner.current;
    if (!owner.active || owner.profileId !== profile.id || owner.busy.has(movieId) || owner.operations[movieId]?.status === "unknown") return;
    const request = (owner.requests.get(movieId) ?? 0) + 1;
    owner.requests.set(movieId, request); owner.busy.add(movieId);
    const owned = () => owner.active && preferenceOwner.current === owner && owner.requests.get(movieId) === request;
    owner.operations = { ...owner.operations, [movieId]: { status: "saving", desired: preference, uncertain: false, message: "Saving preference…" } };
    publishPreferenceUi(owner);
    setError("");
    setPreferences((current) => {
      if (!owned()) return current;
      const next = { ...current }; if (preference) next[movieId] = preference; else delete next[movieId];
      updateCatalogBootstrap(owner.profileId, { preferences: next });
      return next;
    });
    try {
      await setMediaPreference(owner.profileId, movieId, preference);
      if (!owned()) return;
      owner.knownIds = { ...owner.knownIds, [movieId]: true };
      owner.operations = { ...owner.operations, [movieId]: { status: "saved", desired: preference, uncertain: false, message: "Preference saved." } };
      publishPreferenceUi(owner);
      setRefreshVersion((value) => value + 1);
    } catch (requestError) {
      if (!owned()) return;
      const uncertain = (requestError instanceof ApiError && requestError.status === 0) || requestError instanceof SyntaxError;
      await reconcilePreference(owner, movieId, request, uncertain, requestError instanceof Error ? requestError.message : "The preference change was not saved.");
      if (owned() && owner.operations[movieId]?.status !== "saved") throw requestError;
    } finally { owner.busy.delete(movieId); }
  }, [profile.id]);
  const loadMoreRecommendations = useCallback(async () => {
    if (!scope || !recommendation || recommendationLoading || recommendationLoadingMore || pageAbort.current
        || recommendationKey !== requestedKey || activeFeedKey.current !== requestedKey || recommendation.items.length >= recommendation.total) return;
    const key = requestedKey;
    const abort = new AbortController();
    pageAbort.current = abort;
    setRecommendationLoadingMore(true);
    setRecommendationError("");
    try {
      const page = await getRecommendations({ profileId: profile.id, scope, category, limit: FEED_PAGE_SIZE, offset: recommendation.items.length, signal: abort.signal });
      if (!abort.signal.aborted && activeFeedKey.current === key) setRecommendation((current) => current ? appendUnique(current, page) : page);
    } catch (requestError) {
      if (!abort.signal.aborted && activeFeedKey.current === key) setRecommendationError(requestError instanceof Error ? requestError.message : "More recommendations could not be loaded.");
    } finally {
      if (pageAbort.current === abort) {
        pageAbort.current = null;
        if (!abort.signal.aborted && activeFeedKey.current === key) setRecommendationLoadingMore(false);
      }
    }
  }, [category, profile.id, recommendation, recommendationKey, recommendationLoading, recommendationLoadingMore, requestedKey, scope]);

  const loading = catalogLoading;
  return {
    catalogItems: catalogProfile === profile.id ? baseMovies : [], categoriesReady, watchlistKnown, watchlistStatus, watchlistError, retryWatchlist, searchStatus, searchError, retrySearch,
    movies, movieItems, seriesItems, continueWatching, sessions, watchlist, watchlistItems, setWatchlist,
    categories, genres, results: currentResults, recommendation: automaticLanding ? displayedRecommendation : scope && recommendationKey !== requestedKey ? null : recommendation, recommendationLoading: recommendationLoading || (enteringRequest && Boolean(scope)), recommendationRefreshing,
    recommendationLoadingMore, recommendationError: enteringRequest ? "" : recommendationError, recommendationHasMore, retryRecommendations,
    refreshRecommendations, loadMoreRecommendations, resolveMovie, loading, searching, error, setError,
    detailsLoading, detailsError, retryDetails, catalogError, retryCatalog,
    preferences: catalogProfile === profile.id ? preferences : catalogBootstrapCache.get(profile.id)?.preferences ?? {}, updatePreference,
    preferencesKnown: preferenceUi.profileId === profile.id && preferenceUi.known,
    preferenceKnownIds: preferenceUi.profileId === profile.id ? preferenceUi.knownIds : {},
    preferenceOperations: preferenceUi.profileId === profile.id ? preferenceUi.operations : {}, checkPreference,
  };
}
