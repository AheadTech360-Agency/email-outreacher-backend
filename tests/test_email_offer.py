"""An email is only written when its ICP says what we sell (routers.emails.has_offer)."""

from types import SimpleNamespace

from routers.emails import has_offer


def test_an_offer_or_a_product_is_enough() -> None:
    assert has_offer(SimpleNamespace(offer="Faster quoting"), None)
    assert has_offer(SimpleNamespace(offer=""), SimpleNamespace(name="Coldbrew"))
    assert has_offer(None, SimpleNamespace(name="Coldbrew"))


def test_without_either_no_email_is_written() -> None:
    assert not has_offer(None, None)  # the ICP was never set up here
    assert not has_offer(SimpleNamespace(offer="   "), None)


def test_a_blank_icp_description_says_the_icp_is_the_businesses_themselves() -> None:
    """2026-11-02: with no description, "DTF Transfers" read as buyers of transfers, and DTF transfer shops scored 22."""
    from services.prompts import verify_variables

    icp = {"name": "DTF Transfers", "search_terms": ["DTF Transfers", "Gangsheets"], "any_of": ["DTF Transfers"]}
    said = verify_variables(icp, {"description": ""}, {})["icp.description"]
    assert said.startswith("Businesses that make, sell or offer DTF Transfers, Gangsheets themselves")
    assert verify_variables(icp, {"description": "Print shops in Texas"}, {})["icp.description"] == "Print shops in Texas"
