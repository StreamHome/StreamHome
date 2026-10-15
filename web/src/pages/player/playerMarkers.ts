import type { SkipMarkers } from "../../types/api";

export interface MarkerRegion {
  id: string;
  kind: string;
  label: string;
  start: number;
  end: number;
}

function markerLabel(kind: string): string {
  const known: Record<string, string> = {
    intro: "Intro",
    credits: "Credits",
    outro: "Outro",
    recap: "Recap",
    postcredits: "Post-credits",
    post_credits: "Post-credits",
    "post-credits": "Post-credits",
  };
  return Object.prototype.hasOwnProperty.call(known, kind.toLowerCase()) ? known[kind.toLowerCase()]
    : kind.split(/[_-]+/).filter(Boolean).map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join(" ") || kind;
}

/** Derive only when normalized metadata or the effective duration changes. */
export function deriveMarkerRegions(markers: SkipMarkers, duration: number): MarkerRegion[] {
  if (!Number.isFinite(duration) || duration <= 0) return [];
  const regions: MarkerRegion[] = [];
  for (const [kind, ranges] of Object.entries(markers)) {
    const duplicates = new Map<string, number>();
    for (const { start, end: markerEnd } of ranges) {
      const end = Math.min(markerEnd, duration);
      if (start >= end) continue;
      const signature = `${kind}:${start}:${markerEnd}`;
      const ordinal = duplicates.get(signature) ?? 0;
      duplicates.set(signature, ordinal + 1);
      regions.push({ id: `${signature}:${ordinal}`, kind, label: markerLabel(kind), start, end });
    }
  }
  // Chronological precedence is independent of the API object's insertion order.
  return regions.sort((left, right) => left.start - right.start || left.end - right.end
    || (left.kind < right.kind ? -1 : left.kind > right.kind ? 1 : 0));
}

export function activeMarkerRegion(regions: readonly MarkerRegion[], time: number): MarkerRegion | null {
  if (!Number.isFinite(time) || time < 0) return null;
  return regions.find((region) => time >= region.start && time < region.end) ?? null;
}
