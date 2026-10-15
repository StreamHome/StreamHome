import type { SkipMarkerRange, SkipMarkers } from "../types/api";

const DECIMAL_TIMESTAMP = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i;
const MARKER_GROUP_NAME = /^[\p{L}\p{N}_-]+$/u;

function timestamp(value: unknown): number | null {
  // Legacy metadata may contain numeric strings. Avoid Number's null/boolean,
  // empty-string and non-decimal coercions at this wire boundary.
  const candidate = typeof value === "number" ? value
    : typeof value === "string" && DECIMAL_TIMESTAMP.test(value.trim()) ? Number(value) : NaN;
  return Number.isFinite(candidate) && candidate >= 0 ? candidate === 0 ? 0 : candidate : null;
}

export function normalizeSkipMarkers(raw: unknown): SkipMarkers {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  const groups: [string, SkipMarkerRange[]][] = [];
  for (const [kind, value] of Object.entries(raw)) {
    if ([...kind].length > 32 || !MARKER_GROUP_NAME.test(kind) || !Array.isArray(value)) continue;
    const ranges: SkipMarkerRange[] = [];
    for (const item of value) {
      if (!item || typeof item !== "object" || Array.isArray(item)) continue;
      const range = item as Record<string, unknown>;
      const start = timestamp(range.start);
      const end = timestamp(range.end);
      if (start !== null && end !== null && end > start) ranges.push({ start, end });
    }
    if (ranges.length) groups.push([kind, ranges]);
  }
  return Object.fromEntries(groups);
}
