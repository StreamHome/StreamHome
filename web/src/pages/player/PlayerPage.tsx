import React, { useEffect, useRef, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useAuthStore } from '../../stores/authStore';
import { useThemeStore } from '../../stores/themeStore';
import { EmberPlayer } from './EmberPlayer';

// Stubs for other players
const AuroraPlayer = (props: any) => <div className="fixed inset-0 bg-black text-white p-8">Aurora Player Loading...</div>;
const CinemaPlayer = (props: any) => <div className="fixed inset-0 bg-black text-white p-8">Cinema Player Loading...</div>;
const GeminiPlayer = (props: any) => <div className="fixed inset-0 bg-black text-white p-8">Gemini Player Loading...</div>;

export function PlayerPage() {
  const { mediaId } = useParams();
  const navigate = useNavigate();
  const token = useAuthStore(state => state.token);
  const { activeTheme } = useThemeStore();
  
  const videoRef = useRef<HTMLVideoElement>(null);
  
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [volume, setVolume] = useState(1);
  const [isMuted, setIsMuted] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [showControls, setShowControls] = useState(true);
  
  const [selectedQuality, setSelectedQuality] = useState('1080p');
  
  const controlsTimeoutRef = useRef<number | null>(null);

  const videoSrc = `/api/stream/${mediaId}?quality=${selectedQuality}&token=${token}`;

  useEffect(() => {
    const handleMouseMove = () => {
      setShowControls(true);
      if (controlsTimeoutRef.current) clearTimeout(controlsTimeoutRef.current);
      controlsTimeoutRef.current = window.setTimeout(() => {
        if (isPlaying) setShowControls(false);
      }, 3000);
    };

    window.addEventListener('mousemove', handleMouseMove);
    return () => {
      window.removeEventListener('mousemove', handleMouseMove);
      if (controlsTimeoutRef.current) clearTimeout(controlsTimeoutRef.current);
    };
  }, [isPlaying]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const video = videoRef.current;
      if (!video) return;

      switch (e.key.toLowerCase()) {
        case ' ':
          e.preventDefault();
          if (video.paused) video.play();
          else video.pause();
          break;
        case 'f':
          if (!document.fullscreenElement) {
            document.documentElement.requestFullscreen();
          } else {
            document.exitFullscreen();
          }
          break;
        case 'm':
          video.muted = !video.muted;
          break;
        case 'arrowleft':
          video.currentTime = Math.max(0, video.currentTime - 10);
          break;
        case 'arrowright':
          video.currentTime = Math.min(video.duration, video.currentTime + 10);
          break;
        case 'arrowup':
          e.preventDefault();
          video.volume = Math.min(1, video.volume + 0.1);
          break;
        case 'arrowdown':
          e.preventDefault();
          video.volume = Math.max(0, video.volume - 0.1);
          break;
        case 'escape':
          if (!document.fullscreenElement) {
            navigate(-1);
          }
          break;
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [navigate]);

  useEffect(() => {
    const handleFullscreenChange = () => {
      setIsFullscreen(!!document.fullscreenElement);
    };
    document.addEventListener('fullscreenchange', handleFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', handleFullscreenChange);
  }, []);

  const handlePlayPause = () => {
    if (videoRef.current) {
      if (isPlaying) videoRef.current.pause();
      else videoRef.current.play();
    }
  };

  const handleSeek = (time: number) => {
    if (videoRef.current) {
      videoRef.current.currentTime = time;
    }
  };

  const handleVolumeChange = (vol: number) => {
    if (videoRef.current) {
      videoRef.current.volume = vol;
    }
  };

  const handleExit = () => {
    if (document.fullscreenElement) {
      document.exitFullscreen();
    }
    navigate(-1);
  };

  const playerProps = {
    videoRef,
    isPlaying,
    currentTime,
    duration,
    volume,
    isMuted,
    isFullscreen,
    showControls,
    onPlayPause: handlePlayPause,
    onSeek: handleSeek,
    onVolumeChange: handleVolumeChange,
    onToggleMute: () => {
      if (videoRef.current) videoRef.current.muted = !videoRef.current.muted;
    },
    onToggleFullscreen: () => {
      if (!document.fullscreenElement) document.documentElement.requestFullscreen();
      else document.exitFullscreen();
    },
    onExit: handleExit,
    title: "Media Title", // Should fetch actual metadata
    subtitle: "S01E01 - Episode Title"
  };

  return (
    <div className="fixed inset-0 bg-black z-[200] flex items-center justify-center overflow-hidden">
      <video
        ref={videoRef}
        src={videoSrc}
        className="w-full h-full object-contain"
        onPlay={() => setIsPlaying(true)}
        onPause={() => setIsPlaying(false)}
        onTimeUpdate={() => setCurrentTime(videoRef.current?.currentTime || 0)}
        onDurationChange={() => setDuration(videoRef.current?.duration || 0)}
        onVolumeChange={() => {
          setVolume(videoRef.current?.volume || 1);
          setIsMuted(videoRef.current?.muted || false);
        }}
        autoPlay
      />

      {activeTheme === 'aurora' && <AuroraPlayer {...playerProps} />}
      {activeTheme === 'cinema' && <CinemaPlayer {...playerProps} />}
      {activeTheme === 'gemini' && <GeminiPlayer {...playerProps} />}
      {(activeTheme === 'ember' || !activeTheme) && <EmberPlayer {...playerProps} />}
    </div>
  );
}
