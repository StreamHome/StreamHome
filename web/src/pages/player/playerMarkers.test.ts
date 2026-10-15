import { describe, expect, it } from "vitest";
import { activeMarkerRegion, deriveMarkerRegions } from "./playerMarkers";

describe("player marker regions", () => {
  it("places actual Snatch intro and credits on its measured timeline", () => {
    const regions = deriveMarkerRegions({ intro: [{ start: 0, end: 32 }], credits: [{ start: 5964, end: 6166 }] }, 6166.160049);
    expect(regions.map(({ kind, label, start, end }) => ({ kind, label, start, end }))).toEqual([
      { kind: "intro", label: "Intro", start: 0, end: 32 },
      { kind: "credits", label: "Credits", start: 5964, end: 6166 },
    ]);
    expect(activeMarkerRegion(regions, 0)?.kind).toBe("intro");
    expect(activeMarkerRegion(regions, 32)).toBeNull();
    expect(activeMarkerRegion(regions, 5964)?.kind).toBe("credits");
    expect(activeMarkerRegion(regions, 6166)).toBeNull();
  });

  it("clips to duration without mutating source metadata or filling gaps/postcredits", () => {
    const markers = { credits: [{ start: 80, end: 90 }, { start: 95, end: 110 }, { start: 120, end: 130 }] };
    const regions = deriveMarkerRegions(markers, 100);
    expect(regions.map(({ start, end }) => ({ start, end }))).toEqual([{ start: 80, end: 90 }, { start: 95, end: 100 }]);
    expect(markers.credits[1].end).toBe(110);
    expect(activeMarkerRegion(regions, 92)).toBeNull();
    expect(activeMarkerRegion(regions, 100)).toBeNull();
    const extended = deriveMarkerRegions(markers, 140);
    expect(activeMarkerRegion(extended, 135)).toBeNull();
    expect(extended[1].id).toBe(regions[1].id);
  });

  it("withholds regions before a finite positive effective duration is known", () => {
    for (const duration of [0, -1, NaN, Infinity, -Infinity]) {
      expect(deriveMarkerRegions({ intro: [{ start: 0, end: 32 }] }, duration)).toEqual([]);
    }
  });

  it("uses chronological then end/group precedence for overlapping intervals", () => {
    const first = deriveMarkerRegions({ outro: [{ start: 20, end: 50 }], recap: [{ start: 0, end: 30 }], intro: [{ start: 0, end: 30 }, { start: 0, end: 20 }] }, 100);
    const reordered = deriveMarkerRegions({ intro: [{ start: 0, end: 20 }, { start: 0, end: 30 }], recap: [{ start: 0, end: 30 }], outro: [{ start: 20, end: 50 }] }, 100);
    expect(first).toEqual(reordered);
    expect(activeMarkerRegion(first, 10)).toBe(first[0]);
    expect(activeMarkerRegion(first, 20)?.kind).toBe("intro");
    expect(activeMarkerRegion(first, 30)?.kind).toBe("outro");
  });

  it("keeps separate repeated/custom intervals and stable distinct IDs", () => {
    const regions = deriveMarkerRegions({ cold_open: [{ start: 40, end: 50 }, { start: 0, end: 10 }, { start: 0, end: 10 }], post_credits: [{ start: 90, end: 95 }], constructor: [{ start: 60, end: 65 }] }, 100);
    expect(regions.map((region) => region.label)).toEqual(["Cold Open", "Cold Open", "Cold Open", "Constructor", "Post-credits"]);
    expect(new Set(regions.map((region) => region.id)).size).toBe(5);
    expect(activeMarkerRegion(regions, 20)).toBeNull();
    expect(activeMarkerRegion(regions, 96)).toBeNull();
  });

  it("does not activate a region for invalid media positions", () => {
    const regions = deriveMarkerRegions({ intro: [{ start: 0, end: 32 }] }, 100);
    for (const time of [-1, NaN, Infinity, -Infinity]) expect(activeMarkerRegion(regions, time)).toBeNull();
  });
});
