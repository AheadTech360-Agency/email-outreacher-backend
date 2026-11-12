"""CSV downloads of the Companies and People pages, in Coldbrew's own columns: what each page shows, plus the
research, score, smart list and lead state behind it. No database here."""

import csv
import io
from typing import Any

COMPANY_COLUMNS = [
    "Name", "Website", "Domain", "Status", "Score", "Why", "Summary", "Reasons", "Concerns", "ICP", "Smart list", "Source",
    "City", "State", "Phone", "LinkedIn", "Instagram", "Facebook", "X", "People found", "Researched", "Added",
]  # fmt: skip

PEOPLE_COLUMNS = [
    "First name", "Last name", "Title", "Seniority", "Company", "Email", "Email status", "LinkedIn", "Found by", "ICP",
    "Smart list", "Lead", "Queued", "City", "State", "Country", "Phone", "Found",
]  # fmt: skip

STATUS = {"found": "To research", "review": "To approve", "qualified": "Qualified", "not_fit": "Not a fit", "error": "Couldn't finish"}
SOURCE = {"lead_intel": "Lead Intel", "apollo": "Apollo", "google_maps": "Google Maps", "linkedin": "LinkedIn"}
FOUND_BY = {"apollo": "Apollo", "apify_linkedin": "LinkedIn", "manual": "Added by hand or CSV"}


def write(columns: list[str], rows: list[dict[str, Any]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if row.get(c) is None else row.get(c) for c in columns])
    return out.getvalue()


def _day(value: Any) -> str:
    return value.strftime("%Y-%m-%d") if value else ""


def company_row(c: Any, icp: str, smart_list: str, people: int) -> dict[str, Any]:
    detail = c.score_detail or {}
    socials = (c.research or {}).get("socials") or {}
    return {
        "Name": c.name, "Website": c.website_url or (f"https://{c.domain}" if c.domain else ""), "Domain": c.domain,
        "Status": STATUS.get(c.status, c.status), "Score": c.score, "Why": c.status_reason, "Summary": detail.get("summary"),
        "Reasons": " | ".join(detail.get("reasons") or []), "Concerns": " | ".join(detail.get("concerns") or []),
        "ICP": icp, "Smart list": smart_list, "Source": SOURCE.get(c.source, c.source), "City": c.city, "State": c.state,
        "Phone": c.phone, "LinkedIn": c.linkedin_url or socials.get("linkedin"), "Instagram": c.instagram_url or socials.get("instagram"),
        "Facebook": c.facebook_url or socials.get("facebook"), "X": c.twitter_url or socials.get("x"), "People found": people,
        "Researched": _day(c.researched_at), "Added": _day(c.created_at),
    }  # fmt: skip


def person_row(p: Any, company: str, icp: str, smart_list: str, queued_at: Any) -> dict[str, Any]:
    raw = p.raw or {}
    return {
        "First name": p.first_name, "Last name": p.last_name, "Title": p.title, "Seniority": p.seniority, "Company": company,
        "Email": p.email, "Email status": p.email_status, "LinkedIn": p.linkedin_url,
        "Found by": "Website" if p.email_status == "website" else FOUND_BY.get(p.source, p.source), "ICP": icp,
        "Smart list": smart_list, "Lead": "Yes" if queued_at else "No", "Queued": _day(queued_at), "City": raw.get("city"),
        "State": raw.get("state"), "Country": raw.get("country"), "Phone": raw.get("phone"), "Found": _day(p.found_at),
    }  # fmt: skip
