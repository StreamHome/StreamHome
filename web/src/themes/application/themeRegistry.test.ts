import { describe, expect, it } from "vitest";
import { THEME_DEFINITIONS } from "./themeRegistry";

describe("theme definition registry", () => {
  it("keeps every theme on a distinct presentation contract", () => {
    const definitions = Object.values(THEME_DEFINITIONS);
    expect(new Set(definitions.map((theme) => theme.Navigation)).size).toBe(4);
    expect(new Set(definitions.map((theme) => theme.heroVariant)).size).toBe(4);
    expect(new Set(definitions.map((theme) => theme.cardVariant)).size).toBe(4);
    expect(new Set(definitions.map((theme) => theme.detailsVariant)).size).toBe(4);
    expect(new Set(definitions.map((theme) => theme.playerVariant)).size).toBe(4);
  });
});
