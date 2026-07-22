import React, { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { EmberDashboard } from "../../pages/dashboard/ember/EmberDashboard";
import { LegacyThemeAdapter } from "../../pages/dashboard/ThemeDashboard";
import type { ThemeId } from "../../types/theme";
import { avatarPresetBackground } from "../../profile/profileAppearance";
import { AuroraBackground } from "../aurora/AuroraBackground";
import { CinemaBackground } from "../cinema/CinemaBackground";
import { ScanLines } from "../ember/ScanLines";
import { GeminiBackground } from "../gemini/GeminiBackground";
import type { ThemeApplicationProps, ThemeInteractionProfile, ThemeNavigationProps, ThemePresentation } from "./contracts";
import { MOTION_EASE, MOTION_TIMINGS, THEME_MOTION, useAppMotion, type MotionPreference } from "../../motion/motionSystem";
import { BrandLogo } from "../../components/brand/BrandLogo";
import { MobileNavigationIcon, type MobileNavigationView } from "../../components/navigation/MobileNavigationIcon";
export type { ThemeNavigationProps } from "./contracts";

const NAV_ITEMS: Array<{ view: MobileNavigationView; label: string }> = [
  { view: "home", label: "Home" },
  { view: "movies", label: "Movies" },
  { view: "series", label: "Series" },
  { view: "watchlist", label: "My List" },
  { view: "downloads", label: "Downloads" },
];

function SearchForm({ initial, onSearch, compact = false }: { initial?: string; onSearch: (query: string) => void; compact?: boolean }) {
  const [value, setValue] = useState(initial ?? "");
  useEffect(() => setValue(initial ?? ""), [initial]);
  return <form className={`theme-search${compact ? " theme-search--compact" : ""}`} onSubmit={(event) => { event.preventDefault(); onSearch(value.trim()); }}><input aria-label="Search server catalog" value={value} onChange={(event) => setValue(event.target.value)} placeholder="Search catalog" /><button type="submit" aria-label="Submit search">Search</button></form>;
}

type ProfileMenuPlacement = "bottom-end" | "top-start";

function ProfileControl({ profile, onProfiles, onEditProfile, onLogout, placement = "bottom-end" }: Pick<ThemeNavigationProps, "profile" | "onProfiles" | "onEditProfile" | "onLogout"> & { placement?: ProfileMenuPlacement }) {
  const root = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const { preference: motionPreference, setPreference: setMotionPreference } = useAppMotion();
  const opensAbove = placement === "top-start";
  const motionOptions: Array<{ value: MotionPreference; label: string }> = [
    { value: "full", label: "Full" },
    { value: "system", label: "System" },
    { value: "reduced", label: "Reduced" },
  ];

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", escape); };
  }, [open]);

  return <div ref={root} className="theme-profile-menu"><button className="theme-profile-control" onClick={() => setOpen((value) => !value)} aria-haspopup="menu" aria-expanded={open} aria-label={`Open settings for ${profile.name}`}><span style={{ background: avatarPresetBackground(profile) }} /><b>{profile.name}</b></button><AnimatePresence>{open && <motion.div key="profile-menu" className="theme-profile-menu__panel" data-placement={placement} role="menu" initial={{ opacity: 0, y: opensAbove ? 5 : -5, scale: .99 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: opensAbove ? 3 : -3, scale: .995 }} transition={{ duration: MOTION_TIMINGS.menu, ease: MOTION_EASE }}><strong>{profile.name}</strong><small>{profile.id === "1" ? "Administrator" : "Profile"}</small><div className="theme-profile-menu__motion" role="group" aria-label="Animation preference"><span>Motion</span><div>{motionOptions.map((option) => <button key={option.value} type="button" role="menuitemradio" aria-checked={motionPreference === option.value} onClick={() => setMotionPreference(option.value)}>{option.label}</button>)}</div></div><motion.button role="menuitem" initial={{ opacity: 0, x: 4 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: .02, duration: MOTION_TIMINGS.menuItem, ease: MOTION_EASE }} onClick={() => { setOpen(false); onEditProfile(); }}>Edit profile</motion.button><motion.button role="menuitem" initial={{ opacity: 0, x: 4 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: .04, duration: MOTION_TIMINGS.menuItem, ease: MOTION_EASE }} onClick={onProfiles}>Switch profile</motion.button><motion.button role="menuitem" initial={{ opacity: 0, x: 4 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: .06, duration: MOTION_TIMINGS.menuItem, ease: MOTION_EASE }} onClick={onLogout}>Sign out</motion.button></motion.div>}</AnimatePresence></div>;
}

