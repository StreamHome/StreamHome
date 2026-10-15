import type { Episode, Movie, PlaybackSession } from "../../types/api";
import { formatDuration } from "../../utils/format";

export interface DetailsResumeTarget {
  session: PlaybackSession;
  mediaId: string;
  label: string;
  context: string;
}

export function latestResumeSession(movie: Pick<Movie, "id" | "type">, sessions: PlaybackSession[]): PlaybackSession | null {
  return sessions
    .filter((session) => session.movieId === movie.id && !session.isFinished && (session.completionRate < 0.95 || (movie.type === "movie" && !session.episodeId)) && session.timestamp > 0)
    .sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt))[0] ?? null;
}

export function detailsResumeTarget(movie: Movie, episodes: Episode[], sessions: PlaybackSession[]): DetailsResumeTarget | null {
  const session = latestResumeSession(movie, sessions);
  if (!session) return null;

  const timestamp = formatDuration(session.timestamp);
  if (movie.type !== "series" || !session.episodeId) {
    return { session, mediaId: movie.id, label: `Resume from ${timestamp}`, context: `Saved at ${timestamp}` };
  }

  const episode = episodes.find((item) => item.id === session.episodeId);
  const episodeLabel = episode ? `S${episode.seasonNumber} E${episode.episodeNumber}` : "episode";
  return {
    session,
    mediaId: session.episodeId,
    label: `Resume ${episodeLabel} from ${timestamp}`,
    context: episode ? episode.title : `Saved at ${timestamp}`,
  };
}
