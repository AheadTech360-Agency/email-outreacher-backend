"""Filters for paginated lists, written to match what the browser used to do over the whole list (lib/facets.ts), so a
page read from the database shows exactly the rows the old screen showed. Each helper returns a SQL condition, or
None when the filter is off."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, literal, or_, true

SCORE_BANDS = ("low", "medium", "high", "unscored")


def like_escape(text: str) -> str:
    """Text matched literally inside ILIKE: its wildcards and the escape character itself are escaped."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _contains(expr: Any, text: str) -> Any:
    return expr.ilike(f"%{like_escape(text)}%", escape="\\")


def terms(query: str | None) -> list[str]:
    """textMatches: the query lower-cased and split on whitespace."""
    return (query or "").strip().lower().split()


def all_terms(query: str | None, *fields: Any) -> Any | None:
    """textMatches(query, ...fields): every term is a substring of the fields joined with spaces. Terms hold no spaces,
    so a term can't span two fields and "some field contains it" is the same test. Null fields are skipped."""
    found = terms(query)
    if not found:
        return None
    return and_(*[or_(*[_contains(f, t) for f in fields]) for t in found])


def whole_text(query: str | None, haystack: Any) -> Any | None:
    """The whole trimmed query (spaces included) as one case-insensitive substring of `haystack`."""
    q = (query or "").strip().lower()
    return _contains(haystack, q) if q else None


def any_field(query: str | None, *fields: Any) -> Any | None:
    """The whole trimmed query as one substring of any one of the fields."""
    q = (query or "").strip().lower()
    return or_(*[_contains(f, q) for f in fields]) if q else None


def has_text(expr: Any) -> Any:
    """hasValue for a string column: not null and not blank once trimmed."""
    return func.coalesce(func.trim(expr), "") != ""


def presence(value: str | None, has: Any) -> Any | None:
    """presenceMatches: "all" or nothing is no filter, "yes" keeps rows that have it, anything else keeps the rest."""
    if not value or value == "all":
        return None
    return has if value == "yes" else ~has


def score_band(value: str | None, score: Any) -> Any | None:
    """scoreMatches: unscored is null; low <60; medium 60-79; high >=80; an unknown band matches nothing."""
    if not value or value == "all":
        return None
    if value == "unscored":
        return score.is_(None)
    if value == "low":
        return and_(score.is_not(None), score < 60)
    if value == "medium":
        return and_(score.is_not(None), score >= 60, score < 80)
    if value == "high":
        return and_(score.is_not(None), score >= 80)
    return literal(False)


def recent(value: str | None, column: Any, now: datetime) -> Any | None:
    """recentMatches: within the last N days and not in the future; no date never matches."""
    if not value or value == "all":
        return None
    try:
        days = int(value)
    except ValueError:
        return literal(False)
    if days <= 0:
        return literal(False)
    return and_(column.is_not(None), column <= now, column >= now - timedelta(days=days))


def where(*conditions: Any) -> list[Any]:
    """The conditions that are on (None means that filter is off)."""
    out = [c for c in conditions if c is not None]
    return out or [true()]
