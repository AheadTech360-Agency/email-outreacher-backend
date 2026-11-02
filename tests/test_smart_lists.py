"""Smart lists: high-level sorting by Claude, dedupe, and Apollo-format CSV. A fake Claude, no network, no cost."""

import asyncio
import csv
import io
from types import SimpleNamespace
from uuid import uuid4

from dependencies import get_optional_anthropic, get_provider_call_repo, get_smart_list_repo, get_website_reader
from main import app
from services import smart_lists
from services.people_csv import APOLLO_HEADERS, export_csv, template_csv
from tests.conftest import WORKSPACE_ID

H = {"X-Workspace-Id": str(WORKSPACE_ID)}


class FakeClaude:
    """Answers each batch with `pick(entry_text)` for every item; records what it was told."""

    def __init__(self, pick) -> None:
        self.pick, self.prompts = pick, []
        self.messages = self

    async def parse(self, *, model, max_tokens, system, output_format, messages):
        user = messages[0]["content"]
        self.prompts.append(user)
        items = []
        for chunk in user.split('<item id="')[1:]:
            item_id, text = chunk.split('">', 1)
            items.append(smart_lists.Pick(id=item_id, list=self.pick(text), reason="from the site"))
        return SimpleNamespace(parsed_output=smart_lists.Sorted(items=items), usage=SimpleNamespace(input_tokens=1000, output_tokens=100))


def test_list_names_compare_without_case_punctuation_or_and() -> None:
    assert smart_lists.name_key("Food & Beverage") == smart_lists.name_key("food and beverage") == "food beverage"


def test_dedupe_keys() -> None:
    a = SimpleNamespace(name="Ink Co", domain="inkco.com", google_place_id="p1", phone=None, city="Austin")
    b = SimpleNamespace(name="Ink Co.", domain=None, google_place_id=None, phone="(512) 555-0100", city="Austin")
    c = SimpleNamespace(name="Ink  Co", domain=None, google_place_id=None, phone=None, city="austin")
    assert smart_lists.company_key(a) == "domain:inkco.com" and smart_lists.company_key(b) == "phone:5125550100"
    assert smart_lists.company_key(c) == "name:inkco|austin"
    p = SimpleNamespace(email="Ava@X.com", linkedin_url=None, first_name="Ava", last_name="M", organization_name="X")
    q = SimpleNamespace(email=None, linkedin_url="https://www.linkedin.com/in/ava-m/", first_name="Ava", last_name="M", organization_name="X")
    assert (smart_lists.person_key(p), smart_lists.person_key(q)) == ("email:ava@x.com", "linkedin:ava-m")


def test_claude_reuses_existing_lists_and_lists_it_starts() -> None:
    """An existing list spelled differently is the existing list; a list started in one batch is offered to the next."""
    claude = FakeClaude(lambda text: "food and beverage" if "Cafe" in text else "Printing")
    entries = [(f"k{i}", f"Name: {'Cafe' if i % 2 else 'Print shop'} {i}") for i in range(30)]
    out = asyncio.run(smart_lists.sort(claude, "claude-haiku-5-5", "company", ["Food & Beverage"], entries))
    assert {p.list for p in out.picks.values()} == {"Food & Beverage", "Printing"} and len(out.picks) == 30
    assert out.calls == 2 and "- Printing" in claude.prompts[1]


# ---------- The sort endpoint ----------


class FakeRepo:
    def __init__(self, members, known=None) -> None:
        self.members, self.known, self.saved, self.created = members, known or {}, [], {}

    async def unsorted(self, workspace_id, kind, limit):
        return self.members[:limit]

    async def sorted_keys(self, workspace_id, kind):
        return dict(self.known)

    async def lists(self, workspace_id, kind):
        return [(SimpleNamespace(name="Retail", name_key="retail"), 4)]

    async def get_or_create(self, workspace_id, kind, name, key):
        return self.created.setdefault(key, SimpleNamespace(id=uuid4(), name=name))

    async def add_members(self, workspace_id, kind, rows):
        self.saved += rows

    async def count_unsorted(self, workspace_id, kind):
        return 0


class Calls:
    def __init__(self) -> None:
        self.records = []

    async def record(self, *args, **kwargs):
        self.records.append((args, kwargs))


class Reader:
    def __init__(self) -> None:
        self.read_domains = []

    async def read(self, domain, max_pages=1):
        self.read_domains.append(domain)
        return SimpleNamespace(reached=True, text="We print custom DTF transfers and gang sheets.")


def company(name, domain=None, **kw):
    return SimpleNamespace(id=uuid4(), name=name, domain=domain, google_place_id=None, phone=None, city="Austin", state="TX",
                           research=None, raw={}, **kw)  # fmt: skip


def test_sorting_companies_reads_sites_asks_claude_once_per_twin_and_reuses_sorted_twins(client) -> None:
    retail_id = uuid4()
    twin_a, twin_b = company("Ink Co", "inkco.com"), company("Ink Co LLC", "inkco.com")
    already = company("Tee Shop", "teeshop.com")  # a duplicate of one sorted earlier
    repo, calls, reader = FakeRepo([twin_a, twin_b, already], known={"domain:teeshop.com": retail_id}), Calls(), Reader()
    claude = FakeClaude(lambda text: "Printing" if "DTF" in text else "Retail")
    app.dependency_overrides.update({
        get_smart_list_repo: lambda: repo, get_provider_call_repo: lambda: calls, get_optional_anthropic: lambda: claude,
        get_website_reader: lambda: reader,
    })  # fmt: skip
    from background import current_job

    token = current_job.set(SimpleNamespace(job_id=uuid4()))  # as the worker runs it
    try:
        r = client.post("/api/v1/smart-lists/sort", headers=H, json={"kind": "company"})
    finally:
        current_job.reset(token)
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["sorted"], out["byClaude"], out["duplicates"], out["sitesRead"], out["newLists"]) == (3, 1, 2, 1, ["Printing"])
    assert reader.read_domains == ["inkco.com"] and len(claude.prompts) == 1 and claude.prompts[0].count("<item") == 1
    lists = {m: lst for m, lst, *_ in repo.saved}
    assert lists[twin_a.id] == lists[twin_b.id] == repo.created["printing"].id and lists[already.id] == retail_id
    assert calls.records[0][1]["action"] == "Sort smart lists"


def test_sorting_runs_only_as_a_background_job(client) -> None:
    from dependencies import refuse_inline

    app.dependency_overrides.pop(refuse_inline)  # the client fixture turns the guard off; production has it on
    app.dependency_overrides[get_smart_list_repo] = lambda: FakeRepo([])
    r = client.post("/api/v1/smart-lists/sort", headers=H, json={"kind": "company"})
    assert r.status_code == 409 and "background" in r.json()["detail"]


# ---------- CSV ----------


def test_template_and_exports_use_apollos_columns_in_order() -> None:
    header = next(csv.reader(io.StringIO(template_csv())))
    assert header == APOLLO_HEADERS and header[:4] == ["First Name", "Last Name", "Title", "Company Name"] and len(header) == 75
    rows = list(csv.reader(io.StringIO(export_csv([{"Email": "ava@x.com", "Lists": "Retail", "SIC Codes": "5699"}]))))
    assert rows[0] == APOLLO_HEADERS and rows[1][APOLLO_HEADERS.index("Email")] == "ava@x.com"
    assert rows[1][APOLLO_HEADERS.index("SIC Codes")] == "5699" and rows[1][0] == ""
