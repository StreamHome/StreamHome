import React, { useLayoutEffect, useMemo } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useLocation } from "react-router-dom";
import { parseAppQuery } from "../navigation/queryState";
import { MOTION_EASE, MOTION_TIMINGS, resetApplicationScroll, THEME_MOTION, useAppMotion } from "../motion/motionSystem";
import { useThemeStore } from "../stores/themeStore";
import { AdminGate } from "./admin/AdminGate";
import { DashboardRouter } from "./dashboard/DashboardRouter";
import { PlayerPage } from "./player/PlayerPage";

export function AuthenticatedApp() {
  const location = useLocation();
  const query = useMemo(() => parseAppQuery(location.search), [location.search]);
  const theme = useThemeStore((state) => state.activeTheme);
  const { reduced } = useAppMotion();
  const surface = query.view === "watch" ? "watch" : query.view === "admin" ? "admin" : "dashboard";
  useLayoutEffect(() => { resetApplicationScroll(); }, [surface]);
  const reducedVariants = { initial: { opacity: 0 }, animate: { opacity: 1, transition: { duration: MOTION_TIMINGS.reduced } }, exit: { opacity: 0, transition: { duration: MOTION_TIMINGS.reduced } } };
  return <AnimatePresence mode="sync" initial={false}>
    <motion.div
      key={surface}
      className={surface === "dashboard" ? "application-motion-surface" : "standalone-motion-view"}
      variants={reduced ? reducedVariants : THEME_MOTION[theme].view}
      custom={surface === "dashboard" ? -1 : 1}
      initial="initial"
      animate="animate"
      exit="exit"
    >
      {surface === "watch" ? <PlayerPage /> : surface === "admin" ? <AdminGate /> : <DashboardRouter />}
    </motion.div>
  </AnimatePresence>;
}
