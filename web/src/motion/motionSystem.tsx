import React, { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, MotionConfig, motion, type Variants } from "framer-motion";
import type { ThemeId } from "../types/theme";

export const MOTION_TIMINGS = {
  instant: 0.12,
  press: 0.14,
  focus: 0.28,
  menu: 0.26,
  menuEnter: 0.26,
  menuExit: 0.18,
  menuItem: 0.22,
  menuStagger: 0.045,
  dialog: 0.42,
  dialogEnter: 0.42,
  dialogExit: 0.26,
  viewExit: 0.28,
  viewEnter: 0.52,
  view: 0.8,
  rail: 1080,
  billboardExit: 0.88,
  billboardEnter: 1.12,
  billboard: 2,
  billboardCopy: 0.62,
  profileMorph: 0.95,
  profileEntry: 0.62,
  artwork: 0.5,
  list: 0.42,
  notice: 0.32,
  controlsEnter: 0.22,
  controlsExit: 0.42,
  reduced: 0.16,
} as const;

export const MOTION_EASE = [0.16, 1, 0.3, 1] as const;

export interface ThemeMotionDefinition {
  view: Variants;
  billboard: Variants;
  billboardTiming: { enter: number; exit: number };
}

function directional(values: Record<string, string | number>, direction: number) {
  return {
    ...values,
    x: typeof values.x === "number" ? values.x * direction : values.x,
  };
}

const viewVariants = (initial: Record<string, string | number>, exit: Record<string, string | number>): Variants => ({
  initial: (direction: number = 1) => directional(initial, direction),
  animate: {
    opacity: 1,
    x: 0,
    y: 0,
    scale: 1,
    filter: "blur(0px)",
    transition: { duration: MOTION_TIMINGS.viewEnter, ease: MOTION_EASE },
  },
  exit: (direction: number = 1) => ({ ...directional(exit, direction), transition: { duration: MOTION_TIMINGS.viewExit, ease: MOTION_EASE } }),
});

const billboardVariants = (initial: Record<string, string | number>, exit: Record<string, string | number>, timing: { enter: number; exit: number }): Variants => ({
  initial: (direction: number = 1) => ({ ...initial, x: typeof initial.x === "number" ? initial.x * direction : initial.x }),
  animate: { opacity: 1, x: 0, y: 0, scale: 1, filter: "blur(0px)", transition: { duration: timing.enter, ease: MOTION_EASE } },
  exit: (direction: number = 1) => ({ ...exit, x: typeof exit.x === "number" ? exit.x * direction : exit.x, transition: { duration: timing.exit, ease: MOTION_EASE } }),
});

export const REDUCED_BILLBOARD_MOTION: Variants = {
  initial: { opacity: 0 },
  animate: { opacity: 1, transition: { duration: MOTION_TIMINGS.reduced } },
  exit: { opacity: 0, transition: { duration: MOTION_TIMINGS.reduced } },
};

export const THEME_MOTION: Record<ThemeId, ThemeMotionDefinition> = {
  ember: {
    view: viewVariants({ opacity: 0, y: 26, scale: 1.012, filter: "blur(9px)" }, { opacity: 0, y: -16, scale: .992, filter: "blur(7px)" }),
    billboardTiming: { enter: 1.08, exit: .84 },
    billboard: billboardVariants({ opacity: 0, x: 34, scale: 1.022, filter: "blur(11px)" }, { opacity: 0, x: -26, scale: .99, filter: "blur(8px)" }, { enter: 1.08, exit: .84 }),
  },
  aurora: {
    view: viewVariants({ opacity: 0, y: 34, scale: .985, filter: "blur(14px)" }, { opacity: 0, y: -22, scale: 1.008, filter: "blur(12px)" }),
    billboardTiming: { enter: 1.24, exit: .96 },
    billboard: billboardVariants({ opacity: 0, x: 18, y: 28, scale: .985, filter: "blur(16px)" }, { opacity: 0, x: -14, y: -20, scale: 1.012, filter: "blur(13px)" }, { enter: 1.24, exit: .96 }),
  },
  cinema: {
    view: viewVariants({ opacity: 0, x: 24, scale: 1.018, filter: "blur(8px)" }, { opacity: 0, x: -20, scale: 1.026, filter: "blur(7px)" }),
    billboardTiming: { enter: 1.16, exit: .92 },
    billboard: billboardVariants({ opacity: 0, x: 42, scale: 1.028, filter: "blur(8px)" }, { opacity: 0, x: -34, scale: 1.012, filter: "blur(6px)" }, { enter: 1.16, exit: .92 }),
  },
  gemini: {
    view: viewVariants({ opacity: 0, x: 30, y: 12, scale: .982, filter: "blur(10px)" }, { opacity: 0, x: -22, y: -8, scale: .99, filter: "blur(8px)" }),
    billboardTiming: { enter: .98, exit: .76 },
    billboard: billboardVariants({ opacity: 0, x: 36, y: 16, scale: .98, filter: "blur(12px)" }, { opacity: 0, x: -28, y: -10, scale: .99, filter: "blur(9px)" }, { enter: .98, exit: .76 }),
  },
};

