import { useEffect, useMemo, useState } from "react";
import type { Movie } from "../../types/api";

const ROTATION_INTERVAL = 12_000;

export function useRotatingFeature(items: Movie[]) {
  const [index, setIndex] = useState(0);
  const [paused, setPaused] = useState(false);
  const signature = useMemo(() => items.map((item) => item.id).join("|"), [items]);

  useEffect(() => setIndex(0), [signature]);

  useEffect(() => {
    if (paused || items.length < 2 || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const rotate = () => {
      if (!document.hidden) setIndex((current) => (current + 1) % items.length);
    };
    const interval = window.setInterval(rotate, ROTATION_INTERVAL);
    return () => window.clearInterval(interval);
  }, [items.length, paused]);

  return {
    featured: items[index % Math.max(items.length, 1)] ?? null,
    index,
    setIndex,
    setPaused,
  };
}
