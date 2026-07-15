import React, { useState, useEffect } from 'react';
import { motion } from 'framer-motion';
import { useNavigate } from 'react-router-dom';
import { Movie } from '../../types/api';
import { GlassPane } from '../../components/ui/GlassPane';
import { Chip } from '../../components/ui/Chip';
import { Button } from '../../components/ui/Button';
import { formatDuration } from '../../utils/format';

interface EmberDetailsProps {
  movie: Movie;
  onClose: () => void;
}

export function EmberDetails({ movie, onClose }: EmberDetailsProps) {
  const navigate = useNavigate();
  const [plotText, setPlotText] = useState('');
  
  useEffect(() => {
    if (!movie.plot) return;
    let i = 0;
    const interval = setInterval(() => {
      setPlotText(movie.plot.substring(0, i));
      i++;
      if (i > movie.plot.length) clearInterval(interval);
    }, 10);
    return () => clearInterval(interval);
  }, [movie.plot]);

  const handlePlay = () => {
    navigate(`/watch/${movie.id}`);
  };

  return (
    <motion.div 
      initial={{ y: '100%', opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      exit={{ y: '100%', opacity: 0 }}
      transition={{ duration: 0.8, ease: [0.16, 1, 0.3, 1] }}
      className="fixed inset-0 z-[100] flex text-[var(--text-primary)]"
      style={{
        background: 'rgba(30, 16, 11, 0.85)',
        backdropFilter: 'blur(40px)',
      }}
      data-theme="ember"
    >
      {/* Close button */}
      <button 
        onClick={onClose}
        className="absolute top-8 right-8 z-50 text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
      >
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <line x1="18" y1="6" x2="6" y2="18" />
          <line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>

      <div className="w-full h-full max-w-7xl mx-auto flex flex-col md:flex-row p-8 md:p-16 gap-12 overflow-y-auto">
        {/* Left Side: Poster */}
        <div className="w-full md:w-1/3 flex-shrink-0">
          <GlassPane className="p-4" spotlight={true}>
            <img 
              src={movie.poster_url || ''} 
              alt={movie.title}
              className="w-full h-auto aspect-[2/3] object-cover rounded-[calc(var(--radius)-4px)]"
            />
          </GlassPane>
        </div>

        {/* Right Side: Info */}
        <div className="w-full md:w-2/3 flex flex-col pt-4">
          <h1 className="font-[family-name:var(--font-headline)] text-5xl md:text-[64px] font-bold tracking-wider leading-tight mb-6 text-transparent bg-clip-text bg-gradient-to-r from-white to-[var(--text-secondary)]">
            {movie.title}
          </h1>

          <div className="flex flex-wrap gap-3 mb-8">
            <Chip label="4K HDR" />
            {movie.year && <Chip label={movie.year.toString()} />}
            {movie.duration && <Chip label={formatDuration(movie.duration)} />}
            <Chip label="IMDB 8.4" />
          </div>

          <div className="flex gap-4 mb-12">
            <Button variant="primary" onClick={handlePlay}>
              <svg className="w-5 h-5 mr-2" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                <path strokeLinecap="round" strokeLinejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              INITIALIZE PLAYBACK
            </Button>
            <Button variant="secondary">ADD TO WATCHLIST</Button>
            <Button variant="secondary">DOWNLOAD TO DEVICE</Button>
          </div>

          <p className="font-[family-name:var(--font-mono)] text-[var(--text-secondary)] text-sm leading-relaxed tracking-wider min-h-[100px]">
            {plotText}
            <span className="inline-block w-2 h-4 bg-[var(--text-accent)] ml-1 animate-pulse" />
          </p>

          {/* Episode View stub (if series) */}
          {movie.title.includes('Series') && (
            <div className="mt-12">
              <h3 className="font-[family-name:var(--font-mono)] text-[var(--text-muted)] tracking-widest uppercase mb-4 text-xs">
                Seasons
              </h3>
              <div className="flex gap-3 mb-6">
                <Chip label="Season 1" active={true} />
                <Chip label="Season 2" />
                <Chip label="Season 3" />
              </div>
              <div className="flex flex-col gap-3 relative">
                {/* Glowing vertical line */}
                <div className="absolute left-[30px] top-0 bottom-0 w-[2px] bg-[var(--accent-container)] blur-[1px] opacity-50 z-0" />
                
                {[1, 2, 3].map(ep => (
                  <GlassPane 
                    key={ep} 
                    className="h-[60px] flex items-center px-4 relative z-10 cursor-pointer transition-all duration-300 hover:bg-[rgba(255,95,31,0.1)] hover:border-[var(--glass-border-hover)]"
                    spotlight={false}
                  >
                    <div className="w-8 h-8 rounded-full bg-[var(--accent-container)] flex items-center justify-center mr-4 text-black font-bold font-[family-name:var(--font-mono)] text-xs shadow-[0_0_10px_rgba(255,95,31,0.5)]">
                      {ep}
                    </div>
                    <div className="flex-1 font-[family-name:var(--font-headline)]">
                      Episode {ep} Title
                    </div>
                    <div className="font-[family-name:var(--font-mono)] text-[var(--text-muted)] text-xs">
                      45m
                    </div>
                  </GlassPane>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </motion.div>
  );
}
