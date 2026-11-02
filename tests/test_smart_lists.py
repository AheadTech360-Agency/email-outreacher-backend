"""Smart lists: high-level sorting by Claude, dedupe, and Apollo-format CSV. A fake Claude, no network, no cost."""

import asyncio
import csv
import io
from types import SimpleNamespace
from uuid import uuid4

from dependencies import (
    get_icp_profile_repo, get_lead_intel_repo, get_optional_anthropic, get_prompt_repo, get_provider_call_repo, get_smart_list_repo,
    get_website_reader,
)  # fmt: skip
from main import app
from services import smart_lists
from services.people_csv import APOLLO_HEADERS, export_csv, template_csv
from tests.conftest import WORKSPACE_ID

H = {"X-Workspace-Id": str(WORKSPACE_ID)}


SPEC = smart_lists.SortSpec("Sort them.", "ICPS:\n{{sort.icps}}\nLISTS:\n{{sort.lists}}\nITEMS:\n{{sort.items}}", "claude-haiku-5-5", 4096)


class FakeClaude:
    """Answers each batch with `pick(entry_text)` -> list name (or (list, icp) for companies); records the prompts.
    A draft request (output IcpDraft) gets a fixed draft."""

    def __init__(self, pick) -> None:
        self.pick, self.prompts, self.systems = pick, [], []
        self.messages = self

    async def parse(self, *, model, max_tokens, system, output_format, messages):
        usage = SimpleNamespace(input_tokens=1000, output_tokens=100)
        if output_format is smart_lists.IcpDraft:
            draft = smart_lists.IcpDraft(name="Printing", industry="Printing", description="Print shops.", search_terms=["dtf transfers"],
                                         any_of=[], titles=["Owner"], places=[])  # fmt: skip
            return SimpleNamespace(parsed_output=draft, usage=usage)
        user = messages[0]["content"]
        self.prompts.append(user)
        self.systems.append(system)
        items = []
        for chunk in user.split('<item id="')[1:]:
            item_id, text = chunk.split('">', 1)
            got = self.pick(text)
            lst, icp = got if isinstance(got, tuple) else (got, "")
            if output_format is smart_lists.CompanySorted:
                items.append(smart_lists.CompanyPick(id=item_id, list=lst, icp=icp, reason="from the site"))
            else:
                items.append(smart_lists.Pick(id=item_id, list=lst, reason="from the site"))
        return SimpleNamespace(parsed_output=output_format(items=items), usage=usage)


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
    out = asyncio.run(smart_lists.sort(claude, SPEC, "company", ["Food & Beverage"], [], entries))
    assert {p.list for p in out.picks.values()} == {"Food & Beverage", "Printing"} and len(out.picks) == 30
    assert out.calls == 2 and "- Printing" in claude.prompts[1]


def test_the_prompt_is_rendered_and_only_our_icps_count() -> None:
    """The editable prompt is what's sent; an ICP name that isn't one of ours is "none fits"."""
    claude = FakeClaude(lambda text: ("Printing", "dtf transfers") if "Ink" in text else ("Retail", "Made Up ICP"))
    icps = [{"id": uuid4(), "name": "DTF Transfers", "industry": "Printing", "description": "Shops selling DTF transfers",
             "search_terms": ["DTF transfers"], "any_of": []}]  # fmt: skip
    out = asyncio.run(smart_lists.sort(claude, SPEC, "company", [], icps, [("a", "Name: Ink Co"), ("b", "Name: Tee Shop")]))
    assert claude.systems == ["Sort them."] and claude.prompts[0].startswith("ICPS:\n- DTF Transfers; industry: Printing")
    assert (out.picks["a"].icp, out.picks["b"].icp) == ("DTF Transfers", "")


# ---------- The sort endpoint ----------


class FakeRepo:
    def __init__(self, members, known=None) -> None:
        self.todo, self.known, self.saved, self.created, self.assigned = members, known or {}, [], {}, {}

    async def unsorted(self, workspace_id, kind, limit):
        return self.todo[:limit]

    async def sorted_keys(self, workspace_id, kind):
        return dict(self.known)

    async def lists(self, workspace_id, kind):
        return [(SimpleNamespace(name="Retail", name_key="retail", lead_intel_icp_id=None, icp_draft=None), 4)]

    async def get_or_create(self, workspace_id, kind, name, key):
        return self.created.setdefault(key, SimpleNamespace(id=uuid4(), name=name, lead_intel_icp_id=None, icp_draft=None))

    async def add_members(self, workspace_id, kind, rows):
        self.saved += rows

    async def assign_icp(self, workspace_id, icp_id, company_ids):
        self.assigned.setdefault(icp_id, []).extend(company_ids)

    async def members(self, workspace_id, lst):
        ids = {m for m, list_id, *_ in self.saved if list_id == lst.id}
        return [(m, None) for m in self.todo if m.id in ids]

    async def people_at(self, workspace_id, company_ids):
        return []

    async def save(self, lst):
        pass

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

    def jina_off(self, off=True):
        import contextlib

        return contextlib.nullcontext()

    async def read(self, domain, max_pages=1):
        self.read_domains.append(domain)
        return SimpleNamespace(reached=True, text="We print custom DTF transfers and gang sheets.")


