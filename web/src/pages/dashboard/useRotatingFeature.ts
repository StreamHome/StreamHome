import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useAppMotion } from "../../motion/motionSystem";
import type { Movie } from "../../types/api";

const ROTATION_INTERVAL = 10_000;
interface RotationOptions {
  beforeRetire?: () => void;
  canRotate?: () => boolean;
}

export function useRotatingFeature(items: Movie[], options: RotationOptions = {}) {
  const { documentHidden } = useAppMotion();
  const signature = useMemo(() => items.map((item) => item.id).join("|"), [items]);
  const [roster, setRoster] = useState({ signature, items });
  const [index, setIndexState] = useState(0);
  const [focusPaused, setFocusPaused] = useState(false);
  const [userPaused, setUserPaused] = useState(false);
  const [countdownGeneration, setCountdownGeneration] = useState(0);
  const [direction, setDirection] = useState<-1 | 1>(1);
  const [source, setSource] = useState<"automatic" | "manual">("automatic");
  const remainingMs = useRef(ROTATION_INTERVAL);
  const startedAt = useRef<number | null>(null);
  const currentOptions = useRef(options);
  currentOptions.current = options;
  const currentItems = signature === roster.signature ? items : roster.items;
  const effectivelyPaused = focusPaused || userPaused || documentHidden;

  useLayoutEffect(() => {
    if (signature === roster.signature) return;
    // Retire focus while the previous roster is still mounted and interactive.
    currentOptions.current.beforeRetire?.();
    remainingMs.current = ROTATION_INTERVAL;
    startedAt.current = null;
    setRoster({ signature, items });
    setIndexState(0);
    setDirection(1);
    setSource("automatic");
    setCountdownGeneration((value) => value + 1);
  }, [items, roster.signature, signature]);

  const setIndex = (next: number) => {
    if (next === index || next < 0 || next >= currentItems.length) return;
    currentOptions.current.beforeRetire?.();
    remainingMs.current = ROTATION_INTERVAL;
    startedAt.current = null;
    setDirection(next > index ? 1 : -1);
    setSource("manual");
    setCountdownGeneration((value) => value + 1);
    setIndexState(next);
  };

  useEffect(() => {
    if (effectivelyPaused || currentItems.length < 2) return;
    startedAt.current = Date.now();
    const timeout = window.setTimeout(() => {
      // A focus event and an expiring timer may share a task. Read live focus
      // before changing the committed slide, rather than trusting React state.
      if (currentOptions.current.canRotate?.() === false) {
        setFocusPaused(true);
        return;
      }
      currentOptions.current.beforeRetire?.();
      remainingMs.current = ROTATION_INTERVAL;
      startedAt.current = null;
      setDirection(1);
      setSource("automatic");
      setCountdownGeneration((value) => value + 1);
      setIndexState((current) => (current + 1) % currentItems.length);
    }, remainingMs.current);
    return () => {
      window.clearTimeout(timeout);
      if (startedAt.current !== null) {
        remainingMs.current = Math.max(1, remainingMs.current - (Date.now() - startedAt.current));
        startedAt.current = null;
      }
    };
  }, [effectivelyPaused, index, currentItems.length, roster.signature, countdownGeneration]);

  return {
    featured: currentItems[index % Math.max(currentItems.length, 1)] ?? null,
    items: currentItems,
    index,
    setIndex,
    setPaused: setFocusPaused,
    setUserPaused,
    userPaused,
    paused: effectivelyPaused,
    countdownGeneration,
    direction,
    source,
  };
}

export { ROTATION_INTERVAL };
