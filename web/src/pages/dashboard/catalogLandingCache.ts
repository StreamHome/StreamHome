import { useAuthStore } from "../../stores/authStore";
import type { RecommendationFeed } from "../../types/api";

const feeds = new Map<string, { feed: RecommendationFeed; savedAt: number }>();
const MAX_FEEDS = 24;
const MAX_AGE_MS = 5 * 60_000;
let generation = 0;

// Read-only display reuse is confined to the current authenticated browser session.
useAuthStore.subscribe((state, previous) => {
  if (!state.isAuthenticated || state.email !== previous.email) { feeds.clear(); generation += 1; }
});
export const landingCacheGeneration = () => generation;
export function readLandingFeed(key: string): RecommendationFeed | null {
  if (!useAuthStore.getState().isAuthenticated) return null;
  const cached = feeds.get(key);
  if (!cached || Date.now() - cached.savedAt > MAX_AGE_MS) { feeds.delete(key); return null; }
  return cached.feed;
}
export function saveLandingFeed(key: string, feed: RecommendationFeed, owner: number) {
  if (owner !== generation || !useAuthStore.getState().isAuthenticated) return;
  feeds.delete(key);
  feeds.set(key, { feed, savedAt: Date.now() });
  if (feeds.size > MAX_FEEDS) feeds.delete(feeds.keys().next().value!);
}
