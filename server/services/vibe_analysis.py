"""Deterministic crew, trope, and subtitle-pacing analysis for Recommendation Engine V2."""

from __future__ import annotations

import asyncio
import html
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import unquote

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import engine
from models import Episode, Movie
from services.logger import logger

VIBE_ANALYSIS_VERSION = 1
_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "data" / "trope_clusters.json"
_TIMESTAMP = re.compile(r"(?P<start>\d{2}:\d{2}:\d{2}[.,]\d{3})\s+-->\s+(?P<end>\d{2}:\d{2}:\d{2}[.,]\d{3})")
_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)


def normalize_trait(value: str) -> str:
    return " ".join((value or "").strip().casefold().replace("_", " ").split())


def normalize_crew(crew: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    people: Dict[str, Dict[str, Any]] = {}
    for raw in crew or []:
        name = " ".join(str(raw.get("name") or "").split())
        role = " ".join(str(raw.get("role") or raw.get("job") or "").split())
        if not name or not role:
            continue
        key = normalize_trait(name)
        entry = people.setdefault(key, {"name": name, "roles": []})
        if role not in entry["roles"]:
            entry["roles"].append(role)
    return sorted(people.values(), key=lambda item: normalize_trait(item["name"]))


def relevant_crew(credits: Dict[str, Any], creators: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    accepted = {"director", "writer", "screenplay", "story", "teleplay", "creator"}
    rows: List[Dict[str, Any]] = []
    for person in (credits or {}).get("crew", []) or []:
        job = str(person.get("job") or "").strip()
        if normalize_trait(job) in accepted:
            rows.append({"name": person.get("name"), "role": job})
    rows.extend({"name": person.get("name"), "role": "Creator"} for person in creators or [])
    return normalize_crew(rows)


def crew_names(crew: Sequence[Dict[str, Any]], roles: Iterable[str]) -> List[str]:
    wanted = {normalize_trait(role) for role in roles}
    return [
        str(person.get("name"))
        for person in crew or []
        if person.get("name") and any(normalize_trait(role) in wanted for role in person.get("roles", []))
    ]


def _load_registry() -> Dict[str, Any]:
    try:
        with _REGISTRY_PATH.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        logger.error(f"[Vibe Analysis] Failed to load trope registry: {exc}")
        return {"version": 0, "tropes": []}


TROPE_REGISTRY = _load_registry()
TROPE_REGISTRY_VERSION = int(TROPE_REGISTRY.get("version") or 0)


def _trait_matches(term: str, traits: set[str]) -> bool:
    needle = normalize_trait(term)
    return bool(needle) and any(needle == trait or needle in trait or trait in needle for trait in traits)


def compute_trope_vectors(genres: Iterable[str], keywords: Iterable[str], description: str = "") -> List[Dict[str, Any]]:
    genre_traits = {normalize_trait(value) for value in genres or [] if normalize_trait(value)}
    keyword_traits = {normalize_trait(value) for value in keywords or [] if normalize_trait(value)}
    description_traits = {normalize_trait(value) for value in re.findall(r"[\w’'-]+", description or "") if len(value) >= 4}
    all_traits = genre_traits | keyword_traits | description_traits
    output: List[Dict[str, Any]] = []
    for trope in TROPE_REGISTRY.get("tropes", []):
        if any(_trait_matches(term, all_traits) for term in trope.get("exclusions", [])):
            continue
        required = trope.get("required_any", [])
        required_hits = sum(1 for group in required if any(_trait_matches(term, all_traits) for term in group))
        if required and required_hits < len(required):
            continue
        supporting_hits = sum(1 for term in trope.get("supporting", []) if _trait_matches(term, all_traits))
        genre_hits = sum(1 for term in trope.get("genres", []) if _trait_matches(term, genre_traits))
        confidence = min(1.0, 0.55 + required_hits * 0.12 + supporting_hits * 0.06 + genre_hits * 0.04)
        output.append({
            "id": trope["id"],
            "label": trope["label"],
            "railLabel": trope["rail_label"],
            "confidence": round(confidence, 4),
            "registryVersion": TROPE_REGISTRY_VERSION,
        })
    return sorted(output, key=lambda item: (-float(item["confidence"]), item["id"]))


def _timestamp_seconds(value: str) -> float:
    hours, minutes, seconds = value.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


@dataclass(frozen=True)
class DialogueMetrics:
    wpm: float
    word_count: int
    language: str
    confidence: float
    cue_count: int


def analyze_webvtt(path: str | Path, runtime_seconds: float, language: str = "und") -> Optional[DialogueMetrics]:
    if runtime_seconds < 60:
        return None
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    seen: set[str] = set()
    words: List[str] = []
    covered_seconds = 0.0
    cue_count = 0
    for block in re.split(r"\r?\n\s*\r?\n", content):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines or lines[0].upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        timestamp_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timestamp_index is None:
            continue
        match = _TIMESTAMP.search(lines[timestamp_index])
        if not match:
            continue
        text = html.unescape(_TAG.sub(" ", " ".join(lines[timestamp_index + 1:])))
        text = " ".join(text.split())
        dedupe = normalize_trait(text)
        if not dedupe or dedupe in seen:
            continue
        seen.add(dedupe)
        cue_words = [word for word in _WORD.findall(text) if not word.isdigit()]
        if not cue_words:
            continue
        words.extend(cue_words)
        cue_count += 1
        start, end = _timestamp_seconds(match.group("start")), _timestamp_seconds(match.group("end"))
        covered_seconds += max(0.0, min(runtime_seconds, end) - min(runtime_seconds, start))
    if len(words) < 20 or cue_count < 3:
        return None
    runtime_minutes = runtime_seconds / 60.0
    wpm = len(words) / runtime_minutes
    coverage = min(1.0, covered_seconds / max(1.0, runtime_seconds * 0.35))
    sample = min(1.0, len(words) / 300.0)
    confidence = min(1.0, 0.65 * coverage + 0.35 * sample)
    if not math.isfinite(wpm) or wpm <= 0 or confidence < 0.08:
        return None
    return DialogueMetrics(round(wpm, 3), len(words), language or "und", round(confidence, 4), cue_count)


def _runtime_seconds(value: Any, fallback: str = "") -> float:
    try:
        numeric = float(value or 0)
        if numeric > 0:
            return numeric
    except (TypeError, ValueError):
        pass
    hours = re.search(r"([\d.]+)\s*h", fallback or "", re.I)
    minutes = re.search(r"([\d.]+)\s*m", fallback or "", re.I)
    return (float(hours.group(1)) * 3600 if hours else 0.0) + (float(minutes.group(1)) * 60 if minutes else 0.0)


def _media_folder(video_url: str) -> Optional[Path]:
    value = unquote((video_url or "").replace("\\", "/"))
    if not value.startswith("/media/"):
        return None
    candidate = (Path(settings.MEDIA_DIR).resolve() / value[len("/media/"):]).resolve()
    media_root = Path(settings.MEDIA_DIR).resolve()
    try:
        candidate.relative_to(media_root)
    except ValueError:
        return None
    return candidate.parent


def _subtitle_candidate(entity: Movie | Episode, folder: Path) -> tuple[Optional[Path], str]:
    subtitles = list(entity.subtitles or [])
    preferred = [str(item.get("language") or "und") for item in subtitles]
    for language in preferred:
        candidate = folder / f"subtitle_{language}.vtt"
        if candidate.is_file():
            return candidate, language
    candidates = sorted(folder.glob("*.vtt"))
    if candidates:
        return candidates[0], preferred[0] if preferred else "und"
    return None, preferred[0] if preferred else "und"


def _update_metadata_file(folder: Path, entity: Movie | Episode) -> None:
    path = folder / ".metadata" / "metadata.json"
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update({
            "dialogue_wpm": entity.dialogue_wpm,
            "dialogue_word_count": entity.dialogue_word_count,
            "dialogue_language": entity.dialogue_language,
            "dialogue_confidence": entity.dialogue_confidence,
            "vibe_analysis_status": entity.vibe_analysis_status,
            "vibe_analysis_version": entity.vibe_analysis_version,
            "vibe_analyzed_at": entity.vibe_analyzed_at,
        })
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except Exception as exc:
        logger.warning(f"[Vibe Analysis] Could not update {path}: {exc}")


class VibeAnalysisManager:
    def __init__(self) -> None:
        self._queue: Optional[asyncio.Queue[tuple[str, str]]] = None
        self._pending: set[tuple[str, str]] = set()
        self._workers: List[asyncio.Task] = []

    async def start(self) -> None:
        if self._workers:
            return
        self._queue = asyncio.Queue()
        self._workers = [asyncio.create_task(self._worker(index)) for index in range(2)]
        async with AsyncSession(engine) as db:
            movies = list((await db.exec(select(Movie))).all())
            episodes = list((await db.exec(select(Episode))).all())
        for entity in [*movies, *episodes]:
            if entity.video_url and entity.subtitles and (entity.vibe_analysis_version != VIBE_ANALYSIS_VERSION or entity.vibe_analysis_status in {"queued", "processing"}):
                await self.enqueue("movie" if isinstance(entity, Movie) else "episode", entity.id)

    async def stop(self) -> None:
        workers, self._workers = self._workers, []
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._pending.clear()
        self._queue = None

    async def enqueue(self, entity_type: str, entity_id: str) -> None:
        key = (entity_type, entity_id)
        if not self._queue or key in self._pending:
            return
        self._pending.add(key)
        await self._queue.put(key)

    async def _worker(self, worker_index: int) -> None:
        assert self._queue is not None
        while True:
            key = await self._queue.get()
            try:
                await self.analyze_entity(*key)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[Vibe Analysis Worker {worker_index}] {key[0]} {key[1]} failed: {exc}")
            finally:
                self._pending.discard(key)
                self._queue.task_done()

    async def analyze_entity(self, entity_type: str, entity_id: str) -> Optional[DialogueMetrics]:
        model = Movie if entity_type == "movie" else Episode
        async with AsyncSession(engine) as db:
            entity = await db.get(model, entity_id)
            if not entity:
                return None
            entity.vibe_analysis_status = "processing"
            db.add(entity)
            await db.commit()
            folder = _media_folder(entity.video_url)
            if not folder:
                entity.vibe_analysis_status = "unavailable"
                entity.vibe_analysis_version = VIBE_ANALYSIS_VERSION
                entity.vibe_analyzed_at = time.time()
                db.add(entity)
                await db.commit()
                return None
            subtitle, language = _subtitle_candidate(entity, folder)
            runtime = _runtime_seconds(entity.probed_duration, entity.duration)
            metrics = await asyncio.to_thread(analyze_webvtt, subtitle, runtime, language) if subtitle else None
            entity.dialogue_wpm = metrics.wpm if metrics else None
            entity.dialogue_word_count = metrics.word_count if metrics else 0
            entity.dialogue_language = metrics.language if metrics else language
            entity.dialogue_confidence = metrics.confidence if metrics else 0.0
            entity.vibe_analysis_status = "ready" if metrics else "unavailable"
            entity.vibe_analysis_version = VIBE_ANALYSIS_VERSION
            entity.vibe_analyzed_at = time.time()
            db.add(entity)
            if isinstance(entity, Episode):
                await db.flush()
                episodes = list((await db.exec(select(Episode).where(Episode.movie_id == entity.movie_id))).all())
                series = await db.get(Movie, entity.movie_id)
                weighted = [(float(item.dialogue_wpm), max(0.05, float(item.dialogue_confidence or 0.0)) * max(60.0, _runtime_seconds(item.probed_duration, item.duration))) for item in episodes if item.dialogue_wpm]
                if series and weighted:
                    total_weight = sum(weight for _, weight in weighted)
                    series.dialogue_wpm = sum(value * weight for value, weight in weighted) / total_weight
                    series.dialogue_confidence = min(1.0, sum(float(item.dialogue_confidence or 0.0) for item in episodes if item.dialogue_wpm) / max(1, len(weighted)))
                    series.dialogue_word_count = sum(int(item.dialogue_word_count or 0) for item in episodes)
                    series.dialogue_language = entity.dialogue_language
                    series.vibe_analysis_status = "ready"
                    series.vibe_analysis_version = VIBE_ANALYSIS_VERSION
                    series.vibe_analyzed_at = time.time()
                    db.add(series)
            await db.commit()
            await asyncio.to_thread(_update_metadata_file, folder, entity)
            return metrics


vibe_analysis_manager = VibeAnalysisManager()


async def backfill_catalog_vibes(limit: int = 24) -> int:
    """Incrementally enrich legacy catalog records without blocking startup or moving media."""
    from services.tmdb import tmdb_client

    if not tmdb_client.api_key and not tmdb_client.read_access_token:
        return 0
    async with AsyncSession(engine) as db:
        movies = list((await db.exec(select(Movie))).all())
        targets = [
            movie for movie in movies
            if movie.tmdb_id and int(movie.catalog_enrichment_version or 0) < VIBE_ANALYSIS_VERSION
        ][:max(1, limit)]
        changed = 0
        for movie in targets:
            try:
                details = await (tmdb_client.fetch_show_metadata(movie.tmdb_id) if movie.type == "series" else tmdb_client.fetch_movie_metadata(movie.tmdb_id))
            except Exception as exc:
                logger.warning(f"[Vibe Backfill] TMDB enrichment failed for {movie.id}: {exc}")
                continue
            crew = details.get("crew") or []
            keywords = details.get("keywords") or movie.keywords
            tropes = details.get("tropeVectors") or compute_trope_vectors(details.get("genres") or movie.genres, keywords, details.get("description") or movie.description)
            movie.crew = crew or movie.crew
            movie.keywords = keywords
            movie.trope_vectors = tropes
            movie.director = details.get("director") or movie.director
            movie.metadata_refreshed_at = time.time()
            movie.catalog_enrichment_version = VIBE_ANALYSIS_VERSION
            db.add(movie)
            folder = _media_folder(movie.video_url or movie.local_thumbnail_url or movie.thumbnail_url)
            if folder:
                path = folder / ".metadata" / "metadata.json"
                if path.is_file():
                    try:
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        payload.update({"crew": movie.crew, "keywords": movie.keywords, "trope_vectors": movie.trope_vectors, "director": movie.director})
                        temporary = path.with_suffix(".json.tmp")
                        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
                        os.replace(temporary, path)
                    except Exception as exc:
                        logger.warning(f"[Vibe Backfill] Metadata update failed for {movie.id}: {exc}")
            changed += 1
        if changed:
            await db.commit()
        return changed
