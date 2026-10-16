import React, { useLayoutEffect, useRef } from "react";
import { AnimatePresence, motion, useIsPresent } from "framer-motion";
import { REDUCED_BILLBOARD_MOTION, THEME_MOTION, useAppMotion } from "../../motion/motionSystem";
import type { Movie } from "../../types/api";
import type { ThemeId } from "../../types/theme";
import { ROTATION_INTERVAL, useRotatingFeature } from "./useRotatingFeature";

function Slide({ children, theme, direction }: { children: React.ReactNode; theme: ThemeId; direction: -1 | 1 }) {
  const present = useIsPresent();
  const { reduced } = useAppMotion();
  return <motion.div className="billboard-slide" data-billboard-slide data-active={present} aria-hidden={!present || undefined} inert={!present} custom={direction} variants={reduced ? REDUCED_BILLBOARD_MOTION : THEME_MOTION[theme].billboard} initial="initial" animate="animate" exit="exit">{children}</motion.div>;
}

/** A bounded, intrinsic all-roster grid reserves geometry without loading hidden art. */
export function BillboardStack({ items, theme, className, paginationClassName, render }: { items: Movie[]; theme: ThemeId; className: string; paginationClassName: string; render: (movie: Movie, measurement: boolean) => React.ReactNode }) {
  const root = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => () => {
    const element = root.current;
    if (!element?.contains(document.activeElement)) return;
    // Parent fallback/route composition can retire the entire billboard rather
    // than pass an empty roster. Its stable shell anchor outlives that branch.
    const anchor = element.closest<HTMLElement>("[data-catalog-focus-anchor]") ?? element.closest<HTMLElement>("[data-catalog-region]");
    anchor?.focus({ preventScroll: true });
  }, []);
  const retireFocus = () => {
    const focused = document.activeElement;
    if (!root.current?.contains(focused)) return;
    // Markers stay mounted during ordinary transitions. Slide actions and every
    // roster retirement use the catalog anchor, including a collapse to 0/1.
    if (!focused?.closest("[data-billboard-slide]") && root.current.dataset.roster === items.map((item) => item.id).join("|")) return;
    const anchor = root.current.closest<HTMLElement>("[data-catalog-region]");
    anchor?.focus({ preventScroll: true });
  };
  const rotation = useRotatingFeature(items, { beforeRetire: retireFocus, canRotate: () => !root.current?.contains(document.activeElement) });
  const { featured, index, setIndex, setPaused, paused, userPaused, setUserPaused, direction, source, countdownGeneration } = rotation;
  if (!featured) return null;
  return <div ref={root} className={`${className} billboard-stack`} data-roster={rotation.items.map((item) => item.id).join("|")} style={{ "--billboard-rotation-duration": `${ROTATION_INTERVAL}ms` } as React.CSSProperties} data-motion-source={source} data-motion-direction={direction} data-rotation-paused={paused} onFocusCapture={() => setPaused(true)} onBlurCapture={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setPaused(false); }}>
    {rotation.items.map((movie) => <div key={movie.id} className="billboard-measurement" aria-hidden="true" inert>{render(movie, true)}</div>)}
    <AnimatePresence mode="sync" initial={false} custom={direction}><Slide key={featured.id} theme={theme} direction={direction}>{render(featured, false)}</Slide></AnimatePresence>
    {rotation.items.length > 1 && <div className="billboard-controls"><button type="button" className="billboard-pause" aria-label="Pause automatic rotation" aria-pressed={userPaused} onClick={() => setUserPaused((current) => !current)}>{userPaused ? "Rotation paused" : "Rotation running"}</button><div className={paginationClassName} aria-label="Featured media">{rotation.items.map((movie, itemIndex) => <button type="button" key={movie.id} data-active={itemIndex === index} aria-current={itemIndex === index ? "true" : undefined} onClick={() => setIndex(itemIndex)} aria-label={`Show ${movie.title}`}>{itemIndex === index && <span className="billboard-progress" key={countdownGeneration} aria-hidden="true" />}</button>)}</div></div>}
  </div>;
}
