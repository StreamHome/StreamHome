import React, { useEffect, useState } from 'react';
import { useThemeStore } from '../../stores/themeStore';
import { EmberDetails } from './EmberDetails';
import { getMovie } from '../../api/movies';
import { Movie } from '../../types/api';

interface DetailsRouterProps {
  movieId: string;
  onClose: () => void;
}

export function DetailsRouter({ movieId, onClose }: DetailsRouterProps) {
  const { activeTheme } = useThemeStore();
  const [movie, setMovie] = useState<Movie | null>(null);

  useEffect(() => {
    let mounted = true;
    // getMovie doesn't exist yet, we'll assume it will.
    // For now we mock it or fetch from movies list
    getMovie(movieId).then(data => {
      if (mounted) setMovie(data);
    }).catch(err => {
      console.error(err);
      if (mounted) onClose(); // close on error
    });
    return () => { mounted = false; };
  }, [movieId, onClose]);

  if (!movie) return null;

  switch (activeTheme) {
    case 'aurora':
      return <div className="fixed inset-0 z-100 bg-black/80 text-white flex items-center justify-center" onClick={onClose}>Aurora Details Loading...</div>;
    case 'cinema':
      return <div className="fixed inset-0 z-100 bg-black/90 text-white flex items-center justify-center" onClick={onClose}>Cinema Details Loading...</div>;
    case 'gemini':
      return <div className="fixed inset-0 z-100 bg-white/10 backdrop-blur-md text-white flex items-center justify-center" onClick={onClose}>Gemini Details Loading...</div>;
    case 'ember':
    default:
      return <EmberDetails movie={movie} onClose={onClose} />;
  }
}
