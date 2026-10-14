import os
from collections.abc import Mapping
from urllib.parse import urlsplit
from services.ingestion_security import UnsafeIngestionSource
from services.controlled_source import is_controlled_input


NETWORK_PROTOCOL_WHITELIST = "http,tcp,crypto"
LOCAL_MEDIA_FORMAT_WHITELIST = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,mpegts,mp3,aac,ogg,flac,wav,ac3,eac3,webvtt,srt,avi,mpeg,mpegvideo,flv,hevc,h264"
NETWORK_FORMAT_WHITELIST = "hls," + LOCAL_MEDIA_FORMAT_WHITELIST
HLS_EXTENSIONS = (".m3u8", ".m3u")
SOURCE_TYPES = {"auto", "hls"}


def native_media_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Keep native media children on their explicit input transport.

    FFmpeg's HTTP implementation consumes ambient proxy settings independently
    of Python's pinned client. Even a loopback relay URL must never be sent to
    a proxy that can supply an unvalidated manifest or learn its capability.
    """
    inherited = os.environ if environment is None else environment
    return {name: value for name, value in inherited.items()
            if name.casefold() != "proxy" and not name.casefold().endswith("_proxy")}


def local_media_input_options() -> list[str]:
    """Read completed containers without external loaders or network protocols.

    Downloading a file does not make its bytes trustworthy. A disguised local
    HLS/DASH/concat document must never initiate a second uncontrolled fetch.
    """
    return ["-protocol_whitelist", "file", "-format_whitelist", LOCAL_MEDIA_FORMAT_WHITELIST]


def generated_hls_input_options() -> list[str]:
    """Verify application-generated local HLS without allowing network fetches."""
    return ["-protocol_whitelist", "file", "-format_whitelist", NETWORK_FORMAT_WHITELIST]


def normalize_source_type(source_type: str | None) -> str:
    normalized = str(source_type or "auto").strip().lower()
    return normalized if normalized in SOURCE_TYPES else "auto"


def is_http_media_source(url: str) -> bool:
    """Return whether the source uses an FFmpeg-supported HTTP transport."""

    try:
        return urlsplit(url).scheme.lower() in {"http", "https"}
    except ValueError:
        return False


def is_hls_media_source(url: str, source_type: str = "auto") -> bool:
    """Identify named or explicitly declared HLS manifests."""

    if not is_http_media_source(url):
        return False
    if normalize_source_type(source_type) == "hls":
        return True
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    path = parsed.path.lower()
    query = parsed.query.lower()
    return path.endswith(HLS_EXTENSIONS) or "m3u8" in query


def ffmpeg_network_input_options(url: str, source_type: str = "auto") -> list[str]:
    """Build transport and demuxer options for the next FFmpeg input.

    ``allowed_extensions`` and ``extension_picky`` are private HLS demuxer
    options. Passing them to a direct MP4/WebM input makes current FFmpeg builds
    abort with "Option ... not found", so they must only accompany a manifest.
    """

    if not is_http_media_source(url):
        return []
    if not is_controlled_input(url):
        raise UnsafeIngestionSource("FFmpeg network sources must use a controlled media input.")
    options = ["-protocol_whitelist", NETWORK_PROTOCOL_WHITELIST, "-format_whitelist", NETWORK_FORMAT_WHITELIST]
    normalized_source_type = normalize_source_type(source_type)
    if is_hls_media_source(url, normalized_source_type):
        options.extend(["-allowed_extensions", "ALL", "-extension_picky", "0"])
        if normalized_source_type == "hls":
            options.extend(["-f", "hls"])
    return options
