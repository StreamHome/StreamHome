import json
import time
from typing import Optional, List, Any, Dict
from pydantic import BaseModel, ConfigDict
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import UniqueConstraint

def to_camel(s: str) -> str:
    parts = s.split("_")
    return parts[0] + "".join(word.capitalize() for word in parts[1:])

# Helper class for camelCase API schemas
class APIModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel
    )

# ----------------- Database Models -----------------

class Profile(SQLModel, table=True):
    id: str = Field(primary_key=True)
    name: str
    avatar_color: str = Field(default="from-blue-600 to-indigo-600")
    theme: Optional[str] = "netflix"
    pin_enabled: Optional[bool] = Field(default=False)
    pin: Optional[str] = None

class Movie(SQLModel, table=True):
    id: str = Field(primary_key=True)
    title: str
    description: str
    thumbnail_url: str
    banner_url: Optional[str] = None
    video_url: str
    genres_str: str = Field(default="[]")  # Serialized JSON List[str]
    duration: str
    release_year: int
    rating: Optional[str] = "G"
    cast_str: Optional[str] = Field(default="[]")  # Serialized JSON List[str]
    director: Optional[str] = None
    type: Optional[str] = "movie"  # "movie" or "series"
    original_language: Optional[str] = None
    quality: Optional[str] = Field(default="Source")
    languages_str: Optional[str] = Field(default='["en"]')  # Serialized JSON List[str]
    subtitles_str: Optional[str] = Field(default="[]")  # Serialized JSON List[Dict[str, str]]
    vote_average: Optional[float] = Field(default=7.5)
    vote_count: Optional[int] = Field(default=100)
    skip_markers_str: Optional[str] = Field(default="{}")  # Serialized JSON Dict
    hevc_compressed: bool = Field(default=False)
    tmdb_id: Optional[int] = Field(default=None, index=True)
    catalog_source: str = Field(default="server", index=True)  # server or tmdb_cache
    availability: str = Field(default="available", index=True)  # cached, processing, available
    popularity: float = Field(default=0.0)
    cached_at: Optional[float] = Field(default=None)
    metadata_refreshed_at: Optional[float] = Field(default=None)
    remote_thumbnail_url: Optional[str] = Field(default=None)
    remote_banner_url: Optional[str] = Field(default=None)
    local_thumbnail_url: Optional[str] = Field(default=None)
    local_banner_url: Optional[str] = Field(default=None)
    cache_state: Optional[str] = Field(default=None, index=True)
    cache_retry_count: int = Field(default=0)
    cache_next_retry_at: Optional[float] = Field(default=None, index=True)
    cache_last_error: Optional[str] = Field(default=None)
    catalog_enrichment_version: int = Field(default=0, index=True)
    keywords_str: Optional[str] = Field(default="[]")
    collection_name: Optional[str] = Field(default=None, index=True)
    crew_str: Optional[str] = Field(default="[]")
    trope_vectors_str: Optional[str] = Field(default="[]")
    dialogue_wpm: Optional[float] = Field(default=None)
    dialogue_word_count: int = Field(default=0)
    dialogue_language: Optional[str] = Field(default=None)
    dialogue_confidence: float = Field(default=0.0)
    vibe_analysis_status: Optional[str] = Field(default=None, index=True)
    vibe_analysis_version: int = Field(default=0)
    vibe_analyzed_at: Optional[float] = Field(default=None)

    # Rich Media Probe Fields (Additive)
    probed_duration: Optional[float] = Field(default=None)
    container: Optional[str] = Field(default=None)
    codec: Optional[str] = Field(default=None)
    width: Optional[int] = Field(default=None)
    height: Optional[int] = Field(default=None)
    frame_rate: Optional[float] = Field(default=None)
    source_fingerprint: Optional[str] = Field(default=None, index=True)
    audio_metadata_str: Optional[str] = Field(default="[]")

    @property
    def genres(self) -> List[str]:
        try:
            return json.loads(self.genres_str or "[]")
        except Exception:
            return []

    @genres.setter
    def genres(self, val: List[str]):
        self.genres_str = json.dumps(val or [])

    @property
    def cast(self) -> List[str]:
        try:
            return json.loads(self.cast_str or "[]")
        except Exception:
            return []

    @cast.setter
    def cast(self, val: List[str]):
        self.cast_str = json.dumps(val or [])

    @property
    def languages(self) -> List[str]:
        try:
            val = json.loads(self.languages_str or '["en"]')
            if isinstance(val, str):
                return [val]
            return val if isinstance(val, list) else ["en"]
        except Exception:
            return ["en"]

    @languages.setter
    def languages(self, val: List[str]):
        self.languages_str = json.dumps(val or ["en"])

    @property
    def subtitles(self) -> List[Dict[str, str]]:
        try:
            return json.loads(self.subtitles_str or "[]")
        except Exception:
            return []

    @subtitles.setter
    def subtitles(self, val: List[Dict[str, str]]):
        self.subtitles_str = json.dumps(val or [])

    @property
    def skip_markers(self) -> Dict[str, Any]:
        try:
            return json.loads(self.skip_markers_str or "{}")
        except Exception:
            return {}

    @skip_markers.setter
    def skip_markers(self, val: Dict[str, Any]):
        self.skip_markers_str = json.dumps(val or {})

    @property
    def keywords(self) -> List[str]:
        try:
            return json.loads(self.keywords_str or "[]")
        except Exception:
            return []

    @keywords.setter
    def keywords(self, val: List[str]):
        self.keywords_str = json.dumps(val or [])

    @property
    def crew(self) -> List[Dict[str, Any]]:
        try:
            value = json.loads(self.crew_str or "[]")
            return value if isinstance(value, list) else []
        except Exception:
            return []

    @crew.setter
    def crew(self, val: List[Dict[str, Any]]):
        self.crew_str = json.dumps(val or [])

    @property
    def trope_vectors(self) -> List[Dict[str, Any]]:
        try:
            value = json.loads(self.trope_vectors_str or "[]")
            return value if isinstance(value, list) else []
        except Exception:
            return []

    @trope_vectors.setter
    def trope_vectors(self, val: List[Dict[str, Any]]):
        self.trope_vectors_str = json.dumps(val or [])

    @property
    def audio_metadata(self) -> List[Dict[str, Any]]:
        try:
            return json.loads(self.audio_metadata_str or "[]")
        except Exception:
            return []

    @audio_metadata.setter
    def audio_metadata(self, val: List[Dict[str, Any]]):
        self.audio_metadata_str = json.dumps(val or [])

