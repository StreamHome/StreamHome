import React, { createContext, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, MotionConfig, motion, type Variants } from "framer-motion";
import type { ThemeId } from "../types/theme";

export const MOTION_TIMINGS = {
  instant: 0.07,
  press: 0.08,
  focus: 0.14,
  menu: 0.16,
  menuEnter: 0.17,
  menuExit: 0.12,
  menuItem: 0.14,
  menuStagger: 0.018,
  dialog: 0.22,
  dialogEnter: 0.22,
  dialogExit: 0.15,
  viewExit: 0.14,
  viewEnter: 0.24,
  view: 0.24,
  rail: 340,
  billboardExit: 0.24,
  billboardEnter: 0.38,
  billboard: 0.38,
  billboardCopy: 0.24,
  profileMorph: 0.28,
  profileEntry: 0.22,
  artwork: 0.2,
  list: 0.2,
  notice: 0.18,
  controlsEnter: 0.14,
  controlsExit: 0.12,
  reduced: 0.1,
} as const;

export const MOTION_EASE = [0.2, 0, 0, 1] as const;

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
    transition: { duration: MOTION_TIMINGS.viewEnter, ease: MOTION_EASE },
  },
  exit: (direction: number = 1) => ({ ...directional(exit, direction), transition: { duration: MOTION_TIMINGS.viewExit, ease: MOTION_EASE } }),
});

const billboardVariants = (initial: Record<string, string | number>, exit: Record<string, string | number>, timing: { enter: number; exit: number }): Variants => ({
  initial: (direction: number = 1) => ({ ...initial, x: typeof initial.x === "number" ? initial.x * direction : initial.x }),
  animate: { opacity: 1, x: 0, y: 0, scale: 1, transition: { duration: timing.enter, ease: MOTION_EASE } },
  exit: (direction: number = 1) => ({ ...exit, x: typeof exit.x === "number" ? exit.x * direction : exit.x, transition: { duration: timing.exit, ease: MOTION_EASE } }),
});

export const REDUCED_BILLBOARD_MOTION: Variants = {
  initial: { opacity: 0 },
  animate: { opacity: 1, transition: { duration: MOTION_TIMINGS.reduced } },
  exit: { opacity: 0, transition: { duration: MOTION_TIMINGS.reduced } },
};

export const THEME_MOTION: Record<ThemeId, ThemeMotionDefinition> = {
  ember: {
    view: viewVariants({ opacity: 0, y: 8 }, { opacity: 0, y: -6 }),
    billboardTiming: { enter: .34, exit: .2 },
    billboard: billboardVariants({ opacity: 0, x: 14 }, { opacity: 0, x: -10 }, { enter: .34, exit: .2 }),
  },
  aurora: {
    view: viewVariants({ opacity: 0, y: 10, scale: .995 }, { opacity: 0, y: -8 }),
    billboardTiming: { enter: .4, exit: .24 },
    billboard: billboardVariants({ opacity: 0, y: 12, scale: .995 }, { opacity: 0, y: -8 }, { enter: .4, exit: .24 }),
  },
  cinema: {
    view: viewVariants({ opacity: 0, x: 12 }, { opacity: 0, x: -10 }),
    billboardTiming: { enter: .42, exit: .26 },
    billboard: billboardVariants({ opacity: 0, x: 18, scale: 1.008 }, { opacity: 0, x: -14 }, { enter: .42, exit: .26 }),
  },
  gemini: {
    view: viewVariants({ opacity: 0, x: 10, y: 4 }, { opacity: 0, x: -8 }),
    billboardTiming: { enter: .36, exit: .18 },
    billboard: billboardVariants({ opacity: 0, x: 12, y: 4 }, { opacity: 0, x: -10 }, { enter: .36, exit: .18 }),
  },
};

interface MotionContextValue {
  reduced: boolean;
  documentHidden: boolean;
  preference: MotionPreference;
  setPreference: (preference: MotionPreference) => void;
}

export type MotionPreference = "full" | "system" | "reduced";

const MOTION_PREFERENCE_KEY = "streamhome.motion-preference";

