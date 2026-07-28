import { describe, expect, it } from "vitest";

import { languageDisplayName, normalizeLanguageTag } from "./language";

describe("player language tags", () => {
  it("canonicalizes reported legacy values without restricting arbitrary valid tags", () => {
    expect(normalizeLanguageTag("eng")).toBe("en");
    expect(normalizeLanguageTag("SPA")).toBe("es");
    expect(normalizeLanguageTag("fre")).toBe("fr");
    expect(normalizeLanguageTag("tur")).toBe("tr");
    expect(normalizeLanguageTag("pt_BR")).toBe("pt-br");
    expect(normalizeLanguageTag("zh-Hant-TW")).toBe("zh-hant-tw");
  });

  it("uses the browser language database instead of a fixed display-name list", () => {
    expect(languageDisplayName("es", "ES").toLowerCase()).toContain("span");
    expect(languageDisplayName("fr", "FR").toLowerCase()).toContain("french");
  });
});
