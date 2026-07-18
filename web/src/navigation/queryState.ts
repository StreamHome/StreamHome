export const APP_VIEWS = ["home", "movies", "series", "watchlist", "downloads", "search", "details", "watch", "admin"] as const;
export const ADMIN_SECTIONS = ["account", "storage", "downloads"] as const;
export const CATALOG_VIEWS = ["home", "movies", "series"] as const;
export const VIRTUAL_CATEGORIES = ["recommended", "all"] as const;

export type AppView = (typeof APP_VIEWS)[number];
export type AdminSection = (typeof ADMIN_SECTIONS)[number];
export type CatalogView = (typeof CATALOG_VIEWS)[number];
export type VirtualCategory = (typeof VIRTUAL_CATEGORIES)[number];

export interface AppQueryState {
  profile: string;
  view: AppView;
  media?: string;
  genre?: string;
  season?: number;
  q?: string;
  section?: AdminSection;
}

const viewSet = new Set<string>(APP_VIEWS);
const adminSectionSet = new Set<string>(ADMIN_SECTIONS);
const catalogViewSet = new Set<string>(CATALOG_VIEWS);

export function isCatalogView(view: AppView): view is CatalogView {
  return catalogViewSet.has(view);
}

export function preservedCatalogCategory(state: AppQueryState, nextView: AppView): Pick<AppQueryState, "genre"> {
  return isCatalogView(state.view) && isCatalogView(nextView) && state.genre ? { genre: state.genre } : {};
}

function clean(value: string | null): string | undefined {
  const result = value?.trim();
  return result ? result : undefined;
}

export function parseAppQuery(input: string | URLSearchParams): AppQueryState {
  const params = typeof input === "string" ? new URLSearchParams(input) : input;
  const profile = clean(params.get("profile")) ?? "";
  const requestedView = clean(params.get("view"));
  let view: AppView = requestedView && viewSet.has(requestedView) ? requestedView as AppView : "home";
  const media = clean(params.get("media"));

  if ((view === "details" || view === "watch") && !media) view = "home";

  const state: AppQueryState = { profile, view };
  if ((view === "details" || view === "watch") && media) state.media = media;

  if (isCatalogView(view)) {
    const genre = clean(params.get("genre"));
    if (genre) state.genre = genre;
  }

  if (view === "details") {
    const season = Number(params.get("season"));
    if (Number.isInteger(season) && season > 0) state.season = season;
  }

  if (view === "search") {
    const q = clean(params.get("q"));
    if (q) state.q = q;
  }

  if (view === "admin") {
    const section = clean(params.get("section"));
    state.section = section && adminSectionSet.has(section) ? section as AdminSection : "account";
  }

  return state;
}

export function appSearch(state: AppQueryState): string {
  const params = new URLSearchParams();
  if (state.profile) params.set("profile", state.profile);
  params.set("view", state.view);
  if ((state.view === "details" || state.view === "watch") && state.media) params.set("media", state.media);
  if (isCatalogView(state.view) && state.genre) params.set("genre", state.genre);
  if (state.view === "details" && state.season) params.set("season", String(state.season));
  if (state.view === "search" && state.q) params.set("q", state.q);
  if (state.view === "admin") params.set("section", state.section ?? "account");
  return `?${params.toString()}`;
}

export function appUrl(profile: string, view: AppView = "home", options: Omit<Partial<AppQueryState>, "profile" | "view"> = {}): string {
  return `/${appSearch(parseAppQuery(new URLSearchParams(appSearch({ profile, view, ...options }))))}`;
}

export function canonicalAppUrl(input: string | URLSearchParams): string {
  return `/${appSearch(parseAppQuery(input))}`;
}

export function withProfile(state: AppQueryState, profile: string): AppQueryState {
  return { ...state, profile };
}
