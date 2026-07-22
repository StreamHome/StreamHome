import { describe, expect, it } from "vitest";
import { cinematicRailEase, fittedRailLayout, railMotionDuration, railTarget } from "./useAnimatedRail";

describe("controlled category rail motion", () => {
  it("uses a responsive monotonic ease-out curve", () => {
    expect(cinematicRailEase(0)).toBe(0);
    expect(cinematicRailEase(.25)).toBeGreaterThan(.25);
    expect(cinematicRailEase(.75)).toBeGreaterThan(cinematicRailEase(.25));
    expect(cinematicRailEase(1)).toBe(1);
  });

  it("adapts duration to travel distance without becoming sluggish", () => {
    expect(railMotionDuration(200, 1_000)).toBe(240);
    expect(railMotionDuration(800, 1_000)).toBe(360);
    expect(railMotionDuration(2_000, 1_000)).toBe(420);
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

  it("moves by half of the fitted card group and keeps the final page full", () => {
    const itemOffsets = [12, 240, 468, 696, 924, 1152, 1380, 1608, 1836, 2064, 2292, 2520];
    const metrics = { clientWidth: 1600, scrollWidth: 3200, itemOffsets, leadingInset: 12, trailingInset: 12, itemsPerPage: 7 };
    expect(railTarget({ ...metrics, scrollLeft: 0 }, 1)).toBe(912);
    expect(railTarget({ ...metrics, scrollLeft: 912 }, 1)).toBe(1140);
    expect(railTarget({ ...metrics, scrollLeft: 1140 }, 1)).toBe(1140);
    expect(railTarget({ ...metrics, scrollLeft: 1140 }, -1)).toBe(912);
    expect(railTarget({ ...metrics, scrollLeft: 912 }, -1)).toBe(0);
  });

  it("uses half-page steps for even fitted groups", () => {
    const itemOffsets = Array.from({ length: 16 }, (_, index) => 10 + index * 210);
    const metrics = { clientWidth: 1660, scrollWidth: 3370, itemOffsets, leadingInset: 10, trailingInset: 10, itemsPerPage: 8 };
    expect(railTarget({ ...metrics, scrollLeft: 0 }, 1)).toBe(840);
    expect(railTarget({ ...metrics, scrollLeft: 840 }, 1)).toBe(1680);
    expect(railTarget({ ...metrics, scrollLeft: 1680 }, -1)).toBe(840);
  });
});
