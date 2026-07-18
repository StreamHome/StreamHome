import React, { useEffect, useMemo } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useLocation } from "react-router-dom";
import { parseAppQuery } from "../navigation/queryState";
import { MOTION_EASE, MOTION_TIMINGS, resetApplicationScroll, THEME_MOTION, useAppMotion } from "../motion/motionSystem";
import { useThemeStore } from "../stores/themeStore";
import { AccountSecurityPage } from "./AccountSecurityPage";
import { AdminGate } from "./admin/AdminGate";
import { DashboardRouter } from "./dashboard/DashboardRouter";
import { PlayerPage } from "./player/PlayerPage";

export function AuthenticatedApp() {
  const location = useLocation();
  const query = useMemo(() => parseAppQuery(location.search), [location.search]);
  const theme = useThemeStore((state) => state.activeTheme);
  const { reduced } = useAppMotion();
  useEffect(() => {
    if (query.view === "watch" || query.view === "admin") resetApplicationScroll();
  }, [query.section, query.view]);
  const surface = query.view === "watch" ? "watch" : query.view === "admin" && query.section === "security" ? "security" : query.view === "admin" ? "admin" : "dashboard";
  const reducedVariants = { initial: { opacity: 0 }, animate: { opacity: 1, transition: { duration: MOTION_TIMINGS.reduced } }, exit: { opacity: 0, transition: { duration: MOTION_TIMINGS.reduced } } };
  return <AnimatePresence mode="wait" onExitComplete={resetApplicationScroll}>
    <motion.div
      key={surface}
      className={surface === "dashboard" ? "application-motion-surface" : "standalone-motion-view"}
      variants={reduced ? reducedVariants : THEME_MOTION[theme].view}
      custom={surface === "dashboard" ? -1 : 1}
      initial="initial"
      animate="animate"
      exit="exit"
    >
      {surface === "watch" ? <PlayerPage /> : surface === "security" ? <AccountSecurityPage /> : surface === "admin" ? <AdminGate /> : <DashboardRouter />}
    </motion.div>
  </AnimatePresence>;
}
