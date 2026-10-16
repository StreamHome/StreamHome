import { useLayoutEffect, useState, type CSSProperties } from "react";
import { isForcedLandscape, isPhonePlayerViewport, readMobileViewport } from "./mobilePlayer";

interface PlayerEnvironment {
  mobilePlayer: boolean;
  forcedLandscape: boolean;
  width: number;
  height: number;
}

function readEnvironment(previous?: PlayerEnvironment): PlayerEnvironment {
  if (typeof window === "undefined") return { mobilePlayer: false, forcedLandscape: false, width: 0, height: 0 };
  const metrics = readMobileViewport();
  if (previous && (metrics.width <= 0 || metrics.height <= 0)) return previous;
  // Fullscreen and browser chrome can briefly report tablet-sized geometry.
  // Keep the last phone layout through the ambiguous square transition as well.
  if (previous?.mobilePlayer
    && Math.abs(metrics.width - metrics.height) < Math.min(metrics.width, metrics.height) * .1) return previous;
  const mobilePlayer = previous?.mobilePlayer || isPhonePlayerViewport(metrics);
  // Size and rotation must come from the same sample. Orientation media queries
  // can update before the viewport dimensions during native fullscreen entry.
  return { mobilePlayer, forcedLandscape: isForcedLandscape(metrics.width, metrics.height, mobilePlayer),
    width: metrics.width, height: metrics.height };
}

export function useMobilePlayerEnvironment() {
  const [environment, setEnvironment] = useState(() => readEnvironment());
  useLayoutEffect(() => {
    const refresh = () => setEnvironment((previous) => {
      const next = readEnvironment(previous);
      return next.mobilePlayer === previous.mobilePlayer && next.forcedLandscape === previous.forcedLandscape
        && next.width === previous.width && next.height === previous.height
        ? previous : next;
    });
    const orientation = window.matchMedia?.("(orientation: portrait)");
    refresh();
    window.addEventListener("resize", refresh);
    window.addEventListener("orientationchange", refresh);
    window.addEventListener("pageshow", refresh);
    window.visualViewport?.addEventListener("resize", refresh);
    screen.orientation?.addEventListener?.("change", refresh);
    orientation?.addEventListener?.("change", refresh);
    document.addEventListener("fullscreenchange", refresh);
    document.addEventListener("webkitfullscreenchange", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("resize", refresh);
      window.removeEventListener("orientationchange", refresh);
      window.removeEventListener("pageshow", refresh);
      window.visualViewport?.removeEventListener("resize", refresh);
      screen.orientation?.removeEventListener?.("change", refresh);
      orientation?.removeEventListener?.("change", refresh);
      document.removeEventListener("fullscreenchange", refresh);
      document.removeEventListener("webkitfullscreenchange", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);
  const stageStyle: CSSProperties | undefined = environment.mobilePlayer ? {
    "--mobile-player-width": `${Math.max(environment.width, environment.height)}px`,
    "--mobile-player-height": `${Math.min(environment.width, environment.height)}px`,
  } as CSSProperties : undefined;
  return { ...environment, stageStyle };
}
