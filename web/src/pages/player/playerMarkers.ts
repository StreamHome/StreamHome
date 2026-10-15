import type { SkipMarkerRange, SkipMarkers } from "../../types/api";

const TERMINAL_CREDITS_ROUNDING_SECONDS = 0.5;

export interface MarkerRegion {
  id: string;
  kind: string;
  label: string;
  start: number;
  end: number;
  terminalCredits?: boolean;
}

function terminalCreditsTail(markers: SkipMarkers, duration: number): Set<SkipMarkerRange> {
  const credits = Object.entries(markers)
    .filter(([kind]) => kind.toLowerCase() === "credits")
    .flatMap(([, ranges]) => ranges)
    // Inspect raw endpoints: timeline clipping cannot prove a terminal segment.
    .filter((range) => range.start < duration && range.end <= duration + TERMINAL_CREDITS_ROUNDING_SECONDS)
    .sort((left, right) => right.end - left.end || left.start - right.start);
  const last = credits[0];
  const tail = new Set<SkipMarkerRange>();
  if (!last || Math.abs(last.end - duration) > TERMINAL_CREDITS_ROUNDING_SECONDS) return tail;
  let start = last.start;
  for (const range of credits) {
    // Only touching/overlapping ranges connect. The EOF rounding allowance must
    // never erase a positive gap which may contain another scene.
    if (range.end < start) break;
    tail.add(range);
    start = Math.min(start, range.start);
  }
  return tail;
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
  const terminalTail = terminalCreditsTail(markers, duration);
  const regions: MarkerRegion[] = [];
  for (const [kind, ranges] of Object.entries(markers)) {
    const duplicates = new Map<string, number>();
    for (const range of ranges) {
      const { start, end: markerEnd } = range;
      const end = Math.min(markerEnd, duration);
      if (start >= end) continue;
      const signature = `${kind}:${start}:${markerEnd}`;
      const ordinal = duplicates.get(signature) ?? 0;
      duplicates.set(signature, ordinal + 1);
      regions.push({ id: `${signature}:${ordinal}`, kind, label: markerLabel(kind), start, end,
        ...(kind.toLowerCase() === "credits" && terminalTail.has(range)
          ? { terminalCredits: true } : {}),
      });
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

export function canSkipMarkerRegion(region: MarkerRegion): boolean {
  return region.terminalCredits !== true;
}
