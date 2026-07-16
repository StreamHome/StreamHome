import React, { useEffect, useRef, useState } from "react";
import { ProfileSettingsDialog } from "../../components/profile/ProfileSettingsDialog";
import type { AppView } from "../../navigation/queryState";
import { EmberDashboard } from "../../pages/dashboard/ember/EmberDashboard";
import { LegacyThemeAdapter } from "../../pages/dashboard/ThemeDashboard";
import type { ThemeId } from "../../types/theme";
import { avatarBackground } from "../../utils/media";
import { AuroraBackground } from "../aurora/AuroraBackground";
import { CinemaBackground } from "../cinema/CinemaBackground";
import { EmberBackground } from "../ember/EmberBackground";
import { ScanLines } from "../ember/ScanLines";
import { GeminiBackground } from "../gemini/GeminiBackground";
import type { ThemeApplicationProps, ThemeNavigationProps, ThemePresentation } from "./contracts";
export type { ThemeNavigationProps } from "./contracts";

const NAV_ITEMS: Array<{ view: AppView; label: string }> = [
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

function ProfileControl({ profile, onProfiles, onLogout }: Pick<ThemeNavigationProps, "profile" | "onProfiles" | "onLogout">) {
  const root = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", escape); };
  }, [open]);

  return <div ref={root} className="theme-profile-menu"><button className="theme-profile-control" onClick={() => setOpen((value) => !value)} aria-haspopup="menu" aria-expanded={open} aria-label={`Open settings for ${profile.name}`}><span style={{ background: avatarBackground(profile) }} /><b>{profile.name}</b><i aria-hidden="true">⌄</i></button>{open && <div className="theme-profile-menu__panel" role="menu"><strong>{profile.name}</strong><small>{profile.id === "1" ? "Administrator" : "Profile"}</small><button role="menuitem" onClick={() => { setOpen(false); setEditing(true); }}>Edit profile</button><button role="menuitem" onClick={onProfiles}>Switch profile</button><button role="menuitem" onClick={onLogout}>Sign out</button></div>}{editing && <ProfileSettingsDialog profile={profile} onClose={() => setEditing(false)} onDeleted={onProfiles} />}</div>;
}

function MobileCatalogNav({ activeView, onView, isAdmin }: Pick<ThemeNavigationProps, "activeView" | "onView" | "isAdmin">) {
  const items: Array<{ view: AppView; label: string }> = [...NAV_ITEMS, { view: "search", label: "Search" }];
  if (isAdmin) items.push({ view: "admin", label: "Admin" });
  return <nav className="mobile-app-nav" aria-label="Catalog mobile">{items.map((item) => <button key={item.view} data-active={activeView === item.view} onClick={() => onView(item.view)}>{item.label}</button>)}</nav>;
}

function EmberNavigation(props: ThemeNavigationProps) {
  return <><header className="theme-nav theme-nav--ember"><button className="theme-brand" onClick={() => props.onView("home")}><span>STREAMHOME</span><small>CATALOG TERMINAL</small></button><nav aria-label="Catalog">{NAV_ITEMS.map((item) => <button key={item.view} data-active={props.activeView === item.view} onClick={() => props.onView(item.view)}>{item.label}</button>)}</nav><div className="theme-nav__tools"><SearchForm initial={props.query} onSearch={props.onSearch} compact />{props.isAdmin && <button onClick={props.onAdmin}>Admin</button>}<ProfileControl profile={props.profile} onProfiles={props.onProfiles} onLogout={props.onLogout} /></div></header><MobileCatalogNav activeView={props.activeView} onView={props.onView} isAdmin={props.isAdmin} /></>;
}

function AuroraNavigation(props: ThemeNavigationProps) {
  return <><header className="theme-nav theme-nav--aurora"><div className="aurora-pill"><button className="theme-brand" onClick={() => props.onView("home")}>STREAMHOME</button><nav aria-label="Catalog">{NAV_ITEMS.map((item) => <button key={item.view} data-active={props.activeView === item.view} onClick={() => props.onView(item.view)}>{item.label}</button>)}</nav><SearchForm initial={props.query} onSearch={props.onSearch} compact />{props.isAdmin && <button className="nav-admin" onClick={props.onAdmin}>Admin</button>}<ProfileControl profile={props.profile} onProfiles={props.onProfiles} onLogout={props.onLogout} /></div></header><MobileCatalogNav activeView={props.activeView} onView={props.onView} isAdmin={props.isAdmin} /></>;
}

