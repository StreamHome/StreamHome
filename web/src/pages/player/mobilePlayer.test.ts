import { describe, expect, it, vi } from "vitest";
import {
  createLandscapeLockController,
  isForcedLandscape,
  isMobileTapCandidate,
  isPhonePlayerViewport,
  lockPlayerLandscape,
  nextMobileTap,
  shouldShowMobileChrome,
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

describe("mobile tap recognition", () => {
  it("accepts a short stationary tap", () => {
    expect(isMobileTapCandidate({ x: 100, y: 80, at: 1_000 }, { x: 106, y: 84, at: 1_180 })).toBe(true);
  });

  it("rejects swipes, long presses, and invalid timestamps", () => {
    expect(isMobileTapCandidate({ x: 20, y: 20, at: 1_000 }, { x: 80, y: 20, at: 1_150 })).toBe(false);
    expect(isMobileTapCandidate({ x: 20, y: 20, at: 1_000 }, { x: 20, y: 20, at: 1_600 })).toBe(false);
    expect(isMobileTapCandidate({ x: 20, y: 20, at: 1_000 }, { x: 20, y: 20, at: 900 })).toBe(false);
  });

  it("keeps transport controls available during buffering and recovery", () => {
    expect(shouldShowMobileChrome("buffering", false)).toBe(true);
    expect(shouldShowMobileChrome("recovering", false)).toBe(true);
    expect(shouldShowMobileChrome("paused", false)).toBe(false);
    expect(shouldShowMobileChrome("paused", true)).toBe(true);
    expect(shouldShowMobileChrome("playing", false)).toBe(false);
    expect(shouldShowMobileChrome("paused", false, true)).toBe(true);
    expect(shouldShowMobileChrome("paused", false, false, true)).toBe(true);
    expect(shouldShowMobileChrome("playing", true)).toBe(true);
  });
});

describe("mobile orientation locking", () => {
  it("releases a delayed successful lock after exit without starting another request", async () => {
    let finish!: () => void;
    const lock = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    const unlock = vi.fn();
    const controller = createLandscapeLockController({ orientation: { lock, unlock } } as unknown as Screen);
    controller.request();
    controller.request();
    expect(lock).toHaveBeenCalledOnce();
    controller.release();
    finish();
    await Promise.resolve();
    await Promise.resolve();
    expect(unlock).toHaveBeenCalledTimes(2);
    expect(lock).toHaveBeenCalledOnce();
  });

  it("serializes a new foreground request behind an aborted earlier lock", async () => {
    let reject!: (error: Error) => void;
    const lock = vi.fn().mockImplementationOnce(() => new Promise<void>((_, fail) => { reject = fail; })).mockResolvedValue(undefined);
    const controller = createLandscapeLockController({ orientation: { lock, unlock: vi.fn() } } as unknown as Screen);
    controller.request();
    controller.release();
    controller.request();
    expect(lock).toHaveBeenCalledOnce();
    reject(new DOMException("Aborted", "AbortError"));
    await Promise.resolve();
    await Promise.resolve();
    expect(lock).toHaveBeenCalledTimes(2);
    controller.release();
  });

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
