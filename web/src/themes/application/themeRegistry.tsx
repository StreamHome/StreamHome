import React, { useEffect, useState } from "react";
import type { AppView } from "../../navigation/queryState";
import type { Profile } from "../../types/api";
import type { ThemeId } from "../../types/theme";
import { avatarBackground } from "../../utils/media";
import { AuroraBackground } from "../aurora/AuroraBackground";
import { CinemaBackground } from "../cinema/CinemaBackground";
import { EmberBackground } from "../ember/EmberBackground";
import { ScanLines } from "../ember/ScanLines";
import { GeminiBackground } from "../gemini/GeminiBackground";

const NAV_ITEMS: Array<{ view: AppView; label: string }> = [
  { view: "home", label: "Home" }, { view: "movies", label: "Movies" },
  { view: "series", label: "Series" }, { view: "downloads", label: "Downloads" },
];

export interface ThemeNavigationProps {
  profile: Profile;
  activeView: AppView;
  query?: string;
  isAdmin: boolean;
  onView: (view: AppView) => void;
  onSearch: (query: string) => void;
  onProfiles: () => void;
  onAdmin: () => void;
  onLogout: () => void;
}

function SearchForm({ initial, onSearch, compact = false }: { initial?: string; onSearch: (query: string) => void; compact?: boolean }) {
  const [value, setValue] = useState(initial ?? "");
  useEffect(() => setValue(initial ?? ""), [initial]);
  return <form className={`theme-search${compact ? " theme-search--compact" : ""}`} onSubmit={(event) => { event.preventDefault(); onSearch(value.trim()); }}><input aria-label="Search server catalog" value={value} onChange={(event) => setValue(event.target.value)} placeholder="Search catalog" /><button type="submit" aria-label="Submit search">Search</button></form>;
}

function ProfileControl({ profile, onProfiles }: Pick<ThemeNavigationProps, "profile" | "onProfiles">) {
  return <button className="theme-profile-control" onClick={onProfiles} aria-label={`Change profile. Current profile ${profile.name}`}><span style={{ background: avatarBackground(profile) }} /><b>{profile.name}</b></button>;
}

function MobileCatalogNav({ activeView, onView, isAdmin }: Pick<ThemeNavigationProps, "activeView" | "onView" | "isAdmin">) {
  const items: Array<{ view: AppView; label: string }> = [...NAV_ITEMS, { view: "search", label: "Search" }];
  if (isAdmin) items.push({ view: "admin", label: "Admin" });
  return <nav className="mobile-app-nav" aria-label="Catalog mobile">{items.map((item) => <button key={item.view} data-active={activeView === item.view} onClick={() => onView(item.view)}>{item.label}</button>)}</nav>;
}

function EmberNavigation(props: ThemeNavigationProps) {
  return <><header className="theme-nav theme-nav--ember"><button className="theme-brand" onClick={() => props.onView("home")}><span>STREAMHOME</span><small>CATALOG TERMINAL</small></button><nav aria-label="Catalog">{NAV_ITEMS.map((item) => <button key={item.view} data-active={props.activeView === item.view} onClick={() => props.onView(item.view)}>{item.label}</button>)}</nav><div className="theme-nav__tools"><SearchForm initial={props.query} onSearch={props.onSearch} compact />{props.isAdmin && <button onClick={props.onAdmin}>Admin</button>}<ProfileControl profile={props.profile} onProfiles={props.onProfiles} /><button onClick={props.onLogout}>Exit</button></div></header><MobileCatalogNav activeView={props.activeView} onView={props.onView} isAdmin={props.isAdmin} /></>;
}

function AuroraNavigation(props: ThemeNavigationProps) {
  return <><header className="theme-nav theme-nav--aurora"><div className="aurora-pill"><button className="theme-brand" onClick={() => props.onView("home")}>STREAMHOME</button><nav aria-label="Catalog">{NAV_ITEMS.map((item) => <button key={item.view} data-active={props.activeView === item.view} onClick={() => props.onView(item.view)}>{item.label}</button>)}</nav><SearchForm initial={props.query} onSearch={props.onSearch} compact /><ProfileControl profile={props.profile} onProfiles={props.onProfiles} /><button className="nav-exit" onClick={props.onLogout}>Sign out</button></div></header><MobileCatalogNav activeView={props.activeView} onView={props.onView} isAdmin={props.isAdmin} /></>;
}

