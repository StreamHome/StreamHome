from __future__ import annotations

import re
from typing import Optional


SAFE_LANGUAGE_TAG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Common legacy ISO-639 and human-readable values are canonicalized for
# compatibility. Every other valid language tag passes through unchanged, so
# language support is not restricted to this alias table.
LANGUAGE_ALIASES = {
    "eng": "en",
    "english": "en",
    "spa": "es",
    "spanish": "es",
    "fra": "fr",
    "fre": "fr",
    "french": "fr",
    "tur": "tr",
    "turkish": "tr",
}


def normalize_language_tag(value: Optional[str], fallback: str = "und") -> str:
    """Return a filesystem-safe, stable BCP-47-style language tag."""

    normalized = re.sub(r"-+", "-", str(value or "").strip().lower().replace("_", "-")).strip("-")
    normalized = LANGUAGE_ALIASES.get(normalized, normalized)
    if not normalized or not SAFE_LANGUAGE_TAG_RE.fullmatch(normalized):
        return fallback
    return normalized


def language_label(language: str, supplied_label: Optional[str] = None) -> str:
    """Preserve a useful source title and otherwise expose a neutral tag label."""

    normalized = normalize_language_tag(language)
    candidate = str(supplied_label or "").strip()
    if candidate and candidate.lower().replace("_", "-") not in {normalized, str(language).lower()}:
        return candidate
    return normalized.upper()
