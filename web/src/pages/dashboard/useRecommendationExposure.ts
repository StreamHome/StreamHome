import { useEffect, useRef } from "react";
import { sendRecommendationExposures, type RecommendationExposurePayload } from "../../api/recommendations";

const queues = new Map<string, RecommendationExposurePayload[]>();
const timers = new Map<string, number>();
const inFlight = new Set<string>();
const hydrated = new Set<string>();
const MAX_PENDING_EXPOSURES = 200;

function exposureKey(item: RecommendationExposurePayload) {
  return `${item.movie_id}:${item.feed_generation}:${item.surface}:${item.scope}:${item.category}:${item.position}`;
}

function storageKey(profileId: string) {
  return `streamhome_pending_exposures:${profileId}`;
}

function readPending(profileId: string): RecommendationExposurePayload[] {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(storageKey(profileId)) ?? "[]");
    return Array.isArray(parsed) ? parsed.slice(-MAX_PENDING_EXPOSURES) : [];
  } catch {
    return [];
  }
}

function persist(profileId: string, pending: RecommendationExposurePayload[]) {
  try {
    if (pending.length) window.localStorage.setItem(storageKey(profileId), JSON.stringify(pending.slice(-MAX_PENDING_EXPOSURES)));
    else window.localStorage.removeItem(storageKey(profileId));
  } catch {
    // Exposure delivery remains best-effort if browser storage is unavailable.
  }
}

function hydrate(profileId: string) {
  if (hydrated.has(profileId)) return;
  hydrated.add(profileId);
  const current = queues.get(profileId) ?? [];
  const merged = [...readPending(profileId), ...current];
  const unique = Array.from(new Map(merged.map((item) => [exposureKey(item), item])).values()).slice(-MAX_PENDING_EXPOSURES);
  queues.set(profileId, unique);
  persist(profileId, unique);
}

function schedule(profileId: string, delay = 5000) {
  if (!timers.has(profileId)) timers.set(profileId, window.setTimeout(() => void flush(profileId), delay));
}

async function flush(profileId: string, keepalive = false) {
  hydrate(profileId);
  const timer = timers.get(profileId);
  if (timer !== undefined) window.clearTimeout(timer);
  timers.delete(profileId);
  if (inFlight.has(profileId)) return;
  const batch = (queues.get(profileId) ?? []).slice(0, 100);
  if (!batch.length) return;
  inFlight.add(profileId);
  try {
    await sendRecommendationExposures(profileId, batch, keepalive ? { keepalive: true } : undefined);
    const delivered = new Set(batch.map(exposureKey));
    const remaining = (queues.get(profileId) ?? []).filter((item) => !delivered.has(exposureKey(item)));
    queues.set(profileId, remaining);
    persist(profileId, remaining);
    if (remaining.length) schedule(profileId, 1000);
  } catch {
    persist(profileId, queues.get(profileId) ?? batch);
    if (!document.hidden) schedule(profileId, 15000);
  } finally {
    inFlight.delete(profileId);
  }
}

function enqueue(profileId: string, payload: RecommendationExposurePayload) {
  hydrate(profileId);
  const queue = queues.get(profileId) ?? [];
  if (queue.some((item) => exposureKey(item) === exposureKey(payload))) return;
  const next = [...queue, payload].slice(-MAX_PENDING_EXPOSURES);
  queues.set(profileId, next);
  persist(profileId, next);
  schedule(profileId);
}

export function useRecommendationExposure(payload: RecommendationExposurePayload & { profileId: string; enabled?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    hydrate(payload.profileId);
    if ((queues.get(payload.profileId) ?? []).length) schedule(payload.profileId, 1000);
    if (!payload.enabled || !ref.current || typeof IntersectionObserver === "undefined") return;
    let timer: number | undefined;
    let recorded = false;
    const exposure: RecommendationExposurePayload = { movie_id: payload.movie_id, feed_generation: payload.feed_generation, surface: payload.surface, scope: payload.scope, category: payload.category, position: payload.position };
    const onPageHide = () => void flush(payload.profileId, true);
    const observer = new IntersectionObserver(([entry]) => {
      if (recorded) return;
      if (entry.isIntersecting && entry.intersectionRatio >= 0.5) timer = window.setTimeout(() => { recorded = true; enqueue(payload.profileId, exposure); observer.disconnect(); }, 1000);
      else if (timer !== undefined) { window.clearTimeout(timer); timer = undefined; }
    }, { threshold: [0.5] });
    observer.observe(ref.current);
    window.addEventListener("pagehide", onPageHide);
    return () => {
      observer.disconnect();
      window.removeEventListener("pagehide", onPageHide);
      if (timer !== undefined) window.clearTimeout(timer);
      void flush(payload.profileId, true);
    };
  }, [payload.category, payload.enabled, payload.feed_generation, payload.movie_id, payload.position, payload.profileId, payload.scope, payload.surface]);
  return ref;
}
