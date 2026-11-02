"""People from a CSV in Apollo's export format, or typed in by hand. No database here.

The template uses the column names of Apollo's standard people export, so a file exported from
Apollo imports as is. Other spellings of the main columns are accepted too, and any column we don't
use is kept with the person (`extra`), not dropped.

With an "Apollo Contact Id", a person is stored as an Apollo person under that id: the same person
found later by an Apollo search is recognised, and their email can be revealed through Apollo.
"""

import csv
import io
import re
from dataclasses import dataclass, field

from services.leads import EMAIL_PATTERN, FREE_MAIL_DOMAINS, CsvError, clean_lead

MAX_ROWS = 5000

# Our field -> header spellings (compared without case, spaces or punctuation). Apollo's own first.
PERSON_FIELDS: dict[str, list[str]] = {
    "first_name": ["firstname", "first", "givenname"],
    "last_name": ["lastname", "last", "surname", "familyname"],
    "title": ["title", "jobtitle", "position", "role"],
    "company": ["companyname", "company", "companynameforemails", "organization", "organisation", "account"],
    "email": ["email", "emailaddress", "workemail", "businessemail"],
    "email_status": ["emailstatus"],
    "seniority": ["seniority"],
    "linkedin_url": ["personlinkedinurl", "linkedinurl", "linkedin", "linkedinprofile"],
    "website": ["website", "companywebsite", "domain", "companydomain", "url"],
    "company_linkedin_url": ["companylinkedinurl"],
    "facebook_url": ["facebookurl", "facebook"],
    "twitter_url": ["twitterurl", "twitter"],
    "city": ["city"],
    "state": ["state"],
    "country": ["country"],
    "phone": ["workdirectphone", "mobilephone", "corporatephone", "phone"],
    "apollo_contact_id": ["apollocontactid", "apolloid", "contactid", "personid"],
    "apollo_account_id": ["apolloaccountid", "accountid"],
}

# Apollo's people export, every column in Apollo's order: the template, and every CSV we write. A file exported
# from Apollo imports as is, and one we export opens in anything that reads Apollo's.
APOLLO_HEADERS = [
    "First Name", "Last Name", "Title", "Company Name", "Company Name for Emails", "Email", "Email Status",
    "Primary Email Source", "Primary Email Verification Source", "Email Confidence", "Primary Email Catch-all Status",
    "Primary Email Last Verified At", "Seniority", "Departments", "Sub Departments", "Contact Owner",
    "Work Direct Phone", "Home Phone", "Mobile Phone", "Corporate Phone", "Other Phone", "Do Not Call", "Stage",
    "Lists", "Last Contacted", "Account Owner", "# Employees", "Industry", "Keywords", "Person Linkedin Url",
    "Website", "Company Linkedin Url", "Facebook Url", "Twitter Url", "City", "State", "Country", "Company Address",
    "Company City", "Company State", "Company Country", "Company Phone", "Technologies", "Annual Revenue",
    "Total Funding", "Latest Funding", "Latest Funding Amount", "Last Raised At", "Subsidiary of",
    "Subsidiary of (Organization ID)", "Email Sent", "Email Open", "Email Bounced", "Replied", "Demoed",
    "Number of Retail Locations", "Apollo Contact Id", "Apollo Account Id", "Secondary Email",
    "Secondary Email Source", "Secondary Email Status", "Secondary Email Verification Source", "Tertiary Email",
    "Tertiary Email Source", "Tertiary Email Status", "Tertiary Email Verification Source", "Primary Intent Topic",
    "Primary Intent Score", "Secondary Intent Topic", "Secondary Intent Score", "Qualify Contact", "SIC Codes",
    "NAICS Codes", "Apollo Record Id", "Persona Brief",
]  # fmt: skip
TEMPLATE_HEADERS = APOLLO_HEADERS
TEMPLATE_EXAMPLE = {
    "First Name": "Ava", "Last Name": "Morgan", "Title": "Founder", "Company Name": "Brightloom",
    "Company Name for Emails": "Brightloom", "Email": "ava@brightloom.example", "Email Status": "Verified",
    "Seniority": "founder", "Person Linkedin Url": "http://www.linkedin.com/in/ava-morgan-example",
    "Website": "http://www.brightloom.example", "City": "Austin", "State": "Texas", "Country": "United States",
}  # fmt: skip

SENIORITIES = {"owner", "founder", "c_suite", "partner", "vp", "head", "director", "manager", "senior", "entry", "intern"}


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


def template_csv() -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(APOLLO_HEADERS)
    writer.writerow([TEMPLATE_EXAMPLE.get(h, "") for h in APOLLO_HEADERS])
    return out.getvalue()


