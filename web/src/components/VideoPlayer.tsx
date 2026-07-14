import React, { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "motion/react";
import { ArrowLeft, Play, Pause, RotateCcw, RotateCw, Volume2, VolumeX, Maximize, RefreshCw, Settings, Gauge, Languages, Video, Music } from "lucide-react";
import { Movie, Profile } from "../types";

interface VideoPlayerProps {
  movie: Movie;
  activeProfile: Profile;
  onBack: () => void;
  apiBaseUrl: string;
}

const TRANSLATIONS: Record<string, Record<string, string>> = {
  en: {
    back_to_browse: "Back to Browse",
    buffering: "Buffering media...",
    playing_as: "Playing as",
    speed: "Playback Speed",
    quality: "Quality",
    audio: "Audio Track",
    subtitles: "Subtitles",
    none: "None"
  },
  tr: {
    back_to_browse: "Go Back to Dashboard",
    buffering: "Buffering stream...",
    playing_as: "Streaming as",
    speed: "Playback Speed",
    quality: "Resolution",
    audio: "Audio Channel",
    subtitles: "Subtitles",
    none: "None"
  }
};

export default function VideoPlayer({ movie: originalMovie, activeProfile, onBack, apiBaseUrl }: VideoPlayerProps) {
  const activeEpisode = originalMovie.type === "series" && originalMovie.activeEpisodeId
    ? originalMovie.episodes?.find(e => e.id === originalMovie.activeEpisodeId)
    : null;

  const movie = {
    ...originalMovie,
    videoUrl: activeEpisode?.videoUrl || originalMovie.videoUrl || "",
    quality: activeEpisode?.quality || originalMovie.quality || "Source",
    languages: activeEpisode?.languages || originalMovie.languages || ["en"],
    subtitles: activeEpisode?.subtitles || originalMovie.subtitles || [],
  };

  const videoRef = useRef<HTMLVideoElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const pulseIntervalRef = useRef<NodeJS.Timeout | null>(null);
  const seekedOnLoad = useRef<boolean>(false);
  const seekedRef = useRef<boolean>(false);
  const seekTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const targetSeekTimeRef = useRef<number | null>(null);

  const [isPlaying, setIsPlaying] = useState<boolean>(false);
  const [currentTime, setCurrentTime] = useState<number>(0);
  const [duration, setDuration] = useState<number>(0);
  const [volume, setVolume] = useState<number>(1);
  const [isMuted, setIsMuted] = useState<boolean>(false);
  const [playbackRate, setPlaybackRate] = useState<number>(1);
  const [isFullscreen, setIsFullscreen] = useState<boolean>(false);
  const [showSpeedMenu, setShowSpeedMenu] = useState<boolean>(false);
  const [showQualityMenu, setShowQualityMenu] = useState<boolean>(false);
  const [showSubtitlesMenu, setShowSubtitlesMenu] = useState<boolean>(false);
  const [showAudioMenu, setShowAudioMenu] = useState<boolean>(false);

  // Dynamic stream settings
  const [selectedQuality, setSelectedQuality] = useState<string>("1080p");
  const [isAutoMode, setIsAutoMode] = useState<boolean>(true);
  const [selectedAudioTrack, setSelectedAudioTrack] = useState<number>(0);
  const [selectedSubtitle, setSelectedSubtitle] = useState<string>("none");

  // Load language settings from localStorage
  const [lang] = useState<string>(() => localStorage.getItem("stream_pref_lang") || "en");
  const t = (key: string) => {
    const dict = TRANSLATIONS[lang] || TRANSLATIONS["en"];
    return dict[key] || TRANSLATIONS["en"][key] || key;
  };

  // Control overlay visibility state (hide when idle)
  const [controlsVisible, setControlsVisible] = useState<boolean>(true);
  const idleTimeoutRef = useRef<NodeJS.Timeout | null>(null);

  // Format seconds to hh:mm:ss
  const formatTime = (secs: number) => {
    if (isNaN(secs)) return "00:00";
    const hours = Math.floor(secs / 3600);
    const minutes = Math.floor((secs % 3600) / 60);
    const seconds = Math.floor(secs % 60);

    const formattedMinutes = minutes < 10 && hours > 0 ? `0${minutes}` : minutes;
    const formattedSeconds = seconds < 10 ? `0${seconds}` : seconds;

    if (hours > 0) {
      return `${hours}:${formattedMinutes}:${formattedSeconds}`;
    }
    return `${minutes < 10 ? "0" + minutes : minutes}:${formattedSeconds}`;
  };

  // Helper to save final progress on exit
  const saveFinalProgressAndBack = () => {
    console.log("[VideoPlayer] saveFinalProgressAndBack triggered");
    saveProgress();
    console.log("[VideoPlayer] Navigating back to browse dashboard");
    onBack();
  };

  // Shared save function used by pulse, pause, and exit handlers
  const saveProgress = () => {
    if (!videoRef.current) return;
    const timestamp = Math.floor(videoRef.current.currentTime);
    const isFinished = videoRef.current.duration ? (timestamp >= videoRef.current.duration - 15) : false;
    const completionRate = videoRef.current.duration ? (timestamp / videoRef.current.duration) : 0.0;

    const payload = {
      movieId: movie.id,
      movieTitle: movie.title,
      profileId: activeProfile.id,
      profileName: activeProfile.name,
      timestamp: timestamp,
      duration_watched: timestamp,
      completion_rate: completionRate,
      timestampFormatted: formatTime(timestamp),
      updatedAt: new Date().toISOString(),
      episodeId: movie.activeEpisodeId || null,
      is_finished: isFinished || (completionRate >= 0.8),
    };

    // Always save to localStorage first (synchronous, always works)
    const localKey = `continue_watching_${activeProfile.id}`;
    try {
      let localSessions: any[] = [];
      const saved = localStorage.getItem(localKey);
      if (saved) localSessions = JSON.parse(saved);
      const idx = localSessions.findIndex((s: any) =>
        s.movieId === movie.id &&
        (!movie.activeEpisodeId || s.episodeId === movie.activeEpisodeId)
      );
      if (idx > -1) {
        localSessions[idx] = payload;
      } else {
        localSessions.push(payload);
      }
      localStorage.setItem(localKey, JSON.stringify(localSessions));
    } catch (e) {
      console.warn("[Playback Tracker] localStorage save error:", e);
    }

    // Then fire the API request (best-effort, fire-and-forget, never block navigation)
    const token = localStorage.getItem("stream_access_token");
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (token) headers["Authorization"] = `Bearer ${token}`;

    fetch(`${apiBaseUrl}/track`, {
      method: "POST",
      headers: headers,
      body: JSON.stringify(payload),
    }).then((res) => {
      if (!res.ok) console.warn("[Playback Tracker] API save failed:", res.status, res.statusText);
    }).catch((err) => {
      console.warn("[Playback Tracker] API save error:", err);
    });
  };

  // Save on pause
  const hasEverPlayed = useRef(false);
  useEffect(() => {
    if (isPlaying) hasEverPlayed.current = true;
    if (!isPlaying && hasEverPlayed.current && videoRef.current && videoRef.current.paused && videoRef.current.currentTime > 1) {
      saveProgress();
    }
  }, [isPlaying]);

  // Reset seek state when movie changes
  useEffect(() => {
    seekedOnLoad.current = false;
    seekedRef.current = false;
  }, [movie.id]);

  // 5-Second Playback Tracking Pulse
  useEffect(() => {
    const intervalTime = 5000;

    pulseIntervalRef.current = setInterval(() => {
      if (!videoRef.current || videoRef.current.paused) return;
      saveProgress();
    }, intervalTime);

    if (videoRef.current && movie.videoUrl) {
      videoRef.current.play()
        .then(() => setIsPlaying(true))
        .catch((err) => {
          if (err.name !== 'AbortError') console.warn("[Video Player] Autoplay blocked:", err);
        });
    }

    return () => {
      if (pulseIntervalRef.current) {
        clearInterval(pulseIntervalRef.current);
      }
      if (seekTimeoutRef.current) {
        clearTimeout(seekTimeoutRef.current);
      }
    };
  }, [movie.id, activeProfile.id, apiBaseUrl]);

  // Handle Dynamic Downscale Source switching
  const prevQuality = useRef(selectedQuality);
  const prevAudio = useRef(selectedAudioTrack);
  const transcodeOffsetRef = useRef(0);

  // Auto-Adaptive Buffer Engine
  const waitingCountRef = useRef<number>(0);

  useEffect(() => {
    if (!isAutoMode || !videoRef.current) return;
    
    const interval = setInterval(() => {
      if (!videoRef.current || videoRef.current.paused) return;
      
      try {
        const vid = videoRef.current;
        let bufferedEnd = 0;
        for (let i = 0; i < vid.buffered.length; i++) {
          if (vid.buffered.start(i) <= vid.currentTime && vid.buffered.end(i) >= vid.currentTime) {
            bufferedEnd = vid.buffered.end(i);
            break;
          }
        }
        
        const bufferRemaining = bufferedEnd - vid.currentTime;
        
        // If buffer is critically low and we are playing, increment waiting count
        if (bufferRemaining < 2.0 && vid.readyState < 3) {
           waitingCountRef.current += 1;
        } else if (bufferRemaining > 10.0) {
           waitingCountRef.current = Math.max(0, waitingCountRef.current - 1); // decay if healthy
        }

        if (waitingCountRef.current >= 3) {
          // Trigger downshift
          const qualities = ["Source", "1080p", "720p", "480p", "360p", "240p"];
          let currentTarget = selectedQuality === "Source" ? "Source" : selectedQuality;
          
          // Special case: If Source is requested but metadata quality is 1080p, we might already be at 1080p source. 
          // So just map it cleanly.
          const currentIdx = qualities.indexOf(currentTarget);
          
          if (currentIdx !== -1 && currentIdx < qualities.length - 1) {
             const nextQuality = qualities[currentIdx + 1];
             console.log(`[Auto Engine] Buffer struggling (Remaining: ${bufferRemaining.toFixed(1)}s). Downshifting quality: ${selectedQuality} -> ${nextQuality}`);
             setSelectedQuality(nextQuality);
             waitingCountRef.current = 0; // Reset after shift
          }
        }
      } catch (e) {
        // ignore
      }
    }, 2000); // Check every 2 seconds

    return () => clearInterval(interval);
  }, [isAutoMode, selectedQuality]);

  const handleWaiting = () => {
    if (!isAutoMode) return;
    waitingCountRef.current += 2; // A raw waiting event is a strong indicator of stutter
  };

  useEffect(() => {
    if (videoRef.current) {
      const isPaused = videoRef.current.paused;
      const currentPos = videoRef.current.currentTime;
      const serverRoot = apiBaseUrl.replace(/\/api\/?$/, "");
      const mediaId = movie.activeEpisodeId || movie.id;
      const sourceQualityLabel = movie.quality || "Source";
      
      const token = localStorage.getItem("stream_access_token");
      const tokenQuery = token ? `&token=${encodeURIComponent(token)}` : "";
      
      if (prevQuality.current !== selectedQuality || prevAudio.current !== selectedAudioTrack) {
        const isTranscoded = selectedQuality !== "Source" && selectedQuality !== sourceQualityLabel;
        const newSrc = !isTranscoded
          ? `${serverRoot}${movie.videoUrl}`
          : `${apiBaseUrl}/stream/${mediaId}?quality=${selectedQuality}&audio_track=${selectedAudioTrack}&start=${currentPos}${tokenQuery}`;
          
        if (isTranscoded) transcodeOffsetRef.current = currentPos;
          
        videoRef.current.src = newSrc;
        videoRef.current.load();
        
        if (!isTranscoded) {
           videoRef.current.currentTime = currentPos;
        }
        
        if (!isPaused && movie.videoUrl) {
          videoRef.current.play().catch(e => {
            if (e.name !== 'AbortError') console.warn(e);
          });
        }
      }

      // Always ensure the audio element has the correct src matching the selected track/quality
      if (selectedQuality === "Source" || selectedQuality === sourceQualityLabel) {
        const basePath = movie.videoUrl.substring(0, movie.videoUrl.lastIndexOf("/"));
        const langCode = movie.languages[selectedAudioTrack] || "en";
        const newAudioSrc = `${serverRoot}${basePath}/audio/${langCode}.mp3`;
        
        if (audioRef.current && audioRef.current.src !== newAudioSrc) {
          audioRef.current.src = newAudioSrc;
          audioRef.current.load();
          audioRef.current.currentTime = currentPos;
          if (!isPaused) {
            audioRef.current.play().catch(e => {
              if (e.name !== 'AbortError') console.warn(e);
            });
          }
        }
      } else {
        if (audioRef.current && audioRef.current.getAttribute("src")) {
          audioRef.current.removeAttribute("src");
          audioRef.current.load();
        }
      }
      
      prevQuality.current = selectedQuality;
      prevAudio.current = selectedAudioTrack;
    }
  }, [selectedQuality, selectedAudioTrack, movie.id, movie.activeEpisodeId, movie.videoUrl, apiBaseUrl]);

  // Sync volume, mute, and speed to audio and video elements
  useEffect(() => {
    if (videoRef.current) {
      const sourceQualityLabel = movie.quality || "Source";
      if (selectedQuality === "Source" || selectedQuality === sourceQualityLabel) {
        // Video is silent, so we mute it and let audio handle the sound
        videoRef.current.muted = true;
        videoRef.current.volume = 0;
        
        if (audioRef.current) {
          audioRef.current.muted = isMuted;
          audioRef.current.volume = volume;
          audioRef.current.playbackRate = playbackRate;
        }
      } else {
        // Video has merged audio from transcoder, so video handles sound and audio is silent/muted
        videoRef.current.muted = isMuted;
        videoRef.current.volume = volume;
        videoRef.current.playbackRate = playbackRate;
        
        if (audioRef.current) {
          audioRef.current.muted = true;
          audioRef.current.volume = 0;
        }
      }
    }
  }, [volume, isMuted, playbackRate, selectedQuality, movie.quality]);

  // Play/Pause synchronization
  useEffect(() => {
    const sourceQualityLabel = movie.quality || "Source";
    if ((selectedQuality === "Source" || selectedQuality === sourceQualityLabel) && audioRef.current && videoRef.current) {
      if (isPlaying) {
        audioRef.current.currentTime = videoRef.current.currentTime;
        audioRef.current.play().catch(e => {
          if (e.name !== 'AbortError') console.warn(e);
        });
      } else {
        audioRef.current.pause();
      }
    }
  }, [isPlaying, selectedQuality, movie.quality]);

  const resetIdleTimer = () => {
    setControlsVisible(true);
    if (idleTimeoutRef.current) {
      clearTimeout(idleTimeoutRef.current);
    }
    idleTimeoutRef.current = setTimeout(() => {
      if (isPlaying) {
        setControlsVisible(false);
        setShowSpeedMenu(false);
        setShowQualityMenu(false);
        setShowSubtitlesMenu(false);
        setShowAudioMenu(false);
      }
    }, 3000);
  };

  useEffect(() => {
    resetIdleTimer();
    window.addEventListener("mousemove", resetIdleTimer);
    window.addEventListener("keydown", resetIdleTimer);

    return () => {
      window.removeEventListener("mousemove", resetIdleTimer);
      window.removeEventListener("keydown", resetIdleTimer);
      if (idleTimeoutRef.current) clearTimeout(idleTimeoutRef.current);
    };
  }, [isPlaying]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (document.activeElement instanceof HTMLInputElement || document.activeElement instanceof HTMLTextAreaElement) {
        return;
      }

      switch (e.key) {
        case " ":
        case "Enter":
          e.preventDefault();
          handlePlayPause();
          break;
        case "ArrowLeft":
          e.preventDefault();
          handleSkip("backward");
          break;
        case "ArrowRight":
          e.preventDefault();
          handleSkip("forward");
          break;
        case "ArrowUp":
          e.preventDefault();
          setVolume((prev) => {
            const nextVol = Math.min(1, prev + 0.1);
            if (videoRef.current) {
              videoRef.current.volume = nextVol;
              videoRef.current.muted = nextVol === 0;
            }
            setIsMuted(nextVol === 0);
            return nextVol;
          });
          break;
        case "ArrowDown":
          e.preventDefault();
          setVolume((prev) => {
            const nextVol = Math.max(0, prev - 0.1);
            if (videoRef.current) {
              videoRef.current.volume = nextVol;
              videoRef.current.muted = nextVol === 0;
            }
            setIsMuted(nextVol === 0);
            return nextVol;
          });
          break;
        case "f":
        case "F":
          e.preventDefault();
          handleToggleFullscreen();
          break;
        case "m":
        case "M":
          e.preventDefault();
          handleToggleMute();
          break;
        case "Backspace":
        case "Escape":
          if (!document.fullscreenElement) {
            e.preventDefault();
            saveFinalProgressAndBack();
          }
          break;
        default:
          break;
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [duration, volume, isMuted, isPlaying, onBack]);

  const handlePlay = () => {
    setIsPlaying(true);
    const sourceQualityLabel = movie.quality || "Source";
    if ((selectedQuality === "Source" || selectedQuality === sourceQualityLabel) && audioRef.current) {
      audioRef.current.currentTime = videoRef.current?.currentTime || 0;
      audioRef.current.play().catch(e => {
        if (e.name !== 'AbortError') console.warn(e);
      });
    }
  };

  const handlePause = () => {
    setIsPlaying(false);
    if (audioRef.current) {
      audioRef.current.pause();
    }
  };

  const syncAudioTime = () => {
    const sourceQualityLabel = movie.quality || "Source";
    if ((selectedQuality === "Source" || selectedQuality === sourceQualityLabel) && videoRef.current && audioRef.current) {
      const drift = Math.abs(videoRef.current.currentTime - audioRef.current.currentTime);
      if (drift > 0.15) {
        audioRef.current.currentTime = videoRef.current.currentTime;
      }
    }
  };

  const handlePlayPause = () => {
    if (!videoRef.current) return;
    if (videoRef.current.paused) {
      videoRef.current.play().catch(e => {
        if (e.name !== 'AbortError') console.warn(e);
      });
      setIsPlaying(true);
    } else {
      videoRef.current.pause();
      setIsPlaying(false);
    }
    resetIdleTimer();
  };

  const performSeek = (targetTime: number) => {
    if (seekTimeoutRef.current) {
      clearTimeout(seekTimeoutRef.current);
    }
    targetSeekTimeRef.current = targetTime;
    setCurrentTime(targetTime);

    seekTimeoutRef.current = setTimeout(() => {
      if (videoRef.current && targetSeekTimeRef.current !== null) {
        const t = targetSeekTimeRef.current;
        const sourceQualityLabel = movie.quality || "Source";
        const isTranscoded = selectedQuality !== "Source" && selectedQuality !== sourceQualityLabel;
        
        if (!isTranscoded) {
            videoRef.current.currentTime = t;
            if (audioRef.current) audioRef.current.currentTime = t;
        } else {
            transcodeOffsetRef.current = t;
            const serverRoot = apiBaseUrl.replace(/\/api\/?$/, "");
            const mediaId = movie.activeEpisodeId || movie.id;
            const token = localStorage.getItem("stream_access_token");
            const tokenQuery = token ? `&token=${encodeURIComponent(token)}` : "";
            const newSrc = `${apiBaseUrl}/stream/${mediaId}?quality=${selectedQuality}&audio_track=${selectedAudioTrack}&start=${t}${tokenQuery}`;
            videoRef.current.src = newSrc;
            videoRef.current.load();
            videoRef.current.play().catch(e => {
              if (e.name !== 'AbortError') console.warn(e);
            });
        }
        targetSeekTimeRef.current = null;
      }
    }, 250);
  };

  const handleTimeUpdate = () => {
    if (videoRef.current && targetSeekTimeRef.current === null) {
      const sourceQualityLabel = movie.quality || "Source";
      const isTranscoded = selectedQuality !== "Source" && selectedQuality !== sourceQualityLabel;
      
      let actualTime = videoRef.current.currentTime;
      if (isTranscoded) {
         actualTime += transcodeOffsetRef.current;
      }
      setCurrentTime(actualTime);
    }
  };

  const seekToInitialPosition = (videoElement: HTMLVideoElement, initialTimestamp: number) => {
    if (seekedRef.current) return;
    if (initialTimestamp > 5) {
      const videoDuration = videoElement.duration;
      if (isNaN(videoDuration) || videoDuration === Infinity || initialTimestamp < videoDuration - 10) {
        console.log(`[VideoPlayer] Seeking to initial position: ${initialTimestamp}s`);
        
        const sourceQualityLabel = movie.quality || "Source";
        const isTranscoded = selectedQuality !== "Source" && selectedQuality !== sourceQualityLabel;
        
        if (!isTranscoded) {
            videoElement.currentTime = initialTimestamp;
            if (audioRef.current) audioRef.current.currentTime = initialTimestamp;
        } else {
            transcodeOffsetRef.current = initialTimestamp;
            const serverRoot = apiBaseUrl.replace(/\/api\/?$/, "");
            const mediaId = movie.activeEpisodeId || movie.id;
            const token = localStorage.getItem("stream_access_token");
            const tokenQuery = token ? `&token=${encodeURIComponent(token)}` : "";
            const newSrc = `${apiBaseUrl}/stream/${mediaId}?quality=${selectedQuality}&audio_track=${selectedAudioTrack}&start=${initialTimestamp}${tokenQuery}`;
            videoElement.src = newSrc;
            videoElement.load();
            videoElement.play().catch(e => {
              if (e.name !== 'AbortError') console.warn(e);
            });
        }
        
        setCurrentTime(initialTimestamp);
        seekedRef.current = true;
      }
    }
  };

  const getDatabaseDurationSeconds = (): number => {
    if (!movie || !movie.duration) return 0;
    const durStr = movie.duration.toLowerCase();
    let total = 0;
    if (durStr.includes("h")) {
      const parts = durStr.split("h");
      total += parseInt(parts[0] || "0") * 3600;
      if (parts[1] && parts[1].includes("m")) {
        total += parseInt(parts[1].replace("m", "").trim() || "0") * 60;
      }
    } else if (durStr.includes("m")) {
      total += parseInt(durStr.replace("m", "").trim() || "0") * 60;
    } else {
      total = parseInt(durStr || "0") * 60;
    }
    return isNaN(total) ? 0 : total;
  };

  const handleLoadedMetadata = async () => {
    if (videoRef.current) {
      let rawDuration = videoRef.current.duration;
      const dbDuration = getDatabaseDurationSeconds();
      
      // If browser duration is broken (Infinity/NaN) or wildly miscalculated compared to our DB metadata, trust the DB.
      if (!rawDuration || isNaN(rawDuration) || rawDuration === Infinity || (dbDuration > 0 && Math.abs(rawDuration - dbDuration) > 300)) {
        rawDuration = dbDuration > 0 ? dbDuration : rawDuration;
      }
      
      setDuration(rawDuration);

      if (!seekedOnLoad.current) {
        seekedOnLoad.current = true;
        let initialTimestamp = 0;

        const localKey = `continue_watching_${activeProfile.id}`;
        try {
          const saved = localStorage.getItem(localKey);
          if (saved) {
            const localSessions = JSON.parse(saved);
            const found = localSessions.find((s: any) =>
              s.movieId === movie.id &&
              (!movie.activeEpisodeId || s.episodeId === movie.activeEpisodeId)
            );
            if (found && found.timestamp) {
              initialTimestamp = found.timestamp;
            }
          }
        } catch (e) {}

        try {
          const token = localStorage.getItem("stream_access_token");
          const headers: Record<string, string> = {};
          if (token) headers["Authorization"] = `Bearer ${token}`;
          const response = await fetch(`${apiBaseUrl}/track/${activeProfile.id}`, { headers });
          if (response.ok) {
            const sessions = await response.json();
            if (Array.isArray(sessions)) {
              const found = sessions.find((s: any) =>
                s.movieId === movie.id &&
                (!movie.activeEpisodeId || s.episodeId === movie.activeEpisodeId)
              );
              if (found && found.timestamp) {
                initialTimestamp = found.timestamp;
              }
            }
          }
        } catch (err) {
          console.warn("[Video Player] Failed to fetch initial position.");
        }

        if (initialTimestamp > 5) {
          const video = videoRef.current;
          if (video) {
            seekToInitialPosition(video, initialTimestamp);
            
            const onCanPlay = () => {
              seekToInitialPosition(video, initialTimestamp);
              video.removeEventListener("canplay", onCanPlay);
            };
            video.addEventListener("canplay", onCanPlay);
          }
        }
      }
    }
  };

  const handleSeekChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const seekVal = parseFloat(e.target.value);
    performSeek(seekVal);
    resetIdleTimer();
  };

  const handleSkip = (direction: "backward" | "forward") => {
    if (videoRef.current) {
      const amount = direction === "backward" ? -10 : 10;
      const baseTime = targetSeekTimeRef.current !== null ? targetSeekTimeRef.current : videoRef.current.currentTime;
      const newTime = Math.max(0, Math.min(duration, baseTime + amount));
      performSeek(newTime);
    }
    resetIdleTimer();
  };

  const handleVolumeChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const vol = parseFloat(e.target.value);
    setVolume(vol);
    setIsMuted(vol === 0);
    if (videoRef.current) {
      videoRef.current.volume = vol;
      videoRef.current.muted = vol === 0;
    }
    resetIdleTimer();
  };

  const handleToggleMute = () => {
    const nextMuted = !isMuted;
    setIsMuted(nextMuted);
    if (videoRef.current) {
      videoRef.current.muted = nextMuted;
      videoRef.current.volume = nextMuted ? 0 : volume;
    }
    resetIdleTimer();
  };

  const handleSpeedChange = (rate: number) => {
    setPlaybackRate(rate);
    if (videoRef.current) {
      videoRef.current.playbackRate = rate;
    }
    setShowSpeedMenu(false);
    resetIdleTimer();
  };

  const handleToggleFullscreen = () => {
    if (!containerRef.current) return;

    if (!document.fullscreenElement) {
      containerRef.current.requestFullscreen()
        .then(() => setIsFullscreen(true))
        .catch((err) => console.error("[Video Player] Fullscreen error:", err));
    } else {
      document.exitFullscreen()
        .then(() => setIsFullscreen(false))
        .catch((err) => console.error("[Video Player] Exit fullscreen error:", err));
    }
    resetIdleTimer();
  };

  useEffect(() => {
    const onFullscreenChange = () => {
      setIsFullscreen(!!document.fullscreenElement);
    };
    document.addEventListener("fullscreenchange", onFullscreenChange);
    return () => document.removeEventListener("fullscreenchange", onFullscreenChange);
  }, []);

  // Dynamic settings resolved from metadata or fallback defaults
  const sourceQualityLabel = movie.quality || "Source";
  const displayedQuality = isAutoMode ? "Auto" : (selectedQuality === "Source" ? sourceQualityLabel : selectedQuality);
  const qualityOptions = ["Auto", sourceQualityLabel, "1080p", "720p", "480p", "360p", "240p"];

  const audioTracks = movie.languages || ["en"];
  const displayedAudioTrack = audioTracks[selectedAudioTrack]?.toUpperCase() || `CH-${selectedAudioTrack}`;

  return (
    <div
      ref={containerRef}
      id="video-player-page"
      className={`relative w-screen h-screen bg-black overflow-hidden select-none flex items-center justify-center ${
        controlsVisible ? "cursor-default" : "cursor-none"
      }`}
    >
      {/* HTML5 VIDEO STREAM PLAYER */}
      <video
        ref={videoRef}
        src={selectedQuality === "Source" && movie.videoUrl ? `${apiBaseUrl.replace(/\/api\/?$/, "")}${movie.videoUrl}` : undefined}
        className="w-full h-full object-contain"
        onTimeUpdate={handleTimeUpdate}
        onLoadedMetadata={handleLoadedMetadata}
        onClick={handlePlayPause}
        onPlay={handlePlay}
        onPause={handlePause}
        onWaiting={handleWaiting}
        onSeeking={syncAudioTime}
        onSeeked={syncAudioTime}
        onRateChange={() => {
          if (audioRef.current && videoRef.current) {
            audioRef.current.playbackRate = videoRef.current.playbackRate;
          }
        }}
        id="html5-video-player"
        playsInline
      >
        {(movie.subtitles || []).map((sub, idx) => {
          const basePath = movie.videoUrl.substring(0, movie.videoUrl.lastIndexOf("/"));
          const ext = sub.ext || ".vtt"; // default / dynamically parsed extension
          const serverRoot = apiBaseUrl.replace(/\/api\/?$/, "");
          const subUrl = `${serverRoot}${basePath}/subtitle_${sub.language}${ext}`;
          return (
            <track
              key={idx}
              kind="subtitles"
              src={subUrl}
              srcLang={sub.language}
              label={sub.language.toUpperCase()}
              default={selectedSubtitle === sub.language}
            />
          );
        })}
        Your browser does not support the video tag.
      </video>

      {/* SYNCHRONIZED MULTI-TRACK AUDIO PLAYER */}
      <audio
        ref={audioRef}
        id="html5-audio-player"
        className="hidden"
      />

      {/* OVERLAY ELEMENTS (FADE OUT ON IDLE) */}
      <AnimatePresence>
        {controlsVisible && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.3 }}
            className="absolute inset-0 bg-gradient-to-t from-black/60 via-transparent to-black/40 flex flex-col justify-between p-6 z-10"
          >
            {/* TOP HEADER CONTROLS */}
            <header className="flex items-center justify-between w-full">
              <button
                onClick={saveFinalProgressAndBack}
                className="flex items-center space-x-2 text-white hover:text-red-500 transition duration-200 bg-black/40 hover:bg-black/60 px-4 py-2 rounded-lg backdrop-blur-md cursor-pointer"
                id="back-to-browse-btn"
              >
                <ArrowLeft className="w-5 h-5" />
                <span className="font-semibold text-sm">{t("back_to_browse")}</span>
              </button>

              <div className="text-right">
                <h2 className="text-white font-bold font-sans text-base md:text-xl tracking-tight leading-none">
                  {movie.title}
                </h2>
                <span className="text-zinc-400 text-xs mt-1 block">
                  {t("playing_as")} <span className="text-red-500 font-medium">{activeProfile.name}</span>
                </span>
              </div>
            </header>

            {/* MIDDLE SPINNING LOADER FOR BUFFERING */}
            {duration === 0 && (
              <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
                <div className="text-center bg-black/40 p-4 rounded-lg">
                  <RefreshCw className="w-10 h-10 text-red-600 animate-spin mx-auto mb-2" />
                  <p className="text-xs text-zinc-300">{t("buffering")}</p>
                </div>
              </div>
            )}

            {/* BOTTOM HUD CONTROLS */}
            <div className="w-full space-y-4">
              
              {/* TIMELINE PROGRESS SLIDER */}
              <div className="flex items-center space-x-3 w-full">
                <span className="text-xs font-mono text-zinc-300 min-w-[45px]">
                  {formatTime(currentTime)}
                </span>

                <input
                  type="range"
                  min="0"
                  max={duration || 100}
                  step="0.1"
                  value={currentTime}
                  onChange={handleSeekChange}
                  className="w-full h-1.5 bg-zinc-700 rounded-lg appearance-none cursor-pointer accent-red-600 focus:outline-none focus:ring-0 active:accent-red-700"
                  style={{
                    background: `linear-gradient(to right, #E50914 0%, #E50914 ${
                      duration ? (currentTime / duration) * 100 : 0
                    }%, #3f3f46 ${duration ? (currentTime / duration) * 100 : 0}%, #3f3f46 100%)`,
                  }}
                  id="timeline-scrubber"
                />

                <span className="text-xs font-mono text-zinc-300 min-w-[45px]">
                  {formatTime(duration)}
                </span>
              </div>

              {/* MEDIA HUD CONTROL BUTTONS */}
              <div className="flex items-center justify-between w-full">
                
                {/* Left Side Buttons */}
                <div className="flex items-center space-x-4 md:space-x-6">
                  {/* Play / Pause */}
                  <button
                    onClick={handlePlayPause}
                    className="text-white hover:text-red-500 transition-colors p-1.5 hover:bg-white/10 rounded-full cursor-pointer"
                    id="player-play-pause-btn"
                  >
                    {isPlaying ? <Pause className="w-6 h-6 fill-current" /> : <Play className="w-6 h-6 fill-current" />}
                  </button>

                  {/* Skip Backward 10s */}
                  <button
                    onClick={() => handleSkip("backward")}
                    className="text-white hover:text-red-500 transition-colors p-1.5 hover:bg-white/10 rounded-full flex flex-col items-center cursor-pointer"
                    title="Rewind 10 seconds"
                    id="player-rewind-btn"
                  >
                    <RotateCcw className="w-5 h-5" />
                    <span className="text-[9px] font-mono leading-none mt-0.5">10s</span>
                  </button>

                  {/* Skip Forward 10s */}
                  <button
                    onClick={() => handleSkip("forward")}
                    className="text-white hover:text-red-500 transition-colors p-1.5 hover:bg-white/10 rounded-full flex flex-col items-center cursor-pointer"
                    title="Skip 10 seconds"
                    id="player-forward-btn"
                  >
                    <RotateCw className="w-5 h-5" />
                    <span className="text-[9px] font-mono leading-none mt-0.5">10s</span>
                  </button>

                  {/* Volume Control */}
                  <div className="flex items-center space-x-2 group/volume">
                    <button
                      onClick={handleToggleMute}
                      className="text-white hover:text-red-500 transition-colors p-1.5 hover:bg-white/10 rounded-full cursor-pointer"
                      id="player-mute-btn"
                    >
                      {isMuted ? <VolumeX className="w-5 h-5" /> : <Volume2 className="w-5 h-5" />}
                    </button>
                    <input
                      type="range"
                      min="0"
                      max="1"
                      step="0.05"
                      value={isMuted ? 0 : volume}
                      onChange={handleVolumeChange}
                      className="w-16 h-1 bg-zinc-700 rounded-lg appearance-none cursor-pointer accent-white opacity-0 group-hover/volume:opacity-100 transition-opacity duration-200"
                      id="volume-slider"
                    />
                  </div>
                </div>

                {/* Right Side Buttons */}
                <div className="flex items-center space-x-4 relative">
                  {/* Quality Selector */}
                  <div className="relative">
                    <button
                      onClick={() => {
                        setShowQualityMenu(!showQualityMenu);
                        setShowSpeedMenu(false);
                        setShowSubtitlesMenu(false);
                        setShowAudioMenu(false);
                      }}
                      className="text-white hover:text-red-500 transition-colors p-1.5 hover:bg-white/10 rounded-full flex items-center space-x-1 cursor-pointer"
                      title={t("quality")}
                    >
                      <Video className="w-5 h-5" />
                      <span className="text-xs font-semibold">{displayedQuality}</span>
                    </button>
                    {showQualityMenu && (
                      <div className="absolute right-0 bottom-full mb-2 bg-[#181818] border border-zinc-800 rounded-md shadow-2xl py-1 w-28 z-30">
                        {qualityOptions.map((q) => (
                          <button
                            key={q}
                            onClick={() => {
                              if (q === "Auto") {
                                setIsAutoMode(true);
                                setSelectedQuality("1080p"); // Default start for Auto
                              } else {
                                setIsAutoMode(false);
                                setSelectedQuality(q === sourceQualityLabel ? "Source" : q);
                              }
                              setShowQualityMenu(false);
                            }}
                            className={`w-full text-left px-4 py-2 hover:bg-zinc-800 transition duration-200 text-sm font-medium
                              ${
                                (isAutoMode && q === "Auto") || (!isAutoMode && (selectedQuality === "Source" ? sourceQualityLabel : selectedQuality) === q) 
                                  ? "text-red-500 bg-zinc-800/40" 
                                  : "text-zinc-300"
                              }`}
                          >
                            {q}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Audio Track Selector */}
                  <div className="relative">
                    <button
                      onClick={() => {
                        setShowAudioMenu(!showAudioMenu);
                        setShowSpeedMenu(false);
                        setShowQualityMenu(false);
                        setShowSubtitlesMenu(false);
                      }}
                      className="text-white hover:text-red-500 transition-colors p-1.5 hover:bg-white/10 rounded-full flex items-center space-x-1 cursor-pointer"
                      title={t("audio")}
                    >
                      <Music className="w-5 h-5" />
                      <span className="text-xs font-semibold">{displayedAudioTrack}</span>
                    </button>
                    {showAudioMenu && (
                      <div className="absolute right-0 bottom-full mb-2 bg-[#181818] border border-zinc-800 rounded-md shadow-2xl py-1 w-36 z-30">
                        {audioTracks.map((trackLang, trackIdx) => (
                          <button
                            key={trackIdx}
                            onClick={() => {
                              setSelectedAudioTrack(trackIdx);
                              setShowAudioMenu(false);
                            }}
                            className={`w-full text-left px-3 py-1.5 text-xs font-semibold hover:bg-zinc-800 transition cursor-pointer ${
                              selectedAudioTrack === trackIdx ? "text-red-500 bg-zinc-800/40" : "text-zinc-300"
                            }`}
                          >
                            {trackLang.toUpperCase()} {trackIdx === 0 ? "(Default)" : `(Track ${trackIdx + 1})`}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Subtitles Selector */}
                  <div className="relative">
                    <button
                      onClick={() => {
                        setShowSubtitlesMenu(!showSubtitlesMenu);
                        setShowSpeedMenu(false);
                        setShowQualityMenu(false);
                        setShowAudioMenu(false);
                      }}
                      className="text-white hover:text-red-500 transition-colors p-1.5 hover:bg-white/10 rounded-full flex items-center space-x-1 cursor-pointer"
                      title={t("subtitles")}
                    >
                      <Languages className="w-5 h-5" />
                      <span className="text-xs font-semibold">{selectedSubtitle.toUpperCase()}</span>
                    </button>
                    {showSubtitlesMenu && (
                      <div className="absolute right-0 bottom-full mb-2 bg-[#181818] border border-zinc-800 rounded-md shadow-2xl py-1 w-28 z-30">
                        <button
                          onClick={() => {
                            setSelectedSubtitle("none");
                            setShowSubtitlesMenu(false);
                          }}
                          className={`w-full text-left px-3 py-1.5 text-xs font-semibold hover:bg-zinc-800 transition cursor-pointer ${
                            selectedSubtitle === "none" ? "text-red-500 bg-zinc-800/40" : "text-zinc-300"
                          }`}
                        >
                          {t("none")}
                        </button>
                        {(movie.subtitles || []).map((sub, idx) => (
                          <button
                            key={idx}
                            onClick={() => {
                              setSelectedSubtitle(sub.language);
                              setShowSubtitlesMenu(false);
                            }}
                            className={`w-full text-left px-3 py-1.5 text-xs font-semibold hover:bg-zinc-800 transition cursor-pointer ${
                              selectedSubtitle === sub.language ? "text-red-500 bg-zinc-800/40" : "text-zinc-300"
                            }`}
                          >
                            {sub.language.toUpperCase()}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Playback Speed Setting */}
                  <div className="relative">
                    <button
                      onClick={() => {
                        setShowSpeedMenu(!showSpeedMenu);
                        setShowQualityMenu(false);
                        setShowSubtitlesMenu(false);
                        setShowAudioMenu(false);
                      }}
                      className="text-white hover:text-red-500 transition-colors p-1.5 hover:bg-white/10 rounded-full flex items-center space-x-1 cursor-pointer"
                      title={t("speed")}
                      id="player-speed-btn"
                    >
                      <Gauge className="w-5 h-5" />
                      <span className="text-xs font-semibold font-mono">{playbackRate}x</span>
                    </button>

                    {/* Speed Dropdown Menu */}
                    {showSpeedMenu && (
                      <div className="absolute right-0 bottom-full mb-2 bg-[#181818] border border-zinc-800 rounded-md shadow-2xl py-1 w-28 z-30">
                        {[0.5, 1, 1.25, 1.5, 2].map((rate) => (
                          <button
                            key={rate}
                            onClick={() => handleSpeedChange(rate)}
                            className={`w-full text-left px-3 py-1.5 text-xs font-semibold font-mono hover:bg-zinc-800 transition cursor-pointer ${
                              playbackRate === rate ? "text-red-500 bg-zinc-800/40" : "text-zinc-300"
                            }`}
                          >
                            {rate === 1 ? "1.0x (Normal)" : `${rate}x`}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Fullscreen Trigger */}
                  <button
                    onClick={handleToggleFullscreen}
                    className="text-white hover:text-red-500 transition-colors p-1.5 hover:bg-white/10 rounded-full cursor-pointer"
                    title="Fullscreen"
                    id="player-fullscreen-btn"
                  >
                    <Maximize className="w-5 h-5" />
                  </button>
                </div>

              </div>

            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
