"""Everything stored about a person or company, sorted into labelled groups for their pages. No database here.

A person imported from Apollo carries every column of their row (`raw.extra`); the company columns in it (industry,
size, technologies...) describe their company, so they're shown on the company's page, and only the person's own
columns on theirs.
"""

from typing import Any

# Apollo's columns about the company, not the person.
COMPANY_COLUMNS = {
    "Industry", "Keywords", "# Employees", "Technologies", "Annual Revenue", "Total Funding", "Latest Funding",
    "Latest Funding Amount", "Last Raised At", "Subsidiary of", "Subsidiary of (Organization ID)", "Number of Retail Locations",
    "SIC Codes", "NAICS Codes", "Website", "Company Linkedin Url", "Facebook Url", "Twitter Url", "Apollo Account Id",
    "Company Name", "Company Name for Emails",
}  # fmt: skip
# Apollo's sales-tool columns: how the row was handled in Apollo, not who the person is.
CRM_COLUMNS = {
    "Contact Owner", "Account Owner", "Stage", "Lists", "Last Contacted", "Email Sent", "Email Open", "Email Bounced",
    "Replied", "Demoed", "Do Not Call", "Qualify Contact", "Apollo Contact Id", "Apollo Record Id", "Primary Intent Topic",
    "Primary Intent Score", "Secondary Intent Topic", "Secondary Intent Score", "Persona Brief",
}  # fmt: skip
EMAIL_COLUMNS_PREFIX = ("Primary Email", "Secondary Email", "Tertiary Email", "Email Confidence", "Email Status")
# Apollo's long email column names, as the page shows them (the primary address is the one on the page).
EMAIL_LABEL = {
    "Primary Email Source": "Found by", "Primary Email Verification Source": "Verified by",
    "Primary Email Catch-all Status": "Catch-all", "Primary Email Last Verified At": "Last verified",
    "Email Confidence": "Confidence", "Secondary Email": "Second email", "Tertiary Email": "Third email",
}  # fmt: skip
# Already on the page from the person's own fields.
SHOWN = {"First Name", "Last Name", "Title", "Email", "Seniority", "Person Linkedin Url"}


def _filled(value: Any) -> bool:
    return value not in (None, "", [], {}) and str(value).strip().lower() not in ("false", "none", "null")


def _clean(value: Any) -> str:
    text = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
    return text.strip().lstrip("'")  # Apollo writes phone numbers as '+44 ...


def person_groups(raw: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """{group: {label: value}} of a person's stored data, the company's columns left out."""
    raw = raw or {}
    extra = raw.get("extra") or {}
    contact, role, emails, apollo, other = {}, {}, {}, {}, {}
    for label, value in (("Phone", raw.get("phone")), ("City", raw.get("city")), ("State", raw.get("state")), ("Country", raw.get("country"))):
        if _filled(value):
            contact[label] = _clean(value)
    for column, value in extra.items():
        if not _filled(value) or column in SHOWN or column in COMPANY_COLUMNS or column.startswith("Company "):
            continue
        if column in ("Work Direct Phone", "Home Phone", "Mobile Phone", "Corporate Phone", "Other Phone", "City", "State", "Country"):
            contact[column] = _clean(value)
        elif column in ("Departments", "Sub Departments"):
            role[column] = _clean(value)
        elif column.startswith(EMAIL_COLUMNS_PREFIX):
            emails[EMAIL_LABEL.get(column, column)] = _clean(value)
        elif column in CRM_COLUMNS:
            apollo[column] = _clean(value)
        else:
            other[column] = _clean(value)
    groups = {"Contact": contact, "Role": role, "Email details": emails, "In Apollo": apollo, "Other": other}
    return {name: values for name, values in groups.items() if values}


def company_groups(company_raw: dict[str, Any] | None, people_raws: list[dict[str, Any] | None]) -> dict[str, dict[str, str]]:
    """{group: {label: value}} of a company's stored data: its own record, plus the company columns of anyone
    imported at it (the first one that has each column)."""
    raw = company_raw or {}
    about: dict[str, str] = {}
    for label, key in (("Industry", "industry"), ("Employees", "employees"), ("Keywords", "keywords"), ("Address", "address"),
                       ("Country", "country"), ("Google category", "primary_type"), ("Rating", "rating"), ("Reviews", "review_count")):  # fmt: skip
        if _filled(raw.get(key)):
            about[label] = _clean(raw[key]).replace("_", " ") if key == "primary_type" else _clean(raw[key])
    business, tech, codes, more = {}, {}, {}, {}
    for person in people_raws:
        for column, value in ((person or {}).get("extra") or {}).items():
            if not _filled(value) or not (column in COMPANY_COLUMNS or column.startswith("Company ")):
                continue
            if column in ("Company Name", "Company Name for Emails", "Website", "Company Linkedin Url", "Facebook Url", "Twitter Url"):
                continue  # on the page already
            label = {"# Employees": "Employees"}.get(column, column.removeprefix("Company ").strip().capitalize() if column.startswith("Company ") else column)
            if label in about:
                continue
            target = (
                tech if column == "Technologies"
                else codes if column in ("SIC Codes", "NAICS Codes", "Apollo Account Id", "Subsidiary of (Organization ID)")
                else business if column in ("Annual Revenue", "Total Funding", "Latest Funding", "Latest Funding Amount", "Last Raised At",
                                            "Subsidiary of", "Number of Retail Locations")
                else about if column in ("Industry", "Keywords", "# Employees") or column.startswith("Company ")
                else more
            )  # fmt: skip
            target.setdefault(label, _clean(value))
    order = ["Industry", "Employees", "Keywords", "Google category", "Rating", "Reviews", "Phone", "Address", "City", "State", "Country"]
    about = dict(sorted(about.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else len(order)))
    groups = {"About": about, "Business": business, "Technologies": tech, "From Apollo": more, "Codes": codes}
    return {name: values for name, values in groups.items() if values}
