"""Shared TLS settings for outbound HTTPS provider calls."""

from __future__ import annotations

import ssl

try:
    import certifi
except ImportError:  # pragma: no cover - certifi is a declared dependency
    certifi = None


TRUSTED_SSL_CONTEXT = (
    ssl.create_default_context(cafile=certifi.where())
    if certifi
    else ssl.create_default_context()
)
