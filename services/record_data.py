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
    groups = {"Contact": contact, "Role": role, "LinkedIn profile": linkedin_profile_group(raw), "Email details": emails,
              "In Apollo": apollo, "Other": other}  # fmt: skip
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


def _years(job: dict[str, Any]) -> str:
    start = job.get("startDate") or {}
    end = job.get("endDate") or {}
    start = start.get("text") if isinstance(start, dict) else start
    end = end.get("text") if isinstance(end, dict) else end
    span = " – ".join(x for x in (start, end) if x)
    return f" ({job['duration']})" if job.get("duration") else (f" ({span})" if span else "")


def linkedin_profile_group(raw: dict[str, Any] | None) -> dict[str, str]:
    """A person's LinkedIn profile, read by enrichment (or the LinkedIn email search)."""
    raw = raw or {}
    out: dict[str, str] = {}
    if _filled(raw.get("headline")):
        out["Headline"] = _clean(raw["headline"])
    if _filled(raw.get("about")):
        out["About"] = _clean(raw["about"])
    jobs = [j for j in raw.get("experience") or [] if isinstance(j, dict)][:6]
    if jobs:
        out["Experience"] = "\n".join(
            f"{j.get('position') or j.get('title') or 'Role'} at {j.get('companyName') or 'a company'}{_years(j)}" for j in jobs
        )
    schools = [e for e in raw.get("education") or [] if isinstance(e, dict)][:4]
    if schools:
        out["Education"] = "\n".join(
            " · ".join(x for x in (e.get("schoolName") or e.get("title"), e.get("degree") or e.get("subtitle")) if x) for e in schools
        )
    skills = [s.get("name") if isinstance(s, dict) else s for s in raw.get("skills") or []]
    if skills := [s for s in skills if s][:25]:
        out["Skills"] = ", ".join(skills)
    return out


def company_extras(research: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """What enrichment read: the LinkedIn company page and the Instagram profile."""
    research = research or {}
    groups: dict[str, dict[str, str]] = {}
    li = research.get("linkedin") or {}
    if li:
        labels = {"tagline": "Tagline", "description": "About", "industry": "Industry", "employees": "Employees on LinkedIn",
                  "size": "Size", "founded": "Founded", "headquarters": "Headquarters", "specialties": "Specialties",
                  "followers": "Followers", "type": "Type", "url": "Page"}  # fmt: skip
        groups["LinkedIn"] = {label: _clean(li[k]) for k, label in labels.items() if _filled(li.get(k))}
    ig = research.get("instagram") or {}
    if ig:
        values = {"Account": f"@{ig['handle']}" if ig.get("handle") else "", "Name": ig.get("name"), "Bio": ig.get("bio"),
                  "Category": ig.get("category"), "Followers": f"{ig['followers']:,}" if isinstance(ig.get("followers"), int) else ""}  # fmt: skip
        groups["Instagram"] = {k: _clean(v) for k, v in values.items() if _filled(v)}
    return {k: v for k, v in groups.items() if v}
