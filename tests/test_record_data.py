"""What a person's and a company's pages show of their stored data. No database."""

from services.record_data import company_groups, person_groups

DAWN = {
    "phone": "", "city": "Temecula", "state": "California",
    "extra": {"Departments": "Operations", "Stage": "Cold", "Industry": "apparel & fashion", "# Employees": "5",
              "Company City": "Wigton", "Company Phone": "'+44 169 734 3546", "Technologies": "Shopify, Hubspot",
              "Annual Revenue": "2757000", "SIC Codes": "2300", "Email Open": "false", "Title": "President"},
}  # fmt: skip


def test_a_person_shows_their_own_columns_and_not_their_companys() -> None:
    groups = person_groups(DAWN)
    assert groups["Contact"] == {"City": "Temecula", "State": "California"}
    assert groups["Role"] == {"Departments": "Operations"} and groups["In Apollo"] == {"Stage": "Cold"}
    assert not any("Industry" in g or "Technologies" in g for g in groups.values())  # those are the company's


def test_a_company_shows_its_record_and_its_peoples_company_columns() -> None:
    groups = company_groups({"industry": "printing", "address": "1 Main St"}, [DAWN])
    assert groups["About"]["Industry"] == "printing"  # its own record wins
    assert (groups["About"]["Employees"], groups["About"]["City"], groups["About"]["Phone"]) == ("5", "Wigton", "+44 169 734 3546")
    assert groups["Technologies"] == {"Technologies": "Shopify, Hubspot"} and groups["Business"] == {"Annual Revenue": "2757000"}
