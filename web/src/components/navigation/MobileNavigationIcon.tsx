import React from "react";

export type MobileNavigationView = "home" | "movies" | "series" | "watchlist" | "downloads" | "search" | "admin";

export function MobileNavigationIcon({ view }: { view: MobileNavigationView }) {
  const paths: Record<MobileNavigationView, React.ReactNode> = {
    home: <><path d="m3.5 10.5 8.5-7 8.5 7" /><path d="M5.5 9.2V21h13V9.2" /><path d="M9.5 21v-6h5v6" /></>,
    movies: <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="M7 5v14M17 5v14M3 9h4m10 0h4M3 15h4m10 0h4" /></>,
    series: <><rect x="4" y="4" width="16" height="12" rx="2" /><path d="m10 8 5 2-5 2V8ZM8 20h8" /></>,
    watchlist: <path d="M7 4.75A1.75 1.75 0 0 1 8.75 3h6.5A1.75 1.75 0 0 1 17 4.75V21l-5-3-5 3V4.75Z" />,
    downloads: <><path d="M12 3v11" /><path d="m8 10 4 4 4-4" /><path d="M5 20h14" /></>,
    search: <><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" /></>,
    admin: <><path d="M12 3 5 6v5c0 4.7 2.8 8.1 7 10 4.2-1.9 7-5.3 7-10V6l-7-3Z" /><path d="m9 12 2 2 4-4" /></>,
  };

  return <svg className="mobile-navigation-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[view]}</svg>;
}