function CinemaNavigation(props: ThemeNavigationProps) {
  return <><header className="theme-nav theme-nav--cinema"><button className="theme-brand" onClick={() => props.onView("home")}>STREAMHOME</button><nav aria-label="Catalog">{NAV_ITEMS.map((item) => <button key={item.view} data-active={props.activeView === item.view} onClick={() => props.onView(item.view)}>{item.label}</button>)}</nav><div className="theme-nav__tools"><SearchForm initial={props.query} onSearch={props.onSearch} compact />{props.isAdmin && <button onClick={props.onAdmin}>Admin</button>}<ProfileControl profile={props.profile} onProfiles={props.onProfiles} /><button onClick={props.onLogout}>Sign out</button></div></header><MobileCatalogNav activeView={props.activeView} onView={props.onView} isAdmin={props.isAdmin} /></>;
}

function GeminiNavigation(props: ThemeNavigationProps) {
  const [collapsed, setCollapsed] = useState(false);
  return <><aside className="theme-nav theme-nav--gemini" data-collapsed={collapsed}><div className="gemini-brand-row"><button className="theme-brand" onClick={() => props.onView("home")}>{collapsed ? "S" : "STREAMHOME"}</button><button className="gemini-collapse" onClick={() => setCollapsed((value) => !value)} aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}>☰</button></div><nav aria-label="Catalog">{NAV_ITEMS.map((item) => <button key={item.view} data-active={props.activeView === item.view} onClick={() => props.onView(item.view)}><i aria-hidden="true" />{!collapsed && <span>{item.label}</span>}</button>)}{props.isAdmin && <button data-active={props.activeView === "admin"} onClick={props.onAdmin}><i aria-hidden="true" />{!collapsed && <span>Admin</span>}</button>}</nav>{!collapsed && <SearchForm initial={props.query} onSearch={props.onSearch} />}<div className="gemini-profile"><ProfileControl profile={props.profile} onProfiles={props.onProfiles} />{!collapsed && <button onClick={props.onLogout}>Sign out</button>}</div></aside><header className="gemini-mobile-nav"><button className="theme-brand" onClick={() => props.onView("home")}>STREAMHOME</button><ProfileControl profile={props.profile} onProfiles={props.onProfiles} /></header><MobileCatalogNav activeView={props.activeView} onView={props.onView} isAdmin={props.isAdmin} /></>;
}

function EmberBackdrop() { return <><EmberBackground suspendWhenHidden respectReducedMotion /><ScanLines /></>; }

export interface ThemeDefinition {
  id: ThemeId;
  label: string;
  Background: React.ComponentType;
  Navigation: React.ComponentType<ThemeNavigationProps>;
  shellClass: string;
  heroVariant: "terminal" | "editorial" | "cinematic" | "workspace";
  browseVariant: "technical" | "masonry" | "rails" | "modules";
  cardVariant: "sharp" | "glass" | "poster" | "module";
  detailsVariant: "terminal" | "editorial" | "cinematic" | "workspace";
  playerVariant: "terminal" | "minimal" | "cinematic" | "workspace";
}

export const THEME_DEFINITIONS: Record<ThemeId, ThemeDefinition> = {
  ember: { id: "ember", label: "Ember", Background: EmberBackdrop, Navigation: EmberNavigation, shellClass: "theme-app--ember", heroVariant: "terminal", browseVariant: "technical", cardVariant: "sharp", detailsVariant: "terminal", playerVariant: "terminal" },
  aurora: { id: "aurora", label: "Aurora", Background: AuroraBackground, Navigation: AuroraNavigation, shellClass: "theme-app--aurora", heroVariant: "editorial", browseVariant: "masonry", cardVariant: "glass", detailsVariant: "editorial", playerVariant: "minimal" },
  cinema: { id: "cinema", label: "Cinema", Background: CinemaBackground, Navigation: CinemaNavigation, shellClass: "theme-app--cinema", heroVariant: "cinematic", browseVariant: "rails", cardVariant: "poster", detailsVariant: "cinematic", playerVariant: "cinematic" },
  gemini: { id: "gemini", label: "Gemini", Background: GeminiBackground, Navigation: GeminiNavigation, shellClass: "theme-app--gemini", heroVariant: "workspace", browseVariant: "modules", cardVariant: "module", detailsVariant: "workspace", playerVariant: "workspace" },
};

export function getThemeDefinition(id: ThemeId): ThemeDefinition { return THEME_DEFINITIONS[id] ?? THEME_DEFINITIONS.ember; }
