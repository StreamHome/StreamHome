import React, { useLayoutEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useLocation, useNavigate } from "react-router-dom";
import { BrandLogo } from "../../components/brand/BrandLogo";
import { AdminSelect } from "../../components/ui/AdminSelect";
import { CONTENT_REVEAL, MOTION_EASE, MOTION_TIMINGS, resetApplicationScroll, useAppMotion } from "../../motion/motionSystem";
import { appUrl, parseAppQuery, type AdminSection, type StorageView } from "../../navigation/queryState";
import { useViewScrollToTop } from "../../navigation/useViewScrollToTop";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import { getThemeDefinition } from "../../themes/application/themeRegistry";
import { AccountPanel } from "./panels/AccountPanel";
import { ApiKeysPanel } from "./panels/ApiKeysPanel";
import { DownloadsPanel } from "./panels/DownloadsPanel";
import { RecommendationsPanel } from "./panels/RecommendationsPanel";
import { StoragePanel, type StorageDraft } from "./panels/StoragePanel";
import { UpdatesPanel } from "./panels/UpdatesPanel";
import { ProfileDataPanel } from "./panels/ProfileDataPanel";

const PANELS: Array<{ id: AdminSection; label: string }> = [
  { id: "account", label: "Account & Security" },
  { id: "api-keys", label: "API Keys" },
  { id: "profiles", label: "Profile data" },
  { id: "recommendations", label: "Recommendations" },
  { id: "storage", label: "Storage & HEVC" },
  { id: "downloads", label: "Downloads" },
  { id: "updates", label: "Updates" },
];

