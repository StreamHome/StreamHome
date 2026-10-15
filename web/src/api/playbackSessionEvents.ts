const revisions = new Map<string, number>();
const subscribers = new Map<string, Set<() => void>>();

export function getPlaybackSessionRevision(profileId: string): number {
  return revisions.get(profileId) ?? 0;
}

export function subscribePlaybackSessions(profileId: string, callback: () => void): () => void {
  const callbacks = subscribers.get(profileId) ?? new Set<() => void>();
  callbacks.add(callback);
  subscribers.set(profileId, callbacks);
  return () => {
    callbacks.delete(callback);
    if (callbacks.size === 0) subscribers.delete(profileId);
  };
}

export function invalidatePlaybackSessions(profileId: string): void {
  revisions.set(profileId, getPlaybackSessionRevision(profileId) + 1);
  subscribers.get(profileId)?.forEach((callback) => callback());
}
