import { describe, expect, it } from "vitest";
import { appSearch, appUrl, canonicalAppUrl, parseAppQuery } from "./queryState";

describe("query-state navigation", () => {
  it("parses supported application state", () => {
    expect(parseAppQuery("?profile=1&view=details&media=tv_9&season=2&junk=yes")).toEqual({
      profile: "1", view: "details", media: "tv_9", season: 2,
    });
  });

  it("falls back to home when the view or required media is invalid", () => {
    expect(parseAppQuery("?profile=1&view=unknown")).toEqual({ profile: "1", view: "home" });
    expect(parseAppQuery("?profile=1&view=watch")).toEqual({ profile: "1", view: "home" });
  });

  it("keeps only parameters that apply to the active view", () => {
    const state = parseAppQuery("?profile=1&view=series&genre=Science%20Fiction&media=m_1&q=nope&season=3");
    expect(state).toEqual({ profile: "1", view: "series", genre: "Science Fiction" });
    expect(appSearch(state)).toBe("?profile=1&view=series&genre=Science+Fiction");
  });

  it("generates deterministic app URLs", () => {
    expect(appUrl("profile one", "search", { q: "dark city" })).toBe("/?profile=profile+one&view=search&q=dark+city");
    expect(appUrl("1", "watchlist")).toBe("/?profile=1&view=watchlist");
    expect(canonicalAppUrl("?junk=1&view=admin&profile=1&section=invalid")).toBe("/?profile=1&view=admin&section=account");
  });
});
