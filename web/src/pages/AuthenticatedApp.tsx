import React, { lazy, Suspense, useLayoutEffect, useMemo } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useLocation } from "react-router-dom";
import { parseAppQuery } from "../navigation/queryState";
import { MOTION_EASE, MOTION_TIMINGS, resetApplicationScroll, THEME_MOTION, useAppMotion } from "../motion/motionSystem";
import { useThemeStore } from "../stores/themeStore";
import { PlayerRouteLoading } from "./player/PlayerRouteLoading";

const AdminGate = lazy(() => import("./admin/AdminGate").then((module) => ({ default: module.AdminGate })));
const DashboardRouter = lazy(() => import("./dashboard/DashboardRouter").then((module) => ({ default: module.DashboardRouter })));
const PlayerPage = lazy(() => import("./player/PlayerPage").then((module) => ({ default: module.PlayerPage })));

export function AuthenticatedApp() {
  const location = useLocation();
  const query = useMemo(() => parseAppQuery(location.search), [location.search]);
  const theme = useThemeStore((state) => state.activeTheme);
  const { reduced } = useAppMotion();
  const surface = query.view === "watch" ? "watch" : query.view === "admin" ? "admin" : "dashboard";
  useLayoutEffect(() => { resetApplicationScroll(); }, [surface]);
  const opacityVariants = { initial: { opacity: 0 }, animate: { opacity: 1, transition: { duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.viewEnter } }, exit: { opacity: 0, transition: { duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.viewExit } } };
  return <AnimatePresence mode="sync" initial={false}>
    <motion.div
      key={surface}
      className={surface === "dashboard" ? "application-motion-surface" : "standalone-motion-view"}
      variants={reduced || surface === "watch" ? opacityVariants : THEME_MOTION[theme].view}
      custom={surface === "dashboard" ? -1 : 1}
      initial={surface === "watch" ? false : "initial"}
      animate="animate"
      exit="exit"
    >
      <Suspense fallback={surface === "watch" ? <PlayerRouteLoading /> : <div role="status" aria-live="polite" className="application-surface-loading">Loading view...</div>}>
        {surface === "watch" ? <PlayerPage /> : surface === "admin" ? <AdminGate /> : <DashboardRouter />}
      </Suspense>
    </motion.div>
  </AnimatePresence>;
}