function storedMotionPreference(): MotionPreference {
  if (typeof window === "undefined") return "full";
  const stored = window.localStorage.getItem(MOTION_PREFERENCE_KEY);
  return stored === "system" || stored === "reduced" ? stored : "full";
}

const MotionContext = createContext<MotionContextValue>({ reduced: false, documentHidden: false, preference: "full", setPreference: () => undefined });

export function MotionProvider({ children }: { children: React.ReactNode }) {
  const [prefersReduced, setPrefersReduced] = useState(() => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const [documentHidden, setDocumentHidden] = useState(() => typeof document !== "undefined" && document.hidden);
  const [preference, setPreferenceState] = useState<MotionPreference>(storedMotionPreference);
  useEffect(() => {
    const media = typeof window.matchMedia === "function" ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
    const updateMotion = () => setPrefersReduced(Boolean(media?.matches));
    const update = () => setDocumentHidden(document.hidden);
    media?.addEventListener?.("change", updateMotion);
    document.addEventListener("visibilitychange", update);
    return () => {
      media?.removeEventListener?.("change", updateMotion);
      document.removeEventListener("visibilitychange", update);
    };
  }, []);
  const setPreference = (nextPreference: MotionPreference) => {
    window.localStorage.setItem(MOTION_PREFERENCE_KEY, nextPreference);
    setPreferenceState(nextPreference);
  };
  const reduced = preference === "reduced" || (preference === "system" && prefersReduced);
  useEffect(() => {
    document.documentElement.dataset.motionPreference = preference;
    return () => { delete document.documentElement.dataset.motionPreference; };
  }, [preference]);
  const value = useMemo(() => ({ reduced, documentHidden, preference, setPreference }), [documentHidden, preference, reduced]);
  return <MotionConfig reducedMotion={reduced ? "always" : "never"} transition={{ duration: MOTION_TIMINGS.list, ease: MOTION_EASE }}><MotionContext.Provider value={value}>{children}</MotionContext.Provider></MotionConfig>;
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
  shown: { transition: { delayChildren: 0.015, staggerChildren: MOTION_TIMINGS.menuStagger } },
};

export const CONTENT_REVEAL: Variants = {
  hidden: { opacity: 0, y: 8 },
  shown: { opacity: 1, y: 0, transition: { duration: MOTION_TIMINGS.list, ease: MOTION_EASE } },
  exit: { opacity: 0, y: -5, transition: { duration: MOTION_TIMINGS.viewExit, ease: MOTION_EASE } },
};

export function AnimatedState({ stateKey, className, children }: { stateKey: string; className?: string; children: React.ReactNode }) {
  const { reduced } = useAppMotion();
  const transition = { duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.list, ease: MOTION_EASE };
  return <AnimatePresence mode="sync" initial={false}><motion.div
    key={stateKey}
    className={className}
    initial={reduced ? { opacity: 0 } : { opacity: 0, y: 6 }}
    animate={{ opacity: 1, y: 0 }}
    exit={reduced ? { opacity: 0 } : { opacity: 0, y: -4 }}
    transition={transition}
  >{children}</motion.div></AnimatePresence>;
}

export function AnimatedView({ theme, viewKey, children }: { theme: ThemeId; viewKey: string; children: React.ReactNode }) {
  const { reduced } = useAppMotion();
  const definition = THEME_MOTION[theme];
  const previousKey = useRef(viewKey);
  const direction = viewDirection(previousKey.current, viewKey);
  useEffect(() => { previousKey.current = viewKey; }, [viewKey]);
  useLayoutEffect(() => { resetApplicationScroll(); }, [viewKey]);
  const reducedVariants: Variants = {
    initial: { opacity: 0 },
    animate: { opacity: 1, transition: { duration: MOTION_TIMINGS.reduced, ease: MOTION_EASE } },
    exit: { opacity: 0, transition: { duration: MOTION_TIMINGS.reduced, ease: MOTION_EASE } },
  };
  return <AnimatePresence mode="sync" custom={direction} initial={false}>
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
