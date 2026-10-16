import React, { createContext, useContext, useRef, useState } from "react";
import type { PreferenceOperation } from "./useCatalogController";
import type { MediaPreference } from "../../types/api";

const OPTIONS: Array<{ value: Exclude<MediaPreference, null>; label: string; direction: "up" | "down"; count: 1 | 2 }> = [
  { value: "like", label: "Like", direction: "up", count: 1 },
  { value: "love", label: "Love", direction: "up", count: 2 },
  { value: "dislike", label: "Dislike", direction: "down", count: 1 },
];

function ThumbIcon({ direction }: { direction: "up" | "down" }) {
  return <svg className="recommendation-feedback__thumb" data-direction={direction} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M7 10v10H4.8A1.8 1.8 0 0 1 3 18.2v-6.4A1.8 1.8 0 0 1 4.8 10H7Z" /><path d="M7 19.2h9.45a2 2 0 0 0 1.94-1.52l1.45-5.8A1.52 1.52 0 0 0 18.36 10H14l.62-3.1A2.42 2.42 0 0 0 12.25 4h-.45L7 10.15" /></svg>;
}

export interface FeedbackContextValue {
  profileId: string;
  preferences: Record<string, Exclude<MediaPreference, null>>;
  onChange: (movieId: string, preference: MediaPreference) => Promise<void>;
  preferencesKnown?: boolean;
  preferenceKnownIds?: Record<string, true>;
  preferenceOperations?: Record<string, PreferenceOperation>;
  onCheck?: (movieId: string) => Promise<void>;
  feedGeneration: string;
  scope: string;
  category: string;
}

const FeedbackContext = createContext<FeedbackContextValue | null>(null);
export const RecommendationFeedbackProvider = FeedbackContext.Provider;
export function useRecommendationFeedback() { return useContext(FeedbackContext); }

export function feedbackPreference(context: FeedbackContextValue, movieId: string, embedded: MediaPreference = null): MediaPreference {
  const operation = context.preferenceOperations?.[movieId];
  if (operation && ["unknown", "checking"].includes(operation.status)) return null;
  if (context.preferencesKnown || context.preferenceKnownIds?.[movieId] || Object.prototype.hasOwnProperty.call(context.preferences, movieId)) return context.preferences[movieId] ?? null;
  return embedded;
}

export function RecommendationFeedback({ movieId, preference, onChange }: { movieId: string; preference: MediaPreference; onChange: (movieId: string, preference: MediaPreference) => Promise<void> }) {
  const [saving, setSaving] = useState(false);
  const busy = useRef(false);
  const context = useRecommendationFeedback();
  const operation = context?.preferenceOperations?.[movieId];
  const blocked = saving || Boolean(operation && ["saving", "checking", "unknown"].includes(operation.status));
  const unknown = operation?.status === "unknown" || operation?.status === "checking";
  return <><div className="recommendation-feedback" role="group" aria-label="Recommendation feedback">
    {OPTIONS.map((option) => {
      const active = !unknown && preference === option.value;
      return <button key={option.value} type="button" className={`recommendation-feedback__button recommendation-feedback__button--${option.value}`} aria-label={`${active ? "Remove" : "Set"} ${option.label.toLowerCase()} for this title`} aria-pressed={active} disabled={blocked} onClick={(event) => { event.stopPropagation(); if (busy.current || blocked) return; busy.current = true; setSaving(true); void onChange(movieId, active ? null : option.value).catch(() => undefined).finally(() => { busy.current = false; setSaving(false); }); }}><span className="recommendation-feedback__icon" aria-hidden="true">{Array.from({ length: option.count }, (_, index) => <ThumbIcon key={index} direction={option.direction} />)}</span><strong>{option.label}</strong></button>;
    })}
  </div>{operation && <div className="recommendation-feedback-status"><p role={operation.status === "unknown" || operation.status === "error" ? "alert" : "status"}>{operation.message}</p>{operation.status === "unknown" && context?.onCheck && <button type="button" className="catalog-retry" onClick={(event) => { event.stopPropagation(); void context.onCheck?.(movieId); }}>Check preference</button>}</div>}</>;
}
