import React, { useState, useEffect } from "react";
import { motion } from "motion/react";
import { 
  Play, Plus, Check, Clock, ThumbsUp, ThumbsDown, Share2, Download, Trash2, Database,
  ArrowLeft, Tv, Star, User, Calendar, Sparkles, Cpu, Layers, Activity 
} from "lucide-react";
import { Movie, Profile, PlaybackSession, Episode } from "../types";

interface ThemeDetailsPageProps {
  activeProfile: Profile;
  selectedMovieForDetails: Movie;
  setSelectedMovieForDetails: (movie: Movie | null) => void;
  tmdbJson: any;
  setTmdbJson: (val: any) => void;
  movies: Movie[];
  onPlayMovie: (movie: Movie) => void;
  continueWatching: PlaybackSession[];
  watchlist: string[];
  toggleWatchlist: (movieId: string) => void;
  selectedSeason: number;
  setSelectedSeason: (season: number) => void;
  onAddDownload?: (tmdbId: string, mediaType: string) => void;
  apiBaseUrl: string;
  isFetchingEpisodes?: boolean;
}

export default function ThemeDetailsPage({
  activeProfile,
  selectedMovieForDetails,
  setSelectedMovieForDetails,
  tmdbJson,
  setTmdbJson,
  movies,
  onPlayMovie,
  continueWatching,
  watchlist,
  toggleWatchlist,
  selectedSeason,
  setSelectedSeason,
  onAddDownload,
  apiBaseUrl,
  isFetchingEpisodes = false,
}: ThemeDetailsPageProps) {
  const [activeTab, setActiveTab] = useState<"related" | "details" | "episodes">("episodes");
  const [likeStatus, setLikeStatus] = useState<"liked" | "disliked" | null>(null);
  const [browserCachedItems, setBrowserCachedItems] = useState<string[]>([]);
  const [browserDownloadingProgress, setBrowserDownloadingProgress] = useState<Record<string, number>>({});

  const isDownloaded = selectedMovieForDetails ? browserCachedItems.includes(selectedMovieForDetails.id) : false;
  const isDownloading = selectedMovieForDetails ? browserDownloadingProgress[selectedMovieForDetails.id] !== undefined : false;
  const downloadProgress = selectedMovieForDetails ? (browserDownloadingProgress[selectedMovieForDetails.id] || 0) : 0;

  const updateBrowserCachedStatus = async () => {
    try {
      const cache = await caches.open("stream-media-cache");
      const keys = await cache.keys();
      const cachedUrls = keys.map(k => k.url);
      
      const cachedIds: string[] = [];
      if (selectedMovieForDetails) {
        if (selectedMovieForDetails.type === "series") {
          selectedMovieForDetails.episodes?.forEach(ep => {
            if (ep.videoUrl && cachedUrls.some(u => {
              try { return u.endsWith(ep.videoUrl) || decodeURI(u).endsWith(ep.videoUrl); } catch { return u.endsWith(ep.videoUrl); }
            })) {
              cachedIds.push(ep.id);
            }
          });
        } else {
          if (selectedMovieForDetails.videoUrl && cachedUrls.some(u => {
            try { return u.endsWith(selectedMovieForDetails.videoUrl) || decodeURI(u).endsWith(selectedMovieForDetails.videoUrl); } catch { return u.endsWith(selectedMovieForDetails.videoUrl); }
          })) {
            cachedIds.push(selectedMovieForDetails.id);
          }
        }
      }
      setBrowserCachedItems(cachedIds);
    } catch (err) {
      console.warn("Failed to read cache keys:", err);
    }
  };

  useEffect(() => {
    updateBrowserCachedStatus();
  }, [selectedMovieForDetails?.id, selectedMovieForDetails?.episodes]);

  const handleDownloadToBrowser = async (itemId: string, videoUrl: string, title: string) => {
    if (!videoUrl) return;
    
    const absoluteUrl = videoUrl.startsWith("http") ? videoUrl : window.location.origin + videoUrl;
    setBrowserDownloadingProgress(prev => ({ ...prev, [itemId]: 1 }));
    
    try {
      const response = await fetch(absoluteUrl);
      if (!response.ok) throw new Error(`Fetch failed: ${response.status}`);
      
      const contentLength = response.headers.get("content-length");
      const totalBytes = contentLength ? parseInt(contentLength, 10) : 0;
      
      const reader = response.body?.getReader();
      if (!reader) throw new Error("ReadableStream not supported");
      
      let receivedBytes = 0;
      const chunks: Uint8Array[] = [];
      
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        
        chunks.push(value);
        receivedBytes += value.length;
        
        if (totalBytes > 0) {
          const pct = Math.round((receivedBytes / totalBytes) * 100);
          setBrowserDownloadingProgress(prev => ({ ...prev, [itemId]: pct }));
        }
      }
      
      const blob = new Blob(chunks, { type: response.headers.get("content-type") || "video/mp4" });
      const cache = await caches.open("stream-media-cache");
      await cache.put(absoluteUrl, new Response(blob, {
        headers: {
          "Content-Type": blob.type,
          "Content-Length": String(blob.size),
          "Accept-Ranges": "bytes"
        }
      }));
      
      const itemRegistryData: any = {
        id: itemId,
        title: title,
        videoUrl: videoUrl,
        type: selectedMovieForDetails?.type || "movie",
        description: selectedMovieForDetails?.description || "",
        thumbnailUrl: selectedMovieForDetails?.thumbnailUrl || "",
        duration: selectedMovieForDetails?.duration || "",
        releaseYear: selectedMovieForDetails?.releaseYear || 2026,
        genres: selectedMovieForDetails?.genres || [],
        rating: selectedMovieForDetails?.rating || "PG-13",
      };

      if (selectedMovieForDetails?.type === "series" && selectedMovieForDetails.episodes) {
        const ep = selectedMovieForDetails.episodes.find(e => e.id === itemId);
        if (ep) {
          itemRegistryData.title = `${selectedMovieForDetails.title}: S${ep.seasonNumber}E${ep.episodeNumber} - ${ep.title}`;
          itemRegistryData.activeEpisodeId = ep.id;
          itemRegistryData.activeEpisodeNumber = ep.episodeNumber;
          itemRegistryData.activeSeasonNumber = ep.seasonNumber;
          itemRegistryData.thumbnailUrl = ep.thumbnailUrl || selectedMovieForDetails.thumbnailUrl;
          itemRegistryData.duration = ep.duration;
        }
      }

      const registry = JSON.parse(localStorage.getItem("pwa_offline_registry") || "{}");
      registry[itemId] = itemRegistryData;
      localStorage.setItem("pwa_offline_registry", JSON.stringify(registry));
      
      setBrowserCachedItems(prev => [...prev, itemId]);
      
      if (selectedMovieForDetails?.thumbnailUrl) {
        const thumbAbs = selectedMovieForDetails.thumbnailUrl.startsWith("http") ? selectedMovieForDetails.thumbnailUrl : window.location.origin + selectedMovieForDetails.thumbnailUrl;
        try {
          const thumbRes = await fetch(thumbAbs);
          if (thumbRes.ok) await cache.put(thumbAbs, thumbRes.clone());
        } catch(e) {}
      }

      setBrowserDownloadingProgress(prev => {
        const copy = { ...prev };
        delete copy[itemId];
        return copy;
      });
    } catch (err) {
      console.error("Browser download failed:", err);
      alert(`Offline download failed: ${err instanceof Error ? err.message : String(err)}`);
      setBrowserDownloadingProgress(prev => {
        const copy = { ...prev };
        delete copy[itemId];
        return copy;
      });
    }
  };

  const handleRemoveFromBrowser = async (itemId: string, videoUrl: string) => {
    if (!videoUrl) return;
    const absoluteUrl = videoUrl.startsWith("http") ? videoUrl : window.location.origin + videoUrl;
    
    try {
      const cache = await caches.open("stream-media-cache");
      await cache.delete(absoluteUrl);
      
      const registry = JSON.parse(localStorage.getItem("pwa_offline_registry") || "{}");
      delete registry[itemId];
      localStorage.setItem("pwa_offline_registry", JSON.stringify(registry));
      
      setBrowserCachedItems(prev => prev.filter(id => id !== itemId));
    } catch (err) {
      console.error("Failed to delete local cache item:", err);
    }
  };

  const theme = activeProfile.theme || "netflix";

  // Map modal/source data
  const movieData = {
    id: tmdbJson?.id || selectedMovieForDetails.id,
    title: tmdbJson?.title || tmdbJson?.name || selectedMovieForDetails.title || "",
    releaseYear: tmdbJson?.releaseYear || tmdbJson?.release_year || (tmdbJson?.release_date ? new Date(tmdbJson.release_date).getFullYear() : null) || selectedMovieForDetails.releaseYear || 2026,
    rating: tmdbJson?.rating || tmdbJson?.content_rating || selectedMovieForDetails.rating || "PG-13",
    duration: tmdbJson?.duration || (tmdbJson?.runtime ? `${Math.floor(tmdbJson.runtime / 60)}h ${tmdbJson.runtime % 60}m` : selectedMovieForDetails.duration || "1h 45m"),
    userScore: tmdbJson?.userScore || 
               (tmdbJson?.voteAverage ? `${tmdbJson.voteAverage.toFixed(1)}/10` : 
               (tmdbJson?.vote_average ? `${tmdbJson.vote_average.toFixed(1)}/10` : 
               ((selectedMovieForDetails as any).voteAverage ? `${(selectedMovieForDetails as any).voteAverage.toFixed(1)}/10` : 
               ((selectedMovieForDetails as any).vote_average ? `${(selectedMovieForDetails as any).vote_average.toFixed(1)}/10` : "8.4/10")))),
    genres: tmdbJson?.genres?.map((g: any) => typeof g === "string" ? g : g.name) || selectedMovieForDetails.genres || [],
    overview: tmdbJson?.overview || tmdbJson?.description || selectedMovieForDetails.description || "",
    backdropUrl: tmdbJson?.backdropUrl || tmdbJson?.backdrop_path || selectedMovieForDetails.bannerUrl || selectedMovieForDetails.thumbnailUrl || "",
    posterUrl: tmdbJson?.posterUrl || tmdbJson?.poster_path || selectedMovieForDetails.thumbnailUrl || "",
    cast: tmdbJson?.cast || selectedMovieForDetails.cast || [],
    director: tmdbJson?.director || selectedMovieForDetails.director || "Unknown Director",
    type: selectedMovieForDetails.type || "movie"
  };

  const tmdbIdVal = (selectedMovieForDetails as any).tmdb_id || (selectedMovieForDetails as any).tmdbId || movieData.id;
  const isActuallyDownloaded = movies.some(
    (m) => m.id === selectedMovieForDetails?.id || (tmdbIdVal && (m.id === `m_${tmdbIdVal}` || m.id === `tv_${tmdbIdVal}`))
  );

  // Fetch detailed metadata from the backend
  useEffect(() => {
    if (!selectedMovieForDetails) {
      setTmdbJson(null);
      return;
    }

    const fetchTmdbDetails = async () => {
      const tmdbIdRaw = (selectedMovieForDetails as any).tmdb_id || (selectedMovieForDetails as any).tmdbId || selectedMovieForDetails.id;
      if (!tmdbIdRaw) return;
      const tmdbId = String(tmdbIdRaw).replace("discover_", "").replace("m_", "").replace("tv_", "");
      if (!tmdbId || isNaN(Number(tmdbId))) return;

      try {
        const typeStr = selectedMovieForDetails.type === "series" ? "tv" : "movie";
        console.log(`[ThemeDetailsPage] Fetching detailed TMDB metadata for ${typeStr} ID: ${tmdbId}`);
        const token = localStorage.getItem("stream_access_token");
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;
        const res = await fetch(`${apiBaseUrl}/tmdb/${typeStr}/${tmdbId}`, { headers });
        if (res.ok) {
          const detailedData = await res.json();
          console.log("[ThemeDetailsPage] Successfully loaded detailed TMDB metadata:", detailedData);
          setTmdbJson(detailedData);
        }
      } catch (err) {
        console.error("Failed to fetch detailed TMDB metadata:", err);
      }
    };

    setTmdbJson(null);
    fetchTmdbDetails();
  }, [selectedMovieForDetails?.id, apiBaseUrl]);

  // Default to "episodes" if series, otherwise "related"
  useEffect(() => {
    if (movieData.type === "series") {
      setActiveTab("episodes");
    } else {
      setActiveTab("related");
    }
    // Scroll window to top so the details page starts from the top on all themes
    window.scrollTo({ top: 0, behavior: "instant" as any });
  }, [selectedMovieForDetails]);

  // Generate episodes if series is chosen
  const seriesEpisodes: Episode[] = selectedMovieForDetails?.episodes || [];
  const seasons = Array.from(new Set(seriesEpisodes.map(ep => ep.seasonNumber))).sort((a, b) => a - b);
  const displaySeasons = seasons.length > 0 ? seasons : [1, 2];
  const activeSeason = selectedSeason;

  const currentEpisodes: Episode[] = seriesEpisodes.length > 0 
    ? seriesEpisodes.filter(ep => ep.seasonNumber === activeSeason)
    : [
        {
          id: `${movieData.id}_s1_e1`,
          episodeNumber: 1,
          seasonNumber: 1,
          title: "Awakening of the Grid",
          description: `In the premiere episode of ${movieData.title}, we are introduced to the core conflicts and the stunning universe.`,
          thumbnailUrl: movieData.posterUrl,
          videoUrl: selectedMovieForDetails?.videoUrl || "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/Sintel.mp4",
          duration: "10m 53s"
        },
        {
          id: `${movieData.id}_s1_e2`,
          episodeNumber: 2,
          seasonNumber: 1,
          title: "The Celestial Interface",
          description: "An unexpected revelation changes everything. The stakes rise as alliances are tested.",
          thumbnailUrl: movieData.posterUrl,
          videoUrl: selectedMovieForDetails?.videoUrl || "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/Sintel.mp4",
          duration: "14m 48s"
        },
        {
          id: `${movieData.id}_s2_e1`,
          episodeNumber: 1,
          seasonNumber: 2,
          title: "Neon Substrate Phase 2",
          description: "The second season kicks off with higher production values, deeper mysteries, and bigger threats.",
          thumbnailUrl: movieData.posterUrl,
          videoUrl: selectedMovieForDetails?.videoUrl || "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/Sintel.mp4",
          duration: "12m 14s"
        },
        {
          id: `${movieData.id}_s2_e2`,
          episodeNumber: 2,
          seasonNumber: 2,
          title: "Grid Resolution Protocol",
          description: `A shocking conclusion that sets up future adventures in the saga of ${movieData.title}.`,
          thumbnailUrl: movieData.posterUrl,
          videoUrl: selectedMovieForDetails?.videoUrl || "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/Sintel.mp4",
          duration: "9m 56s"
        }
      ].filter(ep => ep.seasonNumber === activeSeason);

  const savedSession = continueWatching.find((s) => s.movieId === movieData.id);
  const savedTimestamp = savedSession?.timestamp || 0;

  // Filter related movies by common genres
  const relatedTitles = movies
    .filter(m => m.id !== movieData.id && m.genres.some(g => movieData.genres.includes(g)))
    .slice(0, 6);

  const handleDownloadSimulation = async () => {
    if (!isActuallyDownloaded) {
      if (onAddDownload && tmdbIdVal) {
        onAddDownload(tmdbIdVal.toString(), movieData.type || "movie");
        setSelectedMovieForDetails(null);
        setTmdbJson(null);
      }
      return;
    }
    
    const itemId = selectedMovieForDetails.id;
    const isCached = browserCachedItems.includes(itemId);
    
    if (isCached) {
      if (confirm("Remove this offline download from your browser?")) {
        await handleRemoveFromBrowser(itemId, selectedMovieForDetails.videoUrl);
      }
      return;
    }
    
    await handleDownloadToBrowser(itemId, selectedMovieForDetails.videoUrl, selectedMovieForDetails.title);
  };

  const handlePlayAction = () => {
    if (!isActuallyDownloaded) {
      if (onAddDownload && tmdbIdVal) {
        onAddDownload(tmdbIdVal.toString(), movieData.type || "movie");
        setSelectedMovieForDetails(null);
        setTmdbJson(null);
      }
      return;
    }
    if (selectedMovieForDetails.type === "series" && savedSession?.episodeId) {
      const targetEpisode = selectedMovieForDetails.episodes?.find(ep => ep.id === savedSession.episodeId);
      if (targetEpisode) {
        onPlayMovie({
          ...selectedMovieForDetails,
          title: `${selectedMovieForDetails.title}: S${targetEpisode.seasonNumber}E${targetEpisode.episodeNumber} - ${targetEpisode.title}`,
          videoUrl: targetEpisode.videoUrl,
          thumbnailUrl: targetEpisode.thumbnailUrl || selectedMovieForDetails.thumbnailUrl,
          duration: targetEpisode.duration,
          activeEpisodeId: targetEpisode.id,
          activeEpisodeNumber: targetEpisode.episodeNumber,
          activeSeasonNumber: targetEpisode.seasonNumber,
        });
        setSelectedMovieForDetails(null);
        setTmdbJson(null);
        return;
      }
    }
    onPlayMovie(selectedMovieForDetails);
    setSelectedMovieForDetails(null);
    setTmdbJson(null);
  };

  const handlePlayEpisode = (episode: Episode) => {
    onPlayMovie({
      ...selectedMovieForDetails,
      title: `${selectedMovieForDetails.title}: S${episode.seasonNumber}E${episode.episodeNumber} - ${episode.title}`,
      videoUrl: episode.videoUrl,
      thumbnailUrl: episode.thumbnailUrl || selectedMovieForDetails.thumbnailUrl,
      duration: episode.duration,
      activeEpisodeId: episode.id,
      activeEpisodeNumber: episode.episodeNumber,
      activeSeasonNumber: episode.seasonNumber,
    });
    setSelectedMovieForDetails(null);
    setTmdbJson(null);
  };

  // Render Theme-Specific Layouts
  if (theme === "prime") {
    /* ------------------------------------------------------------------
       PRIME VIDEO THEME DETAILS VIEW (DEEP BLUE-BLACK MATTE MATURE LAYOUT)
       ------------------------------------------------------------------ */
    return (
      <div className="pt-20 pb-24 text-zinc-300 font-sans min-h-screen bg-[#0b0c10] text-left animate-fade-in" id="prime-details-view">
        {/* Header Navigation Link */}
        <div className="max-w-7xl mx-auto px-6 md:px-12 py-4">
          <button 
            onClick={() => { setSelectedMovieForDetails(null); setTmdbJson(null); }}
            className="flex items-center space-x-2 text-zinc-400 hover:text-white transition group text-sm font-semibold cursor-pointer"
          >
            <ArrowLeft className="w-4 h-4 group-hover:-translate-x-1 transition-transform" />
            <span>Go Back</span>
          </button>
        </div>

        {/* Hero Segment */}
        <div className="relative max-w-7xl mx-auto px-6 md:px-12 grid grid-cols-1 lg:grid-cols-12 gap-8 items-start mb-16">
          {/* Left Column: Media Details & Actions */}
          <div className="col-span-1 lg:col-span-7 space-y-6 z-10">
            {/* Logo details */}
            <div className="flex items-center space-x-2">
              <span className="text-sky-400 font-black tracking-wide text-xs uppercase bg-sky-950/40 border border-sky-500/20 px-2.5 py-0.5 rounded">
                StreamHome
              </span>
              {movieData.type === "series" && (
                <span className="text-zinc-400 text-xs uppercase font-bold tracking-wider">
                  Series • {displaySeasons.length} Season{displaySeasons.length !== 1 ? "s" : ""}
                </span>
              )}
            </div>

            {/* Movie Title */}
            <h1 className="text-3xl sm:text-4xl md:text-5xl font-extrabold text-white tracking-tight leading-tight">
              {movieData.title}
            </h1>

            {/* Buttons & Interactive Controls Row */}
            <div className="flex flex-wrap items-center gap-3">
              <button
                onClick={handlePlayAction}
                className="flex items-center space-x-3 bg-white hover:bg-zinc-200 text-black px-8 py-3.5 rounded-md font-bold transition duration-200 shadow-xl cursor-pointer text-sm sm:text-base"
              >
                {isActuallyDownloaded ? (
                  <Play className="w-5 h-5 fill-current text-black" />
                ) : (
                  <Download className="w-5 h-5 text-black" />
                )}
                <span>
                  {isActuallyDownloaded 
                    ? (savedTimestamp > 0 ? "Resume Watching" : "Watch Now") 
                    : (movieData.type === "series" ? "Add / Ingest Series" : "Download / Ingest Movie")}
                </span>
              </button>

              {/* Watchlist Toggle */}
              <button
                onClick={() => toggleWatchlist(movieData.id)}
                className={`w-12 h-12 rounded-full border flex items-center justify-center transition cursor-pointer bg-zinc-900/60 ${
                  watchlist.includes(movieData.id)
                    ? "border-emerald-500 text-emerald-400 bg-emerald-950/20"
                    : "border-zinc-700 text-zinc-300 hover:text-white hover:border-white hover:bg-zinc-800"
                }`}
                title="Add to Watchlist"
              >
                {watchlist.includes(movieData.id) ? (
                  <Check className="w-5 h-5" />
                ) : (
                  <Plus className="w-5 h-5" />
                )}
              </button>

              {/* Thumbs Up (Like) */}
              <button
                onClick={() => setLikeStatus(likeStatus === "liked" ? null : "liked")}
                className={`w-12 h-12 rounded-full border flex items-center justify-center transition cursor-pointer bg-zinc-900/60 ${
                  likeStatus === "liked"
                    ? "border-sky-500 text-sky-400 bg-sky-950/20"
                    : "border-zinc-700 text-zinc-300 hover:text-white hover:border-white hover:bg-zinc-800"
                }`}
                title="Like"
              >
                <ThumbsUp className="w-5 h-5" />
              </button>

              {/* Thumbs Down (Dislike) */}
              <button
                onClick={() => setLikeStatus(likeStatus === "disliked" ? null : "disliked")}
                className={`w-12 h-12 rounded-full border flex items-center justify-center transition cursor-pointer bg-zinc-900/60 ${
                  likeStatus === "disliked"
                    ? "border-red-500 text-red-400 bg-red-950/20"
                    : "border-zinc-700 text-zinc-300 hover:text-white hover:border-white hover:bg-zinc-800"
                }`}
                title="Dislike"
              >
                <ThumbsDown className="w-5 h-5" />
              </button>

              {/* Download Simulation */}
              <button
                onClick={handleDownloadSimulation}
                className={`w-12 h-12 rounded-full border flex items-center justify-center transition cursor-pointer bg-zinc-900/60 ${
                  isDownloaded
                    ? "border-emerald-500 text-emerald-400"
                    : isDownloading
                    ? "border-sky-500 text-sky-400 animate-pulse"
                    : "border-zinc-700 text-zinc-300 hover:text-white hover:border-white hover:bg-zinc-800"
                }`}
                title="Download to Device"
              >
                {isDownloaded ? (
                  <Check className="w-5 h-5 text-emerald-400" />
                ) : isDownloading ? (
                  <span className="text-[10px] font-bold">{downloadProgress}%</span>
                ) : (
                  <Download className="w-5 h-5" />
                )}
              </button>
            </div>

            {/* Badges and Sub-labels */}
            <div className="flex flex-wrap items-center gap-2.5 text-xs text-zinc-400 font-semibold pt-1">
              {isDownloaded ? (
                <span className="text-emerald-400 font-bold bg-emerald-500/10 px-2 py-0.5 rounded border border-emerald-500/10">
                  Available Offline
                </span>
              ) : (
                <span className="text-cyan-400 font-bold bg-cyan-500/10 px-2 py-0.5 rounded border border-cyan-500/10">
                  Ready to Stream
                </span>
              )}
              <span className="bg-zinc-800 text-zinc-300 px-2 py-0.5 rounded font-mono">
                {movieData.releaseYear}
              </span>
              <span className="border border-zinc-700 px-1.5 py-0.2 rounded font-extrabold uppercase bg-zinc-900">
                {movieData.rating}
              </span>
              <span className="flex items-center gap-1 text-zinc-300">
                <Clock className="w-3.5 h-3.5 text-zinc-500" />
                <span>{movieData.duration}</span>
              </span>
              <span className="bg-sky-500/10 text-sky-400 px-2.5 py-0.5 rounded border border-sky-500/15">
                IMDb {movieData.userScore}
              </span>
            </div>

            {/* Brief Description */}
            <p className="text-sm sm:text-base text-zinc-300 leading-relaxed max-w-2xl font-normal">
              {movieData.overview}
            </p>

            {/* Director / Actor text rows */}
            <div className="space-y-1.5 pt-2 border-t border-zinc-800/60 max-w-xl text-xs sm:text-sm">
              <div>
                <span className="text-zinc-500 font-bold uppercase tracking-wider text-[11px] block sm:inline sm:mr-2">Directors</span>
                <span className="text-white font-medium">{movieData.director}</span>
              </div>
              <div>
                <span className="text-zinc-500 font-bold uppercase tracking-wider text-[11px] block sm:inline sm:mr-2">Starring</span>
                <span className="text-white font-medium">{movieData.cast.slice(0, 4).join(", ") || "Not Specified"}</span>
              </div>
            </div>
          </div>

          {/* Right Column: Giant Matte Artwork Overlay */}
          <div className="col-span-1 lg:col-span-5 relative aspect-video lg:aspect-[4/3] rounded-lg overflow-hidden bg-zinc-950 border border-zinc-850 shadow-2xl">
            <img 
              src={movieData.backdropUrl} 
              alt={movieData.title}
              referrerPolicy="no-referrer"
              className="w-full h-full object-cover opacity-80"
            />
            <div className="absolute inset-0 bg-gradient-to-t from-[#0b0c10] via-[#0b0c10]/40 to-transparent" />
            <div className="absolute inset-0 bg-gradient-to-r from-[#0b0c10] via-transparent to-transparent hidden lg:block" />
          </div>
        </div>

        {/* Tab Panel Section */}
        <div className="max-w-7xl mx-auto px-6 md:px-12 border-t border-zinc-900 pt-8">
          <div className="flex border-b border-zinc-900 mb-6 gap-6">
            {movieData.type === "series" && (
              <button 
                onClick={() => setActiveTab("episodes")}
                className={`pb-4 text-sm sm:text-base font-extrabold uppercase tracking-wider transition cursor-pointer ${
                  activeTab === "episodes" ? "text-sky-400 border-b-2 border-sky-400" : "text-zinc-500 hover:text-zinc-300"
                }`}
              >
                Episodes
              </button>
            )}
            <button 
              onClick={() => setActiveTab("related")}
              className={`pb-4 text-sm sm:text-base font-extrabold uppercase tracking-wider transition cursor-pointer ${
                activeTab === "related" ? "text-sky-400 border-b-2 border-sky-400" : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              Related Titles
            </button>
            <button 
              onClick={() => setActiveTab("details")}
              className={`pb-4 text-sm sm:text-base font-extrabold uppercase tracking-wider transition cursor-pointer ${
                activeTab === "details" ? "text-sky-400 border-b-2 border-sky-400" : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              Details
            </button>
          </div>

          {/* Tab Contents */}
          {activeTab === "episodes" && movieData.type === "series" && (
            <div className="space-y-6">
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-zinc-900 pb-4">
                <div className="flex items-center space-x-3">
                  <span className="text-xs uppercase font-extrabold text-zinc-500 tracking-wider">Select Season</span>
                  <select
                    value={selectedSeason}
                    onChange={(e) => setSelectedSeason(Number(e.target.value))}
                    className="bg-zinc-900 border border-zinc-800 text-white rounded px-3 py-1.5 text-xs font-bold outline-none cursor-pointer focus:border-sky-500 transition"
                  >
                    {displaySeasons.map(s => (
                      <option key={s} value={s}>Season {s}</option>
                    ))}
                  </select>
                </div>
                <span className="text-zinc-500 text-xs font-bold uppercase tracking-wider">{currentEpisodes.length} Episodes Available</span>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {isFetchingEpisodes ? (
                  <>
                    {[1, 2, 3, 4].map(i => (
                      <div key={`skel-def-${i}`} className="flex flex-col sm:flex-row gap-4 p-4 rounded-lg bg-zinc-950 border border-zinc-900 animate-pulse">
                        <div className="relative w-full sm:w-36 aspect-video rounded bg-zinc-900 flex-none" />
                        <div className="flex-1 space-y-2 pt-1">
                          <div className="h-4 w-1/3 bg-zinc-900 rounded" />
                          <div className="h-3 w-full bg-zinc-900 rounded mt-3" />
                          <div className="h-3 w-4/5 bg-zinc-900 rounded" />
                        </div>
                      </div>
                    ))}
                  </>
                ) : currentEpisodes.map((episode) => (
                  <div 
                    key={episode.id}
                    className="group/ep flex flex-col sm:flex-row gap-4 p-4 rounded-lg bg-zinc-950 border border-zinc-900 hover:border-zinc-800 transition duration-150"
                  >
                    <div className="relative w-full sm:w-36 aspect-video rounded overflow-hidden bg-zinc-900 flex-none group-hover/ep:border-zinc-700 transition border border-transparent">
                      <img src={episode.thumbnailUrl || movieData.posterUrl} alt={episode.title} referrerPolicy="no-referrer" className="w-full h-full object-cover opacity-85 group-hover/ep:scale-105 transition duration-300" />
                      <div className="absolute inset-0 flex items-center justify-center bg-black/40 group-hover/ep:bg-black/60 transition">
                        <button 
                          onClick={() => handlePlayEpisode(episode)}
                          className="w-10 h-10 rounded-full bg-white text-black flex items-center justify-center shadow-lg transform scale-90 group-hover/ep:scale-100 transition-all duration-300 cursor-pointer"
                        >
                          <Play className="w-4.5 h-4.5 fill-current ml-0.5" />
                        </button>
                      </div>
                      <span className="absolute bottom-1.5 right-1.5 bg-black/80 px-1 py-0.5 text-[9px] font-mono text-zinc-300 rounded">
                        {episode.duration}
                      </span>
                    </div>

                    <div className="flex-1 text-left space-y-1.5">
                      <div className="flex items-center justify-between gap-2">
                        <h4 className="text-xs sm:text-sm font-extrabold text-white group-hover/ep:text-sky-400 transition">
                          Episode {episode.episodeNumber}: {episode.title}
                        </h4>
                        {/* Episode on-server indicator */}
                        <div
                          className="p-1.5 bg-zinc-900 rounded border border-zinc-800 flex items-center justify-center"
                          title="Available on server"
                        >
                          <Database className="w-3.5 h-3.5 text-emerald-500" />
                        </div>
                      </div>
                      <p className="text-xs text-zinc-400 font-normal leading-relaxed line-clamp-3">
                        {episode.description}
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {activeTab === "related" && (
            <div>
              {relatedTitles.length === 0 ? (
                <div className="text-center py-12 text-zinc-500 text-xs sm:text-sm">No related titles found.</div>
              ) : (
                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-4">
                  {relatedTitles.map((movie) => (
                    <div 
                      key={movie.id}
                      onClick={() => {
                        setSelectedMovieForDetails(movie);
                        setTmdbJson(null);
                        window.scrollTo({ top: 0, behavior: "smooth" });
                      }}
                      className="group cursor-pointer space-y-2 relative"
                    >
                      <div className="aspect-[16/9] sm:aspect-[2/3] rounded overflow-hidden bg-zinc-900 border border-zinc-900 group-hover:border-sky-500 transition-all duration-200 shadow-lg">
                        <img 
                          src={movie.thumbnailUrl} 
                          alt={movie.title}
                          referrerPolicy="no-referrer"
                          className="w-full h-full object-cover group-hover:scale-105 transition duration-300"
                        />
                      </div>
                      <div className="text-left">
                        <div className="text-xs font-bold text-white group-hover:text-sky-400 truncate">{movie.title}</div>
                        <div className="text-[10px] text-zinc-500">{movie.releaseYear} • {movie.duration}</div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {activeTab === "details" && (
            <div className="bg-zinc-950 border border-zinc-900 rounded-xl p-6 md:p-8 grid grid-cols-1 md:grid-cols-2 gap-8 text-xs sm:text-sm text-left">
              <div className="space-y-4">
                <h3 className="text-sm font-extrabold text-white uppercase tracking-wider border-b border-zinc-900 pb-2">Production Info</h3>
                <div className="grid grid-cols-3 gap-y-2.5">
                  <span className="text-zinc-500 font-bold uppercase tracking-wider text-[10px]">Director</span>
                  <span className="col-span-2 text-zinc-300 font-medium">{movieData.director}</span>

                  <span className="text-zinc-500 font-bold uppercase tracking-wider text-[10px]">Genres</span>
                  <span className="col-span-2 text-zinc-300 font-medium">{movieData.genres.join(", ")}</span>

                  <span className="text-zinc-500 font-bold uppercase tracking-wider text-[10px]">Release Year</span>
                  <span className="col-span-2 text-zinc-300 font-medium">{movieData.releaseYear}</span>

                  <span className="text-zinc-500 font-bold uppercase tracking-wider text-[10px]">Age Rating</span>
                  <span className="col-span-2 text-zinc-300 font-medium">{movieData.rating}</span>

                  <span className="text-zinc-500 font-bold uppercase tracking-wider text-[10px]">Runtime</span>
                  <span className="col-span-2 text-zinc-300 font-medium">{movieData.duration}</span>
                </div>
              </div>

              <div className="space-y-4">
                <h3 className="text-sm font-extrabold text-white uppercase tracking-wider border-b border-zinc-900 pb-2">Cast</h3>
                <div className="space-y-3">
                  <p className="text-zinc-300 leading-relaxed">
                    The award-winning lead cast featured in this production:
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {movieData.cast.map(name => (
                      <span key={name} className="bg-zinc-900 text-zinc-300 border border-zinc-800 rounded-md px-3 py-1 font-semibold text-xs">
                        {name}
                      </span>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    );
  }

  if (theme === "apple") {
    /* ------------------------------------------------------------------
       APPLE TV THEME DETAILS VIEW (FROST GLASS LIQUID PREMIUM MINIMALIST)
       ------------------------------------------------------------------ */
    return (
      <div className="pt-20 pb-24 text-zinc-300 font-sans min-h-screen bg-black text-left animate-fade-in relative overflow-hidden" id="apple-details-view">
        {/* Dynamic Frosted Background Gradient Plate */}
        <div 
          className="absolute inset-0 bg-cover bg-center opacity-25 blur-3xl pointer-events-none"
          style={{ backgroundImage: `url("${encodeURI(movieData.backdropUrl)}")` }}
        />

        {/* Back Link */}
        <div className="relative max-w-6xl mx-auto px-6 md:px-12 py-4 z-10">
          <button 
            onClick={() => { setSelectedMovieForDetails(null); setTmdbJson(null); }}
            className="flex items-center space-x-2 text-zinc-400 hover:text-white transition group text-xs sm:text-sm font-medium cursor-pointer"
          >
            <ArrowLeft className="w-4 h-4 group-hover:-translate-x-1 transition-transform" />
            <span>Close Details</span>
          </button>
        </div>

        {/* Apple Premium Main Column */}
        <div className="relative max-w-6xl mx-auto px-6 md:px-12 z-10 grid grid-cols-1 lg:grid-cols-12 gap-12 items-center mb-16">
          {/* Left Hero Backdrop Showcase */}
          <div className="col-span-1 lg:col-span-5 flex justify-center">
            <motion.div 
              whileHover={{ scale: 1.02 }}
              className="w-full max-w-[320px] aspect-[2/3] rounded-2xl overflow-hidden bg-zinc-900 border border-white/10 shadow-[0_25px_50px_-12px_rgba(0,0,0,0.8)] relative group"
            >
              <img 
                src={movieData.posterUrl} 
                alt={movieData.title}
                referrerPolicy="no-referrer"
                className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-102"
              />
              <div className="absolute inset-0 bg-gradient-to-t from-black/60 via-transparent to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-300 flex items-end p-4">
                <span className="text-white text-xs font-semibold tracking-wide flex items-center space-x-1">
                  <span>{movieData.type === "series" ? "TV Series" : "Feature Film"}</span>
                </span>
              </div>
            </motion.div>
          </div>

          {/* Right Movie Details Info Card */}
          <div className="col-span-1 lg:col-span-7 space-y-6">
            <div className="flex items-center space-x-3 text-[10px] tracking-widest font-extrabold uppercase text-white/40">
              <span className="text-white bg-white/10 px-2 py-0.5 rounded border border-white/10">Catalog</span>
              <span>•</span>
              <span>{movieData.type === "series" ? "TV Series" : "Feature Film"}</span>
            </div>

            {/* Title with editorial tracking */}
            <h1 className="text-4xl sm:text-5xl font-semibold tracking-tight text-white font-sans leading-none">
              {movieData.title}
            </h1>

            {/* Apple Metal-Glass Badges Row */}
            <div className="flex flex-wrap items-center gap-3 text-xs text-white/70 font-semibold pt-1">
              <span className="bg-white/15 px-2.5 py-0.5 rounded-full text-white text-[11px] font-medium border border-white/10">
                {movieData.releaseYear}
              </span>
              <span className="bg-white/15 px-2.5 py-0.5 rounded-full text-white text-[11px] font-medium border border-white/10 uppercase">
                {movieData.rating}
              </span>
              <span className="flex items-center gap-1.5 bg-white/10 px-2.5 py-0.5 rounded-full text-[11px] border border-white/5">
                <Clock className="w-3.5 h-3.5 text-zinc-400" />
                <span>{movieData.duration}</span>
              </span>
              <span className="bg-emerald-500/10 text-emerald-300 px-2.5 py-0.5 rounded-full text-[11px] border border-emerald-500/10">
                User Score: {movieData.userScore}
              </span>
            </div>

            {/* Apple Fluid Typography Description */}
            <p className="text-sm sm:text-base text-white/80 leading-relaxed font-light font-sans max-w-2xl">
              {movieData.overview}
            </p>

            {/* Compact Apple Cast Badges */}
            <div className="space-y-2 pt-2 border-t border-white/10 max-w-xl">
              <div className="flex flex-wrap items-center gap-1.5 text-xs sm:text-sm">
                <span className="text-white/40 font-bold uppercase tracking-wider text-[10px] mr-2">Starring</span>
                <span className="text-white font-medium">{movieData.cast.slice(0, 3).join(", ") || "Unknown Stars"}</span>
              </div>
              <div className="flex flex-wrap items-center gap-1.5 text-xs sm:text-sm">
                <span className="text-white/40 font-bold uppercase tracking-wider text-[10px] mr-2">Directed by</span>
                <span className="text-white font-medium">{movieData.director}</span>
              </div>
            </div>

            {/* Action Buttons with high-end glass style */}
            <div className="flex flex-wrap items-center gap-3 pt-4">
              <button
                onClick={handlePlayAction}
                className="flex items-center space-x-3 bg-white hover:bg-white/90 text-black px-8 py-3.5 rounded-xl font-bold transition duration-200 shadow-xl cursor-pointer text-sm sm:text-base"
              >
                {isActuallyDownloaded ? (
                  <Play className="w-4.5 h-4.5 fill-current text-black" />
                ) : (
                  <Download className="w-4.5 h-4.5 text-black" />
                )}
                <span>
                  {isActuallyDownloaded 
                    ? (savedTimestamp > 0 ? "Resume Film" : "Play Now") 
                    : (movieData.type === "series" ? "Add / Ingest Series" : "Download / Ingest Movie")}
                </span>
              </button>

              <button
                onClick={() => toggleWatchlist(movieData.id)}
                className={`flex items-center space-x-2 px-5 py-3.5 rounded-xl border font-bold transition duration-200 text-xs sm:text-sm tracking-wide cursor-pointer ${
                  watchlist.includes(movieData.id)
                    ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-400"
                    : "bg-white/5 border-white/10 text-zinc-300 hover:bg-white/10 hover:text-white hover:border-white/20"
                }`}
              >
                {watchlist.includes(movieData.id) ? (
                  <>
                    <Check className="w-4.5 h-4.5" />
                    <span>In Watchlist</span>
                  </>
                ) : (
                  <>
                    <Plus className="w-4.5 h-4.5" />
                    <span>Add to Queue</span>
                  </>
                )}
              </button>

              <button
                onClick={handleDownloadSimulation}
                className={`w-12 h-12 rounded-xl border flex items-center justify-center transition bg-white/5 border-white/10 hover:bg-white/10 hover:border-white/20 text-zinc-300 hover:text-white cursor-pointer`}
                title="Download Offline"
              >
                {isDownloaded ? (
                  <Check className="w-5 h-5 text-emerald-400" />
                ) : isDownloading ? (
                  <span className="text-[10px] font-bold text-white">{downloadProgress}%</span>
                ) : (
                  <Download className="w-5 h-5" />
                )}
              </button>
            </div>
          </div>
        </div>

        {/* Apple Tab layout sections */}
        <div className="relative max-w-6xl mx-auto px-6 md:px-12 pt-8 border-t border-white/10">
          <div className="flex border-b border-white/10 mb-8 gap-8">
            {movieData.type === "series" && (
              <button 
                onClick={() => setActiveTab("episodes")}
                className={`pb-4 text-sm font-semibold tracking-wide transition cursor-pointer ${
                  activeTab === "episodes" ? "text-white border-b-2 border-white" : "text-zinc-500 hover:text-zinc-300"
                }`}
              >
                Episodes
              </button>
            )}
            <button 
              onClick={() => setActiveTab("related")}
              className={`pb-4 text-sm font-semibold tracking-wide transition cursor-pointer ${
                activeTab === "related" ? "text-white border-b-2 border-white" : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              More Like This
            </button>
            <button 
              onClick={() => setActiveTab("details")}
              className={`pb-4 text-sm font-semibold tracking-wide transition cursor-pointer ${
                activeTab === "details" ? "text-white border-b-2 border-white" : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              Production Details
            </button>
          </div>

          {activeTab === "episodes" && movieData.type === "series" && (
            <div className="space-y-6">
              <div className="flex items-center justify-between border-b border-white/5 pb-3">
                <div className="flex items-center space-x-3">
                  <span className="text-xs uppercase font-extrabold text-zinc-500 tracking-wider">Choose Season</span>
                  <select
                    value={selectedSeason}
                    onChange={(e) => setSelectedSeason(Number(e.target.value))}
                    className="bg-white/5 border border-white/10 text-white rounded-lg px-3 py-1.5 text-xs font-semibold outline-none cursor-pointer focus:border-white transition"
                  >
                    {displaySeasons.map(s => (
                      <option key={s} value={s}>Season {s}</option>
                    ))}
                  </select>
                </div>
                <span className="text-zinc-500 text-xs font-semibold tracking-wider uppercase">{currentEpisodes.length} Episodes Loaded</span>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                {isFetchingEpisodes ? (
                  <>
                    {[1, 2, 3, 4, 5, 6].map(i => (
                      <div key={`skel-app-${i}`} className="flex flex-col rounded-2xl bg-white/5 border border-white/10 overflow-hidden animate-pulse">
                        <div className="relative aspect-video bg-zinc-900" />
                        <div className="p-4 space-y-3">
                          <div className="h-4 w-1/2 bg-white/10 rounded" />
                          <div className="space-y-2">
                            <div className="h-3 w-full bg-white/5 rounded" />
                            <div className="h-3 w-3/4 bg-white/5 rounded" />
                          </div>
                        </div>
                      </div>
                    ))}
                  </>
                ) : currentEpisodes.map((episode) => (
                  <div 
                    key={episode.id}
                    className="group/ep flex flex-col rounded-2xl bg-white/5 border border-white/10 overflow-hidden hover:bg-white/10 transition duration-300"
                  >
                    <div className="relative aspect-video bg-zinc-900 overflow-hidden">
                      <img src={episode.thumbnailUrl || movieData.posterUrl} alt={episode.title} referrerPolicy="no-referrer" className="w-full h-full object-cover opacity-85 group-hover/ep:scale-102 transition duration-500" />
                      <div className="absolute inset-0 flex items-center justify-center bg-black/30 group-hover/ep:bg-black/50 transition">
                        <button 
                          onClick={() => handlePlayEpisode(episode)}
                          className="w-11 h-11 rounded-full bg-white text-black flex items-center justify-center shadow-2xl transform scale-95 group-hover/ep:scale-100 transition duration-300 cursor-pointer"
                        >
                          <Play className="w-4.5 h-4.5 fill-current ml-0.5" />
                        </button>
                      </div>
                      <span className="absolute bottom-2.5 right-2.5 bg-black/80 backdrop-blur-md px-2 py-0.5 text-[9px] font-mono text-zinc-300 rounded-full border border-white/10">
                        {episode.duration}
                      </span>
                    </div>

                    <div className="p-4 text-left space-y-1">
                      <div className="flex items-center justify-between gap-2">
                        <h4 className="text-xs sm:text-sm font-bold text-white group-hover/ep:text-white/90 transition">
                          S{episode.seasonNumber} E{episode.episodeNumber} • {episode.title}
                        </h4>
                        {/* Episode PWA local download control */}
                        <div
                          className="p-1.5 bg-black/60 rounded border border-zinc-800 flex items-center justify-center"
                          title="Available on server"
                        >
                          <Database className="w-3.5 h-3.5 text-emerald-400" />
                        </div>
                      </div>
                      <p className="text-xs text-zinc-400 font-light leading-relaxed line-clamp-2">
                        {episode.description}
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {activeTab === "related" && (
            <div>
              {relatedTitles.length === 0 ? (
                <div className="text-center py-12 text-zinc-500 text-xs sm:text-sm">No related original releases found.</div>
              ) : (
                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-6">
                  {relatedTitles.map((movie) => (
                    <div 
                      key={movie.id}
                      onClick={() => {
                        setSelectedMovieForDetails(movie);
                        setTmdbJson(null);
                        window.scrollTo({ top: 0, behavior: "smooth" });
                      }}
                      className="group cursor-pointer space-y-3"
                    >
                      <div className="aspect-[2/3] rounded-2xl overflow-hidden bg-zinc-900 border border-white/10 group-hover:border-white/30 transition-all duration-300 shadow-xl">
                        <img 
                          src={movie.thumbnailUrl} 
                          alt={movie.title}
                          referrerPolicy="no-referrer"
                          className="w-full h-full object-cover group-hover:scale-103 transition duration-500"
                        />
                      </div>
                      <div className="text-left px-1">
                        <div className="text-xs font-bold text-white group-hover:text-white/90 truncate">{movie.title}</div>
                        <div className="text-[10px] text-zinc-500">{movie.releaseYear} • {movie.duration}</div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {activeTab === "details" && (
            <div className="bg-white/5 border border-white/10 rounded-2xl p-6 md:p-8 grid grid-cols-1 md:grid-cols-2 gap-8 text-xs sm:text-sm text-left">
              <div className="space-y-4">
                <h3 className="text-sm font-bold text-white uppercase tracking-wider border-b border-white/10 pb-2">About this Film</h3>
                <div className="grid grid-cols-3 gap-y-3">
                  <span className="text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Director</span>
                  <span className="col-span-2 text-zinc-300 font-medium">{movieData.director}</span>

                  <span className="text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Genres</span>
                  <span className="col-span-2 text-zinc-300 font-medium">{movieData.genres.join(", ")}</span>

                  <span className="text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Year Released</span>
                  <span className="col-span-2 text-zinc-300 font-medium">{movieData.releaseYear}</span>

                  <span className="text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Maturity Rating</span>
                  <span className="col-span-2 text-zinc-300 font-medium">{movieData.rating}</span>

                  <span className="text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Runtime</span>
                  <span className="col-span-2 text-zinc-300 font-medium">{movieData.duration}</span>
                </div>
              </div>

              <div className="space-y-4">
                <h3 className="text-sm font-bold text-white uppercase tracking-wider border-b border-white/10 pb-2">Cast Credits</h3>
                <div className="space-y-3">
                  <p className="text-zinc-400 font-light leading-relaxed">
                    Featuring award-winning performances from Apple TV+ network partners:
                  </p>
                  <div className="flex flex-wrap gap-2">
                    {movieData.cast.map(name => (
                      <span key={name} className="bg-white/5 text-zinc-300 border border-white/10 rounded-lg px-3 py-1 font-medium text-xs">
                        {name}
                      </span>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    );
  }

  /* ------------------------------------------------------------------
     GEMINI THEME DETAILS VIEW (CYBERPUNK NEON GLOW SCI-FI INTERACTIVE)
     ------------------------------------------------------------------ */
  return (
    <div className="pt-20 pb-24 text-zinc-300 font-mono min-h-screen bg-[#050010] text-left animate-fade-in relative overflow-hidden" id="gemini-details-view">
      {/* Sci-fi Scanlines layer */}
      <div className="absolute inset-0 bg-[linear-gradient(rgba(18,16,16,0)_50%,rgba(0,0,0,0.25)_50%),linear-gradient(90deg,rgba(168,85,247,0.03),rgba(6,182,212,0.02),rgba(168,85,247,0.03))] bg-[size:100%_4px,6px_100%] pointer-events-none z-10" />

      {/* Cyberpunk grid bg */}
      <div className="absolute inset-0 opacity-5 bg-[linear-gradient(to_right,#808080_1px,transparent_1px),linear-gradient(to_bottom,#808080_1px,transparent_1px)] bg-[size:40px_40px] pointer-events-none" />

      {/* Close terminal link */}
      <div className="relative max-w-7xl mx-auto px-6 md:px-12 py-4 z-10">
        <button 
          onClick={() => { setSelectedMovieForDetails(null); setTmdbJson(null); }}
          className="flex items-center space-x-2 text-cyan-400 hover:text-cyan-300 transition group text-xs font-mono cursor-pointer"
        >
          <span>&lt;TERMINATE_QUERY&gt;</span>
        </button>
      </div>

      {/* Hologram Box */}
      <div className="relative max-w-7xl mx-auto px-6 md:px-12 z-10 grid grid-cols-1 lg:grid-cols-12 gap-12 items-center mb-16">
        {/* Left column details */}
        <div className="col-span-1 lg:col-span-7 space-y-6">
          <div className="flex items-center space-x-2 text-xs">
            <span className="text-purple-400 animate-pulse">● CORE_ONLINE</span>
            <span className="text-zinc-600">|</span>
            <span className="text-cyan-400">GEMINI_DEC_PROT v2.8</span>
          </div>

          <div className="space-y-1.5">
            <span className="text-xs text-purple-400 font-bold tracking-widest uppercase block">&gt; MOVIE FILE ANALYZER</span>
            <h1 className="text-3xl sm:text-4xl md:text-5xl font-black text-transparent bg-clip-text bg-gradient-to-r from-purple-400 via-pink-400 to-cyan-400 tracking-tight leading-tight uppercase">
              {movieData.title}
            </h1>
          </div>

          {/* Cyber Badges */}
          <div className="flex flex-wrap items-center gap-2 text-xs font-mono">
            <span className="border border-purple-500/30 text-purple-400 px-2 py-0.5 rounded bg-purple-950/20">
              SYS_RELEASE_YR: {movieData.releaseYear}
            </span>
            <span className="border border-cyan-500/30 text-cyan-400 px-2 py-0.5 rounded bg-cyan-950/20 uppercase">
              SECTOR_RATING: {movieData.rating}
            </span>
            <span className="border border-zinc-700 text-zinc-400 px-2 py-0.5 rounded bg-zinc-900">
              TIME_INDEX: {movieData.duration}
            </span>
            <span className="border border-emerald-500/30 text-emerald-400 px-2 py-0.5 rounded bg-emerald-950/20">
              SYNAPSE_SCORE: {movieData.userScore}
            </span>
          </div>

          {/* Core overview text */}
          <div className="bg-purple-950/10 border border-purple-500/20 p-4 rounded-lg space-y-2">
            <span className="text-[10px] text-purple-400 uppercase tracking-widest font-extrabold">&gt; DOSSIER OBJECTIVE:</span>
            <p className="text-xs sm:text-sm text-zinc-300 leading-relaxed font-light">
              {movieData.overview}
            </p>
          </div>

          {/* Technical specifications logs */}
          <div className="grid grid-cols-2 gap-4 text-xs bg-zinc-950/60 p-4 rounded-md border border-zinc-800">
            <div>
              <span className="text-zinc-500 font-bold block uppercase text-[9px]">DIRECTOR_GENE</span>
              <span className="text-white font-medium block">{movieData.director}</span>
            </div>
            <div>
              <span className="text-zinc-500 font-bold block uppercase text-[9px]">PRIMARY_GENRES</span>
              <span className="text-white font-medium block">{movieData.genres.slice(0, 3).join(", ")}</span>
            </div>
          </div>

          {/* Interactive cyber actions */}
          <div className="flex flex-wrap items-center gap-3">
            <button
              onClick={handlePlayAction}
              className="flex items-center space-x-3 bg-gradient-to-r from-purple-500 to-cyan-500 hover:from-purple-600 hover:to-cyan-600 text-white px-8 py-3.5 rounded-md font-bold transition duration-200 shadow-[0_0_15px_rgba(168,85,247,0.5)] cursor-pointer text-sm uppercase tracking-wider"
            >
              {isActuallyDownloaded ? (
                <Play className="w-5 h-5 fill-current text-white" />
              ) : (
                <Download className="w-5 h-5 text-white" />
              )}
              <span>
                {isActuallyDownloaded 
                  ? (savedTimestamp > 0 ? "RESUME_FEED" : "INITIALIZE_STREAM") 
                  : (movieData.type === "series" ? "INGEST_SERIES" : "DOWNLOAD_MEDIA")}
              </span>
            </button>

            <button
              onClick={() => toggleWatchlist(movieData.id)}
              className={`flex items-center space-x-2 px-5 py-3.5 rounded-md border font-bold transition duration-200 text-xs uppercase tracking-wider cursor-pointer ${
                watchlist.includes(movieData.id)
                  ? "border-emerald-500 text-emerald-400 bg-emerald-950/20 shadow-[0_0_10px_rgba(16,185,129,0.2)]"
                  : "border-purple-500/30 text-purple-400 hover:text-white hover:border-purple-500 bg-purple-950/10"
              }`}
            >
              {watchlist.includes(movieData.id) ? (
                <>
                  <Check className="w-4 h-4" />
                  <span>DECRYPTED_SAVE</span>
                </>
              ) : (
                <>
                  <Plus className="w-4 h-4" />
                  <span>QUEUE_CORE</span>
                </>
              )}
            </button>

            <button
              onClick={handleDownloadSimulation}
              className={`w-12 h-12 rounded-md border flex items-center justify-center transition bg-purple-950/10 border-purple-500/30 text-purple-400 hover:border-purple-500 hover:text-white cursor-pointer`}
              title="Cache Locally"
            >
              {isDownloaded ? (
                <Check className="w-5 h-5 text-emerald-400" />
              ) : isDownloading ? (
                <span className="text-[10px] font-bold text-cyan-400 animate-pulse">{downloadProgress}%</span>
              ) : (
                <Download className="w-5 h-5" />
              )}
            </button>
          </div>
        </div>

        {/* Right column: Holographic Wireframe Artwork */}
        <div className="col-span-1 lg:col-span-5 relative aspect-video lg:aspect-[4/3] rounded-lg overflow-hidden bg-zinc-950 border border-purple-500/30 shadow-[0_0_30px_rgba(168,85,247,0.15)] flex items-center justify-center group">
          <img 
            src={movieData.backdropUrl} 
            alt={movieData.title}
            referrerPolicy="no-referrer"
            className="w-full h-full object-cover opacity-60 group-hover:scale-105 transition duration-500"
          />
          {/* Cyber scanner overlay lines */}
          <div className="absolute inset-x-0 h-0.5 bg-cyan-400/50 shadow-[0_0_8px_rgba(6,182,212,0.8)] top-0 animate-[bounce_3s_infinite]" />
          <div className="absolute inset-0 bg-gradient-to-t from-[#050010] via-transparent to-transparent" />
        </div>
      </div>

      {/* Gemini Tech Tabs */}
      <div className="max-w-7xl mx-auto px-6 md:px-12 pt-8 border-t border-purple-950">
        <div className="flex border-b border-purple-950 mb-8 gap-6">
          {movieData.type === "series" && (
            <button 
              onClick={() => setActiveTab("episodes")}
              className={`pb-4 text-xs font-bold tracking-widest uppercase transition cursor-pointer ${
                activeTab === "episodes" ? "text-cyan-400 border-b-2 border-cyan-400" : "text-zinc-600 hover:text-zinc-400"
              }`}
            >
              &gt; EPISODES_DEC
            </button>
          )}
          <button 
            onClick={() => setActiveTab("related")}
            className={`pb-4 text-xs font-bold tracking-widest uppercase transition cursor-pointer ${
              activeTab === "related" ? "text-cyan-400 border-b-2 border-cyan-400" : "text-zinc-600 hover:text-zinc-400"
            }`}
          >
            &gt; CLUSTER_MAP
          </button>
          <button 
            onClick={() => setActiveTab("details")}
            className={`pb-4 text-xs font-bold tracking-widest uppercase transition cursor-pointer ${
              activeTab === "details" ? "text-cyan-400 border-b-2 border-cyan-400" : "text-zinc-600 hover:text-zinc-400"
            }`}
          >
            &gt; RAW_SPECIFICATION
          </button>
        </div>

        {activeTab === "episodes" && movieData.type === "series" && (
          <div className="space-y-6">
            <div className="flex items-center justify-between border-b border-purple-950 pb-3">
              <div className="flex items-center space-x-3">
                <span className="text-xs uppercase font-extrabold text-zinc-600 tracking-wider">INDEX_SEASON:</span>
                <select
                  value={selectedSeason}
                  onChange={(e) => setSelectedSeason(Number(e.target.value))}
                  className="bg-black border border-purple-500/30 text-cyan-400 rounded px-3 py-1 text-xs font-mono outline-none cursor-pointer focus:border-cyan-400 transition"
                >
                  {displaySeasons.map(s => (
                    <option key={s} value={s}>0{s}.CYB_SEASON</option>
                  ))}
                </select>
              </div>
              <span className="text-zinc-600 text-xs font-mono">{currentEpisodes.length} TRACKS_LOADED</span>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
              {isFetchingEpisodes ? (
                <>
                  {[1, 2, 3, 4, 5, 6].map(i => (
                    <div key={`skel-gem-${i}`} className="group/ep flex flex-col rounded-xl bg-zinc-900/30 border border-zinc-800/50 overflow-hidden animate-pulse">
                      <div className="relative aspect-video bg-zinc-800/50" />
                      <div className="p-5 space-y-3">
                        <div className="h-4 w-2/3 bg-zinc-800/50 rounded" />
                        <div className="space-y-2">
                          <div className="h-3 w-full bg-zinc-800/30 rounded" />
                          <div className="h-3 w-5/6 bg-zinc-800/30 rounded" />
                        </div>
                      </div>
                    </div>
                  ))}
                </>
              ) : currentEpisodes.map((episode) => (
                <div 
                  key={episode.id}
                  className="group/ep flex flex-col sm:flex-row gap-4 p-4 rounded bg-black/60 border border-purple-500/10 hover:border-cyan-500/40 transition duration-300"
                >
                  <div className="relative w-full sm:w-36 aspect-video bg-zinc-950 overflow-hidden border border-purple-500/20">
                    <img src={episode.thumbnailUrl || movieData.posterUrl} alt={episode.title} referrerPolicy="no-referrer" className="w-full h-full object-cover opacity-75 group-hover/ep:scale-105 transition duration-500" />
                    <div className="absolute inset-0 flex items-center justify-center bg-purple-950/20 group-hover/ep:bg-purple-950/40 transition">
                      <button 
                        onClick={() => handlePlayEpisode(episode)}
                        className="w-10 h-10 rounded-full bg-gradient-to-tr from-purple-500 to-cyan-500 text-white flex items-center justify-center shadow-lg transform scale-90 group-hover/ep:scale-100 transition duration-300 cursor-pointer"
                      >
                        <Play className="w-4.5 h-4.5 fill-current ml-0.5" />
                      </button>
                    </div>
                    <span className="absolute bottom-1.5 right-1.5 bg-black/90 px-1.5 py-0.5 text-[9px] font-mono text-cyan-400 rounded border border-cyan-500/30">
                      {episode.duration}
                    </span>
                  </div>

                  <div className="flex-1 text-left space-y-1">
                    <div className="flex items-center justify-between gap-2">
                      <h4 className="text-xs sm:text-sm font-bold text-cyan-400 group-hover/ep:text-white transition">
                        TRK_0{episode.episodeNumber}: {episode.title}
                      </h4>
                      {/* Episode PWA local download control */}
                      <div
                        className="p-1.5 bg-black/60 rounded border border-zinc-800 flex items-center justify-center"
                        title="Available on server"
                      >
                        <Database className="w-3.5 h-3.5 text-emerald-400" />
                      </div>
                    </div>
                    <p className="text-xs text-zinc-500 font-light leading-relaxed line-clamp-3">
                      {episode.description}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {activeTab === "related" && (
          <div>
            {relatedTitles.length === 0 ? (
              <div className="text-center py-12 text-zinc-600 text-xs sm:text-sm">NO DIRECT CLUSTERS CONVERGING.</div>
            ) : (
              <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-4">
                {relatedTitles.map((movie) => (
                  <div 
                    key={movie.id}
                    onClick={() => {
                      setSelectedMovieForDetails(movie);
                      setTmdbJson(null);
                      window.scrollTo({ top: 0, behavior: "smooth" });
                    }}
                    className="group cursor-pointer space-y-2 relative"
                  >
                    <div className="aspect-[2/3] rounded overflow-hidden bg-black border border-purple-500/20 group-hover:border-cyan-400 transition-all duration-300 shadow-md">
                      <img 
                        src={movie.thumbnailUrl} 
                        alt={movie.title}
                        referrerPolicy="no-referrer"
                        className="w-full h-full object-cover opacity-80 group-hover:scale-103 group-hover:opacity-100 transition duration-300"
                      />
                    </div>
                    <div className="text-left px-1">
                      <div className="text-xs font-bold text-zinc-300 group-hover:text-cyan-400 truncate">{movie.title}</div>
                      <div className="text-[10px] text-zinc-500">SYS_{movie.releaseYear} • INDEX_{movie.duration}</div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {activeTab === "details" && (
          <div className="bg-black/80 border border-purple-500/20 rounded p-6 md:p-8 grid grid-cols-1 md:grid-cols-2 gap-8 text-xs text-left">
            <div className="space-y-4">
              <h3 className="text-xs font-bold text-cyan-400 uppercase tracking-widest border-b border-purple-500/20 pb-2">&gt; DECRYPT_METADATA</h3>
              <div className="grid grid-cols-3 gap-y-3">
                <span className="text-zinc-600 font-bold uppercase tracking-wider text-[10px]">CREATOR_ENGINE</span>
                <span className="col-span-2 text-zinc-300">{movieData.director}</span>

                <span className="text-zinc-600 font-bold uppercase tracking-wider text-[10px]">SECTOR_GENRES</span>
                <span className="col-span-2 text-zinc-300">{movieData.genres.join(", ")}</span>

                <span className="text-zinc-600 font-bold uppercase tracking-wider text-[10px]">EPOCH_STAMP</span>
                <span className="col-span-2 text-zinc-300">{movieData.releaseYear}</span>

                <span className="text-zinc-600 font-bold uppercase tracking-wider text-[10px]">SECTOR_RATING</span>
                <span className="col-span-2 text-zinc-300">{movieData.rating}</span>

                <span className="text-zinc-600 font-bold uppercase tracking-wider text-[10px]">TOTAL_INDEX_TIME</span>
                <span className="col-span-2 text-zinc-300">{movieData.duration}</span>
              </div>
            </div>

            <div className="space-y-4">
              <h3 className="text-xs font-bold text-cyan-400 uppercase tracking-widest border-b border-purple-500/20 pb-2">&gt; NODE_ACTORS</h3>
              <div className="space-y-3">
                <p className="text-zinc-500 leading-relaxed">
                  Decrypted biometric indices of primary node responders:
                </p>
                <div className="flex flex-wrap gap-2">
                  {movieData.cast.map(name => (
                    <span key={name} className="bg-purple-950/20 text-purple-300 border border-purple-500/30 rounded px-2.5 py-1 font-bold text-xs">
                      {name}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