export function AdminCenter() {
  const navigate = useNavigate();
  const navRef = useRef<HTMLElement>(null);
  const [navHeight, setNavHeight] = useState(96);
  // Ephemeral edits belong to this admin visit, never to browser authentication.
  const [storageDraft, setStorageDraft] = useState<StorageDraft | undefined>();
  const [genreDrafts, setGenreDrafts] = useState<Record<string, string | undefined>>({});
  useLayoutEffect(() => {
    // Browser history restoration otherwise runs after the section scroll reset.
    const previous = window.history.scrollRestoration;
    window.history.scrollRestoration = "manual";
    return () => { window.history.scrollRestoration = previous; };
  }, []);
  useLayoutEffect(() => {
    const measure = () => { const height = navRef.current?.getBoundingClientRect().height; if (height) setNavHeight(height); };
    measure();
    if (typeof ResizeObserver === "undefined" || !navRef.current) return;
    const observer = new ResizeObserver(measure); observer.observe(navRef.current);
    return () => observer.disconnect();
  }, []);
  const location = useLocation();
  const profile = useProfileStore((state) => state.activeProfile)!;
  const profiles = useProfileStore((state) => state.profiles);
  const theme = useThemeStore((state) => state.activeTheme);
  const definition = getThemeDefinition(theme);
  const query = useMemo(() => parseAppQuery(location.search), [location.search]);
  const section = query.section ?? "account";
  useViewScrollToTop(section);
  const previousStorageView = useRef(query.storageView);
  useLayoutEffect(() => {
    if (section === "storage" && previousStorageView.current !== query.storageView) resetApplicationScroll();
    previousStorageView.current = query.storageView;
  }, [section, query.storageView]);
  const Background = definition.Background;
  const { reduced } = useAppMotion();
  const subjectProfile = profiles.find((item) => item.id === query.adminProfile) ?? profile;
  const profileAware = section === "profiles" || section === "recommendations";
  const select = (next: AdminSection) => navigate(appUrl(profile.id, "admin", { section: next, adminProfile: subjectProfile.id }));
  const selectStorageView = (storageView: StorageView) => navigate(appUrl(profile.id, "admin", { section: "storage", adminProfile: query.adminProfile, storageView }));
  const selectSubject = (subjectId: string) => navigate(appUrl(profile.id, "admin", { section, adminProfile: subjectId, adminCategory: query.adminCategory }));

  return (
    <div className={`theme-app admin-shell ${definition.shellClass}`} data-theme={theme} data-section={section} data-interaction={definition.interaction.id} style={{ "--admin-nav-height": `${navHeight + 16}px` } as React.CSSProperties}>
      <Background />
      <motion.header ref={navRef} className="admin-nav" initial={{ opacity: 0, y: reduced ? 0 : -14 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.viewEnter, ease: MOTION_EASE }}>
        <div className="admin-brand-block"><div className="admin-brand">
          <BrandLogo className="brand-logo--admin" showWordmark={false} />
          <div><p>STREAMHOME</p><strong className="admin-brand-title">Admin center</strong></div>
        </div></div>
        <AdminSelect className="admin-section-select" label="Admin section" value={section} onChange={(value) => select(value as AdminSection)} options={PANELS.map((panel) => ({ value: panel.id, label: panel.label }))} />
        <nav aria-label="Admin sections">
          {PANELS.map((panel) => <motion.button layout key={panel.id} type="button" data-active={section === panel.id} aria-current={section === panel.id ? "page" : undefined} onClick={() => select(panel.id)}>{panel.label}</motion.button>)}
        </nav>
        <div className="admin-nav__profile"><span>Signed in: {profile.name}</span></div>
        {profileAware && <p className="admin-inspection-context">{section === "profiles" ? "Inspecting" : "Editing"}: <strong>{subjectProfile.name}</strong>{section === "profiles" && <> · {{ overview: "Overview", history: "History", watchlist: "Watchlist", recommendations: "Recommendation data", cache: "TMDB cache", activity: "Activity" }[query.adminCategory ?? "overview"]}</>}</p>}
      </motion.header>
      <main className="admin-content">
        <div className="admin-workspace-toolbar"><button className="admin-back-link" data-reduced={reduced} type="button" onClick={() => navigate(appUrl(profile.id, "home"))}><span aria-hidden="true">←</span> Back to StreamHome</button></div>
        {profileAware && <div className="admin-subject-bar">
          <div><p>{section === "profiles" ? "INSPECTING PROFILE" : "EDITING PROFILE"}</p><span>{section === "profiles" ? "Inspect this profile’s records without entering or unlocking it." : "Edit this profile’s recommendation settings. Your viewing profile stays unchanged."}</span></div>
          <div className="admin-subject-control"><AdminSelect label="Selected profile" value={subjectProfile.id} onChange={selectSubject} options={profiles.map((item) => ({ value: item.id, label: item.name }))} /><small className="admin-subject-role">{subjectProfile.id === "1" ? "Administrator profile" : "Standard profile"}</small></div>
        </div>}
        <div hidden={section !== "storage"}>
          <StoragePanel active={section === "storage"} retainedDraft={storageDraft} onDraftChange={setStorageDraft} view={query.storageView ?? "overview"} onViewChange={selectStorageView} />
        </div>
        <AnimatePresence mode="wait" initial={false}>
          <motion.div className="admin-content__transition" key={section} variants={CONTENT_REVEAL} initial="hidden" animate="shown" exit="exit" onAnimationComplete={(animation) => {
            if (animation !== "shown") return;
            const hash = window.location.hash;
            const accountAnchor = section === "account" && /^#account-(authenticator|credentials|sessions|activity)$/.test(hash) ? document.getElementById(hash.slice(1)) : null;
            if (accountAnchor) accountAnchor.scrollIntoView({ block: "start", behavior: "auto" });
            else resetApplicationScroll();
          }}>
            {section === "account" && <AccountPanel />}
            {section === "api-keys" && <ApiKeysPanel />}
            {section === "profiles" && <ProfileDataPanel profileId={subjectProfile.id} />}
            {section === "recommendations" && <RecommendationsPanel profileId={subjectProfile.id} draft={genreDrafts[subjectProfile.id]} onDraftChange={(draft) => setGenreDrafts((previous) => ({ ...previous, [subjectProfile.id]: draft }))} />}
            {section === "downloads" && <DownloadsPanel />}
            {section === "updates" && <UpdatesPanel />}
          </motion.div>
        </AnimatePresence>
      </main>
    </div>
  );
}