function MobileCatalogNav({ activeView, onView, isAdmin }: Pick<ThemeNavigationProps, "activeView" | "onView" | "isAdmin">) {
  const items: Array<{ view: MobileNavigationView; label: string }> = [...NAV_ITEMS, { view: "search", label: "Search" }];
  if (isAdmin) items.push({ view: "admin", label: "Admin" });
  return <nav className="mobile-app-nav" aria-label="Catalog mobile">{items.map((item) => <button key={item.view} type="button" data-active={activeView === item.view} aria-label={item.label} aria-current={activeView === item.view ? "page" : undefined} onClick={() => onView(item.view)}><MobileNavigationIcon view={item.view} /></button>)}</nav>;
}

function EmberNavigation(props: ThemeNavigationProps) {
  return <><header className="theme-nav theme-nav--ember"><button className="theme-brand" onClick={() => props.onView("home")}><BrandLogo className="brand-logo--nav" /></button><nav aria-label="Catalog">{NAV_ITEMS.map((item) => <button key={item.view} data-active={props.activeView === item.view} onClick={() => props.onView(item.view)}>{item.label}</button>)}</nav><div className="theme-nav__tools"><SearchForm initial={props.query} onSearch={props.onSearch} compact />{props.isAdmin && <button onClick={props.onAdmin}>Admin</button>}<ProfileControl profile={props.profile} onEditProfile={props.onEditProfile} onProfiles={props.onProfiles} onLogout={props.onLogout} /></div></header><MobileCatalogNav activeView={props.activeView} onView={props.onView} isAdmin={props.isAdmin} /></>;
}

function AuroraNavigation(props: ThemeNavigationProps) {
  return <><header className="theme-nav theme-nav--aurora"><div className="aurora-pill"><button className="theme-brand" onClick={() => props.onView("home")}><BrandLogo className="brand-logo--nav" /></button><nav aria-label="Catalog">{NAV_ITEMS.map((item) => <button key={item.view} data-active={props.activeView === item.view} onClick={() => props.onView(item.view)}>{item.label}</button>)}</nav><SearchForm initial={props.query} onSearch={props.onSearch} compact />{props.isAdmin && <button className="nav-admin" onClick={props.onAdmin}>Admin</button>}<ProfileControl profile={props.profile} onEditProfile={props.onEditProfile} onProfiles={props.onProfiles} onLogout={props.onLogout} /></div></header><MobileCatalogNav activeView={props.activeView} onView={props.onView} isAdmin={props.isAdmin} /></>;
}

function CinemaNavigation(props: ThemeNavigationProps) {
  const [scrolled, setScrolled] = useState(() => typeof window !== "undefined" && window.scrollY > 24);
  useEffect(() => {
    const update = () => setScrolled(window.scrollY > 24);
    update();
    window.addEventListener("scroll", update, { passive: true });
    return () => window.removeEventListener("scroll", update);
  }, []);
  return <><header className="theme-nav theme-nav--cinema" data-scrolled={scrolled}><button className="theme-brand" onClick={() => props.onView("home")}><BrandLogo className="brand-logo--nav" /></button><nav aria-label="Catalog">{NAV_ITEMS.map((item) => <button key={item.view} data-active={props.activeView === item.view} onClick={() => props.onView(item.view)}>{item.label}</button>)}</nav><div className="theme-nav__tools"><SearchForm initial={props.query} onSearch={props.onSearch} compact />{props.isAdmin && <button onClick={props.onAdmin}>Admin</button>}<ProfileControl profile={props.profile} onEditProfile={props.onEditProfile} onProfiles={props.onProfiles} onLogout={props.onLogout} /></div></header><MobileCatalogNav activeView={props.activeView} onView={props.onView} isAdmin={props.isAdmin} /></>;
}

