import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { MOTION_TIMINGS, useAppMotion } from "./motionSystem";

export const cinematicRailEase = (progress: number): number => {
  const bounded = Math.min(1, Math.max(0, progress));
  return 1 - Math.pow(1 - bounded, 3);
};

export function railMotionDuration(distance: number, viewportWidth: number): number {
  const pageFraction = Math.abs(distance) / Math.max(1, viewportWidth);
  return Math.round(Math.min(420, Math.max(240, MOTION_TIMINGS.rail - 140 + pageFraction * 200)));
}

export function fittedRailLayout(availableWidth: number, naturalCardWidth: number, minimumCardWidth: number, gap: number, itemCount: number) {
  if (availableWidth <= 0 || naturalCardWidth <= 0 || itemCount <= 0) return { columns: 1, cardWidth: Math.max(0, naturalCardWidth) };
  const candidateColumns = Math.max(1, Math.min(itemCount, Math.ceil((availableWidth + gap) / (naturalCardWidth + gap) - .02)));
  const minimumColumns = Math.max(1, Math.min(itemCount, Math.floor((availableWidth + gap) / (minimumCardWidth + gap))));
  const columns = Math.max(1, Math.min(candidateColumns, minimumColumns));
  const cardWidth = Math.min(naturalCardWidth, (availableWidth - gap * (columns - 1)) / columns);
  return { columns, cardWidth: Math.max(0, cardWidth) };
}

type RailTargetMetrics = Pick<HTMLElement, "scrollLeft" | "clientWidth" | "scrollWidth"> & {
  itemOffsets?: readonly number[];
  leadingInset?: number;
  trailingInset?: number;
  itemsPerPage?: number;
};

export function railTarget(element: RailTargetMetrics, direction: -1 | 1): number {
  const maximum = Math.max(0, element.scrollWidth - element.clientWidth);
  const leadingInset = Math.max(0, element.leadingInset ?? 0);
  const trailingInset = Math.max(0, element.trailingInset ?? leadingInset);
  const usableWidth = Math.max(1, element.clientWidth - leadingInset - trailingInset);
  const current = Math.max(0, Math.min(maximum, element.scrollLeft));
  const positions = (element.itemOffsets ?? [])
    .map((offset) => Math.max(0, offset - leadingInset))
    .filter((offset, index, values) => index === 0 || Math.abs(offset - values[index - 1]) > 1);

  if (!positions.length) {
    return Math.max(0, Math.min(maximum, current + direction * usableWidth));
  }

  if (element.itemsPerPage && element.itemsPerPage > 0) {
    const itemsPerPage = Math.max(1, Math.floor(element.itemsPerPage));
    const currentItem = positions.reduce((found, offset, index) => offset <= current + 1 ? index : found, 0);
    const navigationStep = Math.max(1, Math.ceil(itemsPerPage / 2));
    const lastPageStart = Math.max(0, positions.length - itemsPerPage);
    const previousGridPoint = Math.floor(lastPageStart / navigationStep) * navigationStep;
    const targetIndex = direction === 1
      ? Math.min(lastPageStart, currentItem + navigationStep)
      : currentItem === lastPageStart && previousGridPoint < lastPageStart
        ? previousGridPoint
        : Math.max(0, currentItem - navigationStep);
    return Math.min(maximum, positions[targetIndex]);
  }

  if (direction === 1) {
    const pageBoundary = current + usableWidth - 1;
    return Math.min(maximum, positions.find((offset) => offset > pageBoundary) ?? maximum);
  }

  const pageBoundary = current - usableWidth + 1;
  const previousPages = positions.filter((offset) => offset < current - 1 && offset <= pageBoundary);
  const pageStart = previousPages[previousPages.length - 1];
  return pageStart ?? 0;
}

