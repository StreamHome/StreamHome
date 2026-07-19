import React, { createContext, useContext, useState } from "react";
import type { MediaPreference } from "../../types/api";

const OPTIONS: Array<{ value: Exclude<MediaPreference, null>; label: string; glyph: string }> = [
  { value: "like", label: "Like", glyph: "+" },
  { value: "love", label: "Love", glyph: "++" },
  { value: "dislike", label: "Dislike", glyph: "-" },
];

export interface FeedbackContextValue {
  profileId: string;
  preferences: Record<string, Exclude<MediaPreference, null>>;
  onChange: (movieId: string, preference: MediaPreference) => Promise<void>;
  feedGeneration: string;
  scope: string;
  category: string;
}

const FeedbackContext = createContext<FeedbackContextValue | null>(null);
export const RecommendationFeedbackProvider = FeedbackContext.Provider;
export function useRecommendationFeedback() { return useContext(FeedbackContext); }

export function RecommendationFeedback({ movieId, preference, onChange, compact = false }: { movieId: string; preference: MediaPreference; onChange: (movieId: string, preference: MediaPreference) => Promise<void>; compact?: boolean }) {
  const [saving, setSaving] = useState(false);
  return <div className={`recommendation-feedback${compact ? " recommendation-feedback--compact" : ""}`} role="group" aria-label="Recommendation feedback">
    {OPTIONS.map((option) => {
      const active = preference === option.value;
      return <button key={option.value} type="button" className={`recommendation-feedback__button recommendation-feedback__button--${option.value}`} aria-label={`${active ? "Remove" : "Set"} ${option.label.toLowerCase()} for this title`} aria-pressed={active} disabled={saving} onClick={(event) => { event.stopPropagation(); setSaving(true); void onChange(movieId, active ? null : option.value).catch(() => undefined).finally(() => setSaving(false)); }}><span aria-hidden="true">{option.glyph}</span>{!compact && <strong>{option.label}</strong>}</button>;
    })}
  </div>;
}
