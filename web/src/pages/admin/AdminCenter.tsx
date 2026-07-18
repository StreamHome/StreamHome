import React, { useMemo } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useLocation, useNavigate } from "react-router-dom";
import { BrandLogo } from "../../components/brand/BrandLogo";
import { CONTENT_REVEAL, MOTION_EASE, MOTION_TIMINGS, useAppMotion } from "../../motion/motionSystem";
import { appUrl, parseAppQuery, type AdminSection } from "../../navigation/queryState";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import { getThemeDefinition } from "../../themes/application/themeRegistry";
import { AccountPanel } from "./panels/AccountPanel";
import { DownloadsPanel } from "./panels/DownloadsPanel";
import { StoragePanel } from "./panels/StoragePanel";

const PANELS: Array<{ id: AdminSection; label: string }> = [
  { id: "account", label: "Account & Security" },
  { id: "storage", label: "Storage & HEVC" },
  { id: "downloads", label: "Downloads" },
];

export function AdminCenter() {
  const navigate = useNavigate();
  const location = useLocation();
  const profile = useProfileStore((state) => state.activeProfile)!;
  const theme = useThemeStore((state) => state.activeTheme);
  const definition = getThemeDefinition(theme);
  const query = useMemo(() => parseAppQuery(location.search), [location.search]);
  const section = query.section ?? "account";
  const Background = definition.Background;
  const { reduced } = useAppMotion();
  const select = (next: AdminSection) => navigate(appUrl(profile.id, "admin", { section: next }));

  return (
    <div className={`theme-app admin-shell ${definition.shellClass}`} data-theme={theme} data-interaction={definition.interaction.id}>
      <Background />
      <motion.header className="admin-nav" initial={{ opacity: 0, y: reduced ? 0 : -14 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.viewEnter, ease: MOTION_EASE }}>
        <div className="admin-brand">
          <BrandLogo className="brand-logo--admin" showWordmark={false} />
          <div><p>STREAMHOME / CONTROL PLANE</p><h1>Admin center</h1></div>
        </div>
        <nav aria-label="Admin sections">
          {PANELS.map((panel) => <motion.button layout key={panel.id} type="button" data-active={section === panel.id} aria-current={section === panel.id ? "page" : undefined} onClick={() => select(panel.id)}>{panel.label}</motion.button>)}
        </nav>
        <div className="admin-nav__profile"><span>{profile.name}</span><button type="button" onClick={() => navigate(appUrl(profile.id, "home"))}>Exit admin</button></div>
      </motion.header>
      <main className="admin-content">
        <AnimatePresence mode="wait" initial={false}>
          <motion.div className="admin-content__transition" key={section} variants={CONTENT_REVEAL} initial="hidden" animate="shown" exit="exit">
            {section === "account" && <AccountPanel profileId={profile.id} />}
            {section === "storage" && <StoragePanel />}
            {section === "downloads" && <DownloadsPanel />}
          </motion.div>
        </AnimatePresence>
      </main>
    </div>
  );
}
