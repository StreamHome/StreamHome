import React, { useState, useEffect, useRef } from "react";
import { motion, AnimatePresence } from "motion/react";
import { Search, Play, Info, Volume2, LogOut, ChevronLeft, ChevronRight, Settings, AlertTriangle, Check, RefreshCw, Database, X, Clock, Calendar, Film, Plus, Pencil, Trash2, User, Download, Shield } from "lucide-react";
import { Movie, Profile, PlaybackSession } from "../types";
import ThemeDetailsPage from "./ThemeDetailsPage";

interface DashboardProps {
  activeProfile: Profile;
  setActiveProfile: (profile: Profile | null) => void;
  onLogout: () => void;
  onPlayMovie: (movie: Movie) => void;
  apiBaseUrl: string;
  onChangeApiBaseUrl: (url: string) => void;
  apiBearerToken: string;
  onChangeApiBearerToken: (token: string) => void;
  
  profiles: Profile[];
  setProfiles: React.Dispatch<React.SetStateAction<Profile[]>>;
  
  activeTab: string;
  onTabChange: (tab: string) => void;
  
  selectedMovieForDetails: Movie | null;
  setSelectedMovieForDetails: (movie: Movie | null) => void;
  accessToken: string | null;
  onAccountLogout: () => void;
}

interface DownloadItem {
  id: string;
  title: string;
  sourceUrl: string;
  status: "Downloading" | "Compressing with FFmpeg (H.265)" | "Uploading to Google Drive" | "Completed" | "Failed";
  progress: number;
  speed?: string;
  eta?: string;
}

