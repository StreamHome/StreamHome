import { useEffect, useRef } from "react";
import { sendRecommendationExposures, type RecommendationExposurePayload } from "../../api/recommendations";

const queues = new Map<string, RecommendationExposurePayload[]>();
const timers = new Map<string, number>();

function flush(profileId: string) {
  const timer = timers.get(profileId);
  if (timer !== undefined) window.clearTimeout(timer);
  const batch = queues.get(profileId) ?? [];
  queues.set(profileId, []);
  timers.delete(profileId);
  if (batch.length) void sendRecommendationExposures(profileId, batch).catch(() => undefined);
}

function enqueue(profileId: string, payload: RecommendationExposurePayload) {
  const queue = queues.get(profileId) ?? [];
  if (queue.some((item) => item.movie_id === payload.movie_id && item.feed_generation === payload.feed_generation && item.surface === payload.surface)) return;
  queue.push(payload); queues.set(profileId, queue);
  if (!timers.has(profileId)) timers.set(profileId, window.setTimeout(() => flush(profileId), 5000));
}

export function useRecommendationExposure(payload: RecommendationExposurePayload & { profileId: string; enabled?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!payload.enabled || !ref.current || typeof IntersectionObserver === "undefined") return;
    let timer: number | undefined;
    let recorded = false;
    const exposure: RecommendationExposurePayload = { movie_id: payload.movie_id, feed_generation: payload.feed_generation, surface: payload.surface, scope: payload.scope, category: payload.category, position: payload.position };
    const onPageHide = () => flush(payload.profileId);
    const observer = new IntersectionObserver(([entry]) => {
      if (recorded) return;
      if (entry.isIntersecting && entry.intersectionRatio >= 0.5) timer = window.setTimeout(() => { recorded = true; enqueue(payload.profileId, exposure); observer.disconnect(); }, 1000);
      else if (timer !== undefined) { window.clearTimeout(timer); timer = undefined; }
    }, { threshold: [0.5] });
    observer.observe(ref.current);
    window.addEventListener("pagehide", onPageHide);
    return () => { observer.disconnect(); window.removeEventListener("pagehide", onPageHide); if (timer !== undefined) window.clearTimeout(timer); };
  }, [payload.category, payload.enabled, payload.feed_generation, payload.movie_id, payload.position, payload.profileId, payload.scope, payload.surface]);
  return ref;
}