export function useAnimatedRail() {
  const rail = useRef<HTMLDivElement>(null);
  const frame = useRef<number | null>(null);
  const target = useRef<number | null>(null);
  const restoredSnapType = useRef<string | null>(null);
  const { reduced } = useAppMotion();
  const [direction, setDirection] = useState<-1 | 0 | 1>(0);
  const [edges, setEdges] = useState({ previous: false, next: true });
  const [proximity, setProximity] = useState<"previous" | "next" | "none">("none");

  const metrics = useCallback((element: HTMLDivElement, scrollLeft = element.scrollLeft): RailTargetMetrics => {
    const style = getComputedStyle(element);
    return {
      scrollLeft,
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      itemOffsets: Array.from(element.children, (child) => (child as HTMLElement).offsetLeft),
      leadingInset: Number.parseFloat(style.paddingLeft) || 0,
      trailingInset: Number.parseFloat(style.paddingRight) || 0,
      itemsPerPage: Number.parseInt(element.dataset.railPageSize || "", 10) || undefined,
    };
  }, []);

  const updateEdges = useCallback(() => {
    const element = rail.current;
    if (!element) return;
    const current = element.scrollLeft;
    const nextEdges = {
      previous: railTarget(metrics(element), -1) < current - 1,
      next: railTarget(metrics(element), 1) > current + 1,
    };
    setEdges((previous) => previous.previous === nextEdges.previous && previous.next === nextEdges.next ? previous : nextEdges);
  }, [metrics]);

  const fitCards = useCallback(() => {
    const element = rail.current;
    if (!element) return;
    const cards = Array.from(element.children) as HTMLElement[];
    if (!cards.length) return;
    cards.forEach((card) => card.style.removeProperty("--rail-card-fitted"));
    if (window.matchMedia("(max-width: 760px)").matches) {
      delete element.dataset.railPageSize;
      updateEdges();
      return;
    }
    const railStyle = getComputedStyle(element);
    const leadingInset = Number.parseFloat(railStyle.paddingLeft) || 0;
    const trailingInset = Number.parseFloat(railStyle.paddingRight) || 0;
    const gap = Number.parseFloat(railStyle.columnGap || railStyle.gap) || 0;
    const availableWidth = Math.max(0, element.clientWidth - leadingInset - trailingInset);
    const cardStyle = getComputedStyle(cards[0]);
    const naturalCardWidth = cards[0].getBoundingClientRect().width;
    const minimumCardWidth = Number.parseFloat(cardStyle.getPropertyValue("--rail-card-min")) || 154;
    const layout = fittedRailLayout(availableWidth, naturalCardWidth, minimumCardWidth, gap, cards.length);
    cards.forEach((card) => card.style.setProperty("--rail-card-fitted", `${layout.cardWidth}px`));
    element.dataset.railPageSize = String(layout.columns);
    updateEdges();
  }, [updateEdges]);

  const stop = useCallback(() => {
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = null;
    target.current = null;
    const element = rail.current;
    if (element && restoredSnapType.current !== null) {
      element.style.scrollSnapType = restoredSnapType.current;
      restoredSnapType.current = null;
    }
    setDirection(0);
    updateEdges();
  }, [updateEdges]);

  const scroll = useCallback((nextDirection: -1 | 1) => {
    const element = rail.current;
    if (!element) return;
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    const from = element.scrollLeft;
    const basis = target.current ?? from;
    const to = railTarget(metrics(element, basis), nextDirection);
    target.current = to;
    setDirection(nextDirection);
    if (restoredSnapType.current === null) restoredSnapType.current = element.style.scrollSnapType;
    element.style.scrollSnapType = "none";
    if (reduced || Math.abs(to - from) < 1) {
      element.scrollLeft = to;
      stop();
      return;
    }
    const startedAt = performance.now();
    const duration = railMotionDuration(to - from, element.clientWidth);
    const tick = (now: number) => {
      const progress = Math.min(1, (now - startedAt) / duration);
      element.scrollLeft = from + (to - from) * cinematicRailEase(progress);
      if (progress < 1) frame.current = requestAnimationFrame(tick);
      else stop();
    };
    frame.current = requestAnimationFrame(tick);
  }, [metrics, reduced, stop, updateEdges]);

  const onPointerMove = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    if (event.pointerType && event.pointerType !== "mouse") return;
    const bounds = event.currentTarget.getBoundingClientRect();
    const edgeZone = Math.min(112, bounds.width * .14);
    const distanceFromStart = event.clientX - bounds.left;
    const distanceFromEnd = bounds.right - event.clientX;
    const next = distanceFromStart <= edgeZone ? "previous" : distanceFromEnd <= edgeZone ? "next" : "none";
    setProximity((current) => current === next ? current : next);
  }, []);

  const onPointerLeave = useCallback(() => setProximity("none"), []);

  const handleNativeScroll = useCallback(() => {
    if (frame.current !== null) return;
    updateEdges();
  }, [updateEdges]);

  useEffect(() => {
    const element = rail.current;
    if (!element) return;
    element.addEventListener("scroll", handleNativeScroll, { passive: true });
    element.addEventListener("wheel", stop, { passive: true });
    element.addEventListener("pointerdown", stop, { passive: true });
    element.addEventListener("touchstart", stop, { passive: true });
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(fitCards);
    const mutationObserver = typeof MutationObserver === "undefined" ? null : new MutationObserver(fitCards);
    observer?.observe(element);
    mutationObserver?.observe(element, { childList: true });
    fitCards();
    return () => {
      element.removeEventListener("scroll", handleNativeScroll);
      element.removeEventListener("wheel", stop);
      element.removeEventListener("pointerdown", stop);
      element.removeEventListener("touchstart", stop);
      observer?.disconnect();
      mutationObserver?.disconnect();
      if (frame.current !== null) cancelAnimationFrame(frame.current);
    };
  }, [fitCards, handleNativeScroll, stop]);

  return {
    rail,
    scroll,
    direction,
    proximity,
    canScrollPrevious: edges.previous,
    canScrollNext: edges.next,
    stop,
    proximityHandlers: { onPointerMove, onPointerLeave },
  };
}