def export_csv(rows: list[dict[str, str]]) -> str:
    """Rows keyed by Apollo's column names, written with every column in Apollo's order; blank where unknown."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(APOLLO_HEADERS)
    for row in rows:
        writer.writerow([row.get(h) or "" for h in APOLLO_HEADERS])
    return out.getvalue()


def map_headers(headers: list[str]) -> list[str | None]:
    """Our field for each column; None keeps the column as extra.

    Each field takes the column matching its earliest spelling, so when Apollo exports both
    "Company Name" and "Company Name for Emails", the plain name wins.
    """
    keys = [_key(h) for h in headers]
    mapping: list[str | None] = [None] * len(headers)
    for name, spellings in PERSON_FIELDS.items():
        for spelling in spellings:
            col = next((i for i, k in enumerate(keys) if k == spelling and mapping[i] is None), None)
            if col is not None:
                mapping[col] = name
                break
    return mapping


@dataclass
class PersonRow:
    first_name: str = ""
    last_name: str = ""
    title: str = ""
    company: str = ""
    email: str = ""
    email_status: str = ""
    seniority: str = ""
    linkedin_url: str = ""
    website: str = ""
    company_linkedin_url: str = ""
    facebook_url: str = ""
    twitter_url: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    phone: str = ""
    apollo_contact_id: str = ""
    apollo_account_id: str = ""
    extra: dict[str, str] = field(default_factory=dict)
    domain: str = ""

    @property
    def external_id(self) -> tuple[str, str]:
        """(source, id) for eo_stakeholders: Apollo's id when there is one, else a stable key of our own."""
        if self.apollo_contact_id:
            return "apollo", self.apollo_contact_id
        if self.linkedin_url:
            return "manual", "linkedin:" + self.linkedin_url.lower().rstrip("/")
        if self.email:
            return "manual", "email:" + self.email
        return "manual", f"name:{self.first_name} {self.last_name}@{self.domain or self.company}".lower()


def clean_person(values: dict[str, str]) -> tuple[PersonRow | None, str | None]:
    """A tidy person, or why the row can't be used."""
    v = {k: (values.get(k) or "").strip() for k in PERSON_FIELDS}
    lead = clean_lead({
        "first_name": v["first_name"], "last_name": v["last_name"], "email": v["email"], "company": v["company"],
        "title": v["title"], "website": v["website"], "linkedin_url": v["linkedin_url"], "facebook_url": v["facebook_url"],
    })  # fmt: skip
    if not (lead.first_name or lead.last_name):
        return None, "No name"
    if v["email"] and not EMAIL_PATTERN.match(lead.email):
        return None, f"Email isn't valid: {v['email']}"
    if not (lead.email or lead.linkedin_url or lead.company or lead.domain):
        return None, "Needs an email, a LinkedIn URL or a company"
    status = v["email_status"].lower().replace(" ", "_") or ("unverified" if lead.email else "")
    seniority = v["seniority"].lower().replace("-", "_").replace(" ", "_")
    person = PersonRow(
        first_name=lead.first_name, last_name=lead.last_name, title=lead.title, company=lead.company, email=lead.email,
        email_status=status, seniority=seniority if seniority in SENIORITIES else "",
        linkedin_url=lead.linkedin_url, website=lead.website, domain=lead.domain,
        company_linkedin_url=v["company_linkedin_url"], facebook_url=lead.facebook_url, twitter_url=v["twitter_url"],
        city=v["city"], state=v["state"], country=v["country"], phone=v["phone"],
        apollo_contact_id=v["apollo_contact_id"], apollo_account_id=v["apollo_account_id"],
    )  # fmt: skip
    return person, None


@dataclass
class ParsedPeople:
    people: list[tuple[int, PersonRow]]  # (row number in the file, person)
    errors: list[tuple[int, str]]
    duplicates: int
    columns: dict[str, str]  # header -> our field, for the columns we use


def parse_people_csv(text: str) -> ParsedPeople:
    """Rows of a CSV as people. Row numbers count the header as row 1, as a spreadsheet shows them."""
    text = text.lstrip("﻿")
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise CsvError(f"This isn't a readable CSV file: {exc}") from exc
    filled = sum(1 for r in rows[1:] if any(c.strip() for c in r))
    if not rows or filled == 0:
        raise CsvError("The file has no people in it, only a header or nothing at all")
    if filled > MAX_ROWS:
        raise CsvError(f"At most {MAX_ROWS:,} people per file; this one has {filled:,}")
    headers = [h.strip() for h in rows[0]]
    mapping = map_headers(headers)
    if not {"first_name", "last_name"} & set(m for m in mapping if m):
        raise CsvError("No name columns found. Use the template's column names, like First Name and Last Name")

    people: list[tuple[int, PersonRow]] = []
    errors: list[tuple[int, str]] = []
    seen: set[tuple[str, str]] = set()
    duplicates = 0
    for number, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue  # blank lines are skipped but still counted, so row numbers match the file
        values: dict[str, str] = {}
        extra: dict[str, str] = {}
        for col, cell in enumerate(row):
            if col >= len(headers):
                continue
            if mapping[col]:
                values[mapping[col]] = cell
            elif cell.strip() and headers[col]:
                extra[headers[col]] = cell.strip()
        person, problem = clean_person(values)
        if person is None:
            errors.append((number, problem or "Can't be used"))
            continue
        person.extra = extra
        if person.external_id in seen:
            duplicates += 1
            continue
        seen.add(person.external_id)
        people.append((number, person))
    columns = {h: m for h, m in zip(headers, mapping, strict=True) if m}
    return ParsedPeople(people, errors, duplicates, columns)


def company_domain(person: PersonRow) -> str | None:
    """The company's web domain: its website, else a work email's domain."""
    if person.domain:
        return person.domain
    if person.email and "@" in person.email:
        domain = person.email.split("@", 1)[1]
        return None if domain in FREE_MAIL_DOMAINS else domain
    return None
