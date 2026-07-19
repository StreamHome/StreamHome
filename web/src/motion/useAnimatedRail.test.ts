import { describe, expect, it } from "vitest";
import { cinematicRailEase, fittedRailLayout, railTarget } from "./useAnimatedRail";

describe("controlled category rail motion", () => {
  it("uses a smooth monotonic cinematic easing curve", () => {
    expect(cinematicRailEase(0)).toBe(0);
    expect(cinematicRailEase(.25)).toBeLessThan(.25);
    expect(cinematicRailEase(.75)).toBeGreaterThan(cinematicRailEase(.25));
    expect(cinematicRailEase(.5)).toBeCloseTo(.5);
    expect(cinematicRailEase(1)).toBe(1);
  });

  it("scales a partially visible final card into an exact complete group", () => {
    const layout = fittedRailLayout(1800, 216, 164, 18, 20);
    expect(layout.columns).toBe(8);
    expect(layout.cardWidth).toBeCloseTo(209.25);
    expect(layout.cardWidth * layout.columns + 18 * (layout.columns - 1)).toBeCloseTo(1800);
  });

  it("does not crush cards below their minimum fitting width", () => {
    const layout = fittedRailLayout(700, 216, 164, 18, 20);
    expect(layout.columns).toBe(3);
    expect(layout.cardWidth).toBe(216);
  });

  it("moves to complete card-page boundaries without retaining the previous group", () => {
    const itemOffsets = [64, 268, 472, 676, 880, 1084, 1288, 1492, 1696, 1900, 2104, 2308];
    expect(railTarget({ scrollLeft: 0, clientWidth: 1000, scrollWidth: 2600, itemOffsets, leadingInset: 64, trailingInset: 64 }, 1)).toBe(1020);
    expect(railTarget({ scrollLeft: 1020, clientWidth: 1000, scrollWidth: 2600, itemOffsets, leadingInset: 64, trailingInset: 64 }, -1)).toBe(0);
  });

  it("clamps an incomplete final page and supports a measured-width fallback", () => {
    expect(railTarget({ scrollLeft: 1020, clientWidth: 1000, scrollWidth: 2600, itemOffsets: [64, 1084], leadingInset: 64, trailingInset: 64 }, 1)).toBe(1600);
    expect(railTarget({ scrollLeft: 0, clientWidth: 1000, scrollWidth: 2600 }, 1)).toBe(1000);
    expect(railTarget({ scrollLeft: 120, clientWidth: 1000, scrollWidth: 2600 }, -1)).toBe(0);
  });

  it("aligns an incomplete final group without retaining the preceding page", () => {
    const itemOffsets = [12, 240, 468, 696, 924, 1152, 1380, 1608, 1836, 2064, 2292, 2520];
    expect(railTarget({ scrollLeft: 0, clientWidth: 1600, scrollWidth: 3200, itemOffsets, leadingInset: 12, trailingInset: 12, itemsPerPage: 7 }, 1)).toBe(1596);
    expect(railTarget({ scrollLeft: 1596, clientWidth: 1600, scrollWidth: 3200, itemOffsets, leadingInset: 12, trailingInset: 12, itemsPerPage: 7 }, 1)).toBe(1596);
    expect(railTarget({ scrollLeft: 1596, clientWidth: 1600, scrollWidth: 3200, itemOffsets, leadingInset: 12, trailingInset: 12, itemsPerPage: 7 }, -1)).toBe(0);
  });
});
