import time
import math
import json
from typing import List, Dict, Any, Optional
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from db import engine
from models import TelemetryEvent, ProfileTaste, Movie, TelemetryRequest
from services.logger import logger

DECAY_RATE = 0.05  # roughly 5% decay per day

async def process_telemetry_event(profile_id: str, request: TelemetryRequest):
    """Process an incoming telemetry event and update profile taste scores."""
    timestamp = time.time()
    
    # 1. Save raw event
    async with AsyncSession(engine) as db:
        event = TelemetryEvent(
            profile_id=profile_id,
            event_type=request.event_type,
            movie_id=request.movie_id,
            tmdb_id=request.tmdb_id,
            timestamp=timestamp
        )
        if request.metadata_json:
            event.event_metadata = request.metadata_json
        db.add(event)
        await db.commit()

    # 2. Determine score delta
    delta = 0.0
    if request.event_type == "watchlist_add":
        delta = 3.0
    elif request.event_type == "watchlist_remove":
        delta = -3.0
    elif request.event_type == "card_click":
        delta = 1.0
    elif request.event_type == "search_click":
        delta = 2.0
    elif request.event_type == "playback_end":
        meta = request.metadata_json or {}
        completion_rate = meta.get("completion_rate", 0.0)
        if completion_rate >= 0.8:
            delta = 5.0
        elif completion_rate >= 0.5:
            delta = 2.0
        elif completion_rate < 0.05:
            # Strong negative signal for "bounce" or early abandonment
            delta = -5.0
        elif completion_rate <= 0.2:
            delta = -2.0
        else:
            delta = 0.5
            
    if delta == 0.0:
        return

    # 3. Extract tags (genres, actors, director)
    tags_to_update = []
    
    async def extract_from_local_movie(m_id: str):
        async with AsyncSession(engine) as db:
            m = await db.get(Movie, m_id)
            if m:
                for g in m.genres:
                    tags_to_update.append(("genre", g))
                for a in m.cast:
                    tags_to_update.append(("actor", a))
                if m.director:
                    tags_to_update.append(("director", m.director))
                    
    if request.movie_id:
        await extract_from_local_movie(request.movie_id)
    elif request.metadata_json:
        # Fallback to metadata if TMDB media hasn't been added yet
        genres = request.metadata_json.get("genres", [])
        cast = request.metadata_json.get("cast", [])
        director = request.metadata_json.get("director", None)
        for g in genres:
            tags_to_update.append(("genre", g))
        for a in cast:
            tags_to_update.append(("actor", a))
        if director:
            tags_to_update.append(("director", director))

    if not tags_to_update:
        return

    # 4. Update ProfileTaste with decay
    async with AsyncSession(engine) as db:
        for tag_type, tag_value in tags_to_update:
            if not tag_value:
                continue
                
            stmt = select(ProfileTaste).where(
                ProfileTaste.profile_id == profile_id,
                ProfileTaste.tag_type == tag_type,
                ProfileTaste.tag_value == tag_value
            )
            result = await db.exec(stmt)
            taste = result.first()
            
            if taste:
                days_elapsed = (timestamp - taste.last_updated) / 86400.0
                if days_elapsed > 0:
                    decayed_score = taste.score * math.exp(-DECAY_RATE * days_elapsed)
                else:
                    decayed_score = taste.score
                    
                taste.score = decayed_score + delta
                taste.last_updated = timestamp
                db.add(taste)
            else:
                taste = ProfileTaste(
                    profile_id=profile_id,
                    tag_type=tag_type,
                    tag_value=tag_value,
                    score=delta,
                    last_updated=timestamp
                )
                db.add(taste)
                
        await db.commit()

async def get_profile_preferences(profile_id: str) -> Dict[str, List[str]]:
    """Returns top genres, actors, and directors for a profile."""
    timestamp = time.time()
    prefs = {"genre": [], "actor": [], "director": []}
    
    async with AsyncSession(engine) as db:
        stmt = select(ProfileTaste).where(ProfileTaste.profile_id == profile_id)
        result = await db.exec(stmt)
        tastes = result.all()
        
        # Apply current decay for accurate ranking
        scored_tastes = []
        for t in tastes:
            days_elapsed = (timestamp - t.last_updated) / 86400.0
            current_score = t.score * math.exp(-DECAY_RATE * days_elapsed)
            if current_score > 0.5:  # Only consider positive affinities
                scored_tastes.append((t.tag_type, t.tag_value, current_score))
                
        # Sort by score descending
        scored_tastes.sort(key=lambda x: x[2], reverse=True)
        
        # Take top 5 for each category
        for t_type, t_val, score in scored_tastes:
            if len(prefs[t_type]) < 5:
                prefs[t_type].append(t_val)
                
    return prefs

async def calculate_movie_recommendation_score(movie: Movie, profile_id: str) -> float:
    """Calculates a personalized score for a local movie based on profile tastes."""
    timestamp = time.time()
    total_score = 0.0
    
    tags = []
    for g in movie.genres:
        tags.append(("genre", g))
    for a in movie.cast:
        tags.append(("actor", a))
    if movie.director:
        tags.append(("director", movie.director))
        
    async with AsyncSession(engine) as db:
        for t_type, t_val in tags:
            stmt = select(ProfileTaste).where(
                ProfileTaste.profile_id == profile_id,
                ProfileTaste.tag_type == t_type,
                ProfileTaste.tag_value == t_val
            )
            result = await db.exec(stmt)
            taste = result.first()
            if taste:
                days_elapsed = (timestamp - taste.last_updated) / 86400.0
                current_score = taste.score * math.exp(-DECAY_RATE * days_elapsed)
                total_score += current_score
                
    return total_score
