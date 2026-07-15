import React from "react";
import type { Movie, PlaybackSession } from "../../types/api";
import { completionFraction, isPlayableMovie } from "../../utils/media";
import { MediaArtwork } from "./MediaArtwork";
import { ProgressBar } from "../ui/ProgressBar";

interface MediaCardProps {
  movie: Movie;
  playbackSession?: PlaybackSession;
  onSelect?: (movie: Movie) => void;
}

export function MediaCard({ movie, playbackSession, onSelect }: MediaCardProps) {
  const playable = isPlayableMovie(movie);
  return (
    <button
      type="button"
      className="group relative w-[190px] flex-shrink-0 overflow-hidden rounded-[var(--radius)] border border-[var(--glass-border)] bg-[var(--glass-fill)] text-left transition hover:-translate-y-1 hover:border-[var(--glass-border-hover)]"
      onClick={() => onSelect?.(movie)}
    >
      <MediaArtwork src={movie.thumbnailUrl} alt={movie.title} className="aspect-[2/3] w-full object-cover" />
      <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black via-black/80 to-transparent px-4 pb-4 pt-14">
        <div className="line-clamp-2 text-sm font-semibold text-white">{movie.title}</div>
        <div className="mt-1 text-[10px] uppercase tracking-wider text-white/60">
          {playable ? movie.type : "Unavailable on server"}
        </div>
      </div>
      {playbackSession && (
        <ProgressBar className="absolute inset-x-0 bottom-0" progress={completionFraction(playbackSession.completionRate)} />
      )}
    </button>
  );
}
