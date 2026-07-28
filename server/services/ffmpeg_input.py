from urllib.parse import urlsplit


NETWORK_PROTOCOL_WHITELIST = "http,https,tcp,tls,crypto,dns"
HLS_EXTENSIONS = (".m3u8", ".m3u")
SOURCE_TYPES = {"auto", "hls"}


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
    options = ["-protocol_whitelist", NETWORK_PROTOCOL_WHITELIST]
    normalized_source_type = normalize_source_type(source_type)
    if is_hls_media_source(url, normalized_source_type):
        options.extend(["-allowed_extensions", "ALL", "-extension_picky", "0"])
        if normalized_source_type == "hls":
            options.extend(["-f", "hls"])
    return options
