import { describe, expect, it, vi } from "vitest";
import {
  isForcedLandscape,
  isPhonePlayerViewport,
  lockPlayerLandscape,
  nextMobileTap,
  unlockPlayerLandscape,
} from "./mobilePlayer";

describe("mobile player environment", () => {
  it("recognizes portrait and landscape phone viewports without classifying desktops", () => {
    expect(isPhonePlayerViewport({ width: 390, height: 844, maxTouchPoints: 5, coarsePointer: true, hoverUnavailable: true })).toBe(true);
    expect(isPhonePlayerViewport({ width: 844, height: 390, maxTouchPoints: 5, coarsePointer: true, hoverUnavailable: true })).toBe(true);
    expect(isPhonePlayerViewport({ width: 1440, height: 900, maxTouchPoints: 0, coarsePointer: false, hoverUnavailable: false })).toBe(false);
    expect(isPhonePlayerViewport({ width: 1024, height: 768, maxTouchPoints: 5, coarsePointer: true, hoverUnavailable: true })).toBe(false);
  });

  it("forces the visual landscape fallback only for portrait phones", () => {
    expect(isForcedLandscape(390, 844, true)).toBe(true);
    expect(isForcedLandscape(844, 390, true)).toBe(false);
    expect(isForcedLandscape(390, 844, false)).toBe(false);
  });
});

describe("mobile repeated-tap seeking", () => {
  it("waits for a second tap and then accumulates every continuing tap", () => {
    const first = nextMobileTap(null, "right", 1_000);
    const second = nextMobileTap(first.chain, "right", 1_200);
    const third = nextMobileTap(second.chain, "right", 1_400);
    const fourth = nextMobileTap(third.chain, "right", 1_600);

    expect(first.seekDelta).toBe(0);
    expect(second).toMatchObject({ seekDelta: 10, accumulatedSeconds: 10 });
    expect(third).toMatchObject({ seekDelta: 10, accumulatedSeconds: 20 });
    expect(fourth).toMatchObject({ seekDelta: 10, accumulatedSeconds: 30 });
  });

  it("rewinds on the left and starts a new chain after switching sides or timing out", () => {
    const first = nextMobileTap(null, "left", 1_000);
    expect(nextMobileTap(first.chain, "left", 1_200)).toMatchObject({ seekDelta: -10, accumulatedSeconds: 10 });
    expect(nextMobileTap(first.chain, "right", 1_200)).toMatchObject({ seekDelta: 0, accumulatedSeconds: 0 });
    expect(nextMobileTap(first.chain, "left", 2_000)).toMatchObject({ seekDelta: 0, accumulatedSeconds: 0 });
  });
});

describe("mobile orientation locking", () => {
  it("locks and unlocks landscape when the browser exposes the orientation API", async () => {
    const lock = vi.fn().mockResolvedValue(undefined);
    const unlock = vi.fn();
    const screenObject = { orientation: { lock, unlock } } as unknown as Screen;

    await expect(lockPlayerLandscape(screenObject)).resolves.toBe(true);
    expect(lock).toHaveBeenCalledWith("landscape");
    unlockPlayerLandscape(screenObject);
    expect(unlock).toHaveBeenCalledOnce();
  });

  it("keeps the CSS fallback available when native locking is rejected", async () => {
    const screenObject = { orientation: { lock: vi.fn().mockRejectedValue(new Error("Denied")) } } as unknown as Screen;
    await expect(lockPlayerLandscape(screenObject)).resolves.toBe(false);
  });
});
