import React from "react";
import type { Movie, PlaybackSession } from "../../types/api";
import { MediaCard } from "./MediaCard";

interface MediaRowProps {
  title: string;
  items: Movie[];
  playbackSessions?: PlaybackSession[];
  onSelect?: (movie: Movie) => void;
}

export function MediaRow({ title, items, playbackSessions, onSelect }: MediaRowProps) {
  if (!items.length) return null;
  return (
    <section className="px-[var(--spacing-margin-desktop)]">
      <h2 className="mb-4 font-[family-name:var(--font-headline)] text-2xl font-semibold">{title}</h2>
      <div className="flex gap-4 overflow-x-auto pb-5">
        {items.map((movie) => (
          <MediaCard
            key={movie.id}
            movie={movie}
            playbackSession={playbackSessions?.find((session) => session.movieId === movie.id)}
            onSelect={onSelect}
          />
        ))}
      </div>
    </section>
  );
}
