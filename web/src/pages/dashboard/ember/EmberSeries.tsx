import React, { useEffect, useState } from 'react';
import { getMovies } from '../../../api/movies';
import { Movie } from '../../../types/api';
import { GlassPane } from '../../../components/ui/GlassPane';
import { MediaCard } from '../../../components/media/MediaCard';
import { useNavigate } from 'react-router-dom';

export function EmberSeries() {
  const navigate = useNavigate();
  const [series, setSeries] = useState<Movie[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    // Note: The backend currently sends series along with movies or we filter client-side 
    // depending on the API structure. Assuming all data comes from getMovies for now 
    // and filtering mock-style, or assuming backend `type` field exists.
    getMovies().then(data => {
      if (mounted) {
        setSeries(data.filter(m => m.tmdb_id && m.title.includes('Series'))); // Mock filter if type isn't robust yet
        setIsLoading(false);
      }
    });
    return () => { mounted = false; };
  }, []);

  const handleSelectSeries = (movie: Movie) => navigate(`/watch/${movie.id}`);

  if (isLoading) {
    return (
      <div className="w-full h-[80vh] flex items-center justify-center">
        <div className="font-[family-name:var(--font-mono)] text-[var(--text-muted)] tracking-widest uppercase">
          SCANNING SERIES...
        </div>
      </div>
    );
  }

  return (
    <div className="w-full min-h-screen pb-20 px-[var(--spacing-margin-desktop)] pt-8">
      {series.length > 0 && (
        <GlassPane className="w-full h-[40vh] mb-12 relative overflow-hidden" spotlight={false}>
          <img 
            src={series[0].backdrop_url || series[0].poster_url || ''} 
            alt={series[0].title}
            className="absolute inset-0 w-full h-full object-cover opacity-60"
          />
          <div className="absolute inset-0 bg-gradient-to-r from-[var(--bg-body)] to-transparent" />
          <div className="absolute left-[var(--spacing-glass-padding)] bottom-[var(--spacing-glass-padding)] z-10 max-w-xl">
            <h1 className="font-[family-name:var(--font-headline)] text-[var(--text-primary)] text-4xl font-bold tracking-wide">
              {series[0].title}
            </h1>
            <div className="font-[family-name:var(--font-mono)] text-[var(--accent-container)] text-xs mt-2 uppercase tracking-widest">
              NEW EPISODES
            </div>
          </div>
        </GlassPane>
      )}

      <div className="flex flex-wrap gap-6">
        {series.map(s => (
          <div key={s.id} className="relative group">
            <MediaCard 
              movie={s} 
              onSelect={handleSelectSeries} 
            />
            {/* Smart Hover Status Panel */}
            <div className="absolute bottom-[20px] left-[50%] -translate-x-1/2 w-[90%] opacity-0 group-hover:opacity-100 transition-opacity duration-300 pointer-events-none z-30">
              <GlassPane className="py-2 px-3 text-center" spotlight={false}>
                <div className="font-[family-name:var(--font-mono)] text-[10px] text-[var(--text-primary)] tracking-widest">
                  S01E01 • 45:12 REMAINING
                </div>
              </GlassPane>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