export default function Dashboard({
  activeProfile,
  setActiveProfile,
  onLogout,
  onPlayMovie,
  apiBaseUrl,
  onChangeApiBaseUrl,
  apiBearerToken,
  onChangeApiBearerToken,
  profiles,
  setProfiles,
  activeTab,
  onTabChange,
  selectedMovieForDetails,
  setSelectedMovieForDetails,
  accessToken,
  onAccountLogout,
}: DashboardProps) {
  const [movies, setMovies] = useState<Movie[]>([]);
  const [featuredMovie, setFeaturedMovie] = useState<Movie | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [isApiPanelOpen, setIsApiPanelOpen] = useState<boolean>(false);
  const [manualJsonInput, setManualJsonInput] = useState<string>("");
  const [jsonSuccess, setJsonSuccess] = useState<boolean>(false);
  const [watchlist, setWatchlist] = useState<string[]>([]);
  const [continueWatching, setContinueWatching] = useState<PlaybackSession[]>([]);
  const [tmdbJson, setTmdbJson] = useState<any>(null);
  const [selectedSeason, setSelectedSeason] = useState<number>(1);
  const [discoverAction, setDiscoverAction] = useState<any[]>([]);
  const [discoverScifi, setDiscoverScifi] = useState<any[]>([]);
  const [tmdbSearchResults, setTmdbSearchResults] = useState<any[]>([]);
  const [isSearchingTmdb, setIsSearchingTmdb] = useState<boolean>(false);
  const [isFetchingEpisodes, setIsFetchingEpisodes] = useState<boolean>(false);

  const [browserCachedItems, setBrowserCachedItems] = useState<string[]>([]);
  const [browserDownloadingProgress, setBrowserDownloadingProgress] = useState<Record<string, number>>({});
  const [pwaRegistry, setPwaRegistry] = useState<Record<string, any>>({});

  useEffect(() => {
    const registry = JSON.parse(localStorage.getItem("pwa_offline_registry") || "{}");
    setPwaRegistry(registry);
  }, [activeTab, selectedMovieForDetails]);

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
      setPwaRegistry(registry);
      
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
      setPwaRegistry(registry);
      
      setBrowserCachedItems(prev => prev.filter(id => id !== itemId));
    } catch (err) {
      console.error("Failed to delete local cache item:", err);
    }
  };

  useEffect(() => {
    if (!searchQuery.trim()) {
      setTmdbSearchResults([]);
      return;
    }
    const delayDebounceFn = setTimeout(async () => {
      setIsSearchingTmdb(true);
      try {
        const token = localStorage.getItem("stream_access_token");
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;
        const response = await fetch(`${apiBaseUrl}/search?query=${encodeURIComponent(searchQuery)}`, { headers });
        if (response.ok) {
          const data = await response.json();
          const formatted = data.map((item: any) => ({
            id: item.id,
            title: item.title,
            description: item.description,
            thumbnailUrl: item.thumbnailUrl,
            bannerUrl: item.bannerUrl,
            genres: item.genres || [],
            duration: item.duration || "2h",
            releaseYear: item.releaseYear,
            rating: item.rating,
            director: item.director,
            cast: item.cast || [],
            type: item.type || "movie",
            voteAverage: item.voteAverage,
            voteCount: item.voteCount
          }));
          setTmdbSearchResults(formatted);
        }
      } catch (err) {
        console.error("Failed to query TMDB search from backend:", err);
      } finally {
        setIsSearchingTmdb(false);
      }
    }, 500);

    return () => clearTimeout(delayDebounceFn);
  }, [searchQuery, apiBaseUrl]);

  useEffect(() => {
    setSelectedSeason(1);

    const fetchEpisodesForSeries = async () => {
      if (!selectedMovieForDetails || selectedMovieForDetails.type !== "series") return;
      
      const tmdbId = (selectedMovieForDetails as any).tmdb_id || (selectedMovieForDetails as any).tmdbId || 
                     (selectedMovieForDetails.id?.startsWith("tv_") ? selectedMovieForDetails.id.replace("tv_", "") : selectedMovieForDetails.id);
                     
      if (!tmdbId) return;

      try {
        setIsFetchingEpisodes(true);
        console.log(`[Frontend] Fetching episodes for series TMDB ID: ${tmdbId}`);
        const token = localStorage.getItem("stream_access_token");
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;
        const res = await fetch(`${apiBaseUrl}/series/${tmdbId}/episodes`, { headers });
        if (res.ok) {
          const episodesData = await res.json();
          console.log(`[Frontend] Successfully loaded ${episodesData.length} episodes.`);
          setSelectedMovieForDetails({
            ...selectedMovieForDetails,
            episodes: episodesData
          });
        }
      } catch (err) {
        console.error("Failed to fetch series episodes:", err);
      } finally {
        setIsFetchingEpisodes(false);
      }
    };
    
    fetchEpisodesForSeries();
  }, [selectedMovieForDetails?.id, apiBaseUrl]);

  useEffect(() => {
    if (!selectedMovieForDetails) {
      setTmdbJson(null);
      return;
    }

    const fetchTmdbDetails = async () => {
      // Resolve the actual TMDB ID
      const tmdbIdRaw = (selectedMovieForDetails as any).tmdb_id || (selectedMovieForDetails as any).tmdbId || selectedMovieForDetails.id;
      if (!tmdbIdRaw) return;
      const tmdbId = String(tmdbIdRaw).replace("discover_", "").replace("m_", "").replace("tv_", "");
      if (!tmdbId || isNaN(Number(tmdbId))) return;

      try {
        const typeStr = selectedMovieForDetails.type === "series" ? "tv" : "movie";
        console.log(`[Frontend] Fetching detailed TMDB metadata for ${typeStr} ID: ${tmdbId}`);
        const token = localStorage.getItem("stream_access_token");
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;
        const res = await fetch(`${apiBaseUrl}/tmdb/${typeStr}/${tmdbId}`, { headers });
        if (res.ok) {
          const detailedData = await res.json();
          console.log("[Frontend] Successfully loaded detailed TMDB metadata:", detailedData);
          setTmdbJson(detailedData);
        }
      } catch (err) {
        console.error("Failed to fetch detailed TMDB metadata:", err);
      }
    };

    fetchTmdbDetails();
  }, [selectedMovieForDetails?.id, apiBaseUrl]);

  // Download queue states with live SSE data
  const [downloads, setDownloads] = useState<DownloadItem[]>([]);
  const [dlTmdbId, setDlTmdbId] = useState<string>("");
  const [dlMediaType, setDlMediaType] = useState<string>("movie");
  const [dlSeason, setDlSeason] = useState<string>("");
  const [dlEpisode, setDlEpisode] = useState<string>("");
  const [dlVideoUrl, setDlVideoUrl] = useState<string>("");
  const [dlAudioUrl, setDlAudioUrl] = useState<string>("");

  // Active Profile settings state
  const [profileSubTab, setProfileSubTab] = useState<"active" | "manage">("active");
  const [activeProfileName, setActiveProfileName] = useState(activeProfile.name);
  const [activeProfileAvatarColor, setActiveProfileAvatarColor] = useState(activeProfile.avatarColor);
  const [activeProfileTheme, setActiveProfileTheme] = useState(activeProfile.theme || "netflix");
  const [editProfileTheme, setEditProfileTheme] = useState<"netflix" | "prime" | "apple" | "gemini">(activeProfile.theme || "netflix");
  const [activeProfilePinEnabled, setActiveProfilePinEnabled] = useState(!!activeProfile.pinEnabled);
  const [activeProfilePin, setActiveProfilePin] = useState(activeProfile.pin || "");
  const [activeProfileSaveSuccess, setActiveProfileSaveSuccess] = useState(false);

  useEffect(() => {
    setActiveProfileName(activeProfile.name);
    setActiveProfileAvatarColor(activeProfile.avatarColor);
    setActiveProfileTheme(activeProfile.theme || "netflix");
    setEditProfileTheme(activeProfile.theme || "netflix");
    setActiveProfilePinEnabled(!!activeProfile.pinEnabled);
    setActiveProfilePin(activeProfile.pin || "");
  }, [activeProfile, activeTab]);

  const handleUpdateTheme = async (newTheme: "netflix" | "prime" | "apple" | "gemini") => {
    const updated: Profile = {
      ...activeProfile,
      theme: newTheme,
    };

    try {
      const token = localStorage.getItem("stream_access_token");
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const response = await fetch(`${apiBaseUrl}/profiles`, {
        method: "POST",
        headers: headers,
        body: JSON.stringify(updated),
      });
      if (response.ok) {
        const saved = await response.json();
        setActiveProfileTheme(newTheme);
        setActiveProfile(saved);
        localStorage.setItem("stream_active_profile", JSON.stringify(saved));
        if (setProfiles) {
          setProfiles((prev) =>
            prev.map((p) => (p.id === saved.id ? saved : p))
          );
        }
      }
    } catch (err) {
      console.warn("Failed to save profile settings to database:", err);
      setActiveProfileTheme(newTheme);
      setActiveProfile(updated);
      localStorage.setItem("stream_active_profile", JSON.stringify(updated));
    }
  };

  const handleSaveActiveProfile = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeProfileName.trim()) return;

    const updated: Profile = {
      ...activeProfile,
      name: activeProfileName.trim(),
      avatarColor: activeProfileAvatarColor,
      theme: editProfileTheme,
      pinEnabled: activeProfilePinEnabled,
      pin: activeProfilePinEnabled ? activeProfilePin.trim() : undefined,
    };

    try {
      const token = localStorage.getItem("stream_access_token");
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const response = await fetch(`${apiBaseUrl}/profiles`, {
        method: "POST",
        headers: headers,
        body: JSON.stringify(updated),
      });
      if (response.ok) {
        const saved = await response.json();
        setActiveProfileTheme(saved.theme || "netflix");
        setActiveProfile(saved);
        localStorage.setItem("stream_active_profile", JSON.stringify(saved));
        if (setProfiles) {
          setProfiles((prev) =>
            prev.map((p) => (p.id === saved.id ? saved : p))
          );
        }
      }
    } catch (err) {
      console.warn("Failed to save profile settings to database:", err);
      if (setProfiles) {
        setProfiles((prev) =>
          prev.map((p) => (p.id === activeProfile.id ? updated : p))
        );
      }
      setActiveProfileTheme(updated.theme || "netflix");
      setActiveProfile(updated);
      localStorage.setItem("stream_active_profile", JSON.stringify(updated));
    }
    setActiveProfileSaveSuccess(true);
    setTimeout(() => setActiveProfileSaveSuccess(false), 3000);
  };

  // General Settings user preferences
  const [language, setLanguage] = useState<string>(() => localStorage.getItem("stream_pref_lang") || "en");
  const [quality, setQuality] = useState<string>(() => localStorage.getItem("stream_pref_quality") || "auto");
  const [autoplay, setAutoplay] = useState<boolean>(() => localStorage.getItem("stream_pref_autoplay") !== "false");

  // 2FA States
  const [twoFactorEnabled, setTwoFactorEnabled] = useState<boolean>(false);
  const [totpSecret, setTotpSecret] = useState<string>("");
  const [totpProvUri, setTotpProvUri] = useState<string>("");
  const [totpCode, setTotpCode] = useState<string>("");
  const [is2faSetupMode, setIs2faSetupMode] = useState<boolean>(false);
  const [totpMessage, setTotpMessage] = useState<string>("");
  const [totpError, setTotpError] = useState<string>("");

  // Fetch initial 2FA status on settings page load
  useEffect(() => {
    if (activeTab === "settings" && accessToken) {
      const fetch2faStatus = async () => {
        try {
          const res = await fetch(`${apiBaseUrl}/auth/2fa/status`, {
            headers: { "Authorization": `Bearer ${accessToken}` }
          });
          if (res.ok) {
            const data = await res.json();
            setTwoFactorEnabled(data.two_factor_enabled);
          }
        } catch (e) {
          console.warn("Failed to fetch 2FA status", e);
        }
      };
      fetch2faStatus();
    }
  }, [activeTab, accessToken, apiBaseUrl]);

  const handleSetup2fa = async () => {
    setTotpError("");
    setTotpMessage("");
    try {
      const res = await fetch(`${apiBaseUrl}/auth/2fa/setup`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${accessToken}`,
          "Content-Type": "application/json"
        }
      });
      const data = await res.json();
      if (res.ok) {
        setTotpSecret(data.secret);
        setTotpProvUri(data.provisioning_uri);
        setIs2faSetupMode(true);
      } else {
        setTotpError(data.detail || "Failed to initialize 2FA setup");
      }
    } catch (e) {
      setTotpError("Network error initializing 2FA setup");
    }
  };

  const handleVerify2faSetup = async () => {
    setTotpError("");
    setTotpMessage("");
    try {
      const res = await fetch(`${apiBaseUrl}/auth/2fa/verify-setup`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${accessToken}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ code: totpCode })
      });
      const data = await res.json();
      if (res.ok) {
        setTwoFactorEnabled(true);
        setIs2faSetupMode(false);
        setTotpCode("");
        setTotpMessage("Two-factor authentication successfully enabled!");
      } else {
        setTotpError(data.detail || "Verification failed");
      }
    } catch (e) {
      setTotpError("Network error verifying 2FA code");
    }
  };

  const handleDisable2fa = async () => {
    setTotpError("");
    setTotpMessage("");
    const code = prompt("Enter 6-digit TOTP code to confirm disabling 2FA:");
    if (!code) return;
    try {
      const res = await fetch(`${apiBaseUrl}/auth/2fa/disable`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${accessToken}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ code })
      });
      const data = await res.json();
      if (res.ok) {
        setTwoFactorEnabled(false);
        setTotpMessage("Two-factor authentication successfully disabled.");
      } else {
        setTotpError(data.detail || "Unable to disable 2FA");
      }
    } catch (e) {
      setTotpError("Network error disabling 2FA");
    }
  };

  // Rclone interactive simulator
  const [isRcloneRefreshing, setIsRcloneRefreshing] = useState<boolean>(false);
  const [isRcloneEnabled, setIsRcloneEnabled] = useState<boolean>(false);
  const [rcloneRemotePath, setRcloneRemotePath] = useState<string>("gdrive:media");
  const [isRcloneSaving, setIsRcloneSaving] = useState<boolean>(false);

  // Fetch rclone cloud storage settings from backend
  useEffect(() => {
    const fetchSystemSettings = async () => {
      try {
        const token = localStorage.getItem("stream_access_token");
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;
        const res = await fetch(`${apiBaseUrl}/system/settings`, { headers });
        if (res.ok) {
          const data = await res.json();
          setIsRcloneEnabled(data.storageEngine === "CLOUD");
          setRcloneRemotePath(data.rcloneRemotePath || "gdrive:media");
        }
      } catch (err) {
        console.error("Failed to fetch server settings:", err);
      }
    };
    fetchSystemSettings();
  }, []);

  const handleUpdateSystemSettings = async (enabled: boolean, path: string) => {
    setIsRcloneSaving(true);
    try {
      const token = localStorage.getItem("stream_access_token");
      const headers: Record<string, string> = {
        "Content-Type": "application/json"
      };
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const res = await fetch(`${apiBaseUrl}/system/settings`, {
        method: "POST",
        headers: headers,
        body: JSON.stringify({
          storageEngine: enabled ? "CLOUD" : "LOCAL",
          rcloneRemotePath: path
        })
      });
      if (res.ok) {
        const data = await res.json();
        setIsRcloneEnabled(data.storageEngine === "CLOUD");
        setRcloneRemotePath(data.rcloneRemotePath);
      }
    } catch (err) {
      console.error("Failed to save server settings:", err);
    } finally {
      setIsRcloneSaving(false);
    }
  };

  // Profile manager modal states (inline settings)
  const [isCreateModalOpen, setIsCreateModalOpen] = useState<boolean>(false);
  const [isEditModalOpen, setIsEditModalOpen] = useState<boolean>(false);
  const [editingProfile, setEditingProfile] = useState<Profile | null>(null);
  const [newProfileName, setNewProfileName] = useState<string>("");
  const [newAvatarColor, setNewAvatarColor] = useState<string>("from-blue-600 to-indigo-600");
  const [editProfileName, setEditProfileName] = useState<string>("");
  const [editAvatarColor, setEditAvatarColor] = useState<string>("");

  // Persist downloads queue
  useEffect(() => {
    localStorage.setItem("stream_downloads", JSON.stringify(downloads));
  }, [downloads]);

  // Persist preferences
  useEffect(() => {
    localStorage.setItem("stream_pref_lang", language);
  }, [language]);
  useEffect(() => {
    localStorage.setItem("stream_pref_quality", quality);
  }, [quality]);
  useEffect(() => {
    localStorage.setItem("stream_pref_autoplay", autoplay ? "true" : "false");
  }, [autoplay]);

  // Real-time Server-Sent Events (SSE) connection to track active queue metrics
  useEffect(() => {
    if (activeTab !== "downloads") return;

    console.log("[Dashboard] Initializing Server-Sent Events stream for queue tracking...");
    const token = localStorage.getItem("stream_access_token");
    const streamUrl = token 
      ? `${apiBaseUrl}/downloads/stream?token=${encodeURIComponent(token)}`
      : `${apiBaseUrl}/downloads/stream`;
    const eventSource = new EventSource(streamUrl);

    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (Array.isArray(data)) {
          setDownloads(data);
        }
      } catch (err) {
        console.error("[Dashboard] Error parsing SSE payload:", err);
      }
    };

    eventSource.onerror = (err) => {
      console.warn("[Dashboard] EventSource connection failure. Retrying...", err);
    };

    return () => {
      console.log("[Dashboard] Closing EventSource stream.");
      eventSource.close();
    };
  }, [activeTab, apiBaseUrl]);

  const handleAddDownload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!dlTmdbId.trim() || !dlVideoUrl.trim()) return;

    try {
      const payload: any = {
        tmdb_id: parseInt(dlTmdbId.trim()),
        media_type: dlMediaType,
        video_url: dlVideoUrl.trim(),
        audio_url: dlAudioUrl.trim() || null,
        headers: {}
      };
      if (dlMediaType === "tv") {
        payload.season = dlSeason.trim() ? parseInt(dlSeason.trim()) : null;
        payload.episode = dlEpisode.trim() ? parseInt(dlEpisode.trim()) : null;
      }

      const res = await fetch(`${apiBaseUrl}/add-movie`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${apiBearerToken}`,
        },
        body: JSON.stringify(payload),
      });

      if (res.ok) {
        setDlTmdbId("");
        setDlSeason("");
        setDlEpisode("");
        setDlVideoUrl("");
        setDlAudioUrl("");
      } else {
        const errData = await res.json();
        alert(`Failed to ingest media: ${errData.detail || "Unknown error"}`);
      }
    } catch (err) {
      console.error("[Dashboard] Ingestion POST failed:", err);
      alert("Network error: Could not reach the backend server.");
    }
  };

  const handlePreFillDownload = (tmdbId: string, mediaType: string) => {
    onTabChange("downloads");
    setDlTmdbId(tmdbId);
    setDlMediaType(mediaType);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const handleDeleteDownload = async (id: string) => {
    if (confirm("Are you sure you want to delete this download task?")) {
      try {
        await fetch(`${apiBaseUrl}/downloads/${id}`, {
          method: "DELETE",
          headers: {
            "Authorization": `Bearer ${apiBearerToken}`,
          },
        });
      } catch (err) {
        console.error("Failed to delete task:", err);
      }
    }
  };

  const handleRefreshRclone = () => {
    setIsRcloneRefreshing(true);
    setTimeout(() => {
      setIsRcloneRefreshing(false);
    }, 1500);
  };

  const handleCreateProfile = (e: React.FormEvent) => {
    e.preventDefault();
    if (!newProfileName.trim()) return;
    const newP: Profile = {
      id: Date.now().toString(),
      name: newProfileName.trim(),
      avatarColor: newAvatarColor,
    };
    setProfiles((prev) => [...prev, newP]);
    setNewProfileName("");
    setIsCreateModalOpen(false);
  };

  const handleSaveEditProfile = (e: React.FormEvent) => {
    e.preventDefault();
    if (!editingProfile || !editProfileName.trim()) return;
    setProfiles((prev) =>
      prev.map((p) =>
        p.id === editingProfile.id
          ? { ...p, name: editProfileName.trim(), avatarColor: editAvatarColor }
          : p
      )
    );
    setEditingProfile(null);
    setIsEditModalOpen(false);
  };

  const handleDeleteProfile = (id: string) => {
    if (confirm("Are you sure you want to delete this profile?")) {
      setProfiles((prev) => prev.filter((p) => p.id !== id));
      if (editingProfile?.id === id) {
        setEditingProfile(null);
        setIsEditModalOpen(false);
      }
    }
  };

  useEffect(() => {
    // Globally exposed function waiting to accept a JSON data object from the server
    (window as any).updateDetailsModal = (data: any) => {
      console.log("[TMDB API Bridge] Received dynamic movie data:", data);
      setTmdbJson(data);
    };

    return () => {
      delete (window as any).updateDetailsModal;
    };
  }, []);

  // Parse duration string to seconds
  const parseDurationToSeconds = (durationStr: string): number => {
    if (!durationStr) return 3600; // 1 hour fallback
    let totalSeconds = 0;
    
    if (durationStr.includes(":")) {
      const parts = durationStr.split(":").map(Number);
      if (parts.length === 3) {
        return parts[0] * 3600 + parts[1] * 60 + parts[2];
      } else if (parts.length === 2) {
        return parts[0] * 60 + parts[1];
      }
    }

    const hourMatch = durationStr.match(/(\d+)\s*h/);
    const minMatch = durationStr.match(/(\d+)\s*m/);
    const secMatch = durationStr.match(/(\d+)\s*s/);

    if (hourMatch) totalSeconds += parseInt(hourMatch[1]) * 3600;
    if (minMatch) totalSeconds += parseInt(minMatch[1]) * 60;
    if (secMatch) totalSeconds += parseInt(secMatch[1]);

    if (totalSeconds === 0) {
      const justNumbers = durationStr.match(/(\d+)/);
      if (justNumbers) {
        totalSeconds = parseInt(justNumbers[1]) * 60;
      }
    }

    return totalSeconds || 3600;
  };

  const fetchContinueWatching = async () => {
    if (!activeProfile) return;
    
    // 1. Load from local fallback
    const localKey = `continue_watching_${activeProfile.id}`;
    let sessions: PlaybackSession[] = [];
    try {
      const saved = localStorage.getItem(localKey);
      if (saved) {
        sessions = JSON.parse(saved);
      }
    } catch (e) {}

    // 2. Fetch from API
    try {
      const token = localStorage.getItem("stream_access_token");
      const headers: Record<string, string> = {};
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const res = await fetch(`${apiBaseUrl}/track/${activeProfile.id}`, { headers });
      if (res.ok) {
        const data = await res.json();
        if (Array.isArray(data)) {
          sessions = data;
          localStorage.setItem(localKey, JSON.stringify(data));
        }
      }
    } catch (err) {
      console.warn("[Frontend] Failed to fetch continue watching from API, using fallback.");
    }

    // Sort by updatedAt descending
    sessions.sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime());
    setContinueWatching(sessions);
  };

  useEffect(() => {
    if (activeProfile) {
      fetchContinueWatching();
    }
  }, [activeProfile, apiBaseUrl, movies]);

  // Watchlist persistence per-profile (loaded from server)
  const fetchWatchlist = async () => {
    if (!activeProfile) return;
    
    // 1. Load from local fallback
    const localKey = `watchlist_${activeProfile.id}`;
    let list: string[] = [];
    try {
      const saved = localStorage.getItem(localKey);
      if (saved) {
        list = JSON.parse(saved);
      }
    } catch (e) {}

    // 2. Fetch from API
    try {
      const token = localStorage.getItem("stream_access_token");
      const headers: Record<string, string> = {};
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const res = await fetch(`${apiBaseUrl}/watchlist/${activeProfile.id}`, { headers });
      if (res.ok) {
        const data = await res.json();
        if (Array.isArray(data)) {
          list = data;
          localStorage.setItem(localKey, JSON.stringify(data));
        }
      }
    } catch (err) {
      console.warn("[Frontend] Failed to fetch watchlist from API, using fallback.");
    }

    setWatchlist(list);
  };

  useEffect(() => {
    if (activeProfile) {
      fetchWatchlist();
    }
  }, [activeProfile, apiBaseUrl]);

  const toggleWatchlist = async (movieId: string) => {
    if (!activeProfile) return;
    
    // Optimistic UI update
    setWatchlist((prev) => {
      const updated = prev.includes(movieId)
        ? prev.filter((id) => id !== movieId)
        : [...prev, movieId];
      localStorage.setItem(`watchlist_${activeProfile.id}`, JSON.stringify(updated));
      return updated;
    });

    try {
      const token = localStorage.getItem("stream_access_token");
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const res = await fetch(`${apiBaseUrl}/watchlist/toggle`, {
        method: "POST",
        headers: headers,
        body: JSON.stringify({
          profile_id: activeProfile.id,
          movie_id: movieId
        })
      });
      if (res.ok) {
        const data = await res.json();
        if (data && Array.isArray(data.watchlist)) {
          setWatchlist(data.watchlist);
          localStorage.setItem(`watchlist_${activeProfile.id}`, JSON.stringify(data.watchlist));
        }
      } else {
        console.warn("[Frontend] Failed to toggle watchlist on server, status:", res.status);
      }
    } catch (err) {
      console.warn("[Frontend] Error toggling watchlist on server:", err);
    }
  };

  // For header transparent-to-dark transition on scroll
  const [isScrolled, setIsScrolled] = useState<boolean>(false);

  useEffect(() => {
    const handleScroll = () => {
      if (window.scrollY > 30) {
        setIsScrolled(true);
      } else {
        setIsScrolled(false);
      }
    };
    window.addEventListener("scroll", handleScroll);
    return () => window.removeEventListener("scroll", handleScroll);
  }, []);

  // Keyboard Navigation for Movie Posters
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Ignore if user is typing in search query input, API input, etc.
      if (
        document.activeElement instanceof HTMLInputElement ||
        document.activeElement instanceof HTMLTextAreaElement
      ) {
        return;
      }

      // If details modal is open
      if (selectedMovieForDetails) {
        if (e.key === "Escape") {
          e.preventDefault();
          setSelectedMovieForDetails(null);
        } else if (e.key === "Enter") {
          e.preventDefault();
          onPlayMovie(selectedMovieForDetails);
          setSelectedMovieForDetails(null);
        }
        return;
      }

      switch (e.key) {
        case "ArrowLeft":
          e.preventDefault();
          navigatePosters("left");
          break;
        case "ArrowRight":
          e.preventDefault();
          navigatePosters("right");
          break;
        case "ArrowUp":
          e.preventDefault();
          navigatePosters("up");
          break;
        case "ArrowDown":
          e.preventDefault();
          navigatePosters("down");
          break;
        case "Enter":
          {
            const current = document.activeElement as HTMLElement;
            if (current && current.getAttribute("id")?.startsWith("movie-card-")) {
              e.preventDefault();
              current.click();
            }
          }
          break;
        default:
          break;
      }
    };

    const navigatePosters = (direction: "left" | "right" | "up" | "down") => {
      const posters = Array.from(document.querySelectorAll('[id^="movie-card-"]')) as HTMLElement[];
      if (posters.length === 0) return;

      const current = document.activeElement as HTMLElement;
      if (!current || !posters.includes(current)) {
        // If nothing is focused, focus the first poster in the catalog
        posters[0].focus();
        posters[0].scrollIntoView({ behavior: "smooth", block: "nearest" });
        return;
      }

      const currentRect = current.getBoundingClientRect();
      let bestCandidate: HTMLElement | null = null;
      let minDistance = Infinity;

      for (const poster of posters) {
        if (poster === current) continue;
        const rect = poster.getBoundingClientRect();

        let isCorrectDirection = false;
        let distance = 0;

        const currentCenterX = currentRect.left + currentRect.width / 2;
        const currentCenterY = currentRect.top + currentRect.height / 2;
        const rectCenterX = rect.left + rect.width / 2;
        const rectCenterY = rect.top + rect.height / 2;

        if (direction === "left") {
          isCorrectDirection = rectCenterX < currentCenterX && Math.abs(rectCenterY - currentCenterY) < 60;
          distance = currentCenterX - rectCenterX;
        } else if (direction === "right") {
          isCorrectDirection = rectCenterX > currentCenterX && Math.abs(rectCenterY - currentCenterY) < 60;
          distance = rectCenterX - currentCenterX;
        } else if (direction === "up") {
          isCorrectDirection = rectCenterY < currentCenterY;
          const dx = rectCenterX - currentCenterX;
          const dy = currentCenterY - rectCenterY;
          distance = dy * 2.5 + Math.abs(dx);
        } else if (direction === "down") {
          isCorrectDirection = rectCenterY > currentCenterY;
          const dx = rectCenterX - currentCenterX;
          const dy = rectCenterY - currentCenterY;
          distance = dy * 2.5 + Math.abs(dx);
        }

        if (isCorrectDirection && distance < minDistance) {
          minDistance = distance;
          bestCandidate = poster;
        }
      }

      if (bestCandidate) {
        bestCandidate.focus();
        bestCandidate.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [selectedMovieForDetails, onPlayMovie]);

  // API Fetch implementation
  const fetchMoviesData = async () => {
    setIsLoading(true);
    setError(null);
    try {
      console.log(`[Frontend] Fetching movies from API Base: ${apiBaseUrl}`);
      
      // Fetch movies
      const token = localStorage.getItem("stream_access_token");
      const headers: Record<string, string> = {};
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const moviesRes = await fetch(`${apiBaseUrl}/movies`, { headers });
      if (!moviesRes.ok) {
        throw new Error(`Failed to fetch movies (Status: ${moviesRes.status})`);
      }
      const moviesData = await moviesRes.json();
      
      if (Array.isArray(moviesData)) {
        setMovies(moviesData);
      } else {
        throw new Error("Invalid movies data structure received. Expected an array.");
      }

      // Fetch featured movie specifically, or pick the first one
      try {
        const token = localStorage.getItem("stream_access_token");
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;
        const featuredRes = await fetch(`${apiBaseUrl}/movies/featured`, { headers });
        if (featuredRes.ok) {
          const featuredData = await featuredRes.json();
          if (featuredData && typeof featuredData === "object" && featuredData.id) {
            setFeaturedMovie(featuredData);
          } else if (moviesData.length > 0) {
            setFeaturedMovie(moviesData[0]);
          }
        } else if (moviesData.length > 0) {
          setFeaturedMovie(moviesData[0]);
        }
      } catch (err) {
        console.warn("[Frontend] Could not fetch separate featured movie, using first movie as fallback:", err);
        if (moviesData.length > 0) {
          setFeaturedMovie(moviesData[0]);
        }
      }

    } catch (err: any) {
      console.error("[Frontend] Fetch error details:", err);
      setError(err?.message || "Connection to streaming API failed.");
      setMovies([]);
      setFeaturedMovie(null);
    } finally {
      setIsLoading(false);
    }
  };

  const fetchDiscoverData = async () => {
    const typeParam = activeTab === "series" ? "series" : "movie";
    try {
      const token = localStorage.getItem("stream_access_token");
      const headers: Record<string, string> = {};
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const discActionRes = await fetch(`${apiBaseUrl}/discover?category=action&type=${typeParam}`, { headers });
      if (discActionRes.ok) {
        const discActionData = await discActionRes.json();
        setDiscoverAction(discActionData);
      }
    } catch (err) {
      console.warn("[Frontend] Failed to fetch Action discover list:", err);
    }

    try {
      const token = localStorage.getItem("stream_access_token");
      const headers: Record<string, string> = {};
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const discScifiRes = await fetch(`${apiBaseUrl}/discover?category=scifi&type=${typeParam}`, { headers });
      if (discScifiRes.ok) {
        const discScifiData = await discScifiRes.json();
        setDiscoverScifi(discScifiData);
      }
    } catch (err) {
      console.warn("[Frontend] Failed to fetch Sci-Fi discover list:", err);
    }
  };

  // Initial load
  useEffect(() => {
    fetchMoviesData();
  }, [apiBaseUrl]);

  // Tab-dependent discover loading
  useEffect(() => {
    fetchDiscoverData();
  }, [activeTab, apiBaseUrl]);

  // Handle manual JSON paste for developer preview (complying strictly with no hardcoded lists in prod code)
  const handleLoadManualJson = () => {
    try {
      const parsed = JSON.parse(manualJsonInput);
      if (Array.isArray(parsed)) {
        setMovies(parsed);
        if (parsed.length > 0) {
          setFeaturedMovie(parsed[0]);
        }
        setError(null);
        setJsonSuccess(true);
        setTimeout(() => setJsonSuccess(false), 3000);
      } else if (parsed && typeof parsed === "object" && Array.isArray(parsed.movies)) {
        setMovies(parsed.movies);
        setFeaturedMovie(parsed.featured || parsed.movies[0] || null);
        setError(null);
        setJsonSuccess(true);
        setTimeout(() => setJsonSuccess(false), 3000);
      } else {
        alert("Invalid format! Please provide an array of Movie objects or an object with { movies: Movie[] }.");
      }
    } catch (err: any) {
      alert(`JSON parsing failed: ${err.message}`);
    }
  };

  // Extract unique genres from movies
  const genres = Array.from(new Set(movies.flatMap((m) => m.genres || []))) as string[];

  // Filter movies and find appropriate featured item for current view
  const currentMovies = (() => {
    if (activeTab === "movies") {
      return movies.filter((m) => m.type === "movie" || !m.type);
    }
    if (activeTab === "series") {
      return movies.filter((m) => m.type === "series");
    }
    return movies;
  })();

  const currentFeaturedMovie = (() => {
    const mainFeatured = featuredMovie || discoverAction[0] || discoverScifi[0] || null;
    if (!mainFeatured) return null;
    if (activeTab === "movies") {
      if (mainFeatured.type === "movie" || !mainFeatured.type) {
        return mainFeatured;
      }
      return currentMovies[0] || discoverAction[0] || discoverScifi[0] || null;
    }
    if (activeTab === "series") {
      if (mainFeatured.type === "series") {
        return mainFeatured;
      }
      return currentMovies[0] || discoverAction.find(m => m.type === "series") || discoverScifi.find(m => m.type === "series") || null;
    }
    return mainFeatured;
  })();

  const isHeroMovieDownloaded = (() => {
    if (!currentFeaturedMovie) return false;
    const heroTmdbId = (currentFeaturedMovie as any).tmdb_id || (currentFeaturedMovie as any).tmdbId;
    return movies.some(
      (m) => m.id === currentFeaturedMovie.id || 
             (heroTmdbId && (m.id === `m_${heroTmdbId}` || m.id === `tv_${heroTmdbId}`))
    );
  })();

  const currentGenres = Array.from(new Set(currentMovies.flatMap((m) => m.genres || []))) as string[];

  // Filter movies by search query
  const filteredMovies = movies.filter((movie) => {
    const query = searchQuery.toLowerCase();
    return (
      movie.title.toLowerCase().includes(query) ||
      movie.description.toLowerCase().includes(query) ||
      movie.genres.some((g) => g.toLowerCase().includes(query)) ||
      (movie.director && movie.director.toLowerCase().includes(query))
    );
  });

  const getTabClass = (tabId: string) => {
    const isActive = activeTab === tabId && !searchQuery;
    if (activeProfileTheme === "gemini") {
      return isActive 
        ? "text-purple-400 font-bold" 
        : "text-zinc-400";
    }
    if (activeProfileTheme === "netflix") {
      return isActive
        ? "text-white font-bold uppercase tracking-widest text-[11px] border-b-2 border-red-600 pb-1 shadow-[0_4px_12px_rgba(229,9,20,0.4)]"
        : "text-zinc-400 uppercase tracking-widest text-[11px] hover:text-white transition-colors duration-300";
    }
    // Default / fallback for other themes
    return isActive
      ? "text-white font-bold border-b-2 border-[var(--theme-accent)] pb-1"
      : "text-zinc-400 hover:text-white transition-colors duration-200";
  };

  return (
    <div id="main-dashboard" className={`min-h-screen bg-[var(--theme-bg)] text-[#E5E5E5] transition-all duration-300 relative overflow-x-hidden ${
      activeProfileTheme === "gemini" ? "font-mono" : activeProfileTheme === "apple" ? "font-serif" : "font-sans"
    }`}>
      
      {/* HEADER NAVBAR */}
      <header
        id="navbar-header"
        className={`fixed z-50 flex flex-col justify-center transition-all duration-150 ${
          activeProfileTheme === "apple" 
            ? "mx-auto my-3 left-1/2 -translate-x-1/2 w-[95%] max-w-5xl rounded-2xl bg-black/45 backdrop-blur-xl border border-white/10 px-4 py-2.5 shadow-2xl" 
            : `top-0 left-0 right-0 px-4 md:px-12 py-3 md:py-4 ${
                isScrolled 
                  ? "bg-[var(--theme-bg)] border-b border-zinc-800/80 shadow-2xl" 
                  : "bg-gradient-to-b from-black/90 to-transparent border-b-0 border-transparent"
              }`
        }`}
      >
        <div className="flex items-center justify-between w-full">
        <div className="flex items-center space-x-8 md:space-x-10">
          {/* Logo / Brand */}
          <div className="flex items-center space-x-2 cursor-pointer animate-fade-in" onClick={() => { setSearchQuery(""); onTabChange("home"); }}>
            {activeProfileTheme === "prime" ? (
              <div className="flex items-baseline space-x-0.5">
                <span className="text-xl font-black text-white tracking-tight font-sans">prime</span>
                <span className="text-xl font-light text-cyan-400 font-sans">video</span>
              </div>
            ) : activeProfileTheme === "apple" ? (
              <span className="text-2xl font-serif italic font-extrabold text-white tracking-tight">
                tv<sup className="text-xs font-sans not-italic font-normal">+</sup>
              </span>
            ) : activeProfileTheme === "gemini" ? (
              <span className="text-lg font-black text-transparent bg-clip-text bg-gradient-to-r from-purple-400 to-cyan-400 tracking-widest uppercase font-mono">
                GEMINI
              </span>
            ) : (
              <span className="text-3xl font-normal text-[var(--theme-accent)] tracking-tight uppercase" style={{ fontFamily: "var(--theme-title-font, 'Bebas Neue', sans-serif)" }}>
                StreamHome
              </span>
            )}
          </div>

          {/* Navigation Links (Desktop) */}
          <nav className={`hidden md:flex items-center space-x-6 text-sm font-medium ${
            activeProfileTheme === "gemini" ? "text-xs" : ""
          }`}>
            <button
              onClick={() => { setSearchQuery(""); onTabChange("home"); }}
              className={`cursor-pointer ${getTabClass("home")}`}
            >
              {activeProfileTheme === "gemini" ? "[home]" : "Home"}
            </button>
            <button
              onClick={() => { setSearchQuery(""); onTabChange("movies"); }}
              className={`cursor-pointer ${getTabClass("movies")}`}
            >
              {activeProfileTheme === "gemini" ? "[movies]" : activeProfileTheme === "prime" ? "Movies" : "Movies"}
            </button>
            <button
              onClick={() => { setSearchQuery(""); onTabChange("series"); }}
              className={`cursor-pointer ${getTabClass("series")}`}
            >
              {activeProfileTheme === "gemini" ? "[series]" : activeProfileTheme === "prime" ? "Tv Shows" : "Series"}
            </button>
            <button
              onClick={() => { setSearchQuery(""); onTabChange("mylist"); }}
              className={`cursor-pointer ${getTabClass("mylist")}`}
            >
              {activeProfileTheme === "gemini" ? "[db_list]" : activeProfileTheme === "prime" ? "My List" : activeProfileTheme === "apple" ? "WatchList" : "My List"}
            </button>
            <button
              onClick={() => { setSearchQuery(""); onTabChange("downloads"); }}
              className={`cursor-pointer ${getTabClass("downloads")}`}
            >
              {activeProfileTheme === "gemini" ? "[pipeline]" : activeProfileTheme === "prime" ? "Downloads" : activeProfileTheme === "apple" ? "Library" : "Downloads"}
            </button>
            <button
              onClick={() => { setSearchQuery(""); onTabChange("settings"); }}
              className={`cursor-pointer ${getTabClass("settings")}`}
            >
              {activeProfileTheme === "gemini" ? "[config]" : "Settings"}
            </button>
          </nav>
        </div>

        {/* Header Right */}
        <div className="flex items-center space-x-3 md:space-x-6">
          {/* Search bar */}
          <div className="relative flex items-center">
            <Search className="w-4 h-4 text-gray-400 absolute left-3 pointer-events-none" />
            <input
              type="text"
              placeholder="Titles, genres..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="bg-black/40 border border-white/20 rounded-sm py-1.5 pl-9 pr-3 text-xs text-white w-32 sm:w-48 md:w-64 focus:w-44 sm:focus:w-64 md:focus:w-72 focus:border-white/40 transition-all outline-none"
              id="search-input"
            />
          </div>

          {/* Developer / API Settings Button */}
          <button
            onClick={() => setIsApiPanelOpen(true)}
            className="p-2 hover:bg-zinc-800 rounded-full text-zinc-400 hover:text-white transition-colors duration-200 relative"
            title="Configure Backend API Connection"
            id="open-api-panel-btn"
          >
            <Database className="w-5 h-5" />
            {error && (
              <span className="absolute top-1 right-1 w-2.5 h-2.5 bg-amber-500 rounded-full animate-pulse" />
            )}
          </button>

          {/* User Profile Info */}
          <div className="flex items-center space-x-2 group relative">
            <div className={`w-8 h-8 rounded bg-gradient-to-tr ${activeProfile.avatarColor} flex items-center justify-center border border-zinc-700 font-bold text-xs cursor-pointer text-white`}>
              {activeProfile.name[0]}
            </div>
            <span className="hidden sm:inline text-sm font-medium text-zinc-300 group-hover:text-white cursor-pointer select-none">
              {activeProfile.name}
            </span>

            {/* Logout drop overlay dropdown */}
            <div className="absolute right-0 top-full pt-2 opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all duration-200 z-50">
              <div className="bg-[#121212] border border-zinc-800 rounded-md shadow-2xl p-1.5 w-48 space-y-0.5">
                <button
                  onClick={() => onTabChange("profile-settings")}
                  className="w-full flex items-center space-x-2.5 px-3 py-2 text-xs font-semibold text-zinc-400 hover:text-white hover:bg-zinc-800/60 rounded-md transition-colors text-left"
                >
                  <User className="w-4 h-4 text-zinc-500" />
                  <span>Profile Settings</span>
                </button>
                <button
                  onClick={() => onTabChange("settings")}
                  className="w-full flex items-center space-x-2.5 px-3 py-2 text-xs font-semibold text-zinc-400 hover:text-white hover:bg-zinc-800/60 rounded-md transition-colors text-left"
                >
                  <Settings className="w-4 h-4 text-zinc-500" />
                  <span>General Settings</span>
                </button>
                <button
                  onClick={() => onTabChange("downloads")}
                  className="w-full flex items-center space-x-2.5 px-3 py-2 text-xs font-semibold text-zinc-400 hover:text-white hover:bg-zinc-800/60 rounded-md transition-colors text-left"
                >
                  <Clock className="w-4 h-4 text-zinc-500" />
                  <span>Download Manager</span>
                </button>
                <div className="h-px bg-zinc-800 my-1" />
                <button
                  onClick={onLogout}
                  className="w-full flex items-center space-x-2.5 px-3 py-2 text-xs font-semibold text-zinc-400 hover:text-white hover:bg-zinc-800/60 rounded-md transition-colors text-left"
                >
                  <LogOut className="w-4 h-4 text-zinc-500" />
                  <span>Switch Profile</span>
                </button>
                <button
                  onClick={onAccountLogout}
                  className="w-full flex items-center space-x-2.5 px-3 py-2 text-xs font-semibold text-red-400 hover:text-red-300 hover:bg-zinc-800/60 rounded-md transition-colors text-left"
                >
                  <LogOut className="w-4 h-4 text-red-500" />
                  <span>Sign Out Account</span>
                </button>
              </div>
            </div>
          </div>
        </div>
        </div>
        
        {/* Navigation Links (Mobile Top Text) */}
        <nav className="flex md:hidden items-center justify-center space-x-6 w-full mt-3 text-xs font-light text-white/80">
          <button onClick={() => { setSearchQuery(""); onTabChange("home"); }} className={`cursor-pointer pb-1 ${activeTab === "home" && !searchQuery ? "text-white font-medium border-b border-white" : ""}`}>
            Home
          </button>
          <button onClick={() => { setSearchQuery(""); onTabChange("movies"); }} className={`cursor-pointer pb-1 ${activeTab === "movies" && !searchQuery ? "text-white font-medium border-b border-white" : ""}`}>
            Movies
          </button>
          <button onClick={() => { setSearchQuery(""); onTabChange("series"); }} className={`cursor-pointer pb-1 ${activeTab === "series" && !searchQuery ? "text-white font-medium border-b border-white" : ""}`}>
            TV Series
          </button>
        </nav>
      </header>

      {/* Bottom Navigation Bar (Mobile) */}
      <div className="md:hidden fixed bottom-0 left-0 right-0 z-50 bg-black/90 backdrop-blur-xl border-t border-white/10 pb-safe pt-2 px-6 flex items-center justify-between shadow-2xl">
        <button onClick={() => { setSearchQuery(""); onTabChange("home"); }} className={`flex flex-col items-center justify-center p-2 transition ${activeTab === "home" && !searchQuery ? "text-white scale-110" : "text-white/50"}`}>
          <Database className="w-6 h-6" />
        </button>
        <button onClick={() => { setSearchQuery(""); onTabChange("mylist"); }} className={`flex flex-col items-center justify-center p-2 transition ${activeTab === "mylist" && !searchQuery ? "text-white scale-110" : "text-white/50"}`}>
          <Plus className="w-6 h-6" />
        </button>
        <button onClick={() => { setSearchQuery(""); onTabChange("downloads"); }} className={`flex flex-col items-center justify-center p-2 transition ${activeTab === "downloads" && !searchQuery ? "text-white scale-110" : "text-white/50"}`}>
          <Download className="w-6 h-6" />
        </button>
        <button onClick={() => { setSearchQuery(""); onTabChange("settings"); }} className={`flex flex-col items-center justify-center p-2 transition ${activeTab === "settings" && !searchQuery ? "text-white scale-110" : "text-white/50"}`}>
          <Settings className="w-6 h-6" />
        </button>
      </div>

      {selectedMovieForDetails && activeProfile.theme && activeProfile.theme !== "netflix" ? (
        <ThemeDetailsPage
          activeProfile={activeProfile}
          selectedMovieForDetails={selectedMovieForDetails}
          setSelectedMovieForDetails={setSelectedMovieForDetails}
          tmdbJson={tmdbJson}
          setTmdbJson={setTmdbJson}
          movies={movies}
          onPlayMovie={onPlayMovie}
          continueWatching={continueWatching}
          watchlist={watchlist}
          toggleWatchlist={toggleWatchlist}
          selectedSeason={selectedSeason}
          setSelectedSeason={setSelectedSeason}
          onAddDownload={handlePreFillDownload}
          apiBaseUrl={apiBaseUrl}
          isFetchingEpisodes={isFetchingEpisodes}
        />
      ) : (
        <>
          {/* SEARCH RESULTS VIEW */}
          {searchQuery && (() => {
            const localTmdbIds = new Set(filteredMovies.map(m => {
              const idDigits = m.id.replace("m_", "").replace("tv_", "").replace("discover_", "");
              return idDigits;
            }));
            
            const uniqueTmdbResults = tmdbSearchResults.filter(item => {
              const idStr = String(item.id).replace("discover_", "").replace("m_", "").replace("tv_", "");
              return !localTmdbIds.has(idStr);
            });
            
            const combinedResults = [...filteredMovies, ...uniqueTmdbResults];
            
            return (
              <div id="search-results-section" className="pt-28 px-6 md:px-12 pb-12">
                <h2 className="text-xl md:text-2xl font-sans font-medium tracking-tight mb-6">
                  Search results for <span className="text-red-500">"{searchQuery}"</span>
                </h2>
                {isSearchingTmdb && combinedResults.length === 0 ? (
                  <div className="flex items-center justify-center py-20 text-zinc-500">
                    <RefreshCw className="w-6 h-6 text-red-600 animate-spin mr-3" />
                    <span>Searching TMDB catalog...</span>
                  </div>
                ) : combinedResults.length === 0 ? (
                  <div className="text-center py-20 text-zinc-500">
                    <p className="text-lg">No matches found.</p>
                    <p className="text-sm mt-1">Try searching another genre, title, or director.</p>
                  </div>
                ) : (
                  <div className="relative group/row pb-20 md:pb-0">
                    <div className="grid grid-cols-[repeat(auto-fill,minmax(110px,1fr))] sm:grid-cols-[repeat(auto-fill,minmax(150px,1fr))] md:grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3 sm:gap-4 md:gap-5">
                      {combinedResults.map((movie) => (
                        <MovieCard key={movie.id} movie={movie} onPlay={setSelectedMovieForDetails} moviesOnServer={movies} />
                      ))}
                    </div>
                  </div>
                )}
              </div>
            );
          })()}

      {/* DEFAULT DASHBOARD FLOW */}
      {!searchQuery && (
        <>
          {(activeTab === "home" || activeTab === "movies" || activeTab === "series") ? (
            <>
              {/* HERO FEATURED BANNER */}
              <section id="featured-banner" className="relative h-[56.25vw] md:h-[70vh] min-h-[420px] max-h-[720px] w-full flex items-end overflow-hidden">
                <AnimatePresence mode="wait">
                  {isLoading ? (
                    <div key="loading-banner" className="absolute inset-0 bg-zinc-900 animate-pulse flex items-center justify-center">
                      <div className="text-zinc-500 text-sm">
                        <span>Loading Hero Title...</span>
                      </div>
                    </div>
                  ) : currentFeaturedMovie ? (
                    <motion.div
                      key={currentFeaturedMovie.id}
                      initial={{ opacity: 0 }}
                      animate={{ opacity: 1 }}
                      exit={{ opacity: 0 }}
                      transition={{ duration: 0.8 }}
                      className="absolute inset-0"
                    >
                      <div
                        className={`absolute inset-0 bg-cover bg-center transition-all duration-500 ${
                          activeProfileTheme === "netflix" ? "animate-kenburns" : ""
                        }`}
                        style={{
                          backgroundImage: `url("${encodeURI(currentFeaturedMovie.bannerUrl || currentFeaturedMovie.thumbnailUrl)}")`,
                        }}
                      />
                      {/* Top dark gradient to completely cover and fade out the top border/glare of the backdrop photo */}
                      <div className="absolute inset-x-0 top-0 h-40 bg-gradient-to-b from-[var(--theme-bg)] via-[var(--theme-bg)]/80 to-transparent z-10 pointer-events-none" />
                      
                      {/* Multi-step gradient overlays to match premium style */}
                      <div className="absolute inset-0 bg-gradient-to-r from-black/85 via-black/45 to-transparent z-10" />
                      <div className="absolute inset-x-0 bottom-0 h-1/2 bg-gradient-to-t from-[var(--theme-bg)] via-transparent to-transparent z-10" />
                      
                      {/* Deep bottom-to-top gradient to seamlessly fade into main content body */}
                      <div className="absolute inset-x-0 bottom-0 h-[28%] bg-gradient-to-t from-[var(--theme-bg)] via-[var(--theme-bg)]/80 to-transparent z-10 pointer-events-none" />

                      {/* Banner content */}
                      <div className={`absolute ${
                        activeProfileTheme === "netflix" ? "bottom-20 md:bottom-24" : "bottom-14"
                      } left-6 md:left-12 max-w-xl pr-6 z-10 ${
                        activeProfileTheme === "gemini" ? "font-mono" : activeProfileTheme === "apple" ? "font-serif" : "font-sans"
                      }`}>
                        <div className={`flex items-center space-x-3 mb-3 ${
                          activeProfileTheme === "netflix" ? "animate-fade-in-up" : ""
                        }`}>
                          <span className={`text-white font-extrabold text-[10px] uppercase tracking-widest px-2 py-0.5 rounded shadow-md ${
                            activeProfileTheme === "prime" ? "bg-cyan-600" :
                            activeProfileTheme === "apple" ? "bg-zinc-800 border border-white/20" :
                            activeProfileTheme === "gemini" ? "bg-[#a855f7] border border-cyan-400" : "bg-[var(--theme-accent)]"
                          }`}>
                            {currentFeaturedMovie.type === "series" ? "Featured Series" : "Featured Movie"}
                          </span>
                          <span className="text-zinc-300 font-medium text-xs">
                            {currentFeaturedMovie.releaseYear}
                          </span>
                          <span className="border border-zinc-500 text-zinc-300 font-semibold text-[10px] px-1.5 py-0.2 rounded uppercase">
                            {currentFeaturedMovie.rating || "PG-13"}
                          </span>
                          <span className="text-zinc-300 font-medium text-xs">
                            {currentFeaturedMovie.duration}
                          </span>
                        </div>

                        <h1 
                          className={`text-3xl md:text-5xl lg:text-6xl font-bold tracking-tight leading-none mb-4 text-white drop-shadow-lg ${
                            activeProfileTheme === "apple" ? "font-serif tracking-normal" : 
                            activeProfileTheme === "netflix" ? "font-normal uppercase tracking-wide" : "font-sans"
                          } ${
                            activeProfileTheme === "netflix" ? "animate-fade-in-up animation-delay-100" : ""
                          }`}
                          style={activeProfileTheme === "netflix" ? { fontFamily: "var(--theme-title-font, 'Bebas Neue', sans-serif)" } : undefined}
                        >
                          {currentFeaturedMovie.title}
                        </h1>

                        <p className={`text-zinc-300 text-sm md:text-base leading-relaxed mb-6 line-clamp-3 text-shadow-sm ${
                          activeProfileTheme === "netflix" ? "animate-fade-in-up animation-delay-200" : ""
                        }`}>
                          {currentFeaturedMovie.description}
                        </p>

                        <div className={`flex items-center space-x-4 ${
                          activeProfileTheme === "netflix" ? "animate-fade-in-up animation-delay-300" : ""
                        }`}>
                          {isHeroMovieDownloaded ? (
                            <button
                              onClick={() => onPlayMovie(currentFeaturedMovie)}
                              className={`flex items-center space-x-2.5 bg-white hover:bg-zinc-200 active:scale-95 text-black font-bold px-6 py-2.5 rounded-sm shadow-lg transition-all duration-200 cursor-pointer ${
                                activeProfileTheme === "apple" ? "rounded-full" : activeProfileTheme === "prime" ? "rounded-lg" : ""
                              }`}
                              id={`hero-play-btn-${currentFeaturedMovie.id}`}
                            >
                              <Play className="w-5 h-5 fill-current" />
                              <span>Play</span>
                            </button>
                          ) : (
                            <button
                              onClick={() => {
                                const tmdbId = (currentFeaturedMovie as any).tmdb_id || (currentFeaturedMovie as any).tmdbId;
                                if (tmdbId) {
                                  handlePreFillDownload(tmdbId.toString(), currentFeaturedMovie.type || "movie");
                                }
                              }}
                              className={`flex items-center space-x-2.5 bg-[var(--theme-accent)] hover:bg-[var(--theme-accent-hover)] active:scale-95 text-white font-bold px-6 py-2.5 rounded-sm shadow-lg transition-all duration-200 cursor-pointer ${
                                activeProfileTheme === "apple" ? "rounded-full" : activeProfileTheme === "prime" ? "rounded-lg" : ""
                              }`}
                              id={`hero-download-btn-${currentFeaturedMovie.id}`}
                            >
                              <Play className="w-5 h-5 fill-current" />
                              <span>Find Source / Add</span>
                            </button>
                          )}
                          <button
                            onClick={() => {
                              setSelectedMovieForDetails(currentFeaturedMovie);
                            }}
                            className={`flex items-center space-x-2 bg-zinc-650/70 hover:bg-zinc-650/50 active:scale-95 text-white font-bold px-6 py-2.5 rounded-sm shadow-lg backdrop-blur-md transition-all duration-200 ${
                              activeProfileTheme === "apple" ? "rounded-full" : activeProfileTheme === "prime" ? "rounded-lg" : ""
                            }`}
                          >
                            <Info className="w-5 h-5" />
                            <span>More Info</span>
                          </button>
                        </div>
                      </div>
                    </motion.div>
                  ) : (
                    <div key="no-banner" className="absolute inset-0 bg-gradient-to-t from-[#141414] to-zinc-900 flex items-center justify-center p-6 text-center">
                      <div className="max-w-md">
                        <AlertTriangle className="w-12 h-12 text-amber-500 mx-auto mb-4" />
                        <h3 className="text-lg md:text-xl font-semibold mb-2">No Content Loaded</h3>
                        <p className="text-zinc-400 text-sm mb-6">
                          No titles found for this category. The video platform is successfully built but requires connection to a live API or developer JSON.
                        </p>
                        <button
                          onClick={() => setIsApiPanelOpen(true)}
                          className="inline-flex items-center space-x-2 bg-red-600 hover:bg-red-700 text-white text-sm font-semibold px-5 py-2.5 rounded transition-all"
                        >
                          <Settings className="w-4 h-4" />
                          <span>Configure API Endpoint</span>
                        </button>
                      </div>
                    </div>
                  )}
                </AnimatePresence>
              </section>

              {/* STREAMING BROWSING ROWS */}
              <div id="movie-catalog-rows" className={`relative z-10 px-6 md:px-12 pb-24 space-y-12 ${
                activeProfileTheme === "netflix" ? "-mt-4 md:-mt-6 animate-fade-in-up" : "-mt-12"
              }`}>
                
                {/* Loading Skeletons */}
                {isLoading && (
                  <div className="space-y-10">
                    {[1, 2].map((rowIdx) => (
                      <div key={rowIdx} className="space-y-4">
                        <div className="h-6 w-48 bg-zinc-800 rounded animate-pulse" />
                        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-4">
                          {[1, 2, 3, 4, 5].map((cardIdx) => (
                            <div key={cardIdx} className="aspect-video bg-zinc-800 rounded animate-pulse" />
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {/* Real Genre Rows */}
                {!isLoading && (currentMovies.length > 0 || discoverAction.length > 0 || discoverScifi.length > 0) && (() => {
                  const getSeedFromDate = () => {
                    const d = new Date();
                    return d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate();
                  };
                  const seededRandom = (s: number) => {
                    const x = Math.sin(s) * 10000;
                    return x - Math.floor(x);
                  };
                  const seededShuffle = <T,>(arr: T[], s: number): T[] => {
                    let m = arr.length, t, i;
                    const array = [...arr];
                    while (m) {
                      i = Math.floor(seededRandom(s + m) * m--);
                      t = array[m];
                      array[m] = array[i];
                      array[i] = t;
                    }
                    return array;
                  };
                  const finishedMovieIds = continueWatching
                    .filter(s => s.is_finished || (s as any).isFinished || ((s as any).completion_rate && (s as any).completion_rate >= 0.8) || ((s as any).completionRate && (s as any).completionRate >= 0.8))
                    .map(s => s.movieId || (s as any).movie_id);
                  const unconsumedMovies = currentMovies.filter(m => !finishedMovieIds.includes(m.id));
                  const seed = getSeedFromDate();
                  const recommendedMovies = seededShuffle(unconsumedMovies, seed) as Movie[];

                  // Deduplicate TV Series sessions so only the most recent episode appears in the Continue Watching row.
                  // Also strips out "ghost sessions" where a series was played without an active episode selected.
                  const deduplicatedContinueWatching = (() => {
                    const uniqueMap = new Map<string, PlaybackSession>();
                    for (const s of continueWatching) {
                      const mId = s.movieId || (s as any).movie_id;
                      const movie = currentMovies.find(m => m.id === mId);
                      
                      // Skip buggy ghost sessions (Series played without an episode)
                      if (movie && movie.type === "series" && !s.episodeId) {
                        continue;
                      }
                      
                      // Keep the first (most recently updated) session for each movie/series
                      if (!uniqueMap.has(mId)) {
                        uniqueMap.set(mId, s);
                      }
                    }
                    return Array.from(uniqueMap.values());
                  })();

                  return (
                    <>
                      {/* Continue Watching Row */}
                      <ContinueWatchingRow
                        sessions={deduplicatedContinueWatching}
                        movies={currentMovies}
                        onPlay={onPlayMovie}
                        parseDurationToSeconds={parseDurationToSeconds}
                      />

                      {/* Recommended for You Row */}
                      {recommendedMovies.length > 0 && (
                        <MovieRow
                          title="Recommended for You"
                          movies={recommendedMovies}
                          onPlay={setSelectedMovieForDetails}
                          moviesOnServer={movies}
                          onAddDownload={handlePreFillDownload}
                          activeProfileTheme={activeProfileTheme}
                        />
                      )}

                      {/* Discovery Row: Action */}
                      {discoverAction.length > 0 && (
                        <MovieRow
                          title="Trending Action (Global Discover)"
                          movies={discoverAction}
                          onPlay={setSelectedMovieForDetails}
                          moviesOnServer={movies}
                          onAddDownload={handlePreFillDownload}
                          activeProfileTheme={activeProfileTheme}
                        />
                      )}

                      {/* Discovery Row: Sci-Fi */}
                      {discoverScifi.length > 0 && (
                        <MovieRow
                          title="Popular Sci-Fi (Global Discover)"
                          movies={discoverScifi}
                          onPlay={setSelectedMovieForDetails}
                          moviesOnServer={movies}
                          onAddDownload={handlePreFillDownload}
                          activeProfileTheme={activeProfileTheme}
                        />
                      )}

                      {/* Row for All Movies first */}
                      <MovieRow 
                        title={activeTab === "series" ? "All Series" : activeTab === "movies" ? "All Movies" : "All Releases"} 
                        movies={currentMovies.filter(m => !finishedMovieIds.includes(m.id))} 
                        onPlay={setSelectedMovieForDetails}
                        moviesOnServer={movies}
                        onAddDownload={handlePreFillDownload}
                        activeProfileTheme={activeProfileTheme}
                      />

                      {/* Rows per Genre */}
                      {currentGenres.map((genre) => {
                        const genreMovies = currentMovies.filter((m) => m.genres?.includes(genre) && !finishedMovieIds.includes(m.id));
                        if (genreMovies.length === 0) return null;
                        return (
                          <MovieRow
                            key={genre}
                            title={genre}
                            movies={genreMovies}
                            onPlay={setSelectedMovieForDetails}
                            moviesOnServer={movies}
                            onAddDownload={handlePreFillDownload}
                            activeProfileTheme={activeProfileTheme}
                          />
                        );
                      })}
                    </>
                  );
                })()}
              </div>
            </>
          ) : null}

          {activeTab === "mylist" && (
            /* MY LIST DEDICATED FULL VIEW PAGE */
            <div id="mylist-page" className={`pt-28 px-6 md:px-12 pb-24 min-h-[85vh] max-w-7xl mx-auto ${
              activeProfileTheme === "netflix" ? "animate-fade-in-up" : ""
            }`}>
              <div className="flex items-baseline justify-between border-b border-white/5 pb-4 mb-8">
                <div>
                  <h1 className="text-2xl sm:text-3xl font-black tracking-tight text-white uppercase font-sans text-left">
                    My Saved Watchlist
                  </h1>
                  <p className="text-zinc-400 text-xs mt-1 text-left">
                    Your personalized collection of favorited films and series
                  </p>
                </div>
                <div className="text-zinc-400 text-xs font-bold uppercase tracking-widest bg-zinc-800/40 px-3 py-1 rounded border border-zinc-700/50">
                  {movies.filter((m) => watchlist.includes(m.id)).length} Titles
                </div>
              </div>

              {(() => {
                const watchlistMovies = movies.filter((m) => watchlist.includes(m.id));
                if (watchlistMovies.length === 0) {
                  return (
                    <div className="flex flex-col items-center justify-center py-24 text-center max-w-sm mx-auto">
                      <div className="w-16 h-16 rounded-full bg-zinc-900 flex items-center justify-center border border-white/10 text-[#E50914] mb-4">
                        <Plus className="w-6 h-6 rotate-45" />
                      </div>
                      <h3 className="text-base font-extrabold text-white uppercase tracking-wider mb-2 text-center">
                        Your Watchlist is Empty
                      </h3>
                      <p className="text-zinc-400 text-xs leading-relaxed mb-6 text-center">
                        Explore titles from the home feed and click 'Add to Watchlist' in the details dialog to build your personal streaming library.
                      </p>
                      <button
                        onClick={() => onTabChange("home")}
                        className="px-6 py-2.5 bg-[#E50914] hover:bg-red-700 text-white font-bold rounded-sm text-xs uppercase tracking-widest transition-colors duration-200 cursor-pointer"
                      >
                        Browse Home Catalog
                      </button>
                    </div>
                  );
                }

                return (
                  <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-6">
                    {watchlistMovies.map((movie) => (
                      <div key={movie.id} className="flex flex-col group/listitem">
                        <MovieCard movie={movie} onPlay={setSelectedMovieForDetails} moviesOnServer={movies} />
                        <div className="mt-2 flex items-center justify-between px-1">
                          <span className="text-[10px] text-zinc-400 truncate max-w-[120px] font-medium text-left">
                            {movie.director || "Unknown"}
                          </span>
                          <button
                            onClick={() => toggleWatchlist(movie.id)}
                            className="text-[#E50914] hover:text-red-400 text-[10px] font-bold uppercase tracking-widest transition cursor-pointer"
                          >
                            Remove
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                );
              })()}
            </div>
          )}

          {activeTab === "downloads" && (
            /* DOWNLOAD MANAGER VIEW */
            <div id="downloads-page" className={`pt-28 px-6 md:px-12 pb-24 min-h-[85vh] max-w-6xl mx-auto ${
              activeProfileTheme === "netflix" ? "animate-fade-in-up" : ""
            }`}>
              <div className="flex flex-col md:flex-row md:items-center justify-between border-b border-zinc-800 pb-5 mb-8 gap-4 text-left">
                <div>
                  <h1 className="text-2xl sm:text-3xl font-black tracking-tight text-white uppercase text-left">
                    Media Ingestion & Download Queue
                  </h1>
                  <p className="text-zinc-400 text-xs mt-1 text-left">
                    Monitor files scraped via the browser extension, FFmpeg H.265 compression, and Google Drive upload progress.
                  </p>
                </div>
                
                <div className="flex items-center space-x-2 text-xs">
                  <span className="bg-zinc-800 text-zinc-300 px-3 py-1.5 rounded border border-zinc-700 font-mono">
                    Active Queue: {downloads.filter(d => d.status !== "Completed").length}
                  </span>
                  <span className="bg-[#E50914]/20 text-[#E50914] px-3 py-1.5 rounded border border-[#E50914]/30 font-mono">
                    Ingested: {downloads.filter(d => d.status === "Completed").length}
                  </span>
                </div>
              </div>

              {/* Manual Add Form to simulate extension behavior */}
              <div className="bg-[#121212] border border-zinc-800 rounded-xl p-5 mb-8 text-left">
                <h3 className="text-sm font-bold uppercase tracking-wider text-zinc-300 mb-3 flex items-center space-x-2">
                  <Plus className="w-4 h-4 text-red-500" />
                  <span>Trigger Manual Media Ingestion</span>
                </h3>
                <form onSubmit={handleAddDownload} className="space-y-4">
                  <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">
                    <div className="space-y-1">
                      <label className="text-[10px] uppercase font-bold text-zinc-500">TMDB ID</label>
                      <input
                        type="number"
                        placeholder="e.g., 550"
                        required
                        value={dlTmdbId}
                        onChange={(e) => setDlTmdbId(e.target.value)}
                        className="w-full bg-zinc-950 border border-zinc-800 rounded p-2 text-xs text-white outline-none focus:border-red-600 transition"
                      />
                    </div>
                    <div className="space-y-1">
                      <label className="text-[10px] uppercase font-bold text-zinc-500">Media Type</label>
                      <select
                        value={dlMediaType}
                        onChange={(e) => setDlMediaType(e.target.value)}
                        className="w-full bg-zinc-950 border border-zinc-800 rounded p-2 text-xs text-white outline-none focus:border-red-600 transition cursor-pointer"
                      >
                        <option value="movie">Movie</option>
                        <option value="tv">TV Show</option>
                      </select>
                    </div>
                    {dlMediaType === "tv" && (
                      <>
                        <div className="space-y-1">
                          <label className="text-[10px] uppercase font-bold text-zinc-500">Season</label>
                          <input
                            type="number"
                            placeholder="e.g., 1"
                            required
                            value={dlSeason}
                            onChange={(e) => setDlSeason(e.target.value)}
                            className="w-full bg-zinc-950 border border-zinc-800 rounded p-2 text-xs text-white outline-none focus:border-red-600 transition"
                          />
                        </div>
                        <div className="space-y-1">
                          <label className="text-[10px] uppercase font-bold text-zinc-500">Episode</label>
                          <input
                            type="number"
                            placeholder="e.g., 3"
                            required
                            value={dlEpisode}
                            onChange={(e) => setDlEpisode(e.target.value)}
                            className="w-full bg-zinc-950 border border-zinc-800 rounded p-2 text-xs text-white outline-none focus:border-red-600 transition"
                          />
                        </div>
                      </>
                    )}
                  </div>

                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div className="space-y-1">
                      <label className="text-[10px] uppercase font-bold text-zinc-500">Video Stream URL</label>
                      <input
                        type="url"
                        placeholder="https://domain.com/video.m3u8"
                        required
                        value={dlVideoUrl}
                        onChange={(e) => setDlVideoUrl(e.target.value)}
                        className="w-full bg-zinc-950 border border-zinc-800 rounded p-2 text-xs text-white outline-none focus:border-red-600 transition"
                      />
                    </div>
                    <div className="space-y-1">
                      <label className="text-[10px] uppercase font-bold text-zinc-500">Audio Stream URL (Optional)</label>
                      <input
                        type="url"
                        placeholder="https://domain.com/audio.m3u8"
                        value={dlAudioUrl}
                        onChange={(e) => setDlAudioUrl(e.target.value)}
                        className="w-full bg-zinc-950 border border-zinc-800 rounded p-2 text-xs text-white outline-none focus:border-red-600 transition"
                      />
                    </div>
                  </div>

                  <div className="flex justify-end pt-2">
                    <button
                      type="submit"
                      className="bg-red-600 hover:bg-red-750 text-white font-bold px-8 py-2 rounded text-xs uppercase tracking-wider transition-colors duration-200 cursor-pointer text-center"
                    >
                      Queue Ingestion
                    </button>
                  </div>
                </form>
              </div>

              {/* Progress List removed as per user request */}

              {/* Browser Offline Library (PWA Caches) */}
              <div className="mt-12 pt-8 border-t border-zinc-800 space-y-4">
                <div className="text-left">
                  <h2 className="text-lg sm:text-xl font-black tracking-tight text-white uppercase">
                    Browser Offline Library (PWA Local Storage)
                  </h2>
                  <p className="text-zinc-400 text-xs mt-1">
                    Manage media downloaded locally on this device/browser. You can play these items even when completely offline.
                  </p>
                </div>
                
                {Object.keys(pwaRegistry).length === 0 ? (
                  <div className="text-center py-10 bg-zinc-950/40 rounded-xl border border-zinc-800/60">
                    <Download className="w-8 h-8 text-zinc-700 mx-auto mb-3" />
                    <h4 className="text-xs font-bold text-zinc-500 uppercase tracking-widest">No local offline downloads</h4>
                    <p className="text-[11px] text-zinc-600 mt-1">
                      Go to any movie detail modal or TV episode list and click "Download to Browser" to cache items offline.
                    </p>
                  </div>
                ) : (
                  <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-4">
                    {Object.values(pwaRegistry).map((regItem: any) => (
                      <div 
                        key={regItem.id} 
                        className="bg-[#121212] border border-zinc-800 rounded-lg overflow-hidden flex flex-col hover:border-zinc-700 transition"
                      >
                        <div className="relative aspect-video bg-zinc-950">
                          <img 
                            src={regItem.thumbnailUrl || regItem.posterUrl} 
                            alt={regItem.title} 
                            className="w-full h-full object-cover opacity-80"
                          />
                          <div className="absolute inset-0 bg-black/40 flex items-center justify-center opacity-0 hover:opacity-100 transition-opacity">
                            <button
                              onClick={() => {
                                // Play local item!
                                onPlayMovie(regItem);
                              }}
                              className="w-10 h-10 rounded-full bg-red-600 text-white flex items-center justify-center shadow-lg active:scale-90 transition-transform"
                            >
                              <Play className="w-5 h-5 fill-current ml-0.5" />
                            </button>
                          </div>
                        </div>
                        <div className="p-3.5 space-y-1.5 flex-1 flex flex-col justify-between text-left">
                          <div>
                            <div className="flex items-center justify-between">
                              <h5 className="font-extrabold text-white text-xs truncate max-w-[150px] uppercase">
                                {regItem.title}
                              </h5>
                              <span className="text-[9px] bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 font-extrabold px-1.5 py-0.2 rounded uppercase">
                                Offline Ready
                              </span>
                            </div>
                            <p className="text-[10px] text-zinc-500 line-clamp-2 mt-1">
                              {regItem.description || regItem.overview}
                            </p>
                          </div>
                          <div className="flex items-center justify-between pt-2 border-t border-zinc-900/60 mt-2 text-[10px] text-zinc-400">
                            <span>{regItem.duration || regItem.releaseYear}</span>
                            <button
                              onClick={() => {
                                if (confirm("Remove this offline download from your browser?")) {
                                  handleRemoveFromBrowser(regItem.id, regItem.videoUrl);
                                }
                              }}
                              className="text-zinc-500 hover:text-red-500 transition cursor-pointer"
                              title="Delete local copy"
                            >
                              <Trash2 className="w-3.5 h-3.5" />
                            </button>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}

          {activeTab === "settings" && (
            /* GENERAL SETTINGS VIEW */
            <div id="general-settings-page" className={`pt-28 px-6 md:px-12 pb-24 min-h-[85vh] max-w-4xl mx-auto space-y-8 text-left ${
              activeProfileTheme === "netflix" ? "animate-fade-in-up" : ""
            }`}>
              <div>
                <h1 className="text-2xl sm:text-3xl font-black tracking-tight text-white uppercase text-left">
                  General Settings
                </h1>
                <p className="text-zinc-400 text-xs mt-1 text-left">
                  Adjust your streaming preferences, language configuration, and review active Rclone backend storage stats.
                </p>
              </div>

              {/* Active Rclone Storage Box */}
              <div className="bg-[#121212] border border-zinc-800 rounded-xl p-5 md:p-6 space-y-4">
                <div className="flex items-center justify-between border-b border-zinc-800 pb-3">
                  <div className="flex items-center space-x-2.5">
                    <Database className="w-5 h-5 text-red-500" />
                    <h3 className="text-sm font-bold uppercase tracking-wider text-zinc-200">
                      Rclone Cloud Storage Status
                    </h3>
                  </div>
                  <button 
                    type="button"
                    onClick={handleRefreshRclone}
                    className="p-1.5 bg-zinc-900 hover:bg-zinc-800 text-zinc-400 hover:text-white rounded border border-zinc-800 transition flex items-center space-x-1 text-xs cursor-pointer font-semibold"
                    disabled={isRcloneRefreshing}
                  >
                    <RefreshCw className={`w-3.5 h-3.5 ${isRcloneRefreshing ? "animate-spin text-red-500" : ""}`} />
                    <span className="hidden sm:inline">Refresh Storage Status</span>
                  </button>
                </div>

                <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                  <div className="bg-zinc-950 p-3 rounded border border-zinc-900">
                    <div className="text-[10px] uppercase font-extrabold text-zinc-500 tracking-wider">Mount Point Status</div>
                    <div className="text-xs sm:text-sm font-bold text-emerald-400 mt-1 flex items-center space-x-1.5">
                      <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
                      <span>Active Mount</span>
                    </div>
                    <div className="text-[9px] text-zinc-500 font-mono mt-0.5">/mnt/rclone/gdrive</div>
                  </div>
                  
                  <div className="bg-zinc-950 p-3 rounded border border-zinc-900">
                    <div className="text-[10px] uppercase font-extrabold text-zinc-500 tracking-wider">Storage Capacity</div>
                    <div className="text-xs sm:text-sm font-bold text-white mt-1">15.00 TB Total</div>
                    <div className="text-[9px] text-zinc-400 font-mono mt-0.5">Google Drive Remote</div>
                  </div>

                  <div className="bg-zinc-950 p-3 rounded border border-zinc-900">
                    <div className="text-[10px] uppercase font-extrabold text-zinc-500 tracking-wider">Used / Available</div>
                    <div className="text-xs sm:text-sm font-bold text-white mt-1">8.42 TB / 6.58 TB</div>
                    <div className="text-[9px] text-zinc-400 font-mono mt-0.5">56.1% Consumption</div>
                  </div>

                  <div className="bg-zinc-950 p-3 rounded border border-zinc-900">
                    <div className="text-[10px] uppercase font-extrabold text-zinc-500 tracking-wider">Sync Pipeline Speed</div>
                    <div className="text-xs sm:text-sm font-bold text-red-500 mt-1">24.5 MB/s</div>
                    <div className="text-[9px] text-zinc-400 font-mono mt-0.5">rclone daemon active</div>
                  </div>
                </div>

                <div className="bg-zinc-950/50 p-3 rounded border border-zinc-900 text-xs font-mono text-zinc-500 space-y-1.5 text-left">
                  <div className="flex justify-between">
                    <span>Rclone Core Version:</span>
                    <span className="text-zinc-400">rclone v1.66.0 (2026 stable)</span>
                  </div>
                  <div className="flex justify-between">
                    <span>Google Client Credentials:</span>
                    <span className="text-zinc-400">gdrive-cloud-storage (Authorized OAuth)</span>
                  </div>
                  <div className="flex justify-between">
                    <span>Active OS Mount PID:</span>
                    <span className="text-zinc-400">14092 (service daemon)</span>
                  </div>
                </div>

                {/* Rclone Storage Control Panel */}
                <div className="border-t border-zinc-800/80 pt-4 mt-2 space-y-4">
                  <div className="flex items-center justify-between bg-zinc-950/40 p-3 rounded border border-zinc-900">
                    <div className="space-y-0.5 text-left">
                      <span className="text-xs font-bold text-zinc-300">Enable Cloud Storage Upload (Rclone)</span>
                      <p className="text-[10px] text-zinc-500">Automatically move finished local downloads to cloud storage remote.</p>
                    </div>
                    <div className="flex items-center space-x-2">
                      {isRcloneSaving && <span className="text-[10px] text-zinc-500 animate-pulse">saving...</span>}
                      <label className="relative inline-flex items-center cursor-pointer select-none">
                        <input 
                          type="checkbox" 
                          checked={isRcloneEnabled} 
                          onChange={(e) => handleUpdateSystemSettings(e.target.checked, rcloneRemotePath)}
                          className="sr-only peer" 
                        />
                        <div className="w-9 h-5 bg-zinc-800 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-zinc-400 after:border-zinc-300 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-red-600 peer-checked:after:bg-white"></div>
                      </label>
                    </div>
                  </div>

                  {isRcloneEnabled && (
                    <div className="space-y-1.5 text-left">
                      <label className="text-xs uppercase font-extrabold text-zinc-400 tracking-wider">Rclone Remote Target Path</label>
                      <div className="flex items-center space-x-2">
                        <input 
                          type="text" 
                          value={rcloneRemotePath}
                          onChange={(e) => setRcloneRemotePath(e.target.value)}
                          onBlur={(e) => handleUpdateSystemSettings(isRcloneEnabled, e.target.value)}
                          className="flex-1 bg-zinc-950 border border-zinc-800 rounded p-2 text-xs text-white outline-none focus:border-[var(--theme-accent)] transition font-mono"
                          placeholder="e.g. gdrive:media"
                        />
                        <button
                          type="button"
                          onClick={() => handleUpdateSystemSettings(isRcloneEnabled, rcloneRemotePath)}
                          className="px-3 py-2 bg-red-600 hover:bg-red-700 text-white rounded text-xs transition cursor-pointer font-bold"
                        >
                          Save Remote
                        </button>
                      </div>
                      <p className="text-[10px] text-zinc-500">Specify the remote name and optionally a subfolder (e.g. <code>rclone_remote:Movies</code>).</p>
                    </div>
                  )}
                </div>
              </div>

              {/* Streaming Preferences */}
              <div className="bg-[#121212] border border-zinc-800 rounded-xl p-5 md:p-6 space-y-6">
                <h3 className="text-sm font-bold uppercase tracking-wider text-zinc-200 border-b border-zinc-800 pb-3">
                  Streaming & Interface Preferences
                </h3>

                <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                  <div className="space-y-1.5">
                    <label className="text-xs uppercase font-extrabold text-zinc-400 tracking-wider">Language Settings</label>
                    <select 
                      value={language}
                      onChange={(e) => setLanguage(e.target.value)}
                      className="w-full bg-zinc-950 border border-zinc-800 rounded p-2.5 text-xs text-white outline-none focus:border-[var(--theme-accent)] transition cursor-pointer"
                    >
                      <option value="en">English (US)</option>
                      <option value="es">Español (ES)</option>
                      <option value="fr">Français (FR)</option>
                      <option value="de">Deutsch (DE)</option>
                      <option value="ja">日本語 (JP)</option>
                    </select>
                    <p className="text-[10px] text-zinc-500 text-left">Applies to subtitles, metadata overrides, and UI captions.</p>
                  </div>

                  <div className="space-y-1.5">
                    <label className="text-xs uppercase font-extrabold text-zinc-400 tracking-wider">Video Playback Quality</label>
                    <select 
                      value={quality}
                      onChange={(e) => setQuality(e.target.value)}
                      className="w-full bg-zinc-950 border border-zinc-800 rounded p-2.5 text-xs text-white outline-none focus:border-[var(--theme-accent)] transition cursor-pointer"
                    >
                      <option value="auto">Auto (Best Quality for connection)</option>
                      <option value="4k">4K Ultra HD (Highest Data)</option>
                      <option value="1080p">1080p Full HD</option>
                      <option value="720p">720p Standard</option>
                    </select>
                    <p className="text-[10px] text-zinc-500 text-left">Adapts real-time stream segment buffer ratios.</p>
                  </div>

                  <div className="space-y-1.5">
                    <label className="text-xs uppercase font-extrabold text-zinc-400 tracking-wider">Interface Theme</label>
                    <select 
                      value={activeProfileTheme}
                      onChange={(e) => handleUpdateTheme(e.target.value as any)}
                      className="w-full bg-zinc-950 border border-zinc-800 rounded p-2.5 text-xs text-white outline-none focus:border-[var(--theme-accent)] transition cursor-pointer"
                    >
                      <option value="netflix">Netflix Signature</option>
                      <option value="prime">Prime Video Slate</option>
                      <option value="apple">Apple TV+ Minimalist</option>
                      <option value="gemini">Gemini Cyber Glow</option>
                    </select>
                    <p className="text-[10px] text-zinc-500 text-left">Morphic layouts, pages, and components dynamically.</p>
                  </div>
                </div>

                <div className="flex items-center justify-between bg-zinc-950/40 p-3 rounded border border-zinc-900">
                  <div className="space-y-0.5 text-left">
                    <span className="text-xs font-bold text-zinc-300">Continuous Auto-Play</span>
                    <p className="text-[10px] text-zinc-500">Automatically launch the next episode of a series.</p>
                  </div>
                  <label className="relative inline-flex items-center cursor-pointer select-none">
                    <input 
                      type="checkbox" 
                      checked={autoplay} 
                      onChange={(e) => setAutoplay(e.target.checked)}
                      className="sr-only peer" 
                    />
                    <div className="w-9 h-5 bg-zinc-800 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-zinc-400 after:border-zinc-300 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-red-600 peer-checked:after:bg-white"></div>
                  </label>
                </div>

                {/* Membership Details */}
                <div className="border-t border-zinc-800 pt-5 flex flex-wrap gap-4 items-center justify-between">
                  <div className="flex items-center space-x-3 text-left">
                    <div className="p-2 bg-red-600/10 border border-red-500/20 text-[#E50914] rounded">
                      <User className="w-5 h-5" />
                    </div>
                    <div className="text-left">
                      <div className="text-xs font-bold text-white">Streaming Account Status</div>
                      <div className="text-[10px] text-zinc-400">Premium Single-Tenant License</div>
                    </div>
                  </div>
                  <div className="text-right text-[10px] font-mono text-zinc-500">
                    <div>Billing Period: <span className="text-zinc-300">Monthly ($0.00 Self-Hosted)</span></div>
                    <div>Next Cycle: <span className="text-zinc-300">August 02, 2026</span></div>
                  </div>
                </div>
              </div>

              {/* Two-Factor Authentication (2FA) */}
              <div className="bg-[#121212] border border-zinc-800 rounded-xl p-5 md:p-6 space-y-6">
                <div className="flex items-center justify-between border-b border-zinc-800 pb-3">
                  <div className="flex items-center space-x-2.5">
                    <Shield className="w-5 h-5 text-red-500" />
                    <h3 className="text-sm font-bold uppercase tracking-wider text-zinc-200">
                      Two-Factor Authentication (2FA)
                    </h3>
                  </div>
                  <span className={`px-2 py-0.5 rounded font-mono text-[10px] font-bold uppercase ${twoFactorEnabled ? "bg-green-500/10 text-green-400 border border-green-500/20" : "bg-zinc-800 text-zinc-400 border border-zinc-700"}`}>
                    {twoFactorEnabled ? "Active & Secured" : "Disabled"}
                  </span>
                </div>

                {totpMessage && (
                  <div className="p-3 bg-green-500/10 border border-green-500/20 rounded-lg text-green-400 text-xs font-semibold">
                    {totpMessage}
                  </div>
                )}
                {totpError && (
                  <div className="p-3 bg-red-500/10 border border-red-500/20 rounded-lg text-red-400 text-xs font-semibold">
                    {totpError}
                  </div>
                )}

                {!twoFactorEnabled ? (
                  !is2faSetupMode ? (
                    <div className="space-y-4 text-left">
                      <p className="text-xs text-zinc-400 leading-relaxed">
                        Add an extra layer of security to your StreamHome account. When enabled, you will be prompted for a 6-digit TOTP validation code from your authenticator app (Google Authenticator, Authy, etc.) during sign-in.
                      </p>
                      <button
                        onClick={handleSetup2fa}
                        className="bg-[var(--theme-accent)] hover:bg-[var(--theme-accent-hover)] text-white font-bold px-6 py-2 rounded-lg text-xs uppercase tracking-wider transition duration-200 cursor-pointer"
                      >
                        Enable 2FA Authentication
                      </button>
                    </div>
                  ) : (
                    <div className="space-y-5 text-left border-t border-zinc-900 pt-4">
                      <div className="flex flex-col md:flex-row md:items-center gap-6">
                        {totpProvUri && (
                          <div className="bg-white p-2.5 rounded-lg border border-zinc-800 shrink-0 flex items-center justify-center">
                            <img
                              src={`https://api.qrserver.com/v1/create-qr-code/?size=150x150&data=${encodeURIComponent(totpProvUri)}`}
                              alt="TOTP Setup QR Code"
                              className="w-32 h-32"
                            />
                          </div>
                        )}
                        <div className="space-y-3">
                          <h4 className="text-xs font-bold uppercase text-zinc-300">Scan QR Code</h4>
                          <p className="text-[11px] text-zinc-500 leading-relaxed">
                            Scan the QR code with your authenticator application. If you cannot scan QR codes, configure manually using the secret key below:
                          </p>
                          <div className="bg-zinc-950 border border-zinc-900 p-2.5 rounded font-mono text-xs text-zinc-400 select-all tracking-wider text-center">
                            {totpSecret}
                          </div>
                        </div>
                      </div>

                      <div className="space-y-2.5 border-t border-zinc-900 pt-4">
                        <label className="block text-xs uppercase font-bold text-zinc-400 tracking-wider">
                          Enter Verification Code
                        </label>
                        <div className="flex gap-3">
                          <input
                            type="text"
                            maxLength={6}
                            placeholder="e.g. 123456"
                            value={totpCode}
                            onChange={(e) => setTotpCode(e.target.value.replace(/\D/g, ""))}
                            className="bg-zinc-950 border border-zinc-800 text-center tracking-widest focus:border-[var(--theme-accent)] focus:outline-none rounded-md px-3 py-2 text-sm font-bold text-white transition w-40 font-mono"
                          />
                          <button
                            onClick={handleVerify2faSetup}
                            className="bg-[var(--theme-accent)] hover:bg-[var(--theme-accent-hover)] text-white font-bold px-6 py-2 rounded-lg text-xs uppercase tracking-wider transition duration-200 cursor-pointer"
                          >
                            Verify & Enable
                          </button>
                          <button
                            onClick={() => {
                              setIs2faSetupMode(false);
                              setTotpError("");
                            }}
                            className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-zinc-400 hover:text-white px-6 py-2 rounded-lg text-xs uppercase tracking-wider transition duration-200 cursor-pointer"
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    </div>
                  )
                ) : (
                  <div className="space-y-4 text-left">
                    <p className="text-xs text-zinc-400 leading-relaxed">
                      Two-factor authentication is currently enabled on your account. Logins are protected by your authenticator code keys.
                    </p>
                    <button
                      onClick={handleDisable2fa}
                      className="bg-red-600/10 hover:bg-red-600/20 text-red-500 border border-red-500/20 font-bold px-6 py-2 rounded-lg text-xs uppercase tracking-wider transition duration-200 cursor-pointer"
                    >
                      Disable 2FA Security
                    </button>
                  </div>
                )}
              </div>

            </div>
          )}

          {activeTab === "profile-settings" && (
            /* PROFILE SETTINGS VIEW */
            <div id="profile-settings-page" className={`pt-28 px-6 md:px-12 pb-24 min-h-[85vh] max-w-4xl mx-auto space-y-8 text-left ${
              activeProfileTheme === "netflix" ? "animate-fade-in-up" : ""
            }`}>
              <div className="flex flex-col md:flex-row md:items-center justify-between border-b border-zinc-800 pb-5 gap-4">
                <div>
                  <h1 className="text-2xl sm:text-3xl font-black tracking-tight text-white uppercase text-left">
                    {profileSubTab === "active" ? "Profile Settings" : "Manage Profiles"}
                  </h1>
                  <p className="text-zinc-400 text-xs mt-1 text-left">
                    {profileSubTab === "active" 
                      ? `Customize the avatar color, name, theme, and PIN locking mechanism for active profile: ${activeProfile.name}.`
                      : "Add new streaming profiles, delete redundant ones, edit names, or customize avatar gradients."}
                  </p>
                </div>
                
                <div className="flex items-center bg-zinc-900 border border-zinc-800 p-1 rounded-lg shrink-0">
                  <button
                    onClick={() => setProfileSubTab("active")}
                    className={`px-4 py-1.5 rounded-md text-xs font-bold uppercase transition ${profileSubTab === "active" ? "bg-[var(--theme-accent)] text-white" : "text-zinc-400 hover:text-white"}`}
                  >
                    Active Profile
                  </button>
                  <button
                    onClick={() => setProfileSubTab("manage")}
                    className={`px-4 py-1.5 rounded-md text-xs font-bold uppercase transition ${profileSubTab === "manage" ? "bg-[var(--theme-accent)] text-white" : "text-zinc-400 hover:text-white"}`}
                  >
                    Manage All
                  </button>
                </div>
              </div>

              {profileSubTab === "active" ? (
                /* ACTIVE PROFILE SETTINGS FORM */
                <form onSubmit={handleSaveActiveProfile} className="bg-[#121212] border border-zinc-800 rounded-xl p-6 md:p-8 space-y-6">
                  {activeProfileSaveSuccess && (
                    <div className="bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 text-xs p-3 rounded-lg flex items-center space-x-2">
                      <Check className="w-4 h-4 shrink-0" />
                      <span>Settings for profile <strong>{activeProfileName}</strong> saved successfully! Theme updated in real-time.</span>
                    </div>
                  )}

                  <div className="flex flex-col md:flex-row gap-8 items-start">
                    {/* Left side: Avatar editor */}
                    <div className="flex flex-col items-center space-y-4 shrink-0 w-full md:w-auto">
                      <div className={`w-28 h-28 rounded-xl bg-gradient-to-tr ${activeProfileAvatarColor} flex items-center justify-center border border-zinc-700 shadow-2xl text-white font-extrabold text-4xl uppercase`}>
                        {activeProfileName ? activeProfileName[0] : "?"}
                      </div>
                      
                      <div className="space-y-1.5 text-center">
                        <label className="text-[10px] uppercase font-extrabold text-zinc-500 tracking-wider">Change Avatar Color</label>
                        <div className="grid grid-cols-6 gap-2">
                          {[
                            "from-blue-600 to-indigo-600",
                            "from-red-600 to-rose-600",
                            "from-green-500 to-emerald-600",
                            "from-amber-500 to-orange-600",
                            "from-purple-600 to-pink-600",
                            "from-cyan-500 to-blue-500"
                          ].map((color) => (
                            <button
                              key={color}
                              type="button"
                              onClick={() => setActiveProfileAvatarColor(color)}
                              className={`w-6 h-6 rounded-full bg-gradient-to-tr ${color} border-2 transition ${activeProfileAvatarColor === color ? "border-white scale-110 shadow-lg" : "border-transparent hover:scale-105"}`}
                              title="Select Avatar color"
                            />
                          ))}
                        </div>
                      </div>
                    </div>

                    {/* Right side: Input fields */}
                    <div className="flex-1 w-full space-y-6">
                      <div className="space-y-2">
                        <label className="block text-xs font-bold uppercase tracking-wider text-zinc-400">Profile Name</label>
                        <input
                          type="text"
                          required
                          value={activeProfileName}
                          onChange={(e) => setActiveProfileName(e.target.value)}
                          placeholder="Profile name"
                          className="w-full bg-zinc-950 border border-zinc-800 focus:border-[var(--theme-accent)] focus:ring-1 focus:ring-[var(--theme-accent)] focus:outline-none rounded-lg p-3 text-sm text-white transition"
                        />
                      </div>

                      <div className="space-y-2">
                        <label className="block text-xs font-bold uppercase tracking-wider text-zinc-400">Custom UI Theme</label>
                        <select
                          value={editProfileTheme}
                          onChange={(e) => setEditProfileTheme(e.target.value as any)}
                          className="w-full bg-zinc-950 border border-zinc-800 focus:border-[var(--theme-accent)] focus:ring-1 focus:ring-[var(--theme-accent)] focus:outline-none rounded-lg p-3 text-sm text-white transition cursor-pointer"
                        >
                          <option value="netflix">Netflix Theme (Signature Red)</option>
                          <option value="prime">Prime Video Theme (Tech Blue)</option>
                          <option value="apple">Apple TV Theme (Minimalist White)</option>
                          <option value="gemini">Google Gemini Theme (Purple/Mono)</option>
                        </select>
                        <p className="text-[11px] text-zinc-500">Theme changes fonts, layout corner curves, borders, and accent highlights instantly.</p>
                      </div>

                      {/* Security PIN code lock */}
                      <div className="bg-zinc-950/60 border border-zinc-900 rounded-lg p-4 space-y-4">
                        <div className="flex items-center justify-between">
                          <div className="space-y-0.5">
                            <label className="text-xs font-bold uppercase tracking-wider text-zinc-300">Profile Lock (Security PIN)</label>
                            <p className="text-[10px] text-zinc-500">Require a 4-digit PIN to access this profile.</p>
                          </div>
                          
                          <label className="relative inline-flex items-center cursor-pointer">
                            <input 
                              type="checkbox" 
                              checked={activeProfilePinEnabled}
                              onChange={(e) => setActiveProfilePinEnabled(e.target.checked)}
                              className="sr-only peer"
                            />
                            <div className="w-9 h-5 bg-zinc-800 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-zinc-400 after:border-zinc-300 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-[var(--theme-accent)] peer-checked:after:bg-white"></div>
                          </label>
                        </div>

                        {activeProfilePinEnabled && (
                          <div className="space-y-2 pt-2 border-t border-zinc-900/60">
                            <label className="block text-[10px] uppercase font-bold text-zinc-400 tracking-wider">Set 4-Digit Security PIN</label>
                            <input
                              type="password"
                              maxLength={4}
                              pattern="[0-9]{4}"
                              placeholder="PIN (e.g. 1234)"
                              required={activeProfilePinEnabled}
                              value={activeProfilePin}
                              onChange={(e) => setActiveProfilePin(e.target.value.replace(/\D/g, ""))}
                              className="w-32 bg-zinc-900 border border-zinc-800 text-center tracking-widest focus:border-[var(--theme-accent)] focus:outline-none rounded-md p-2.5 text-lg font-bold text-white transition font-mono"
                            />
                          </div>
                        )}
                      </div>
                    </div>
                  </div>

                  <div className="flex justify-end pt-4 border-t border-zinc-800/80">
                    <button
                      type="submit"
                      className="bg-[var(--theme-accent)] hover:bg-[var(--theme-accent-hover)] text-white font-bold px-8 py-2.5 rounded-lg text-xs uppercase tracking-wider transition duration-200 cursor-pointer text-center"
                    >
                      Save Profile Settings
                    </button>
                  </div>
                </form>
              ) : (
                /* ORIGINAL MANAGE PROFILES GRID */
                <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-6">
                  {profiles.map((profile) => (
                    <div 
                      key={profile.id}
                      className="bg-[#121212] border border-zinc-800 rounded-xl p-4 flex items-center space-x-4 hover:border-zinc-700 transition relative group text-left"
                    >
                      <div className={`w-14 h-14 rounded bg-gradient-to-tr ${profile.avatarColor} flex items-center justify-center border border-zinc-700 shadow-xl shrink-0 text-white font-extrabold text-sm uppercase`}>
                        {profile.name[0]}
                      </div>
                      
                      <div className="space-y-1 flex-1 overflow-hidden text-left">
                        <div className="text-sm font-bold text-white truncate">{profile.name}</div>
                        <div className="text-[10px] text-zinc-500 font-mono">Profile ID: {profile.id}</div>
                      </div>

                      <div className="flex space-x-1 shrink-0">
                        <button
                          type="button"
                          onClick={() => {
                            setEditingProfile(profile);
                            setEditProfileName(profile.name);
                            setEditAvatarColor(profile.avatarColor);
                            setIsEditModalOpen(true);
                          }}
                          className="p-1.5 bg-zinc-950 hover:bg-zinc-800 text-zinc-400 hover:text-white rounded border border-zinc-800 transition cursor-pointer"
                          title="Edit Profile"
                        >
                          <Pencil className="w-3.5 h-3.5" />
                        </button>
                        
                        <button
                          type="button"
                          onClick={() => handleDeleteProfile(profile.id)}
                          className="p-1.5 bg-zinc-950 hover:bg-red-950/30 text-zinc-400 hover:text-red-500 rounded border border-zinc-800 hover:border-red-900/40 transition cursor-pointer"
                          title="Delete Profile"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </div>
                  ))}

                  {/* Add Profile Block */}
                  {profiles.length < 8 && (
                    <button
                      type="button"
                      onClick={() => {
                        setNewProfileName("");
                        setNewAvatarColor("from-purple-600 to-pink-600");
                        setIsCreateModalOpen(true);
                      }}
                      className="border-2 border-dashed border-zinc-800 hover:border-zinc-600 rounded-xl p-4 flex items-center justify-center space-x-3 text-zinc-500 hover:text-zinc-300 transition h-22 bg-zinc-950/20 cursor-pointer text-center flex-row w-full justify-center"
                    >
                      <Plus className="w-5 h-5" />
                      <span className="text-xs uppercase font-extrabold tracking-wider">Add Profile</span>
                    </button>
                  )}
                </div>
              )}
            </div>
          )}


        </>
      )}
        </>
      )}

      {/* API DIAGNOSTIC / DEVELOPER SIDEBAR MODAL */}
      <AnimatePresence>
        {isApiPanelOpen && (
          <div className="fixed inset-0 z-50 flex justify-end">
            {/* Backdrop */}
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              onClick={() => setIsApiPanelOpen(false)}
              className="absolute inset-0 bg-black/60 backdrop-blur-xs"
            />

            {/* Panel drawer */}
            <motion.div
              initial={{ x: "100%" }}
              animate={{ x: 0 }}
              exit={{ x: "100%" }}
              transition={{ type: "spring", damping: 25, stiffness: 200 }}
              className="relative w-full max-w-lg bg-[#181818] border-l border-zinc-800 h-full overflow-y-auto p-6 md:p-8 flex flex-col z-10 text-white"
              id="api-setup-panel"
            >
              <div className="flex items-center justify-between pb-4 border-b border-zinc-800 mb-6">
                <div className="flex items-center space-x-2">
                  <Database className="w-5 h-5 text-red-500" />
                  <h3 className="text-lg font-bold font-sans">API Endpoint Diagnostics</h3>
                </div>
                <button
                  onClick={() => setIsApiPanelOpen(false)}
                  className="text-zinc-400 hover:text-white transition"
                  id="close-api-panel-btn"
                >
                  ✕
                </button>
              </div>

              {/* Endpoint Connection Status */}
              <div className="space-y-4 mb-6">
                <div>
                  <label className="block text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-2">
                    Backend Base API URL
                  </label>
                  <div className="flex space-x-2">
                    <input
                      type="text"
                      value={apiBaseUrl}
                      onChange={(e) => onChangeApiBaseUrl(e.target.value)}
                      placeholder="/api"
                      className="bg-zinc-900 border border-zinc-700 focus:border-[var(--theme-accent)] focus:ring-1 focus:ring-[var(--theme-accent)] focus:outline-none rounded px-3 py-1.5 text-sm font-mono flex-1 text-white"
                      id="api-base-url-input"
                    />
                    <button
                      onClick={fetchMoviesData}
                      className="bg-zinc-800 hover:bg-zinc-700 p-2 rounded border border-zinc-700 text-zinc-300 hover:text-white cursor-pointer"
                      title="Test/Reload Connection"
                    >
                      <RefreshCw className="w-4 h-4" />
                    </button>
                  </div>
                </div>

                <div>
                  <label className="block text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-2">
                    API Bearer Token
                  </label>
                  <input
                    type="password"
                    value={apiBearerToken}
                    onChange={(e) => onChangeApiBearerToken(e.target.value)}
                    placeholder="secure-token-123"
                    className="w-full bg-zinc-900 border border-zinc-700 focus:border-[var(--theme-accent)] focus:ring-1 focus:ring-[var(--theme-accent)] focus:outline-none rounded px-3 py-1.5 text-sm font-mono text-white"
                    id="api-bearer-token-input"
                  />
                  <p className="text-[11px] text-zinc-500 mt-1">
                    Set relative path <code className="text-zinc-300">/api</code> for server proxy, or input complete Python endpoint like <code className="text-zinc-300">http://localhost:8000/api</code>.
                  </p>
                </div>

                <div className="bg-zinc-900/60 rounded p-4 border border-zinc-800 space-y-3">
                  <h4 className="text-xs font-semibold text-zinc-300 uppercase tracking-wider">Required API Integrations</h4>
                  
                  <div className="space-y-2 text-xs">
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-zinc-400">GET /movies</span>
                      <span className={`px-2 py-0.5 rounded font-mono font-semibold ${movies.length > 0 ? "bg-green-500/10 text-green-400" : "bg-zinc-800 text-zinc-500"}`}>
                        {movies.length > 0 ? `Connected (${movies.length} items)` : "Unloaded"}
                      </span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-zinc-400">GET /movies/featured</span>
                      <span className={`px-2 py-0.5 rounded font-mono font-semibold ${featuredMovie ? "bg-green-500/10 text-green-400" : "bg-zinc-800 text-zinc-500"}`}>
                        {featuredMovie ? "Connected" : "Unloaded"}
                      </span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-zinc-400">POST /track</span>
                      <span className="text-zinc-500 italic">Dispatches every 10s on play</span>
                    </div>
                  </div>
                </div>
              </div>

              {/* Paste Mock Data Blueprint */}
              <div className="flex-1 flex flex-col min-h-[250px]">
                <div className="flex items-center justify-between mb-2">
                  <label className="block text-xs font-semibold text-zinc-400 uppercase tracking-wider">
                    Direct JSON Injection (Test Layout)
                  </label>
                  {jsonSuccess && (
                    <span className="text-[11px] text-green-400 flex items-center space-x-0.5">
                      <Check className="w-3 h-3" /> <span>Loaded successfully!</span>
                    </span>
                  )}
                </div>
                <textarea
                  value={manualJsonInput}
                  onChange={(e) => setManualJsonInput(e.target.value)}
                  placeholder={JSON.stringify(DEFAULT_TEMPLATE_DATA, null, 2)}
                  className="bg-zinc-950 border border-zinc-800 focus:border-red-600 focus:ring-1 focus:ring-red-600 focus:outline-none rounded p-3 text-xs font-mono flex-1 text-zinc-300 resize-none mb-4"
                  id="manual-json-textarea"
                />
                
                <div className="flex space-x-2">
                  <button
                    onClick={handleLoadManualJson}
                    className="flex-1 bg-red-600 hover:bg-red-700 text-white font-semibold py-2 rounded text-sm transition"
                    id="inject-json-btn"
                  >
                    Inject Layout Data
                  </button>
                  <button
                    onClick={() => setManualJsonInput(JSON.stringify(DEFAULT_TEMPLATE_DATA, null, 2))}
                    className="bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-xs px-3 rounded border border-zinc-700 transition"
                  >
                    Paste Template
                  </button>
                </div>
              </div>
            </motion.div>
          </div>
        )}        {/* IMMERSIVE MOVIE DETAILS MODAL */}
      {selectedMovieForDetails && (!activeProfile.theme || activeProfile.theme === "netflix") && (() => {
          const modalData = {
            id: selectedMovieForDetails.id,
            title: tmdbJson?.title || tmdbJson?.name || selectedMovieForDetails.title || "",
            releaseYear: tmdbJson?.releaseYear || tmdbJson?.release_year || (tmdbJson?.release_date ? new Date(tmdbJson.release_date).getFullYear() : null) || selectedMovieForDetails.releaseYear || "",
            rating: tmdbJson?.rating || tmdbJson?.content_rating || selectedMovieForDetails.rating || "G",
            duration: tmdbJson?.duration || (tmdbJson?.runtime ? `${Math.floor(tmdbJson.runtime / 60)}h ${tmdbJson.runtime % 60}m` : selectedMovieForDetails.duration || ""),
            userScore: tmdbJson?.userScore || 
                       (tmdbJson?.voteAverage ? `${tmdbJson.voteAverage.toFixed(1)}/10` : 
                       (tmdbJson?.vote_average ? `${tmdbJson.vote_average.toFixed(1)}/10` : 
                       ((selectedMovieForDetails as any).voteAverage ? `${(selectedMovieForDetails as any).voteAverage.toFixed(1)}/10` : 
                       ((selectedMovieForDetails as any).vote_average ? `${(selectedMovieForDetails as any).vote_average.toFixed(1)}/10` : "8.0/10")))),
            genres: tmdbJson?.genres?.map((g: any) => typeof g === "string" ? g : g.name) || selectedMovieForDetails.genres || [],
            overview: tmdbJson?.overview || tmdbJson?.description || selectedMovieForDetails.description || "",
            backdropUrl: tmdbJson?.backdropUrl || tmdbJson?.backdrop_path || selectedMovieForDetails.bannerUrl || selectedMovieForDetails.thumbnailUrl || "",
            posterUrl: tmdbJson?.posterUrl || tmdbJson?.poster_path || selectedMovieForDetails.thumbnailUrl || "",
            cast: tmdbJson?.cast || selectedMovieForDetails.cast || [],
            savedTimestamp: tmdbJson?.savedTimestamp || tmdbJson?.timestamp || 0,
            type: selectedMovieForDetails.type || "movie"
          };

          const savedSession = continueWatching.find((s) => 
            s.movieId === modalData.id &&
            !(s.is_finished || (s as any).isFinished || ((s as any).completion_rate && (s as any).completion_rate >= 0.8) || ((s as any).completionRate && (s as any).completionRate >= 0.8))
          );
          const savedTimestamp = savedSession?.timestamp || modalData.savedTimestamp || 0;

          const tmdbId = (selectedMovieForDetails as any).tmdb_id || (selectedMovieForDetails as any).tmdbId || modalData.id;
          const isActuallyDownloaded = movies.some(
            (m) => m.id === selectedMovieForDetails?.id || (tmdbId && (m.id === `m_${tmdbId}` || m.id === `tv_${tmdbId}`))
          );

          return (
            <div className="fixed inset-0 z-50 flex items-center justify-center p-4 md:p-6" id="movie-details-modal">
              {/* Dark Backdrop overlay */}
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                onClick={() => {
                  setSelectedMovieForDetails(null);
                  setTmdbJson(null);
                }}
                className="absolute inset-0 bg-black/85 backdrop-blur-xs"
              />

              {/* Modal Card Box */}
              <motion.div
                initial={{ opacity: 0, scale: 0.9, y: 30 }}
                animate={{ opacity: 1, scale: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.9, y: 30 }}
                transition={{ type: "spring", damping: 25, stiffness: 150 }}
                className="relative max-w-3xl w-full bg-[#181818] text-white rounded-md overflow-hidden shadow-[0_0_50px_rgba(229,9,20,0.15)] border border-red-950/20 flex flex-col max-h-[90vh] md:max-h-[85vh] z-10 font-sans"
              >
                {/* Backdrop Cover Image Container - Made more compact */}
                <div id="modal-backdrop-container" className="relative h-40 sm:h-52 md:h-64 w-full flex-none overflow-hidden bg-zinc-950">
                  <img
                    id="modal-backdrop-img"
                    src={modalData.backdropUrl}
                    alt={modalData.title}
                    referrerPolicy="no-referrer"
                    className="w-full h-full object-cover transition-opacity duration-300"
                  />
                  {/* Visual Backdrop Overlay Gradients */}
                  <div className="absolute inset-0 bg-gradient-to-t from-[#181818] via-[#181818]/60 via-black/50 to-transparent" />
                  
                  {/* Absolute Close icon */}
                  <button
                    onClick={() => {
                      setSelectedMovieForDetails(null);
                      setTmdbJson(null);
                    }}
                    className="absolute top-3 right-3 z-30 p-1.5 bg-black/60 hover:bg-black/85 rounded-full text-white/80 hover:text-white transition-colors duration-200 border border-white/10"
                    title="Close Dialog"
                    id="modal-close-btn"
                  >
                    <X className="w-4.5 h-4.5" />
                  </button>

                  {/* Overlapping Movie Title */}
                  <div className="absolute bottom-0 left-0 right-0 p-4 sm:p-6 bg-gradient-to-t from-[#181818] to-transparent z-10">
                    <h2 
                      id="modal-title" 
                      className="text-2xl sm:text-3xl md:text-4.5xl font-normal leading-none tracking-wide text-white drop-shadow-md uppercase"
                      style={{ fontFamily: "var(--theme-title-font, 'Bebas Neue', sans-serif)" }}
                    >
                      {modalData.title}
                    </h2>
                  </div>
                </div>

                {/* Scrollable details body - Optimized padding and spacing */}
                <div className="p-4 sm:p-5 overflow-y-auto space-y-4 flex-1 bg-[#181818]">
                  <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
                    {/* Left Column: Compact vertical poster container */}
                    <div className="col-span-1 flex flex-col items-center md:items-start">
                      <div id="modal-poster-container" className="w-24 sm:w-28 md:w-full max-w-[120px] aspect-[2/3] rounded-md overflow-hidden bg-zinc-900 border border-white/10 shadow-lg relative group">
                        <img
                          id="modal-poster-img"
                          src={modalData.posterUrl}
                          alt={modalData.title}
                          referrerPolicy="no-referrer"
                          className="w-full h-full object-cover transition-transform duration-300 group-hover:scale-105"
                        />
                      </div>
                    </div>

                    {/* Right Column: Text metadata, badges, overview, cast list */}
                    <div className="col-span-1 md:col-span-3 space-y-4">
                      {/* Horizontal metadata bar - Made tight */}
                      <div className="flex flex-wrap items-center gap-2.5 text-xs text-zinc-400 font-semibold">
                        <span id="modal-release-year" className="bg-zinc-800 text-zinc-300 px-2 py-0.5 rounded border border-zinc-700 font-mono text-[11px]">
                          {modalData.releaseYear}
                        </span>
                        <span id="modal-rating" className="border border-white/20 px-1.5 py-0.2 text-[9px] rounded font-extrabold uppercase tracking-widest text-zinc-300 bg-white/5">
                          {modalData.rating}
                        </span>
                        <span id="modal-duration" className="flex items-center gap-1 text-zinc-300 text-[11px]">
                          <Clock className="w-3.5 h-3.5 text-zinc-500" />
                          <span>{modalData.duration}</span>
                        </span>
                        <span id="modal-user-score" className="text-emerald-400 font-bold bg-emerald-500/10 px-2 py-0.5 rounded border border-emerald-500/20 text-[11px]">
                          {modalData.userScore || "N/A"}
                        </span>
                      </div>

                      {/* Plot Summary / Overview Paragraph - Text sizes tightened */}
                      <div className="space-y-1">
                        <h4 className="text-[10px] uppercase font-extrabold text-zinc-500 tracking-wider">Overview</h4>
                        <p id="modal-overview" className="text-xs sm:text-sm text-zinc-300 leading-relaxed font-normal">
                          {modalData.overview}
                        </p>
                      </div>

                      {/* Episodic Series Layout */}
                      {modalData.type === "series" && (() => {
                        const seriesEpisodes = selectedMovieForDetails?.episodes || [];
                        const seasons = Array.from(new Set(seriesEpisodes.map(ep => ep.seasonNumber))).sort((a, b) => a - b);
                        const activeSeason = selectedSeason;
                        const displaySeasons = seasons.length > 0 ? seasons : [1, 2];
                        const displayEpisodes = seriesEpisodes.length > 0
                          ? seriesEpisodes.filter(ep => ep.seasonNumber === activeSeason)
                          : [
                              {
                                id: `${modalData.id}_s1_e1`,
                                episodeNumber: 1,
                                seasonNumber: 1,
                                title: "Awakening of the Grid",
                                description: `In the premiere episode of ${modalData.title}, we are introduced to the core conflicts and the stunning universe.`,
                                thumbnailUrl: modalData.posterUrl,
                                videoUrl: selectedMovieForDetails?.videoUrl || "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/Sintel.mp4",
                                duration: "10m 53s"
                              },
                              {
                                id: `${modalData.id}_s1_e2`,
                                episodeNumber: 2,
                                seasonNumber: 1,
                                title: "The Celestial Interface",
                                description: "An unexpected revelation changes everything. The stakes rise as alliances are tested.",
                                thumbnailUrl: modalData.posterUrl,
                                videoUrl: selectedMovieForDetails?.videoUrl || "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/Sintel.mp4",
                                duration: "14m 48s"
                              },
                              {
                                id: `${modalData.id}_s2_e1`,
                                episodeNumber: 1,
                                seasonNumber: 2,
                                title: "Neon Substrate Phase 2",
                                description: "The second season kicks off with higher production values, deeper mysteries, and bigger threats.",
                                thumbnailUrl: modalData.posterUrl,
                                videoUrl: selectedMovieForDetails?.videoUrl || "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/Sintel.mp4",
                                duration: "12m 14s"
                              },
                              {
                                id: `${modalData.id}_s2_e2`,
                                episodeNumber: 2,
                                seasonNumber: 2,
                                title: "Grid Resolution Protocol",
                                description: `A shocking conclusion that sets up future adventures in the saga of ${modalData.title}.`,
                                thumbnailUrl: modalData.posterUrl,
                                videoUrl: selectedMovieForDetails?.videoUrl || "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/Sintel.mp4",
                                duration: "9m 56s"
                              }
                            ].filter(ep => ep.seasonNumber === activeSeason);

                        return (
                          <div className="space-y-3 pt-3 border-t border-zinc-800" id="series-episodic-layout">
                            <div className="flex items-center justify-between border-b border-zinc-850 pb-2">
                              <div className="flex items-center space-x-3">
                                <h4 className="text-[10px] uppercase font-extrabold text-zinc-500 tracking-wider">Episodes</h4>
                                <select
                                  id="season-select"
                                  value={selectedSeason}
                                  onChange={(e) => setSelectedSeason(Number(e.target.value))}
                                  className="bg-zinc-900 border border-zinc-800 hover:border-zinc-700 text-white rounded px-2.5 py-1 text-[11px] font-bold outline-none cursor-pointer focus:border-red-600 transition"
                                >
                                  {displaySeasons.map(s => (
                                    <option key={s} value={s}>Season {s}</option>
                                  ))}
                                </select>
                              </div>
                              <span className="text-zinc-500 text-[10px] uppercase font-extrabold tracking-wider">{displayEpisodes.length} Episodes</span>
                            </div>

                            <div className="space-y-3.5" id="episodes-container">
                              {isFetchingEpisodes ? (
                                <div className="space-y-4 py-6">
                                  {[1, 2, 3].map(i => (
                                    <div key={`skel-${i}`} className="flex flex-col sm:flex-row items-start gap-4 p-3 rounded-lg bg-zinc-900/40 border border-zinc-800/50 animate-pulse">
                                      <div className="relative w-full sm:w-32 aspect-video rounded-md bg-zinc-800/50 flex-none" />
                                      <div className="flex-1 space-y-2 w-full pt-1">
                                        <div className="h-3 w-1/3 bg-zinc-800/50 rounded" />
                                        <div className="h-2 w-full bg-zinc-800/30 rounded mt-2" />
                                        <div className="h-2 w-4/5 bg-zinc-800/30 rounded" />
                                      </div>
                                    </div>
                                  ))}
                                </div>
                              ) : displayEpisodes.map((episode) => (
                                <div
                                  key={episode.id}
                                  className="group/episode flex flex-col sm:flex-row items-start gap-4 p-3 rounded-lg bg-zinc-900/40 hover:bg-zinc-900/80 border border-zinc-800/50 hover:border-zinc-700/60 transition-all duration-200"
                                >
                                  {/* Thumbnail placeholder with play hover button */}
                                  <div className="relative w-full sm:w-32 aspect-video rounded-md overflow-hidden bg-zinc-950 border border-zinc-800 flex-none group-hover/episode:border-zinc-700 transition">
                                    <img
                                      src={episode.thumbnailUrl || modalData.posterUrl}
                                      alt={episode.title}
                                      referrerPolicy="no-referrer"
                                      className="w-full h-full object-cover group-hover/episode:scale-105 transition duration-300 opacity-85"
                                    />
                                    <div className="absolute inset-0 flex items-center justify-center bg-black/35 group-hover/episode:bg-black/55 transition">
                                      <button
                                        onClick={() => {
                                          const episodeMovie: Movie = {
                                            ...selectedMovieForDetails!,
                                            id: selectedMovieForDetails!.id,
                                            title: `${selectedMovieForDetails!.title}: S${episode.seasonNumber}E${episode.episodeNumber} - ${episode.title}`,
                                            videoUrl: episode.videoUrl,
                                            thumbnailUrl: episode.thumbnailUrl || selectedMovieForDetails!.thumbnailUrl,
                                            duration: episode.duration,
                                            activeEpisodeId: episode.id,
                                            activeEpisodeNumber: episode.episodeNumber,
                                            activeSeasonNumber: episode.seasonNumber,
                                          };
                                          onPlayMovie(episodeMovie);
                                          setSelectedMovieForDetails(null);
                                          setTmdbJson(null);
                                        }}
                                        className="w-8 h-8 rounded-full bg-red-600 text-white flex items-center justify-center shadow-lg transform scale-90 opacity-0 group-hover/episode:scale-100 group-hover/episode:opacity-100 transition-all duration-300"
                                        title="Play Episode"
                                      >
                                        <Play className="w-4 h-4 fill-current ml-0.5" />
                                      </button>
                                    </div>
                                    <span className="absolute bottom-1 right-1 bg-black/85 px-1 py-0.2 text-[9px] font-mono text-zinc-300 rounded border border-zinc-800">
                                      {episode.duration}
                                    </span>
                                  </div>

                                  {/* Episode metadata */}
                                  <div className="flex-1 space-y-1 text-left">
                                    <div className="flex items-center justify-between gap-2">
                                      <h5 className="text-xs font-bold text-white group-hover/episode:text-red-500 transition duration-150">
                                        {episode.episodeNumber}. {episode.title}
                                      </h5>
                                      
                                      {/* Episode on-server indicator */}
                                      <div
                                        className="p-1.5 bg-black/60 rounded border border-zinc-800 flex items-center justify-center"
                                        title="Available on server"
                                      >
                                        <Database className="w-3.5 h-3.5 text-emerald-400" />
                                      </div>
                                    </div>
                                    <p className="text-[11px] text-zinc-400 font-normal leading-relaxed line-clamp-2">
                                      {episode.description}
                                    </p>
                                  </div>
                                </div>
                              ))}
                            </div>
                          </div>
                        );
                      })()}

                      {/* Genre Tags container */}
                      <div className="space-y-1">
                        <h4 className="text-[10px] uppercase font-extrabold text-zinc-500 tracking-wider">Genres</h4>
                        <div id="modal-genres-container" className="flex flex-wrap gap-1.5">
                          {modalData.genres.map((genre: string) => (
                            <button
                              key={genre}
                              className="text-[9px] sm:text-[10px] uppercase font-extrabold tracking-wider border border-[var(--theme-accent)]/40 text-[var(--theme-accent)] px-2 py-0.5 rounded bg-[var(--theme-accent)]/5 hover:bg-[var(--theme-accent)]/20 hover:border-[var(--theme-accent)] transition duration-200"
                            >
                              {genre}
                            </button>
                          ))}
                        </div>
                      </div>

                      {/* Cast List row or grid - More compact layout */}
                      <div className="space-y-1">
                        <h4 className="text-[10px] uppercase font-extrabold text-zinc-500 tracking-wider">Cast</h4>
                        <div id="modal-cast-container" className="grid grid-cols-2 sm:grid-cols-3 gap-1.5">
                          {modalData.cast && modalData.cast.length > 0 ? (
                            modalData.cast.slice(0, 6).map((member: any) => {
                              const name = typeof member === "string" ? member : member.name;
                              const character = typeof member === "string" ? "" : member.character;
                              return (
                                <div key={name} className="bg-zinc-900/60 p-1.5 px-2 rounded border border-zinc-800 text-[11px]">
                                  <div className="font-bold text-zinc-200 truncate">{name}</div>
                                  {character && <div className="text-[9px] text-zinc-500 truncate italic">{character}</div>}
                                </div>
                              );
                            })
                          ) : (
                            <div className="text-zinc-500 italic text-xs">No cast members available</div>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                </div>

                {/* Sticky bottom Action Bar - Reduced vertical spacing */}
                <div className="px-5 sm:px-6 py-3 bg-[#121212] border-t border-zinc-800 flex flex-wrap items-center justify-between gap-3 flex-none">
                  <div className="flex flex-wrap items-center gap-3">
                    <button
                      onClick={() => {
                        if (!isActuallyDownloaded) {
                          handlePreFillDownload(tmdbId.toString(), selectedMovieForDetails.type || "movie");
                          setSelectedMovieForDetails(null);
                          setTmdbJson(null);
                          return;
                        }
                        // Click play now routes the user to the Video Player page with the specific movie ID
                        const targetMovie = selectedMovieForDetails || (movies.find(m => m.id === modalData.id));
                        if (targetMovie) {
                          if (targetMovie.type === "series" && savedSession?.episodeId) {
                            const targetEpisode = targetMovie.episodes?.find(ep => ep.id === savedSession.episodeId);
                            if (targetEpisode) {
                              const episodeMovie: Movie = {
                                ...targetMovie,
                                title: `${targetMovie.title}: S${targetEpisode.seasonNumber}E${targetEpisode.episodeNumber} - ${targetEpisode.title}`,
                                videoUrl: targetEpisode.videoUrl,
                                thumbnailUrl: targetEpisode.thumbnailUrl || targetMovie.thumbnailUrl,
                                duration: targetEpisode.duration,
                                activeEpisodeId: targetEpisode.id,
                                activeEpisodeNumber: targetEpisode.episodeNumber,
                                activeSeasonNumber: targetEpisode.seasonNumber,
                              };
                              onPlayMovie(episodeMovie);
                              setSelectedMovieForDetails(null);
                              setTmdbJson(null);
                              return;
                            }
                          }
                          onPlayMovie(targetMovie);
                        }
                        setSelectedMovieForDetails(null);
                        setTmdbJson(null);
                      }}
                      className="px-6 py-2.5 sm:px-8 sm:py-3 bg-[#E50914] hover:bg-red-700 active:scale-95 text-white font-bold rounded-sm shadow-lg flex items-center gap-2 text-xs md:text-sm uppercase tracking-widest transition-all duration-200"
                      id="modal-play-btn"
                    >
                      {isActuallyDownloaded ? (
                        savedTimestamp > 0 ? (
                          <>
                            <Clock className="w-4.5 h-4.5 text-white animate-pulse" />
                            <span>Resume from {Math.floor(savedTimestamp / 60) || 1} Minutes</span>
                          </>
                        ) : (
                          <>
                            <Play className="w-4.5 h-4.5 fill-current" />
                            <span>Play Now</span>
                          </>
                        )
                      ) : (
                        <>
                          <Download className="w-4.5 h-4.5 text-white" />
                          <span>{selectedMovieForDetails.type === "series" ? "Add / Ingest Series" : "Download / Ingest Movie"}</span>
                        </>
                      )}
                    </button>

                    {/* Add to Watchlist Button */}
                    <button
                      onClick={() => toggleWatchlist(modalData.id)}
                      className={`px-5 py-2.5 sm:py-3 border rounded-sm font-bold text-xs md:text-sm uppercase tracking-widest transition-all duration-200 active:scale-95 flex items-center gap-2 ${
                        watchlist.includes(modalData.id)
                          ? "border-green-500 text-green-500 bg-green-500/10 hover:bg-green-500/20"
                          : "border-zinc-500 text-zinc-300 hover:text-white hover:border-white hover:bg-white/5"
                      }`}
                      id="modal-watchlist-btn"
                    >
                      {watchlist.includes(modalData.id) ? (
                        <>
                          <Check className="w-4.5 h-4.5" />
                          <span>In Watchlist</span>
                        </>
                      ) : (
                        <>
                          <Plus className="w-4.5 h-4.5" />
                          <span>Add to Watchlist</span>
                        </>
                      )}
                    </button>

                    {/* PWA Download to Browser Button (Movies only) */}
                    {isActuallyDownloaded && selectedMovieForDetails.type === "movie" && (() => {
                      const isCached = browserCachedItems.includes(modalData.id);
                      const progress = browserDownloadingProgress[modalData.id];
                      
                      if (progress !== undefined) {
                        return (
                          <button
                            disabled
                            className="px-5 py-2.5 sm:py-3 border border-red-500/30 bg-red-500/10 text-red-400 rounded-sm font-bold text-xs md:text-sm uppercase tracking-widest flex items-center gap-2 cursor-not-allowed"
                          >
                            <span className="w-4 h-4 rounded-full border-2 border-t-transparent border-red-500 animate-spin" />
                            <span>Downloading {progress}%</span>
                          </button>
                        );
                      }
                      
                      if (isCached) {
                        return (
                          <button
                            onClick={() => {
                              if (confirm("Remove this offline download from your browser?")) {
                                handleRemoveFromBrowser(modalData.id, selectedMovieForDetails.videoUrl);
                              }
                            }}
                            className="px-5 py-2.5 sm:py-3 border border-red-500/40 text-red-500 bg-red-500/5 hover:bg-red-500/15 rounded-sm font-bold text-xs md:text-sm uppercase tracking-widest transition-all duration-200 active:scale-95 flex items-center gap-2"
                          >
                            <Trash2 className="w-4.5 h-4.5" />
                            <span>Delete Offline</span>
                          </button>
                        );
                      }
                      
                      return (
                        <button
                          onClick={() => handleDownloadToBrowser(modalData.id, selectedMovieForDetails.videoUrl, selectedMovieForDetails.title)}
                          className="px-5 py-2.5 sm:py-3 border border-zinc-500 text-zinc-300 hover:text-white hover:border-white hover:bg-white/5 rounded-sm font-bold text-xs md:text-sm uppercase tracking-widest transition-all duration-200 active:scale-95 flex items-center gap-2"
                        >
                          <Download className="w-4.5 h-4.5" />
                          <span>Save Offline</span>
                        </button>
                      );
                    })()}
                  </div>
                </div>
              </motion.div>
            </div>
          );
        })()}
      </AnimatePresence>
    </div>
  );
}

/* HORIZONTAL SCROLLABLE MOVIE ROW COMPONENT */
/* HORIZONTAL SCROLLABLE MOVIE ROW COMPONENT */
interface MovieRowProps {
  key?: string;
  title: string;
  movies: Movie[];
  onPlay: (movie: Movie) => void;
  moviesOnServer?: Movie[];
  onAddDownload?: (tmdbId: string, mediaType: string) => void;
  activeProfileTheme?: string;
}

function MovieRow({ title, movies, onPlay, moviesOnServer, onAddDownload, activeProfileTheme }: MovieRowProps) {
  const rowRef = useRef<HTMLDivElement>(null);
  const [showLeftArrow, setShowLeftArrow] = useState(false);
  const [showRightArrow, setShowRightArrow] = useState(true);

  const checkScrollPosition = () => {
    if (rowRef.current) {
      const { scrollLeft, scrollWidth, clientWidth } = rowRef.current;
      setShowLeftArrow(scrollLeft > 5);
      setShowRightArrow(scrollLeft < scrollWidth - clientWidth - 5);
    }
  };

  const handleScroll = (direction: "left" | "right") => {
    if (rowRef.current) {
      const { clientWidth } = rowRef.current;
      const scrollAmount = direction === "left" ? -clientWidth * 0.75 : clientWidth * 0.75;
      rowRef.current.scrollBy({ left: scrollAmount, behavior: "smooth" });
    }
  };

  useEffect(() => {
    const el = rowRef.current;
    if (el) {
      el.addEventListener("scroll", checkScrollPosition);
      checkScrollPosition();
    }
    return () => el?.removeEventListener("scroll", checkScrollPosition);
  }, [movies]);

  return (
    <div className="space-y-2 relative group/row">
      <h3 className={`text-lg md:text-xl font-semibold tracking-tight text-zinc-200 hover:text-white transition duration-200 pl-1 cursor-pointer text-left ${
        activeProfileTheme === "gemini" ? "font-mono text-cyan-400" : activeProfileTheme === "apple" ? "font-serif tracking-normal text-white" : ""
      }`}>
        {activeProfileTheme === "gemini" ? `> SELECT * FROM ${title.toUpperCase().replace(/\s+/g, "_")}` : title}
      </h3>

      <div className="relative -mx-6 md:-mx-12">
        {/* Left Scroll Arrow */}
        {showLeftArrow && (
          <button
            onClick={() => handleScroll("left")}
            className="absolute left-0 top-0 bottom-0 w-10 md:w-14 bg-black/65 hover:bg-black/85 text-white z-40 flex items-center justify-center opacity-0 group-hover/row:opacity-100 transition-all duration-300 cursor-pointer"
            id={`scroll-left-${title.replace(/\s+/g, "-").toLowerCase()}`}
          >
            <ChevronLeft className="w-6 h-6 hover:scale-125 transition-transform" />
          </button>
        )}

        {/* Right Scroll Arrow */}
        {showRightArrow && (
          <button
            onClick={() => handleScroll("right")}
            className="absolute right-0 top-0 bottom-0 w-10 md:w-14 bg-black/65 hover:bg-black/85 text-white z-40 flex items-center justify-center opacity-0 group-hover/row:opacity-100 transition-all duration-300 cursor-pointer"
            id={`scroll-right-${title.replace(/\s+/g, "-").toLowerCase()}`}
          >
            <ChevronRight className="w-6 h-6 hover:scale-125 transition-transform" />
          </button>
        )}

        {/* Scrollable Container */}
        <div
          ref={rowRef}
          className="flex space-x-4 overflow-x-auto scrollbar-hide py-4 px-6 md:px-12 scroll-smooth"
          style={{ scrollbarWidth: "none" }}
        >
          {movies.map((movie) => (
            <div 
              key={movie.id} 
              className="flex-none hover:z-30 relative transition-all duration-300 w-[calc((100%-16px)/2)] sm:w-[calc((100%-32px)/3)] md:w-[calc((100%-48px)/4)] lg:w-[calc((100%-64px)/5)]"
            >
              <MovieCard movie={movie} onPlay={onPlay} moviesOnServer={moviesOnServer} onAddDownload={onAddDownload} activeProfileTheme={activeProfileTheme} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/* INDIVIDUAL MOVIE CARD COMPONENT WITH PREMIUM HOVER DETAILS */
interface MovieCardProps {
  key?: string;
  movie: Movie;
  onPlay: (movie: Movie) => void;
  moviesOnServer?: Movie[];
  onAddDownload?: (tmdbId: string, mediaType: string) => void;
  activeProfileTheme?: string;
}

function MovieCard({ movie, onPlay, moviesOnServer, onAddDownload, activeProfileTheme }: MovieCardProps) {
  const tmdbId = (movie as any).tmdb_id || (movie as any).tmdbId;
  const isDownloaded = (movie.videoUrl && movie.videoUrl.startsWith("/media/")) || moviesOnServer?.some(
    (m) => m.id === movie.id || (tmdbId && (m.id === `m_${tmdbId}` || m.id === `tv_${tmdbId}`))
  ) || false;

  const handleCardClick = () => {
    onPlay(movie);
  };

  if (activeProfileTheme === "prime") {
    return (
      <div 
        onClick={handleCardClick}
        className="aspect-video w-full rounded-lg overflow-hidden bg-[#151a21] border border-cyan-850/20 hover:border-cyan-400 hover:scale-105 hover:shadow-[0_0_15px_rgba(26,152,255,0.2)] transition-all duration-300 cursor-pointer relative group"
        id={`movie-card-${movie.id}`}
      >
        <img src={movie.thumbnailUrl} alt={movie.title} className="w-full h-full object-cover" />
        <div className="absolute inset-0 bg-gradient-to-t from-black/80 via-black/30 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-300 flex flex-col justify-end p-2 text-left">
          <h4 className="text-xs font-bold text-white tracking-wide truncate">{movie.title}</h4>
          <span className="text-[9px] text-cyan-400 font-semibold">{movie.releaseYear} • {movie.duration}</span>
        </div>
        {/* DOWNLOADED LOGO BADGE */}
        {isDownloaded && (
          <div className="absolute top-2 left-2 bg-black/70 backdrop-blur-md border border-emerald-500/30 text-emerald-400 p-1 rounded-md shadow-md z-20 flex items-center justify-center" title="Local Server File">
            <Database className="w-3 h-3 text-emerald-400" />
          </div>
        )}
      </div>
    );
  }

  if (activeProfileTheme === "apple") {
    return (
      <div 
        onClick={handleCardClick}
        className="flex flex-col text-left cursor-pointer group"
        id={`movie-card-${movie.id}`}
      >
        <div className="aspect-video w-full rounded-xl overflow-hidden bg-[#222] border border-white/10 hover:border-white/30 hover:scale-105 transition-all duration-300 shadow-lg relative">
          <img src={movie.thumbnailUrl} alt={movie.title} className="w-full h-full object-cover group-hover:scale-110 transition-transform duration-500" />
          <div className="absolute inset-0 bg-black/20 opacity-0 group-hover:opacity-100 flex items-center justify-center transition-opacity duration-300">
            <Play className="w-8 h-8 text-white fill-current" />
          </div>
          {/* DOWNLOADED LOGO BADGE */}
          {isDownloaded && (
            <div className="absolute top-2 left-2 bg-black/70 backdrop-blur-md border border-emerald-500/30 text-emerald-400 p-1 rounded-md shadow-md z-20 flex items-center justify-center" title="Local Server File">
              <Database className="w-3 h-3 text-emerald-400" />
            </div>
          )}
        </div>
        <h4 className="text-[11px] font-bold text-zinc-100 mt-2 tracking-wide truncate group-hover:text-white uppercase font-sans">
          {movie.title}
        </h4>
        <span className="text-[9px] text-zinc-400 font-serif italic">
          {movie.releaseYear} • {movie.rating || "PG-13"}
        </span>
      </div>
    );
  }

  if (activeProfileTheme === "gemini") {
    return (
      <div 
        onClick={handleCardClick}
        className="aspect-video w-full rounded-none bg-[#030712] border border-purple-500/30 hover:border-cyan-400 hover:shadow-[0_0_10px_rgba(168,85,247,0.3)] hover:scale-105 transition-all duration-300 cursor-pointer relative group font-mono text-left"
        id={`movie-card-${movie.id}`}
      >
        <img src={movie.thumbnailUrl} alt={movie.title} className="w-full h-full object-cover opacity-80 group-hover:opacity-45 transition-opacity duration-300" />
        <div className="absolute top-1.5 right-1.5 bg-black/60 border border-purple-500/40 text-purple-400 text-[8px] px-1 rounded">
          {movie.duration}
        </div>
        {/* DOWNLOADED LOGO BADGE */}
        {isDownloaded && (
          <div className="absolute top-1.5 left-1.5 bg-black/70 backdrop-blur-md border border-emerald-500/30 text-emerald-400 p-1 rounded-sm shadow-md z-20 flex items-center justify-center" title="Local Server File">
            <Database className="w-2.5 h-2.5 text-emerald-400" />
          </div>
        )}
        <div className="absolute inset-x-0 bottom-0 p-2 bg-black/85 border-t border-purple-500/20 opacity-0 group-hover:opacity-100 transition-opacity duration-300 flex flex-col justify-end">
          <h4 className="text-[10px] font-bold text-cyan-400 truncate">&gt; {movie.title}</h4>
          <div className="flex justify-between text-[8px] text-zinc-500 mt-1">
            <span>RES: {movie.rating || "Source"}</span>
            <span>YEAR: {movie.releaseYear}</span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div
      onClick={handleCardClick}
      tabIndex={0}
      className="group relative rounded-md overflow-hidden bg-[#181818] cursor-pointer border border-zinc-800/40 hover:border-red-600 hover:scale-[1.08] hover:z-30 focus:scale-[1.08] focus:outline-none transition-all duration-300 aspect-video shadow-2xl hover:shadow-[0_20px_25px_-5px_rgba(0,0,0,0.8),_0_10px_10px_-5px_rgba(0,0,0,0.8)]"
      id={`movie-card-${movie.id}`}
    >
      {/* Thumbnail */}
      <img
        src={movie.thumbnailUrl}
        alt={movie.title}
        referrerPolicy="no-referrer"
        className="w-full h-full object-cover group-hover:opacity-30 group-focus:opacity-30 transition-opacity duration-300"
      />

      {/* DOWNLOADED LOGO BADGE */}
      {isDownloaded && (
        <div className="absolute top-2 left-2 bg-black/75 backdrop-blur-md border border-emerald-500/30 text-emerald-400 p-1.5 rounded-full shadow-md z-20 flex items-center justify-center transition-transform group-hover:scale-90" title="Local Server File">
          <Database className="w-3 h-3 text-emerald-400" />
        </div>
      )}

      {/* Hover Info Overlay with Slide-Up Transition */}
      <div className="absolute inset-0 flex flex-col justify-end p-3.5 bg-gradient-to-t from-black via-black/90 to-transparent opacity-0 translate-y-3 group-hover:opacity-100 group-hover:translate-y-0 group-focus:opacity-100 group-focus:translate-y-0 transition-all duration-300 ease-out">
        <h4 className="text-sm md:text-md font-normal text-white mb-1 line-clamp-1 uppercase tracking-wider text-left border-b border-red-600/30 pb-0.5" style={{ fontFamily: "var(--theme-title-font, 'Bebas Neue', sans-serif)" }}>
          {movie.title}
        </h4>
        <div className="flex items-center space-x-2 text-[9px] md:text-[10px] text-zinc-400 mb-2 font-medium">
          <span className="text-zinc-300">{movie.releaseYear}</span>
          <span className="border border-zinc-700 px-1.5 py-0.2 rounded-[1px] text-[8px] md:text-[9px] uppercase bg-zinc-900/60 text-zinc-300 font-extrabold">
            {movie.rating || "PG-13"}
          </span>
          {movie.duration && <span className="text-zinc-500">•</span>}
          <span className="text-zinc-300">{movie.duration}</span>
        </div>
        <p className="text-[9px] md:text-[10px] text-zinc-400 line-clamp-2 leading-relaxed mb-3 text-left">
          {movie.description}
        </p>

        <button
          className="w-full bg-white hover:bg-zinc-200 text-black font-bold py-1.5 rounded-sm text-[10px] md:text-xs flex items-center justify-center space-x-1.5 shadow-lg transition-transform active:scale-95 cursor-pointer uppercase tracking-wider"
        >
          <Info className="w-3.5 h-3.5" />
          <span>View Details</span>
        </button>
      </div>
    </div>
  );
}

/* CONTINUE WATCHING COMPONENTS */
function ContinueWatchingCard({
  movie,
  session,
  onPlay,
  parseDurationToSeconds,
}: {
  movie: Movie;
  session: PlaybackSession;
  onPlay: (movie: Movie) => void;
  parseDurationToSeconds: (d: string) => number;
}) {
  const episode = movie.type === "series" && session.episodeId ? movie.episodes?.find((ep) => ep.id === session.episodeId) : undefined;
  const totalSeconds = episode ? parseDurationToSeconds(episode.duration) : parseDurationToSeconds(movie.duration);
  const watchedSeconds = session.timestamp;
  const percentage = Math.min(100, Math.max(1, Math.floor((watchedSeconds / totalSeconds) * 100)));

  // Helper to format position
  const formatPosition = (secs: number) => {
    const mins = Math.floor(secs / 60);
    const remainingSecs = secs % 60;
    return `${mins}:${remainingSecs < 10 ? "0" : ""}${remainingSecs}`;
  };

  const handleClick = () => {
    if (movie.type === "series" && episode) {
      const episodeMovie: Movie = {
        ...movie,
        title: `${movie.title}: S${episode.seasonNumber}E${episode.episodeNumber} - ${episode.title}`,
        videoUrl: episode.videoUrl,
        thumbnailUrl: episode.thumbnailUrl || movie.thumbnailUrl,
        duration: episode.duration,
        activeEpisodeId: episode.id,
        activeEpisodeNumber: episode.episodeNumber,
        activeSeasonNumber: episode.seasonNumber,
      };
      onPlay(episodeMovie);
    } else {
      onPlay(movie);
    }
  };

  return (
    <div
      onClick={handleClick}
      tabIndex={0}
      className="group relative rounded-md overflow-hidden bg-[#222] cursor-pointer border border-white/10 hover:border-white/30 hover:scale-105 hover:z-30 focus:scale-105 focus:outline-none focus:ring-4 focus:ring-[#E50914] focus:border-transparent transition-all duration-300 aspect-video shadow-2xl"
      id={`movie-card-cw-${movie.id}`}
    >
      {/* Thumbnail */}
      <img
        src={episode?.thumbnailUrl || movie.thumbnailUrl}
        alt={movie.title}
        referrerPolicy="no-referrer"
        className="w-full h-full object-cover group-hover:opacity-40 transition-opacity duration-300"
      />

      {/* Red Play Overlay on Hover */}
      <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity duration-300 bg-black/40">
        <div className="w-10 h-10 rounded-full bg-white/20 backdrop-blur-md border border-white/40 flex items-center justify-center text-white scale-90 group-hover:scale-100 transition-transform duration-300">
          <Play className="w-5 h-5 fill-current ml-0.5 text-white" />
        </div>
      </div>

      {/* Info Overlay at bottom */}
      <div className="absolute bottom-0 inset-x-0 bg-gradient-to-t from-black via-black/80 to-transparent p-2.5 flex flex-col justify-end pt-6">
        <h4 className="text-xs font-bold text-white line-clamp-1 truncate uppercase tracking-tight">
          {episode ? `${movie.title} - S${episode.seasonNumber}E${episode.episodeNumber}` : movie.title}
        </h4>
        <div className="flex items-center justify-between text-[9px] text-zinc-400 mt-0.5 font-medium">
          <span>Resume at {formatPosition(watchedSeconds)}</span>
          <span>{percentage}% watched</span>
        </div>
        
        {/* Progress Bar Container */}
        <div className="w-full h-1 bg-zinc-700/80 rounded-full mt-1.5 overflow-hidden">
          <div
            className="h-full bg-[#E50914] rounded-full transition-all duration-300"
            style={{ width: `${percentage}%` }}
          />
        </div>
      </div>
    </div>
  );
}

interface ContinueWatchingRowProps {
  sessions: PlaybackSession[];
  movies: Movie[];
  onPlay: (movie: Movie) => void;
  parseDurationToSeconds: (d: string) => number;
}

function ContinueWatchingRow({ sessions, movies, onPlay, parseDurationToSeconds }: ContinueWatchingRowProps) {
  const rowRef = useRef<HTMLDivElement>(null);
  const [showLeftArrow, setShowLeftArrow] = useState(false);
  const [showRightArrow, setShowRightArrow] = useState(true);

  const checkScrollPosition = () => {
    if (rowRef.current) {
      const { scrollLeft, scrollWidth, clientWidth } = rowRef.current;
      setShowLeftArrow(scrollLeft > 5);
      setShowRightArrow(scrollLeft < scrollWidth - clientWidth - 5);
    }
  };

  const handleScroll = (direction: "left" | "right") => {
    if (rowRef.current) {
      const { clientWidth } = rowRef.current;
      const scrollAmount = direction === "left" ? -clientWidth * 0.75 : clientWidth * 0.75;
      rowRef.current.scrollBy({ left: scrollAmount, behavior: "smooth" });
    }
  };

  useEffect(() => {
    const el = rowRef.current;
    if (el) {
      el.addEventListener("scroll", checkScrollPosition);
      checkScrollPosition();
    }
    return () => el?.removeEventListener("scroll", checkScrollPosition);
  }, [sessions]);

  // Map sessions to movies, filtering out finished sessions
  const validItems = sessions
    .filter((session) => {
      const isFinished = session.is_finished || (session as any).isFinished || 
                         ((session as any).completion_rate && (session as any).completion_rate >= 0.8) || 
                         ((session as any).completionRate && (session as any).completionRate >= 0.8);
      return !isFinished;
    })
    .map((session) => {
      const movie = movies.find((m) => m.id === session.movieId);
      return movie ? { movie, session } : null;
    })
    .filter(Boolean) as { movie: Movie; session: PlaybackSession }[];

  if (validItems.length === 0) return null;

  return (
    <div className="space-y-2 relative group/row" id="continue-watching-row">
      <h3 className="text-lg md:text-xl font-bold tracking-tight text-white hover:text-red-500 transition duration-200 pl-1 cursor-pointer flex items-center gap-2 font-sans">
        <span className="w-1 h-5 bg-[#E50914] rounded-sm"></span>
        Continue Watching
      </h3>

      <div className="relative -mx-6 md:-mx-12">
        {/* Left Scroll Arrow */}
        {showLeftArrow && (
          <button
            onClick={() => handleScroll("left")}
            className="absolute left-0 top-0 bottom-0 w-10 md:w-14 bg-black/75 hover:bg-black/90 text-white z-40 flex items-center justify-center opacity-0 group-hover/row:opacity-100 transition-all duration-300 border-r border-white/5 cursor-pointer"
            id="scroll-left-continue-watching"
          >
            <ChevronLeft className="w-6 h-6 hover:scale-125 transition-transform" />
          </button>
        )}

        {/* Right Scroll Arrow */}
        {showRightArrow && (
          <button
            onClick={() => handleScroll("right")}
            className="absolute right-0 top-0 bottom-0 w-10 md:w-14 bg-black/75 hover:bg-black/90 text-white z-40 flex items-center justify-center opacity-0 group-hover/row:opacity-100 transition-all duration-300 border-l border-white/5 cursor-pointer"
            id="scroll-right-continue-watching"
          >
            <ChevronRight className="w-6 h-6 hover:scale-125 transition-transform" />
          </button>
        )}

        {/* Scrollable Container */}
        <div
          ref={rowRef}
          className="flex space-x-4 overflow-x-auto scrollbar-hide py-4 px-6 md:px-12 scroll-smooth"
          style={{ scrollbarWidth: "none" }}
        >
          {validItems.map(({ movie, session }) => (
            <div 
              key={`${movie.id}-${session.episodeId || 'default'}`}
              className="flex-none hover:z-30 relative transition-all duration-300 w-[calc((100%-16px)/2)] sm:w-[calc((100%-32px)/3)] md:w-[calc((100%-48px)/4)] lg:w-[calc((100%-64px)/5)]"
            >
              <ContinueWatchingCard
                movie={movie}
                session={session}
                onPlay={onPlay}
                parseDurationToSeconds={parseDurationToSeconds}
              />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

const DEFAULT_TEMPLATE_DATA: any[] = [];
