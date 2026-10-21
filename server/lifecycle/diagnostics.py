"""Pure diagnostic redaction shared by application and protected control readers."""
from __future__ import annotations

import re


# Quotes and angle brackets may belong to a submitted path or an escaped dump;
# they cannot prove where a private URL ends. Whitespace bounds the diagnostic.
_SOURCE_URL = re.compile(r"https?:(?:\\*/){2}\S+", re.IGNORECASE)
_ENCODED_SOURCE_URL = re.compile(r"https?%(?:25)*3a(?:%(?:25)*2f){2}\S+", re.IGNORECASE)
_SQL_PARAMETERS = re.compile(r"(^\[parameters:\s*)[^\r\n]*", re.IGNORECASE | re.MULTILINE)
_SENSITIVE_QUERY_VALUE = re.compile(
    r"([?&](?:ticket|[a-z0-9_-]*token|auth(?:orization)?|api[_-]?key|password|passwd|[a-z0-9_-]*secret|signature|sig|code|state)=)[^&#\s\"]*",
    re.IGNORECASE,
)
_SENSITIVE_HEADER = re.compile(
    r"(?<![?&a-z0-9_/-])([\\]*[\"']?(?:authorization|proxy-authorization|cookie|set-cookie|[a-z0-9_-]*(?:token|secret|password|api[-_]?key|signature)[a-z0-9_-]*)[\\]*[\"']?\s*[:=]\s*)"
    r"[^\r\n]*",
    re.IGNORECASE,
)


def redact_diagnostic(value: str) -> str:
    """Keep source addresses and credentials out of application diagnostics."""
    # Positional SQL parameters can contain private input without a sensitive
    # field label. Keep the statement and driver error, not the parameter values.
    value = _SQL_PARAMETERS.sub(r"\1[REDACTED]", value)
    # SQL repr and nested JSON add escaping layers with ambiguous quote bounds.
    # Redact the sensitive record's tail rather than trusting a quote delimiter.
    value = _SENSITIVE_HEADER.sub(r"\1[REDACTED]", value)
    value = _SOURCE_URL.sub("[REDACTED_URL]", value)
    value = _ENCODED_SOURCE_URL.sub("[REDACTED_URL]", value)
    return _SENSITIVE_QUERY_VALUE.sub(r"\1[REDACTED]", value)