class Episode(SQLModel, table=True):
    id: str = Field(primary_key=True)
    movie_id: str = Field(foreign_key="movie.id")
    episode_number: int
    season_number: int
    title: str
    description: str
    thumbnail_url: str
    video_url: str
    duration: str
    quality: Optional[str] = Field(default="Source")
    languages_str: Optional[str] = Field(default='["en"]')  # Serialized JSON List[str]
    subtitles_str: Optional[str] = Field(default="[]")  # Serialized JSON List[Dict[str, str]]
    skip_markers_str: Optional[str] = Field(default="{}")  # Serialized JSON Dict
    hevc_compressed: bool = Field(default=False)

    # Rich Media Probe Fields (Additive)
    probed_duration: Optional[float] = Field(default=None)
    container: Optional[str] = Field(default=None)
    codec: Optional[str] = Field(default=None)
    width: Optional[int] = Field(default=None)
    height: Optional[int] = Field(default=None)
    frame_rate: Optional[float] = Field(default=None)
    source_fingerprint: Optional[str] = Field(default=None, index=True)
    audio_metadata_str: Optional[str] = Field(default="[]")
    dialogue_wpm: Optional[float] = Field(default=None)
    dialogue_word_count: int = Field(default=0)
    dialogue_language: Optional[str] = Field(default=None)
    dialogue_confidence: float = Field(default=0.0)
    vibe_analysis_status: Optional[str] = Field(default=None, index=True)
    vibe_analysis_version: int = Field(default=0)
    vibe_analyzed_at: Optional[float] = Field(default=None)

    @property
    def languages(self) -> List[str]:
        try:
            val = json.loads(self.languages_str or '["en"]')
            if isinstance(val, str):
                return [val]
            return val if isinstance(val, list) else ["en"]
        except Exception:
            return ["en"]

    @languages.setter
    def languages(self, val: List[str]):
        self.languages_str = json.dumps(val or ["en"])

    @property
    def subtitles(self) -> List[Dict[str, str]]:
        try:
            return json.loads(self.subtitles_str or "[]")
        except Exception:
            return []

    @subtitles.setter
    def subtitles(self, val: List[Dict[str, str]]):
        self.subtitles_str = json.dumps(val or [])

    @property
    def skip_markers(self) -> Dict[str, Any]:
        try:
            return json.loads(self.skip_markers_str or "{}")
        except Exception:
            return {}

    @skip_markers.setter
    def skip_markers(self, val: Dict[str, Any]):
        self.skip_markers_str = json.dumps(val or {})

    @property
    def audio_metadata(self) -> List[Dict[str, Any]]:
        try:
            return json.loads(self.audio_metadata_str or "[]")
        except Exception:
            return []

    @audio_metadata.setter
    def audio_metadata(self, val: List[Dict[str, Any]]):
        self.audio_metadata_str = json.dumps(val or [])

class TelemetryEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: str = Field(index=True)
    event_type: str  # card_click, search_click, watchlist_add, watchlist_remove, playback_end
    movie_id: Optional[str] = None
    tmdb_id: Optional[int] = None
    metadata_json: Optional[str] = Field(default="{}")
    timestamp: float
    dedupe_key: Optional[str] = Field(default=None, index=True)

    @property
    def event_metadata(self) -> Dict[str, Any]:
        try:
            return json.loads(self.metadata_json or "{}")
        except Exception:
            return {}

    @event_metadata.setter
    def event_metadata(self, val: Dict[str, Any]):
        self.metadata_json = json.dumps(val or {})

class ProfileTaste(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("profile_id", "tag_type", "tag_value", name="uq_profile_taste_tag"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: str = Field(index=True)
    tag_type: str  # genre, actor, director
    tag_value: str = Field(index=True)
    score: float = Field(default=0.0)
    last_updated: float

class ProfileVibeVector(SQLModel, table=True):
    profile_id: str = Field(primary_key=True)
    dialogue_wpm_mean: Optional[float] = Field(default=None)
    dialogue_wpm_stddev: Optional[float] = Field(default=None)
    dialogue_confidence: float = Field(default=0.0)
    dialogue_sample_weight: float = Field(default=0.0)
    algorithm_version: str = Field(default="v2")
    updated_at: float = Field(default_factory=time.time)

class ProfileMediaPreference(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("profile_id", "movie_id", name="uq_profile_media_preference"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: str = Field(index=True)
    movie_id: str = Field(foreign_key="movie.id", index=True)
    preference: str = Field(index=True)  # like, love, dislike
    updated_at: float = Field(index=True)

class ProfileOnboardingPreference(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("profile_id", "kind", "value", name="uq_profile_onboarding_preference"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: str = Field(index=True)
    kind: str = Field(index=True)  # genre or title
    value: str = Field(index=True)
    updated_at: float

class RecommendationExposure(SQLModel, table=True):
    id: str = Field(primary_key=True)
    profile_id: str = Field(index=True)
    movie_id: str = Field(foreign_key="movie.id", index=True)
    feed_generation: str = Field(index=True)
    surface: str = Field(index=True)
    scope: str = Field(index=True)
    category: str = Field(index=True)
    position: int
    shown_at: float = Field(index=True)
    dedupe_key: str = Field(unique=True, index=True)

class ViewingAttempt(SQLModel, table=True):
    id: str = Field(primary_key=True)
    profile_id: str = Field(index=True)
    movie_id: str = Field(index=True)
    episode_id: Optional[str] = Field(default=None, index=True)
    started_at: float = Field(index=True)
    last_seen_at: float
    max_completion: float = Field(default=0.0)
    duration_watched: int = Field(default=0)
    completed_at: Optional[float] = Field(default=None)
    early_exit_recorded: bool = Field(default=False)
    rewatch_reward: float = Field(default=0.0)

class PlaybackMilestone(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("attempt_id", "milestone", name="uq_playback_attempt_milestone"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    attempt_id: str = Field(foreign_key="viewingattempt.id", index=True)
    milestone: int
    recorded_at: float

class ProfileRecommendation(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("profile_id", "movie_id", name="uq_profile_recommendation_movie"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: str = Field(index=True)
    movie_id: str = Field(foreign_key="movie.id", index=True)
    media_type: str = Field(index=True)
    score: float = Field(default=0.0)
    reasons_str: str = Field(default="[]")
    reason_details_str: str = Field(default="[]")
    generated_at: float = Field(index=True)
    candidate_source: str = Field(default="ranked", index=True)
    source_confidence: float = Field(default=0.5)

    @property
    def reasons(self) -> List[str]:
        try:
            return json.loads(self.reasons_str or "[]")
        except Exception:
            return []

    @reasons.setter
    def reasons(self, val: List[str]):
        self.reasons_str = json.dumps(val or [])

    @property
    def reason_details(self) -> List[Dict[str, Any]]:
        try:
            value = json.loads(self.reason_details_str or "[]")
            return value if isinstance(value, list) else []
        except Exception:
            return []

    @reason_details.setter
    def reason_details(self, val: List[Dict[str, Any]]):
        self.reason_details_str = json.dumps(val or [])

class RecommendationRefreshState(SQLModel, table=True):
    profile_id: str = Field(primary_key=True)
    taste_version: int = Field(default=0)
    last_ranked_at: Optional[float] = Field(default=None)
    last_tmdb_refresh_at: Optional[float] = Field(default=None)
    next_tmdb_refresh_at: Optional[float] = Field(default=None)
    refresh_requested: bool = Field(default=True)
    last_error: Optional[str] = Field(default=None)

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    password_hash: str
    totp_secret: Optional[str] = Field(default=None)
    two_factor_enabled: bool = Field(default=False)
    failed_login_attempts: int = Field(default=0)
    lockout_until: Optional[float] = Field(default=None)
    last_login_at: Optional[float] = Field(default=None)
    last_login_ip: Optional[str] = Field(default=None)
    last_login_device: Optional[str] = Field(default=None)

class AuthSession(SQLModel, table=True):
    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    created_at: float = Field(index=True)
    last_seen_at: float
    expires_at: float = Field(index=True)
    revoked_at: Optional[float] = Field(default=None, index=True)
    reauthenticated_at: Optional[float] = Field(default=None)
    ip_address: str = Field(default="Unknown")
    device_label: str = Field(default="Unknown device")

class AuthChallenge(SQLModel, table=True):
    id: str = Field(primary_key=True)
    token_hash: str = Field(unique=True, index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    purpose: str
    created_at: float
    expires_at: float = Field(index=True)
    attempts: int = Field(default=0)
    used_at: Optional[float] = Field(default=None)

class RecoveryCode(SQLModel, table=True):
    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    code_hash: str = Field(unique=True, index=True)
    created_at: float
    used_at: Optional[float] = Field(default=None, index=True)

class SecurityEvent(SQLModel, table=True):
    id: str = Field(primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    event_type: str = Field(index=True)
    outcome: str
    created_at: float = Field(index=True)
    ip_address: str = Field(default="Unknown")
    device_label: str = Field(default="Unknown device")
    session_id: Optional[str] = Field(default=None, index=True)
    details: Optional[str] = Field(default=None)

class DriveSetupJob(SQLModel, table=True):
    id: str = Field(primary_key=True)
    session_hash: str = Field(index=True)
    state_hash: str = Field(unique=True, index=True)
    status: str = Field(index=True)
    remote_name: str
    audience: str = Field(default="external")
    publishing_status: str = Field(default="production")
    public_url: str
    selected_path: Optional[str] = Field(default=None)
    progress: str = Field(default="Waiting for Google authorization")
    error_code: Optional[str] = Field(default=None)
    created_at: float
    updated_at: float
    expires_at: float = Field(index=True)

class PlaybackSession(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("profile_id", "movie_id", "episode_id", name="uq_profile_movie_episode_playback_session"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: str
    movie_id: str
    episode_id: Optional[str] = None
    timestamp: int  # Current position in seconds
    duration_watched: Optional[int] = Field(default=0)  # Cumulative seconds watched
    completion_rate: Optional[float] = Field(default=0.0)  # Ratio of watched to total duration
    updated_at: str
    is_finished: Optional[bool] = Field(default=False)

class PlaybackRun(SQLModel, table=True):
    id: str = Field(primary_key=True)
    profile_id: str = Field(index=True)
    movie_id: str = Field(index=True)
    episode_id: Optional[str] = Field(default=None, index=True)
    auth_session_id: Optional[str] = Field(default=None)
    sequence_number: int = Field(default=1)
    lifecycle_state: str = Field(default="active")  # "active", "finished", "expired", "abandoned"
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    last_seen_at: float = Field(default_factory=time.time)
    last_progress_at: float = Field(default_factory=time.time)
    total_seconds_played: int = Field(default=0)

class DownloadTask(SQLModel, table=True):
    id: str = Field(primary_key=True)
    tmdb_id: int
    title: Optional[str] = "Media Stream"
    media_type: str  # "movie" or "series"/"tv"
    season: Optional[int] = None
    episode: Optional[int] = None
    video_url: str
    audio_url: Optional[str] = None
    headers_str: Optional[str] = Field(default="{}")  # Serialized JSON headers
    status: str = "PENDING"  # PENDING, DOWNLOADING, MERGING, COMPLETED, FAILED
    subtitles_str: Optional[str] = Field(default="[]")  # Serialized JSON List[Dict[str, str]]
    quality: Optional[str] = Field(default=None)
    language: Optional[str] = Field(default=None)
    error_message: Optional[str] = Field(default=None)
    has_video: Optional[bool] = Field(default=None)
    has_audio: Optional[bool] = Field(default=None)
    scan_quality: Optional[str] = Field(default=None)
    skip_markers_str: Optional[str] = Field(default="{}")  # Serialized JSON Dict
    created_at: str

    @property
    def headers(self) -> Dict[str, str]:
        try:
            return json.loads(self.headers_str or "{}")
        except Exception:
            return {}

    @headers.setter
    def headers(self, val: Dict[str, str]):
        self.headers_str = json.dumps(val or {})

    @property
    def subtitles(self) -> List[Dict[str, str]]:
        try:
            return json.loads(self.subtitles_str or "[]")
        except Exception:
            return []

    @subtitles.setter
    def subtitles(self, val: List[Dict[str, str]]):
        self.subtitles_str = json.dumps(val or [])

    @property
    def skip_markers(self) -> Dict[str, Any]:
        try:
            return json.loads(self.skip_markers_str or "{}")
        except Exception:
            return {}

    @skip_markers.setter
    def skip_markers(self, val: Dict[str, Any]):
        self.skip_markers_str = json.dumps(val or {})

class WatchlistItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: str
    movie_id: str
    created_at: str


# ----------------- API Response / Request Schemas -----------------

class ProfileResponse(APIModel):
    id: str
    name: str
    avatar_color: Optional[str] = "from-blue-600 to-indigo-650"
    theme: Optional[str]
    pin_enabled: Optional[bool]
    pin: Optional[str]

class EpisodeResponse(APIModel):
    id: str
    episode_number: int
    season_number: int
    title: str
    description: str
    thumbnail_url: str
    video_url: str
    duration: str
    quality: Optional[str] = "Source"
    languages: List[str] = ["en"]
    subtitles: List[Dict[str, str]] = []
    skip_markers: Dict[str, Any] = {}
    dialogue_wpm: Optional[float] = None
    dialogue_confidence: float = 0.0

class MovieResponse(APIModel):
    id: str
    title: str
    description: str
    thumbnail_url: str
    banner_url: Optional[str]
    video_url: str
    genres: List[str]
    duration: str
    release_year: int
    rating: Optional[str]
    cast: List[str]
    director: Optional[str]
    type: Optional[str]
    quality: Optional[str] = "Source"
    languages: List[str] = ["en"]
    subtitles: List[Dict[str, str]] = []
    vote_average: Optional[float] = 7.5
    vote_count: Optional[int] = 100
    skip_markers: Dict[str, Any] = {}
    episodes: Optional[List[EpisodeResponse]] = None
    remote_thumbnail_url: Optional[str] = None
    remote_banner_url: Optional[str] = None
    local_thumbnail_url: Optional[str] = None
    local_banner_url: Optional[str] = None
    cache_state: Optional[str] = None
    source: str = "server"
    availability: str = "available"
    crew: List[Dict[str, Any]] = []
    trope_vectors: List[Dict[str, Any]] = []
    dialogue_wpm: Optional[float] = None
    dialogue_confidence: float = 0.0

    @classmethod
    def from_db(cls, movie: Movie, episodes: Optional[List[Episode]] = None) -> "MovieResponse":
        return cls(
            id=movie.id,
            title=movie.title,
            description=movie.description,
            thumbnail_url=movie.thumbnail_url,
            banner_url=movie.banner_url,
            video_url=movie.video_url,
            genres=movie.genres,
            duration=movie.duration,
            release_year=movie.release_year,
            rating=movie.rating,
            cast=movie.cast,
            director=movie.director,
            type=movie.type,
            quality=movie.quality or "Source",
            languages=movie.languages,
            subtitles=movie.subtitles,
            vote_average=movie.vote_average,
            vote_count=movie.vote_count,
            skip_markers=movie.skip_markers,
            episodes=[
                EpisodeResponse(
                    id=e.id,
                    episode_number=e.episode_number,
                    season_number=e.season_number,
                    title=e.title,
                    description=e.description,
                    thumbnail_url=e.thumbnail_url,
                    video_url=e.video_url,
                    duration=e.duration,
                    quality=e.quality or "Source",
                    languages=e.languages,
                    subtitles=e.subtitles,
                    skip_markers=e.skip_markers,
                    dialogue_wpm=e.dialogue_wpm,
                    dialogue_confidence=e.dialogue_confidence,
                )
                for e in episodes
            ] if episodes else None,
            remote_thumbnail_url=movie.remote_thumbnail_url,
            remote_banner_url=movie.remote_banner_url,
            local_thumbnail_url=movie.local_thumbnail_url,
            local_banner_url=movie.local_banner_url,
            cache_state=movie.cache_state,
            source=movie.catalog_source,
            availability=movie.availability,
            crew=movie.crew,
            trope_vectors=movie.trope_vectors,
            dialogue_wpm=movie.dialogue_wpm,
            dialogue_confidence=movie.dialogue_confidence,
        )

class DiscoverMovieResponse(APIModel):
    id: str
    tmdb_id: int
    title: str
    description: str
    thumbnail_url: str
    banner_url: Optional[str] = None
    genres: List[str] = []
    duration: str = "2h 10m"
    release_year: int = 2026
    rating: Optional[str] = "PG-13"
    vote_average: float = 7.5
    vote_count: int = 1000
    director: Optional[str] = "Unknown"
    cast: List[str] = []
    type: Optional[str] = "movie"
    source: str = "tmdb_cache"
    availability: str = "cached"
    remote_thumbnail_url: Optional[str] = None
    remote_banner_url: Optional[str] = None
    local_thumbnail_url: Optional[str] = None
    local_banner_url: Optional[str] = None
    cache_state: Optional[str] = None

class RecommendationCategoryResponse(APIModel):
    value: str
    label: str
    affinity: float = 0.0
    server_count: int = 0
    cached_count: int = 0

class RecommendationItemResponse(APIModel):
    media: MovieResponse
    source: str
    availability: str
    score: float
    reasons: List[str] = []
    reason_details: List[Dict[str, Any]] = []
    viewer_preference: Optional[str] = None
    candidate_source: str = "ranked"
    source_confidence: float = 0.5

class RecommendationVibeRailResponse(APIModel):
    id: str
    label: str
    trope_ids: List[str] = []
    reason_code: str = "trope_match"
    items: List[RecommendationItemResponse] = []

class RecommendationFeedResponse(APIModel):
    profile_id: str
    scope: str
    category: str
    generated_at: float
    stale: bool = False
    total: int
    offset: int
    limit: int
    categories: List[RecommendationCategoryResponse] = []
    items: List[RecommendationItemResponse] = []
    watch_again: List[RecommendationItemResponse] = []
    vibe_rails: List[RecommendationVibeRailResponse] = []
    algorithm_version: str = "v2"

class PlaybackSessionResponse(APIModel):
    movie_id: str
    profile_id: str
    timestamp: int
    duration_watched: Optional[int] = 0
    completion_rate: Optional[float] = 0.0
    updated_at: str
    episode_id: Optional[str]
    is_finished: Optional[bool]

class SubtitleInput(BaseModel):
    language: str
    url: str

class TelemetryRequest(BaseModel):
    event_type: str
    movie_id: Optional[str] = None
    tmdb_id: Optional[int] = None
    metadata_json: Optional[Dict[str, Any]] = None

class MediaPreferenceRequest(BaseModel):
    preference: Optional[str] = None

class RecommendationExposureInput(BaseModel):
    movie_id: str
    feed_generation: str
    surface: str
    scope: str
    category: str
    position: int = 0

class RecommendationExposureBatch(BaseModel):
    exposures: List[RecommendationExposureInput] = []

class RecommendationOnboardingRequest(BaseModel):
    genres: List[str] = []
    title_ids: List[str] = []

class DownloadAddRequest(BaseModel):
    tmdb_id: int
    media_type: str  # "movie" or "tv" / "series"
    season: Optional[int] = None
    episode: Optional[int] = None
    video_url: str
    audio_url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    subtitles: Optional[List[SubtitleInput]] = None
    quality: Optional[str] = None
    language: Optional[str] = None
    skip_markers: Optional[Dict[str, Any]] = None
