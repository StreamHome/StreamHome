import React, { useEffect, useState } from 'react';
import { getMovies } from '../../../api/movies';
import { Movie } from '../../../types/api';
import { GlassPane } from '../../../components/ui/GlassPane';
import { Chip } from '../../../components/ui/Chip';
import { MediaCard } from '../../../components/media/MediaCard';
import { useNavigate } from 'react-router-dom';

export function EmberMovies() {
  const navigate = useNavigate();
  const [movies, setMovies] = useState<Movie[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [activeFilter, setActiveFilter] = useState('All');

  useEffect(() => {
    let mounted = true;
    getMovies().then(data => {
      if (mounted) {
        setMovies(data);
        setIsLoading(false);
      }
    });
    return () => { mounted = false; };
  }, []);

  const handleSelectMovie = (movie: Movie) => navigate(`/watch/${movie.id}`);

  if (isLoading) {
    return (
      <div className="w-full h-[80vh] flex items-center justify-center">
        <div className="font-[family-name:var(--font-mono)] text-[var(--text-muted)] tracking-widest uppercase">
          SCANNING CATALOG...
        </div>
      </div>
    );
  }

  const filters = ['All', 'Action', 'Sci-Fi', 'Thriller', 'Horror', 'Drama'];

  return (
    <div className="w-full min-h-screen pb-20 px-[var(--spacing-margin-desktop)] pt-8">
      {/* Compact Hero Billboard */}
      {movies.length > 0 && (
        <GlassPane className="w-full h-[40vh] mb-12 relative overflow-hidden" spotlight={false}>
          <img 
            src={movies[0].backdrop_url || movies[0].poster_url || ''} 
            alt={movies[0].title}
            className="absolute inset-0 w-full h-full object-cover opacity-60"
          />
          <div className="absolute inset-0 bg-gradient-to-r from-[var(--bg-body)] to-transparent" />
          <div className="absolute left-[var(--spacing-glass-padding)] bottom-[var(--spacing-glass-padding)] z-10 max-w-xl">
            <h1 className="font-[family-name:var(--font-headline)] text-[var(--text-primary)] text-4xl font-bold tracking-wide">
              {movies[0].title}
            </h1>
            <p className="font-[family-name:var(--font-body)] text-[var(--text-secondary)] mt-2 line-clamp-2">
              {movies[0].plot}
            </p>
          </div>
        </GlassPane>
      )}

      {/* Filter Bar */}
      <div className="flex gap-4 mb-8 overflow-x-auto no-scrollbar pb-2 sticky top-[80px] z-20 py-2" style={{ background: 'rgba(30,16,11,0.8)', backdropFilter: 'blur(12px)' }}>
        {filters.map(filter => (
          <Chip 
            key={filter} 
            label={filter} 
            active={activeFilter === filter} 
            onClick={() => setActiveFilter(filter)} 
          />
        ))}
      </div>

      {/* Grid */}
      <div className="flex flex-wrap gap-6">
        {movies.map(movie => (
          <MediaCard 
            key={movie.id} 
            movie={movie} 
            onSelect={handleSelectMovie} 
          />
        ))}
      </div>
    </div>
  );
}