function CinemaNavigation(props: ThemeNavigationProps) {
  return <><header className="theme-nav theme-nav--cinema"><button className="theme-brand" onClick={() => props.onView("home")}>STREAMHOME</button><nav aria-label="Catalog">{NAV_ITEMS.map((item) => <button key={item.view} data-active={props.activeView === item.view} onClick={() => props.onView(item.view)}>{item.label}</button>)}</nav><div className="theme-nav__tools"><SearchForm initial={props.query} onSearch={props.onSearch} compact />{props.isAdmin && <button onClick={props.onAdmin}>Admin</button>}<ProfileControl profile={props.profile} onProfiles={props.onProfiles} onLogout={props.onLogout} /></div></header><MobileCatalogNav activeView={props.activeView} onView={props.onView} isAdmin={props.isAdmin} /></>;
}

function GeminiNavigation(props: ThemeNavigationProps) {
  const [collapsed, setCollapsed] = useState(false);
  return <><aside className="theme-nav theme-nav--gemini" data-collapsed={collapsed}><div className="gemini-brand-row"><button className="theme-brand" onClick={() => props.onView("home")}>{collapsed ? "S" : "STREAMHOME"}</button><button className="gemini-collapse" onClick={() => setCollapsed((value) => !value)} aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}>☰</button></div><nav aria-label="Catalog">{NAV_ITEMS.map((item) => <button key={item.view} data-active={props.activeView === item.view} onClick={() => props.onView(item.view)}><i aria-hidden="true" />{!collapsed && <span>{item.label}</span>}</button>)}{props.isAdmin && <button data-active={props.activeView === "admin"} onClick={props.onAdmin}><i aria-hidden="true" />{!collapsed && <span>Admin</span>}</button>}</nav>{!collapsed && <SearchForm initial={props.query} onSearch={props.onSearch} />}<div className="gemini-profile"><ProfileControl profile={props.profile} onProfiles={props.onProfiles} onLogout={props.onLogout} /></div></aside><header className="gemini-mobile-nav"><button className="theme-brand" onClick={() => props.onView("home")}>STREAMHOME</button><ProfileControl profile={props.profile} onProfiles={props.onProfiles} onLogout={props.onLogout} /></header><MobileCatalogNav activeView={props.activeView} onView={props.onView} isAdmin={props.isAdmin} /></>;
}

function EmberBackdrop() { return <><EmberBackground suspendWhenHidden respectReducedMotion /><ScanLines /></>; }

export interface ThemeDefinition extends ThemePresentation { Application: React.ComponentType<ThemeApplicationProps>; }

export const THEME_DEFINITIONS: Record<ThemeId, ThemeDefinition> = {
  ember: { id: "ember", label: "Ember", Application: EmberDashboard, Background: EmberBackdrop, Navigation: EmberNavigation, shellClass: "theme-app--ember", heroVariant: "terminal", browseVariant: "technical", cardVariant: "sharp", detailsVariant: "terminal", playerVariant: "terminal" },
  aurora: { id: "aurora", label: "Aurora", Application: LegacyThemeAdapter, Background: AuroraBackground, Navigation: AuroraNavigation, shellClass: "theme-app--aurora", heroVariant: "editorial", browseVariant: "masonry", cardVariant: "glass", detailsVariant: "editorial", playerVariant: "minimal" },
  cinema: { id: "cinema", label: "Cinema", Application: LegacyThemeAdapter, Background: CinemaBackground, Navigation: CinemaNavigation, shellClass: "theme-app--cinema", heroVariant: "cinematic", browseVariant: "rails", cardVariant: "poster", detailsVariant: "cinematic", playerVariant: "cinematic" },
  gemini: { id: "gemini", label: "Gemini", Application: LegacyThemeAdapter, Background: GeminiBackground, Navigation: GeminiNavigation, shellClass: "theme-app--gemini", heroVariant: "workspace", browseVariant: "modules", cardVariant: "module", detailsVariant: "workspace", playerVariant: "workspace" },
};

export function getThemeDefinition(id: ThemeId): ThemeDefinition { return THEME_DEFINITIONS[id] ?? THEME_DEFINITIONS.ember; }
