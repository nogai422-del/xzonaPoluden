from __future__ import annotations

from .roles import parse_profile, validate_nickname


def extract_nickname(text: str | None) -> str | None:
    """Backward-compatible nickname extractor used by legacy paths.

    v5 profile registration uses two lines (nickname + position), but historical
    one-line records are still recognized so old databases/history can migrate.
    """
    profile = parse_profile(text, allow_legacy=True)
    return profile.nickname if profile else None
