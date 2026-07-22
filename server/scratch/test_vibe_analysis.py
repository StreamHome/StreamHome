"""Focused deterministic regressions for Recommendation Engine V2."""

import tempfile
from pathlib import Path

from models import Movie
from services.recommendation import reason_details, score_movie
from services.vibe_analysis import analyze_webvtt, compute_trope_vectors, relevant_crew


def media(movie_id: str, director: str, wpm: float, tropes=None) -> Movie:
    item = Movie(
        id=movie_id,
        title=movie_id,
        description="",
        thumbnail_url="",
        banner_url="",
        video_url="/media/example.mp4",
        duration="2h",
        release_year=2025,
        rating="PG-13",
        director=director,
        type="movie",
        vote_average=7.5,
        vote_count=1000,
        popularity=25,
    )
    item.genres = ["Crime", "Comedy"]
    item.cast = ["Actor"]
    item.crew = [{"name": director, "roles": ["Director"]}]
    item.trope_vectors = tropes or []
    item.dialogue_wpm = wpm
    item.dialogue_confidence = 0.95
    return item


def test_crew_and_tropes() -> None:
    crew = relevant_crew({"crew": [
        {"name": "Jane Doe", "job": "Director"},
        {"name": "Jane Doe", "job": "Screenplay"},
        {"name": "Ignored", "job": "Gaffer"},
    ]})
    assert crew == [{"name": "Jane Doe", "roles": ["Director", "Screenplay"]}]
    tropes = compute_trope_vectors(["Action", "Comedy", "Crime"], ["buddy cop", "dark comedy", "detective"])
    assert tropes and tropes[0]["id"] == "neo_noir_buddy_action"
    assert tropes[0]["railLabel"] == "Witty Banter & Bullets"


def test_webvtt_pacing() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "subtitle_en.vtt"
        dialogue = " ".join("dialogue" for _ in range(90))
        conversation = " ".join("conversation" for _ in range(90))
        path.write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:20.000\nHello there friend.\n\n"
            f"00:00:21.000 --> 00:01:05.000\n<i>{dialogue}</i>\n\n"
            f"00:01:06.000 --> 00:01:40.000\n{conversation}\n\n"
            "00:01:41.000 --> 00:01:55.000\nHello there friend.\n",
            encoding="utf-8",
        )
        metrics = analyze_webvtt(path, 120, "en")
        assert metrics and metrics.word_count == 183, "duplicate cues and tags must not inflate words"
        assert 90 <= metrics.wpm <= 95
        assert metrics.confidence > 0.5


def test_ranker_v2_components_and_reasons() -> None:
    matching = media("m_1", "Guy Ritchie", 118, [{"id": "neo_noir_buddy_action", "label": "Neo-Noir Buddy Action", "railLabel": "Witty Banter & Bullets", "confidence": 0.95}])
    unrelated = media("m_2", "Other Director", 40)
    tastes = {("director", "guy ritchie"): 2.0, ("trope", "neo noir buddy action"): 2.0}
    pacing = {"mean": 120.0, "stddev": 15.0, "confidence": 0.9}
    matching_score, matching_reasons = score_movie(matching, tastes, True, pacing_profile=pacing)
    unrelated_score, _ = score_movie(unrelated, tastes, True, pacing_profile=pacing)
    assert matching_score > unrelated_score * 1.35
    assert "Guy Ritchie" in matching_reasons[0]
    details = reason_details(matching_reasons)
    assert details[0]["code"] == "auteur_director" and details[0]["subject"] == "Guy Ritchie"


if __name__ == "__main__":
    test_crew_and_tropes()
    test_webvtt_pacing()
    test_ranker_v2_components_and_reasons()
    print("Recommendation V2 vibe analysis checks passed.")
