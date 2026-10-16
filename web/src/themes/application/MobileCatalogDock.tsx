import React from "react";
import type { MobileNavigationView, ThemeNavigationProps } from "./contracts";

const ITEMS: Array<{ view: MobileNavigationView; label: string }> = [
  { view: "home", label: "Home" },
  { view: "movies", label: "Movies" },
  { view: "series", label: "Series" },
  { view: "watchlist", label: "My List" },
  { view: "downloads", label: "Downloads" },
  { view: "search", label: "Search" },
];

function DockIcon({ view }: { view: MobileNavigationView }) {
  const shapes: Record<MobileNavigationView, React.ReactNode> = {
    home: <path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-6v-8h-4v8H4a1 1 0 0 1-1-1Z" />,
    movies: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 4v16M17 4v16M3 9h4m-4 6h4m10-6h4m-4 6h4" /></>,
    series: <><rect x="3" y="3" width="18" height="15" rx="2" /><path d="M8 22h8m-6-15 6 4-6 4Z" /></>,
    watchlist: <path d="M6 3h12v19l-6-4-6 4Z" />,
    downloads: <path d="M12 3v12m-5-5 5 5 5-5M4 21h16" />,
    search: <><circle cx="10.5" cy="10.5" r="7.5" /><path d="m16 16 5 5" /></>,
    admin: <><path d="m12 3 8 4v6c0 4-8 9-8 9s-8-5-8-9V7Z" /><path d="m8 12 3 3 5-6" /></>,
  };

  return <svg className="catalog-dock-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">{shapes[view]}</svg>;
}

export function MobileCatalogDock({ activeView, onView, isAdmin }: Pick<ThemeNavigationProps, "activeView" | "onView" | "isAdmin">) {
  const items = isAdmin ? [...ITEMS, { view: "admin" as const, label: "Admin" }] : ITEMS;
  return <footer className="catalog-dock-row"><nav className="catalog-dock" aria-label="Catalog mobile">{items.map(({ view, label }) =>
    <button key={view} type="button" aria-label={label} aria-current={activeView === view ? "page" : undefined} onClick={() => onView(view)}>
      <DockIcon view={view} /><span>{label}</span>
    </button>,
  )}</nav></footer>;
}
