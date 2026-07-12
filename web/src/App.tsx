import { useState, useEffect } from "react";
import ProfileSelector from "./components/ProfileSelector";
import Dashboard from "./components/Dashboard";
import VideoPlayer from "./components/VideoPlayer";
import LoginScreen from "./components/LoginScreen";
import { Profile, Movie } from "./types";

const normalizeApiBaseUrl = (url: string): string => {
  let cleaned = url.trim();
  if (cleaned.endsWith("/")) {
    cleaned = cleaned.slice(0, -1);
  }
  if (!cleaned.endsWith("/api")) {
    cleaned = cleaned + "/api";
  }
  return cleaned;
};

export default function App() {
  // Profiles state - initialized from localStorage cache, updated by database fetch
  const [profiles, setProfiles] = useState<Profile[]>(() => {
    try {
      const cached = localStorage.getItem("stream_profiles_list");
      return cached ? JSON.parse(cached) : [];
    } catch (e) {
      return [];
    }
  });
  const [activeProfile, setActiveProfile] = useState<Profile | null>(null);
  const [activeMovie, setActiveMovie] = useState<Movie | null>(null);
  const [apiBaseUrl, setApiBaseUrl] = useState<string>("/api");
  const [apiBearerToken, setApiBearerToken] = useState<string>("secure-token-123");
  const [accessToken, setAccessToken] = useState<string | null>(() => localStorage.getItem("stream_access_token"));
  const [activeTab, setActiveTab] = useState<string>("home");
  const [selectedMovieForDetails, setSelectedMovieForDetails] = useState<Movie | null>(null);
  
  // Movies catalog state - initialized from localStorage cache, updated by database fetch
  const [allMovies, setAllMovies] = useState<Movie[]>(() => {
    try {
      const cached = localStorage.getItem("stream_movies_catalog");
      return cached ? JSON.parse(cached) : [];
    } catch (e) {
      return [];
    }
  });

  // Load profiles from backend database
  useEffect(() => {
    const fetchProfiles = async () => {
      try {
        const token = localStorage.getItem("stream_access_token");
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;
        const res = await fetch(`${apiBaseUrl}/profiles`, { headers });
        if (res.ok) {
          const data = await res.json();
          if (Array.isArray(data)) {
            setProfiles(data);
            localStorage.setItem("stream_profiles_list", JSON.stringify(data));
            
            // Sync active profile if one is selected
            const savedProfile = localStorage.getItem("stream_active_profile");
            if (savedProfile) {
              const parsed = JSON.parse(savedProfile);
              const foundProfile = data.find((p: Profile) => p.id === parsed.id);
              if (foundProfile) {
                setActiveProfile(foundProfile);
                localStorage.setItem("stream_active_profile", JSON.stringify(foundProfile));
              }
            }
          }
        }
      } catch (e) {
        console.warn("[App] Failed to fetch profiles from API, using fallback", e);
      }
    };
    fetchProfiles();
  }, [apiBaseUrl, accessToken]);

  // Load API base URL configuration on mount
  useEffect(() => {
    const savedApiBaseUrl = localStorage.getItem("stream_api_base_url");
    if (savedApiBaseUrl) {
      setApiBaseUrl(normalizeApiBaseUrl(savedApiBaseUrl));
    }
    const savedToken = localStorage.getItem("stream_api_bearer_token");
    if (savedToken) {
      setApiBearerToken(savedToken);
    }
  }, []);

  // Fetch all movies at the top level to share across VideoPlayer, Details Modal, and Rows
  useEffect(() => {
    const fetchMovies = async () => {
      try {
        const token = localStorage.getItem("stream_access_token");
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;
        const res = await fetch(`${apiBaseUrl}/movies`, { headers });
        if (res.ok) {
          const data = await res.json();
          if (Array.isArray(data)) {
            setAllMovies(data);
            localStorage.setItem("stream_movies_catalog", JSON.stringify(data));
          }
        }
      } catch (e) {
        console.warn("[App] Failed to retrieve movies from live API backend", e);
      }
    };
    fetchMovies();
  }, [apiBaseUrl, accessToken]);

  // Read URL query parameters and synchronize them to React State
  const syncStateFromUrl = () => {
    const searchParams = new URLSearchParams(window.location.search);
    const profileId = searchParams.get("profile");
    const viewParam = searchParams.get("view") || "home";
    const watchId = searchParams.get("watch");
    const movieId = searchParams.get("movie");

    // 1. Profile sync
    if (profileId) {
      const foundProfile = profiles.find((p) => p.id === profileId);
      if (foundProfile) {
        setActiveProfile(foundProfile);
      } else {
        setActiveProfile(null);
      }
    } else {
      const savedProfile = localStorage.getItem("stream_active_profile");
      if (savedProfile) {
        try {
          const parsed = JSON.parse(savedProfile);
          const foundProfile = profiles.find((p) => p.id === parsed.id);
          if (foundProfile) {
            setActiveProfile(foundProfile);
            // Sync profile to URL search parameters
            const url = new URL(window.location.href);
            url.searchParams.set("profile", foundProfile.id);
            window.history.replaceState({}, "", url.pathname + url.search);
          } else {
            setActiveProfile(null);
          }
        } catch (e) {
          setActiveProfile(null);
        }
      } else {
        setActiveProfile(null);
      }
    }

    // 2. View Tab sync
    setActiveTab(viewParam);

    // 3. Playback / Watching movie sync
    if (watchId) {
      const foundMovie = allMovies.find((m) => m.id === watchId);
      if (foundMovie) {
        setActiveMovie(foundMovie);
      } else if (allMovies.length > 0) {
        // Fallback or loading wait state
        setActiveMovie(null);
      } else {
        setActiveMovie(null);
      }
    } else {
      setActiveMovie(null);
    }

    // 4. Detail modal movie sync
    if (movieId) {
      const foundMovieForDetails = allMovies.find((m) => m.id === movieId);
      if (foundMovieForDetails) {
        setSelectedMovieForDetails(foundMovieForDetails);
      }
    } else {
      setSelectedMovieForDetails(null);
    }
  };

  // Synchronize on load and whenever pops/history navigations occur
  useEffect(() => {
    syncStateFromUrl();
  }, [profiles, allMovies]);

  useEffect(() => {
    window.addEventListener("popstate", syncStateFromUrl);
    return () => window.removeEventListener("popstate", syncStateFromUrl);
  }, [profiles, allMovies]);

  // Synchronize view=login when not logged in
  useEffect(() => {
    const url = new URL(window.location.href);
    if (!accessToken) {
      if (url.searchParams.get("view") !== "login") {
        url.search = "?view=login";
        window.history.replaceState({}, "", url.pathname + url.search);
      }
    } else {
      if (url.searchParams.get("view") === "login") {
        url.searchParams.delete("view");
        window.history.replaceState({}, "", url.pathname + url.search);
        syncStateFromUrl();
      }
    }
  }, [accessToken]);

  // Selection handlers that drive browser query string changes (pushState)
  const handleSelectProfile = (profile: Profile) => {
    setActiveProfile(profile);
    localStorage.setItem("stream_active_profile", JSON.stringify(profile));

    const url = new URL(window.location.href);
    url.searchParams.set("profile", profile.id);
    url.searchParams.set("view", "home");
    window.history.pushState({}, "", url.pathname + url.search);
  };

  const handleLogout = () => {
    setActiveProfile(null);
    setActiveMovie(null);
    setSelectedMovieForDetails(null);
    localStorage.removeItem("stream_active_profile");

    const url = new URL(window.location.href);
    url.searchParams.delete("profile");
    url.searchParams.delete("view");
    url.searchParams.delete("watch");
    url.searchParams.delete("movie");
    window.history.pushState({}, "", url.pathname + url.search);
  };

  const handleAccountLogout = () => {
    setAccessToken(null);
    localStorage.removeItem("stream_access_token");
    handleLogout();
  };

  const handlePlayMovie = (movie: Movie) => {
    setActiveMovie(movie);
    setSelectedMovieForDetails(null);

    const url = new URL(window.location.href);
    url.searchParams.set("watch", movie.id);
    url.searchParams.delete("movie");
    window.history.pushState({}, "", url.pathname + url.search);
  };

  const handleBackToDashboard = () => {
    console.log("[App] handleBackToDashboard called - clearing activeMovie state");
    setActiveMovie(null);

    const url = new URL(window.location.href);
    url.searchParams.delete("watch");
    window.history.pushState({}, "", url.pathname + url.search);
  };

  const handleTabChange = (tab: string) => {
    setActiveTab(tab);
    setSelectedMovieForDetails(null);

    const url = new URL(window.location.href);
    url.searchParams.set("view", tab);
    url.searchParams.delete("movie");
    window.history.pushState({}, "", url.pathname + url.search);
  };

  const handleSelectMovieForDetails = (movie: Movie | null) => {
    setSelectedMovieForDetails(movie);

    const url = new URL(window.location.href);
    if (movie) {
      url.searchParams.set("movie", movie.id);
    } else {
      url.searchParams.delete("movie");
    }
    window.history.pushState({}, "", url.pathname + url.search);
  };

  const handleChangeApiBaseUrl = (newUrl: string) => {
    const normalized = normalizeApiBaseUrl(newUrl);
    setApiBaseUrl(normalized);
    localStorage.setItem("stream_api_base_url", normalized);
  };

  const handleChangeApiBearerToken = (newToken: string) => {
    setApiBearerToken(newToken);
    localStorage.setItem("stream_api_bearer_token", newToken);
  };

  // Dynamic theme configuration mapper
  const getThemeConfig = (theme: "netflix" | "prime" | "apple" | "gemini") => {
    switch (theme) {
      case "prime":
        return {
          "--theme-bg": "#0b0c10",
          "--theme-accent": "#1A98FF",
          "--theme-accent-hover": "#0077D6",
          "--theme-accent-rgb": "26, 152, 255",
          "--theme-radius": "12px", // Soft curves
          "--theme-nav-radius": "9999px", // Rounded pills
          "--theme-font": "Inter, sans-serif",
          "--theme-border": "rgba(26, 152, 255, 0.2)",
          "--theme-border-hover": "rgba(26, 152, 255, 0.5)",
          "--theme-text-transform": "none",
          "--theme-tracking": "normal",
        };
      case "apple":
        return {
          "--theme-bg": "#101010",
          "--theme-accent": "#E5E5E7",
          "--theme-accent-hover": "#FFFFFF",
          "--theme-accent-rgb": "229, 229, 231",
          "--theme-radius": "20px", // High-end rounded
          "--theme-nav-radius": "12px",
          "--theme-font": "system-ui, -apple-system, sans-serif",
          "--theme-border": "rgba(255, 255, 255, 0.15)",
          "--theme-border-hover": "rgba(255, 255, 255, 0.4)",
          "--theme-text-transform": "none",
          "--theme-tracking": "0.15em", // Elegant spacing
        };
      case "gemini":
        return {
          "--theme-bg": "#030712",
          "--theme-accent": "#A855F7", // purple
          "--theme-accent-hover": "#06B6D4",
          "--theme-accent-rgb": "168, 85, 247",
          "--theme-radius": "6px", // Technical sharp/modern rounded
          "--theme-nav-radius": "6px",
          "--theme-font": "'JetBrains Mono', monospace",
          "--theme-border": "rgba(168, 85, 247, 0.3)",
          "--theme-border-hover": "rgba(6, 182, 212, 0.6)",
          "--theme-text-transform": "none",
          "--theme-tracking": "-0.01em",
        };
      case "netflix":
      default:
        return {
          "--theme-bg": "#020202",
          "--theme-accent": "#E50914",
          "--theme-accent-hover": "#C10712",
          "--theme-accent-rgb": "229, 9, 20",
          "--theme-radius": "2px", // Sharp blocky
          "--theme-nav-radius": "2px",
          "--theme-font": '"Montserrat", "Inter", sans-serif',
          "--theme-title-font": "'Bebas Neue', sans-serif",
          "--theme-border": "rgba(229, 9, 20, 0.2)",
          "--theme-border-hover": "rgba(229, 9, 20, 0.6)",
          "--theme-text-transform": "uppercase",
          "--theme-tracking": "0.15em",
        };
    }
  };

  const activeTheme = activeProfile?.theme || "netflix";
  const themeConfig = getThemeConfig(activeTheme);

  return (
    <div 
      className="bg-[#050505] min-h-screen text-[#E5E5E5] theme-font transition-all duration-300"
      data-theme={activeTheme}
      style={themeConfig as any}
    >
      {!accessToken ? (
        <LoginScreen
          onLoginSuccess={(token, email) => {
            setAccessToken(token);
            localStorage.setItem("stream_access_token", token);
          }}
          apiBaseUrl={apiBaseUrl}
        />
      ) : activeProfile && activeMovie ? (
        /* 1. Playing Active Movie View */
        <VideoPlayer
          movie={activeMovie}
          activeProfile={activeProfile}
          onBack={handleBackToDashboard}
          apiBaseUrl={apiBaseUrl}
        />
      ) : activeProfile ? (
        /* 2. Main Browse Dashboard (Supports Home, Movies, Series, MyList, Downloads, Settings tabs) */
        <Dashboard
          activeProfile={activeProfile}
          setActiveProfile={setActiveProfile}
          onLogout={handleLogout}
          onPlayMovie={handlePlayMovie}
          apiBaseUrl={apiBaseUrl}
          onChangeApiBaseUrl={handleChangeApiBaseUrl}
          apiBearerToken={apiBearerToken}
          onChangeApiBearerToken={handleChangeApiBearerToken}
          
          profiles={profiles}
          setProfiles={setProfiles}
          
          activeTab={activeTab}
          onTabChange={handleTabChange}
          
          selectedMovieForDetails={selectedMovieForDetails}
          setSelectedMovieForDetails={handleSelectMovieForDetails}
          accessToken={accessToken}
          onAccountLogout={handleAccountLogout}
        />
      ) : (
        /* 3. Initial Profile Selection Screen */
        <ProfileSelector 
          profiles={profiles} 
          setProfiles={setProfiles}
          onSelectProfile={handleSelectProfile}
          apiBaseUrl={apiBaseUrl}
        />
      )}
    </div>
  );
}
