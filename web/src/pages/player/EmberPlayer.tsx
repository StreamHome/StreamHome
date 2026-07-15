import React, { useState, useRef, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { GlassPane } from '../../components/ui/GlassPane';
import { formatDuration } from '../../utils/format';
import { cn } from '../../utils/cn';

interface EmberPlayerProps {
  videoRef: React.RefObject<HTMLVideoElement | null>;
  isPlaying: boolean;
  currentTime: number;
  duration: number;
  volume: number;
  isMuted: boolean;
  isFullscreen: boolean;
  showControls: boolean;
  onPlayPause: () => void;
  onSeek: (time: number) => void;
  onVolumeChange: (vol: number) => void;
  onToggleMute: () => void;
  onToggleFullscreen: () => void;
  onExit: () => void;
  title: string;
  subtitle: string;
}

export function EmberPlayer(props: EmberPlayerProps) {
  const { 
    isPlaying, currentTime, duration, volume, isMuted, showControls, 
    onPlayPause, onSeek, onVolumeChange, onToggleMute, onToggleFullscreen, onExit,
    title, subtitle 
  } = props;

  const scrubberRef = useRef<HTMLDivElement>(null);
  const [hoverX, setHoverX] = useState<number | null>(null);

  const handleScrubberClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!scrubberRef.current) return;
    const rect = scrubberRef.current.getBoundingClientRect();
    const pos = (e.clientX - rect.left) / rect.width;
    onSeek(pos * duration);
  };

  const handleScrubberMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!scrubberRef.current) return;
    const rect = scrubberRef.current.getBoundingClientRect();
    const pos = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    setHoverX(pos);
  };

  const progressPercent = duration > 0 ? (currentTime / duration) * 100 : 0;
  const hoverPercent = hoverX !== null ? hoverX * 100 : 0;

  return (
    <div className="absolute inset-0 pointer-events-none" data-theme="ember">
      
      {/* Top Bar (Visible when paused or controls shown) */}
      <AnimatePresence>
        {(!isPlaying || showControls) && (
          <motion.div 
            initial={{ opacity: 0, y: -50 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -50 }}
            className="absolute top-0 w-full p-8 flex justify-between items-start pointer-events-auto z-50 bg-gradient-to-b from-[rgba(30,16,11,0.8)] to-transparent"
          >
            <button onClick={onExit} className="text-white hover:text-[var(--accent-container)] transition-colors">
              <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M19 12H5M12 19l-7-7 7-7" />
              </svg>
            </button>
            
            <div className="text-right">
              <h1 className="font-[family-name:var(--font-headline)] text-2xl font-bold tracking-wider text-white drop-shadow-md">
                {title}
              </h1>
              <h2 className="font-[family-name:var(--font-mono)] text-sm text-[var(--text-secondary)] tracking-widest uppercase mt-1">
                {subtitle}
              </h2>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Pause Overlay Vignette */}
      <AnimatePresence>
        {!isPlaying && (
          <motion.div 
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="absolute inset-0 bg-black/40 backdrop-blur-[2px] z-10"
          />
        )}
      </AnimatePresence>

      {/* Center Play/Pause Indicator */}
      <AnimatePresence>
        {!isPlaying && (
          <motion.div
            initial={{ scale: 0.8, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.8, opacity: 0 }}
            className="absolute inset-0 flex items-center justify-center z-20"
          >
            <button onClick={onPlayPause} className="w-24 h-24 rounded-full border-2 border-[var(--glass-border-hover)] bg-[var(--glass-fill)] backdrop-blur-md flex items-center justify-center text-white hover:bg-[rgba(255,95,31,0.2)] hover:border-[var(--accent-container)] hover:text-[var(--accent-container)] hover:shadow-[0_0_30px_rgba(255,95,31,0.5)] transition-all duration-300 pointer-events-auto group">
              <svg className="w-10 h-10 ml-2 group-hover:scale-110 transition-transform" viewBox="0 0 24 24" fill="currentColor">
                <path d="M8 5v14l11-7z" />
              </svg>
            </button>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Controls Container */}
      <AnimatePresence>
        {(!isPlaying || showControls) && (
          <motion.div 
            initial={{ opacity: 0, y: 50 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 50 }}
            className="absolute bottom-0 w-full px-16 pb-8 pt-24 bg-gradient-to-t from-[rgba(30,16,11,0.95)] via-[rgba(30,16,11,0.6)] to-transparent pointer-events-auto z-50 flex flex-col gap-6"
          >
            
            {/* Scrubber Area */}
            <div 
              className="relative w-full h-8 group flex items-center cursor-pointer"
              onMouseMove={handleScrubberMouseMove}
              onMouseLeave={() => setHoverX(null)}
              onClick={handleScrubberClick}
              ref={scrubberRef}
            >
              <div className="absolute w-full h-[2px] bg-[rgba(255,255,255,0.2)] rounded-full overflow-hidden">
                <div 
                  className="h-full bg-[var(--accent-container)] transition-all duration-100 ease-linear"
                  style={{ width: `${progressPercent}%`, filter: 'drop-shadow(0 0 5px rgba(255,95,31,0.8))' }}
                />
              </div>

              {/* Hover Frame Preview */}
              {hoverX !== null && (
                <div 
                  className="absolute bottom-10 -translate-x-1/2 pointer-events-none"
                  style={{ left: `${hoverPercent}%` }}
                >
                  <GlassPane className="w-[160px] aspect-[16/9] border border-[var(--glass-border-hover)] flex items-center justify-center font-[family-name:var(--font-mono)] text-xs text-white" spotlight={false}>
                    {formatDuration(hoverX * duration)}
                  </GlassPane>
                  <div className="absolute -bottom-2 left-1/2 -translate-x-1/2 w-0 h-0 border-l-4 border-r-4 border-t-8 border-transparent border-t-[var(--glass-border-hover)]" />
                </div>
              )}
            </div>

            {/* Bottom Row */}
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-6">
                <button onClick={onPlayPause} className="text-white hover:text-[var(--accent-container)] transition-colors">
                  {isPlaying ? (
                    <svg width="28" height="28" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>
                  ) : (
                    <svg width="28" height="28" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
                  )}
                </button>

                <div className="flex items-center gap-3 group">
                  <button onClick={onToggleMute} className="text-white hover:text-[var(--accent-container)] transition-colors">
                    {isMuted || volume === 0 ? (
                      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M11 5L6 9H2v6h4l5 4V5z"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>
                    ) : (
                      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M11 5L6 9H2v6h4l5 4V5z"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>
                    )}
                  </button>
                  <input 
                    type="range" 
                    min="0" max="1" step="0.01" 
                    value={isMuted ? 0 : volume}
                    onChange={(e) => onVolumeChange(parseFloat(e.target.value))}
                    className="w-20 opacity-0 group-hover:opacity-100 transition-opacity accent-[var(--accent-container)]"
                  />
                </div>

                <div className="font-[family-name:var(--font-mono)] text-sm tracking-widest text-[var(--text-secondary)]">
                  {formatDuration(currentTime)} / {formatDuration(duration)}
                </div>
              </div>

              <div className="flex items-center gap-6">
                <button className="font-[family-name:var(--font-mono)] text-sm text-[var(--text-secondary)] hover:text-white transition-colors tracking-widest">
                  1080P
                </button>
                <button className="font-[family-name:var(--font-mono)] text-sm text-[var(--text-secondary)] hover:text-white transition-colors tracking-widest">
                  SUB
                </button>
                <button onClick={onToggleFullscreen} className="text-white hover:text-[var(--accent-container)] transition-colors">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/>
                  </svg>
                </button>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

    </div>
  );
}
