import React from "react";
import type { Movie } from "../../types/api";
import { mediaAvailability } from "../../utils/media";

export function AvailabilityBadge({ movie, variant = "shared" }: { movie: Movie; variant?: "shared" | "ember" }) {
  const availability = mediaAvailability(movie);
  const label = availability === "available"
    ? "Stored on StreamHome server"
    : availability === "processing"
      ? "Media processing"
      : "Metadata cached — media unavailable";
  const icon = availability === "available" ? "database" : availability === "processing" ? "sync" : "cloud";

  return <span className={`media-availability media-availability--${variant}`} data-availability={availability} data-icon={icon} role="img" aria-label={label} title={label}>
    {availability === "available" && <svg className="media-availability__icon" viewBox="0 0 24 24" fill="none" aria-hidden="true"><ellipse cx="12" cy="5" rx="7" ry="3" /><path d="M5 5v6c0 1.66 3.13 3 7 3s7-1.34 7-3V5" /><path d="M5 11v6c0 1.66 3.13 3 7 3s7-1.34 7-3v-6" /></svg>}
    {availability === "cached" && <svg className="media-availability__icon" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M7.5 18.5h9.25a4.25 4.25 0 0 0 .47-8.47A5.75 5.75 0 0 0 6.3 8.7 4.9 4.9 0 0 0 7.5 18.5Z" /></svg>}
    {availability === "processing" && <svg className="media-availability__icon" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M20 7h-5V2" /><path d="M19.1 7A8 8 0 0 0 5.6 5.6L4 7" /><path d="M4 17h5v5" /><path d="M4.9 17a8 8 0 0 0 13.5 1.4L20 17" /></svg>}
  </span>;
}

export function RecommendationReason({ movie }: { movie: Movie }) {
  const detail = movie.recommendationReasonDetails?.[0];
  const fallback = detail?.fallbackText || movie.recommendationReasons?.[0];
  if (!fallback) return null;
  const subject = detail?.subject?.trim();
  const reason = detail?.code === "auteur_director" && subject
    ? `Because you love ${subject}${subject.endsWith("s") ? "'" : "'s"} directing style.`
    : detail?.code === "auteur_writer" && subject
      ? `Because you love ${subject}${subject.endsWith("s") ? "'" : "'s"} writing.`
      : detail?.code === "trope_match" && subject
        ? `A ${subject.toLocaleLowerCase()} matching your taste.`
        : fallback;
  return <span className="recommendation-reason" data-reason-code={detail?.code ?? "legacy"}>{reason}</span>;
}
