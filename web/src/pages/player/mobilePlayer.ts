export type MobileTapSide = "left" | "right";

export interface MobileTapChain {
  side: MobileTapSide;
  lastTapAt: number;
  seekSteps: number;
}

export interface MobileTapResult {
  chain: MobileTapChain;
  seekDelta: number;
  accumulatedSeconds: number;
}

export interface MobileViewportMetrics {
  width: number;
  height: number;
  maxTouchPoints: number;
  coarsePointer: boolean;
  hoverUnavailable: boolean;
}

export interface MobilePointerSample {
  x: number;
  y: number;
  at: number;
}

interface LockableScreenOrientation {
  lock?: (orientation: "landscape") => Promise<void>;
  unlock?: () => void;
}

const MOBILE_SHORT_EDGE_LIMIT = 700;
const MOBILE_LONG_EDGE_LIMIT = 1_200;
export const MOBILE_TAP_CHAIN_WINDOW = 300;
export const MOBILE_TAP_MAX_DURATION = 450;
export const MOBILE_TAP_MAX_DISTANCE = 14;
export const MOBILE_SEEK_FEEDBACK_MS = 700;
export const MOBILE_SEEK_CONTROLS_GRACE_MS = 1_000;

export function isPhonePlayerViewport(metrics: MobileViewportMetrics): boolean {
  const shortEdge = Math.min(metrics.width, metrics.height);
  const longEdge = Math.max(metrics.width, metrics.height);
  const hasTouchInput = metrics.maxTouchPoints > 0 || metrics.coarsePointer || metrics.hoverUnavailable;
  return hasTouchInput && shortEdge <= MOBILE_SHORT_EDGE_LIMIT && longEdge <= MOBILE_LONG_EDGE_LIMIT;
}

export function readMobileViewport(windowObject: Window = window): MobileViewportMetrics {
  const matches = (query: string) => typeof windowObject.matchMedia === "function" && windowObject.matchMedia(query).matches;
  return {
    width: windowObject.innerWidth,
    height: windowObject.innerHeight,
    maxTouchPoints: windowObject.navigator.maxTouchPoints,
    coarsePointer: matches("(pointer: coarse)"),
    hoverUnavailable: matches("(hover: none)"),
  };
}

export function isMobileTapCandidate(
  start: MobilePointerSample,
  end: MobilePointerSample,
  maxDistance = MOBILE_TAP_MAX_DISTANCE,
  maxDuration = MOBILE_TAP_MAX_DURATION,
): boolean {
  const elapsed = end.at - start.at;
  if (elapsed < 0 || elapsed > maxDuration) return false;
  return Math.hypot(end.x - start.x, end.y - start.y) <= maxDistance;
}

export function shouldShowMobileChrome(phase: string, requested: boolean, menuOpen = false, scrubbing = false, seekControlsHidden = false): boolean {
  // Pause reveals chrome on entry, but a deliberate surface tap may dismiss it.
  return requested || menuOpen || scrubbing || (!seekControlsHidden && ["loading", "buffering", "recovering", "preparing"].includes(phase));
}

export function nextMobileTap(
  current: MobileTapChain | null,
  side: MobileTapSide,
  now: number,
  chainWindow = MOBILE_TAP_CHAIN_WINDOW,
): MobileTapResult {
  if (!current || current.side !== side || now - current.lastTapAt > chainWindow) {
    return {
      chain: { side, lastTapAt: now, seekSteps: 0 },
      seekDelta: 0,
      accumulatedSeconds: 0,
    };
  }

  const seekSteps = current.seekSteps + 1;
  const direction = side === "left" ? -1 : 1;
  return {
    chain: { side, lastTapAt: now, seekSteps },
    seekDelta: direction * 10,
    accumulatedSeconds: seekSteps * 10,
  };
}

export function isForcedLandscape(width: number, height: number, mobilePlayer: boolean): boolean {
  return mobilePlayer && height > width;
}

export async function lockPlayerLandscape(screenObject: Screen = screen): Promise<boolean> {
  const orientation = screenObject.orientation as LockableScreenOrientation | undefined;
  if (!orientation?.lock) return false;
  try {
    await orientation.lock("landscape");
    return true;
  } catch {
    return false;
  }
}

export function unlockPlayerLandscape(screenObject: Screen = screen): void {
  const orientation = screenObject.orientation as LockableScreenOrientation | undefined;
  try {
    orientation?.unlock?.();
  } catch {
    // Some mobile browsers throw when the document is no longer active.
  }
}

export function createLandscapeLockController(screenObject: Screen = screen) {
  let wanted = false;
  let pending = false;
  let revision = 0;
  const request = () => {
    wanted = true;
    revision += 1;
    run();
  };
  const run = () => {
    if (pending || !wanted) return;
    pending = true;
    const started = revision;
    void lockPlayerLandscape(screenObject).then(() => {
      pending = false;
      if (!wanted) unlockPlayerLandscape(screenObject);
      else if (started !== revision) run();
    });
  };
  const release = () => {
    wanted = false;
    revision += 1;
    unlockPlayerLandscape(screenObject);
  };
  return { request, release };
}