function GeminiNavigation(props: ThemeNavigationProps) {
  const [collapsed, setCollapsed] = useState(false);
  return <><aside className="theme-nav theme-nav--gemini" data-collapsed={collapsed}><div className="gemini-brand-row"><button className="theme-brand" onClick={() => props.onView("home")}><BrandLogo className="brand-logo--nav" showWordmark={!collapsed} /></button><button className="gemini-collapse" onClick={() => setCollapsed((value) => !value)} aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}>☰</button></div><nav aria-label="Catalog">{NAV_ITEMS.map((item) => <button key={item.view} data-active={props.activeView === item.view} onClick={() => props.onView(item.view)}><i aria-hidden="true" />{!collapsed && <span>{item.label}</span>}</button>)}{props.isAdmin && <button data-active={props.activeView === "admin"} onClick={props.onAdmin}><i aria-hidden="true" />{!collapsed && <span>Admin</span>}</button>}</nav>{!collapsed && <SearchForm initial={props.query} onSearch={props.onSearch} />}<div className="gemini-profile"><ProfileControl placement="top-start" profile={props.profile} onEditProfile={props.onEditProfile} onProfiles={props.onProfiles} onLogout={props.onLogout} /></div></aside><header className="gemini-mobile-nav"><button className="theme-brand" onClick={() => props.onView("home")}><BrandLogo className="brand-logo--nav" /></button><ProfileControl profile={props.profile} onEditProfile={props.onEditProfile} onProfiles={props.onProfiles} onLogout={props.onLogout} /></header><MobileCatalogNav activeView={props.activeView} onView={props.onView} isAdmin={props.isAdmin} /></>;
}

function EmberBackdrop() { return <ScanLines />; }

export interface ThemeDefinition extends ThemePresentation { Application: React.ComponentType<ThemeApplicationProps>; }

const THEME_INTERACTIONS: Record<ThemeId, ThemeInteractionProfile> = {
  ember: { id: "terminal", action: "edge-grow", navigation: "signal-line", card: "technical-tilt", rail: "square", timing: { enterMs: 170, exitMs: 290, pressMs: 90 }, easing: "cubic-bezier(.16,1,.3,1)" },
  aurora: { id: "editorial", action: "soft-bloom", navigation: "glass-pill", card: "floating-glass", rail: "orb", timing: { enterMs: 260, exitMs: 440, pressMs: 120 }, easing: "cubic-bezier(.22,1,.36,1)" },
  cinema: { id: "cinematic", action: "theatrical-grow", navigation: "spotlight-underline", card: "poster-depth", rail: "cinema-disc", timing: { enterMs: 230, exitMs: 360, pressMs: 100 }, easing: "cubic-bezier(.12,.78,.18,1)" },
  gemini: { id: "workspace", action: "modular-lift", navigation: "directional-rail", card: "module-elevation", rail: "rounded-module", timing: { enterMs: 190, exitMs: 310, pressMs: 90 }, easing: "cubic-bezier(.2,.8,.2,1)" },
};

export const THEME_DEFINITIONS: Record<ThemeId, ThemeDefinition> = {
  ember: { id: "ember", label: "Ember", Application: EmberDashboard, Background: EmberBackdrop, Navigation: EmberNavigation, shellClass: "theme-app--ember", heroVariant: "terminal", browseVariant: "technical", cardVariant: "sharp", detailsVariant: "terminal", playerVariant: "terminal", interaction: THEME_INTERACTIONS.ember, motion: THEME_MOTION.ember },
  aurora: { id: "aurora", label: "Aurora", Application: LegacyThemeAdapter, Background: AuroraBackground, Navigation: AuroraNavigation, shellClass: "theme-app--aurora", heroVariant: "editorial", browseVariant: "masonry", cardVariant: "glass", detailsVariant: "editorial", playerVariant: "minimal", interaction: THEME_INTERACTIONS.aurora, motion: THEME_MOTION.aurora },
  cinema: { id: "cinema", label: "Cinema", Application: LegacyThemeAdapter, Background: CinemaBackground, Navigation: CinemaNavigation, shellClass: "theme-app--cinema", heroVariant: "cinematic", browseVariant: "rails", cardVariant: "poster", detailsVariant: "cinematic", playerVariant: "cinematic", interaction: THEME_INTERACTIONS.cinema, motion: THEME_MOTION.cinema },
  gemini: { id: "gemini", label: "Gemini", Application: LegacyThemeAdapter, Background: GeminiBackground, Navigation: GeminiNavigation, shellClass: "theme-app--gemini", heroVariant: "workspace", browseVariant: "modules", cardVariant: "module", detailsVariant: "workspace", playerVariant: "workspace", interaction: THEME_INTERACTIONS.gemini, motion: THEME_MOTION.gemini },
};

export function getThemeDefinition(id: ThemeId): ThemeDefinition { return THEME_DEFINITIONS[id] ?? THEME_DEFINITIONS.ember; }
