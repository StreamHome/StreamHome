import { useLayoutEffect, useState } from "react";
import { isForcedLandscape, isPhonePlayerViewport, readMobileViewport } from "./mobilePlayer";

function readEnvironment(wasPhone = false) {
  if (typeof window === "undefined") return { mobilePlayer: false, forcedLandscape: false };
  const metrics = readMobileViewport();
  // Fullscreen and browser chrome can briefly report tablet-sized geometry.
  // Once this viewing session is a phone, retain its controls and rotation fallback.
  const mobilePlayer = wasPhone || isPhonePlayerViewport(metrics);
  const portrait = typeof window.matchMedia === "function"
    ? window.matchMedia("(orientation: portrait)").matches
    : isForcedLandscape(metrics.width, metrics.height, true);
  return { mobilePlayer, forcedLandscape: mobilePlayer && portrait };
}

export function useMobilePlayerEnvironment() {
  const [environment, setEnvironment] = useState(() => readEnvironment());
  useLayoutEffect(() => {
    const refresh = () => setEnvironment((previous) => {
      const next = readEnvironment(previous.mobilePlayer);
      return next.mobilePlayer === previous.mobilePlayer && next.forcedLandscape === previous.forcedLandscape
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
  return environment;
}
