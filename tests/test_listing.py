"""The list filters match what the browser used to do (lib/facets.ts); checked here as SQL, and against the real data by
comparing every list with a copy of the old browser filters."""

from datetime import UTC, datetime

from sqlalchemy import column, select
from sqlalchemy.dialects import postgresql

from services import listing

NOW = datetime(2026, 11, 12, tzinfo=UTC)


def sql(condition) -> str:
    return str(select(column("x")).where(condition).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_like_text_is_matched_literally() -> None:
    assert listing.like_escape("50%_off\\") == "50\\%\\_off\\\\"
    assert "'%50\\%%'" in sql(listing.whole_text(" 50% ", column("name"))).replace("%%", "%")


def test_off_filters_add_nothing() -> None:
    for off in (None, "", "all"):
        assert listing.presence(off, column("a") == 1) is None
        assert listing.score_band(off, column("s")) is None
        assert listing.recent(off, column("d"), NOW) is None
    assert listing.all_terms("   ", column("a")) is None and listing.whole_text("", column("a")) is None


def test_every_word_must_be_somewhere() -> None:
    text = sql(listing.all_terms("Dallas  TX", column("city"), column("state"))).replace("%%", "%")
    assert text.count("ILIKE") == 4 and " AND " in text and "'%dallas%'" in text and "'%tx%'" in text


def test_presence_yes_and_anything_else() -> None:
    has = column("a") == 1
    assert "a = 1" in sql(listing.presence("yes", has))
    assert "a != 1" in sql(listing.presence("no", has))


def test_score_bands() -> None:
    s = column("s")
    assert "s IS NULL" in sql(listing.score_band("unscored", s))
    assert "s < 60" in sql(listing.score_band("low", s))
    assert "s >= 60" in sql(listing.score_band("medium", s)) and "s < 80" in sql(listing.score_band("medium", s))
    assert "s >= 80" in sql(listing.score_band("high", s))
    assert "false" in sql(listing.score_band("bogus", s)).lower()


def test_recent_days_never_in_the_future() -> None:
    text = sql(listing.recent("7", column("d"), NOW))
    assert "d IS NOT NULL" in text and "d <=" in text and "d >=" in text


def test_smart_list_candidates_build_with_search_and_facts() -> None:
    from uuid import uuid4

    from crud import SmartListRepo

    for kind in ("company", "person"):
        model, name, detail, current, (where, _, order) = SmartListRepo(None)._candidates(
            uuid4(), kind, uuid4(), "none", "none", {"processed": "no", "emails": "yes"}, "shop"
        )
        text = str(select(model.id).where(*where).compile(dialect=postgresql.dialect()))
        # The facts drop nulls first: a NOT IN over a list holding a null would match nobody.
        assert "IS NOT NULL" in text and "NOT IN" in text and "ILIKE" in text