interface MotionContextValue {
  reduced: boolean;
  documentHidden: boolean;
}

const MotionContext = createContext<MotionContextValue>({ reduced: false, documentHidden: false });

export function MotionProvider({ children }: { children: React.ReactNode }) {
  const [prefersReduced, setPrefersReduced] = useState(() => typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const [documentHidden, setDocumentHidden] = useState(() => typeof document !== "undefined" && document.hidden);
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const updateMotion = () => setPrefersReduced(media.matches);
    const update = () => setDocumentHidden(document.hidden);
    media.addEventListener?.("change", updateMotion);
    document.addEventListener("visibilitychange", update);
    return () => {
      media.removeEventListener?.("change", updateMotion);
      document.removeEventListener("visibilitychange", update);
    };
  }, []);
  const value = useMemo(() => ({ reduced: Boolean(prefersReduced), documentHidden }), [documentHidden, prefersReduced]);
  return <MotionConfig reducedMotion="user"><MotionContext.Provider value={value}>{children}</MotionContext.Provider></MotionConfig>;
}

export function useAppMotion(): MotionContextValue {
  return useContext(MotionContext);
}

export function resetApplicationScroll(): void {
  const root = document.getElementById("root");
  if (root) {
    root.scrollTop = 0;
    root.scrollLeft = 0;
  }
  window.scrollTo({ top: 0, left: 0, behavior: "auto" });
}

const VIEW_ORDER = ["home", "movies", "series", "watchlist", "downloads", "search", "details", "watch", "admin"];

function viewDirection(previous: string, next: string): -1 | 1 {
  const previousIndex = VIEW_ORDER.indexOf(previous.split(":")[0]);
  const nextIndex = VIEW_ORDER.indexOf(next.split(":")[0]);
  if (previousIndex < 0 || nextIndex < 0 || previousIndex === nextIndex) return 1;
  return nextIndex > previousIndex ? 1 : -1;
}

export const CONTENT_STAGGER: Variants = {
  hidden: {},
  shown: { transition: { delayChildren: 0.08, staggerChildren: 0.065 } },
};

export const CONTENT_REVEAL: Variants = {
  hidden: { opacity: 0, y: 18, filter: "blur(7px)" },
  shown: { opacity: 1, y: 0, filter: "blur(0px)", transition: { duration: MOTION_TIMINGS.list, ease: MOTION_EASE } },
  exit: { opacity: 0, y: -8, filter: "blur(4px)", transition: { duration: MOTION_TIMINGS.viewExit, ease: MOTION_EASE } },
};

export function AnimatedState({ stateKey, className, children }: { stateKey: string; className?: string; children: React.ReactNode }) {
  const { reduced } = useAppMotion();
  const transition = { duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.list, ease: MOTION_EASE };
  return <AnimatePresence mode="wait" initial={false}><motion.div
    key={stateKey}
    className={className}
    initial={reduced ? { opacity: 0 } : { opacity: 0, y: 12, filter: "blur(6px)" }}
    animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
    exit={reduced ? { opacity: 0 } : { opacity: 0, y: -8, filter: "blur(4px)" }}
    transition={transition}
  >{children}</motion.div></AnimatePresence>;
}

export function AnimatedView({ theme, viewKey, children }: { theme: ThemeId; viewKey: string; children: React.ReactNode }) {
  const { reduced } = useAppMotion();
  const definition = THEME_MOTION[theme];
  const previousKey = useRef(viewKey);
  const direction = viewDirection(previousKey.current, viewKey);
  useEffect(() => { previousKey.current = viewKey; }, [viewKey]);
  const reducedVariants: Variants = {
    initial: { opacity: 0 },
    animate: { opacity: 1, transition: { duration: MOTION_TIMINGS.reduced, ease: MOTION_EASE } },
    exit: { opacity: 0, transition: { duration: MOTION_TIMINGS.reduced, ease: MOTION_EASE } },
  };
  return <AnimatePresence mode="wait" custom={direction} onExitComplete={resetApplicationScroll}>
    <motion.div
      className="motion-view"
      key={viewKey}
      custom={direction}
      variants={reduced ? reducedVariants : definition.view}
      initial="initial"
      animate="animate"
      exit="exit"
    >{children}</motion.div>
  </AnimatePresence>;
}