def company(name, domain=None, **kw):
    return SimpleNamespace(id=uuid4(), name=name, domain=domain, google_place_id=None, phone=None, city="Austin", state="TX",
                           research=None, raw={}, **kw)  # fmt: skip


DTF_ICP = {"id": uuid4(), "name": "DTF Transfers", "industry": "Printing", "enabled": True, "search_terms": ["DTF transfers"], "any_of": []}


class Prompts:
    async def for_icp(self, workspace_id, kind, chosen_id):
        return None  # the built-in default


class LeadIntel:
    async def list_icps(self):
        return [DTF_ICP, {**DTF_ICP, "id": uuid4(), "name": "Switched off", "enabled": False}]


class Profiles:
    async def get_many(self, workspace_id, ids):
        return {DTF_ICP["id"]: SimpleNamespace(description="Shops that sell DTF transfers")}


def run_sort_endpoint(client, repo, claude, reader=None):
    calls = Calls()
    app.dependency_overrides.update({
        get_smart_list_repo: lambda: repo, get_provider_call_repo: lambda: calls, get_optional_anthropic: lambda: claude,
        get_website_reader: lambda: reader or Reader(), get_prompt_repo: lambda: Prompts(), get_lead_intel_repo: lambda: LeadIntel(),
        get_icp_profile_repo: lambda: Profiles(),
    })  # fmt: skip
    from background import current_job

    token = current_job.set(None)  # report() is a no-op outside a job; refuse_inline is off in tests
    try:
        r = client.post("/api/v1/smart-lists/sort", headers=H, json={"kind": "company"})
    finally:
        current_job.reset(token)
    assert r.status_code == 200, r.text
    return r.json(), calls


def test_sorting_companies_reads_sites_asks_claude_once_per_twin_and_reuses_sorted_twins(client) -> None:
    retail_id = uuid4()
    twin_a, twin_b = company("Ink Co", "inkco.com"), company("Ink Co LLC", "inkco.com")
    already = company("Tee Shop", "teeshop.com")  # a duplicate of one sorted earlier
    repo, reader = FakeRepo([twin_a, twin_b, already], known={"domain:teeshop.com": retail_id}), Reader()
    claude = FakeClaude(lambda text: ("Printing", "DTF Transfers") if "DTF" in text else "Retail")
    out, calls = run_sort_endpoint(client, repo, claude, reader)
    assert (out["sorted"], out["byClaude"], out["duplicates"], out["sitesRead"], out["newLists"]) == (3, 1, 2, 1, ["Printing"])
    assert reader.read_domains == ["inkco.com"] and len(claude.prompts) == 1 and claude.prompts[0].count("<item") == 1
    lists = {m: lst for m, lst, *_ in repo.saved}
    assert lists[twin_a.id] == lists[twin_b.id] == repo.created["printing"].id and lists[already.id] == retail_id
    # Both twins fit the DTF ICP (the switched-off ICP isn't offered); no draft, since an ICP fits.
    assert out["matchedIcp"] == 2 and repo.assigned == {DTF_ICP["id"]: [twin_a.id, twin_b.id]} and out["drafted"] == []
    assert "Switched off" not in claude.prompts[0] and "Shops that sell DTF transfers" in claude.prompts[0]
    assert calls.records[0][1]["action"] == "Sort smart lists"


def test_a_new_list_no_icp_fits_gets_a_draft_icp_not_a_new_icp(client) -> None:
    shop = company("Bolt Hardware", "bolthardware.com")
    repo = FakeRepo([shop])
    out, calls = run_sort_endpoint(client, repo, FakeClaude(lambda text: ("Hardware Stores", "")))
    assert out["drafted"] == ["Hardware Stores"] and out["matchedIcp"] == 0 and repo.assigned == {}
    assert repo.created["hardware stores"].icp_draft["search_terms"] == ["dtf transfers"]
    assert [c[1]["action"] for c in calls.records] == ["Sort smart lists", "Draft ICP"]


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


def test_sites_are_read_with_plain_requests_only_and_batches_run_together(client) -> None:
    """2026-11-02: Jina's rate limit made 1,607 homepages take over an hour. Sorting reads with plain requests only
    (a site that blocks us is skipped), and after the first batch, Claude calls run several at once."""
    shops = [company(f"Shop {i}", f"shop{i}.com") for i in range(60)]
    reader = Reader()
    jina = []
    reader.jina_off = lambda off=True: (jina.append(off), __import__("contextlib").nullcontext())[1]
    out, _ = run_sort_endpoint(client, FakeRepo(shops), FakeClaude(lambda text: ("Retail", "")), reader)
    assert jina == [True] and len(reader.read_domains) == 60 and out["sorted"] == 60 and out["byClaude"] == 60
