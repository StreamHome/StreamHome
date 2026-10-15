const LANGUAGE_ALIASES: Record<string, string> = {
  eng: "en",
  english: "en",
  spa: "es",
  spanish: "es",
  fra: "fr",
  fre: "fr",
  french: "fr",
  tur: "tr",
  turkish: "tr",
};

export function normalizeLanguageTag(value: string | null | undefined, fallback = "und"): string {
  const cleaned = String(value ?? "").trim().replace(/_/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "").toLowerCase();
  const aliased = LANGUAGE_ALIASES[cleaned] ?? cleaned;
  if (!aliased || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(aliased)) return fallback;
  try {
    return Intl.getCanonicalLocales(aliased)[0]?.toLowerCase() ?? aliased;
  } catch {
    return aliased;
  }
}

export function languageDisplayName(language: string, suppliedLabel = ""): string {
  const normalized = normalizeLanguageTag(language);
  try {
    const displayName = new Intl.DisplayNames(["en"], { type: "language" }).of(normalized);
    if (displayName) {
      const label = suppliedLabel.trim();
      if (!label || normalizeLanguageTag(label, "") === normalized || label.toLowerCase() === displayName.toLowerCase()) return displayName;
      return label.toLowerCase().includes(displayName.toLowerCase()) ? label : `${displayName} · ${label}`;
    }
  } catch {
    // Older browsers retain the supplied server label or neutral language tag.
  }
  return suppliedLabel.trim() || normalized.toUpperCase();
}
