from datetime import UTC, datetime
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
from pydantic import ValidationError

from dependencies import get_current_user, get_product_repo
from main import app
from schemas import ProductIn
from services.auth import CurrentUser
from services.prompts import product_variables, render_product
from tests.conftest import WORKSPACE_ID


def field(key="features", label="Features", value="Weekly creative tests"):
    return {"key":key,"label":label,"value":value}


def note(body="Only claim verified results"):
    return {"id":str(uuid4()),"body":body}


def test_custom_fields_validate_without_notes_and_reject_builtin_or_redundant_names():
    for fields in [[field("offer")], [field(label="Pricing or offer")], [field(), field()],
                   [field("a", "My Field"), field("b", "my-field")],
                   [field("a", "Other"), field("other", "Third")], [field("Invalid key")], [field(label="   ")]]:
        with pytest.raises(ValidationError): ProductIn(name="Product", customFields=fields)
    assert ProductIn(name="P",customFields=[field()]).custom_fields[0].key == "features"


def test_notes_validate_duplicates_and_size_limits():
    for notes in [[note("  ")], [note("Same text"), note(" SAME   text ")], [note("x"*2001)]]:
        with pytest.raises(ValidationError): ProductIn(name="P",notes=notes)
    with pytest.raises(ValidationError): ProductIn(name="P",customFields=[field("x"+str(i)) for i in range(31)])
    value = ProductIn(name="P",notes=[note("  Real fact  ")])
    assert value.notes[0].body == "Real fact"


def test_all_product_variables_render_and_unreferenced_context_is_automatically_included():
    variables = product_variables({"name":"P","custom_fields":[field()],"notes":[note()]})
    assert variables["product.custom.features"] == "Weekly creative tests"
    assert "Only claim verified results" in variables["product.notes"]
    for template in ["Company verification", "Hook", "Email", "Followup"]:
        rendered = render_product(template, variables)
        assert "Weekly creative tests" in rendered and "Only claim verified results" in rendered
    rendered = render_product("{{product.custom.features}}", variables)
    assert rendered.count("Weekly creative tests") == 1
    rendered = render_product("{{product.details}}", variables)
    assert rendered.count("Weekly creative tests") == 1 and rendered.count("Only claim verified results") == 1
    assert render_product("Hello", product_variables(None)) == "Hello"
    assert render_product("{{product.custom.deleted}}", variables).startswith("\n\nAdditional")


class Products:
    def __init__(self): self.rows = {}
    async def workspace_exists(self, ws): return ws == WORKSPACE_ID
    async def name_taken(self, ws, name, except_id=None): return False
    async def used_by(self, ws, id): return []
    async def create(self, ws, values):
        row = NS(id=uuid4(), workspace_id=ws, created_at=datetime.now(UTC), updated_at=datetime.now(UTC), archived=False, **values)
        self.rows[row.id] = row
        return row
    async def get(self, ws, id):
        row = self.rows.get(id)
        return row if row and row.workspace_id == ws else None
    async def update(self, row, values):
        for k,v in values.items(): setattr(row,k,v)
        return row
    async def list(self, ws): return [r for r in self.rows.values() if r.workspace_id == ws]


def test_product_api_persists_edits_and_deletes_custom_fields_and_notes(client):
    products = Products()
    app.dependency_overrides[get_product_repo] = lambda: products
    headers = {"X-Workspace-Id":str(WORKSPACE_ID)}
    payload = {"name":"Product","customFields":[field()],"notes":[note(),note("Another fact")]}
    created = client.post("/api/v1/products",headers=headers,json=payload)
    assert created.status_code == 201, created.text
    id = created.json()["id"]
    assert created.json()["customFields"] == payload["customFields"]
    assert client.get("/api/v1/products",headers=headers).json()[0]["notes"] == payload["notes"]
    payload["notes"][0]["body"] = "Edited fact"
    payload["notes"].pop()
    payload["customFields"] = []
    updated = client.put(f"/api/v1/products/{id}",headers=headers,json=payload)
    assert updated.status_code == 200 and updated.json()["notes"] == payload["notes"]
    assert updated.json()["customFields"] == []
    payload["notes"] = []
    assert client.put(f"/api/v1/products/{id}",headers=headers,json=payload).json()["notes"] == []
    assert client.put(f"/api/v1/products/{uuid4()}",headers=headers,json=payload).status_code == 404


def test_sales_cannot_write_product_fields_or_notes(client):
    products = Products()
    app.dependency_overrides[get_product_repo] = lambda: products
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(uuid4(),"s@example.com","Sales","sales",[WORKSPACE_ID])
    response = client.post("/api/v1/products",headers={"X-Workspace-Id":str(WORKSPACE_ID)},json={"name":"P","customFields":[field()]})
    assert response.status_code == 403 and not products.rows


def test_generation_renders_custom_variables_in_system_and_user_without_paid_calls():
    import asyncio
    from dataclasses import replace
    from tests.test_generation import ctx, fake, EMAIL, HOOK
    from services.generation import EmailWriter, EmailDraft
    context = ctx(with_items=False)
    context.profile = {**context.profile, "product":{"custom_fields":[field()],"notes":[note()]}}
    spec = replace(EMAIL, system="Use {{product.custom.features}}", user="Notes: {{product.notes}}")
    client, seen = fake([(EmailDraft(subject="s",body="b",confidence=80),"end_turn")])
    result = asyncio.run(EmailWriter("fake",client=client).write(context,HOOK,spec))
    assert result.error is None
    assert seen[0]["system"] == "Use Weekly creative tests"
    assert "Only claim verified results" in seen[0]["messages"][0]["content"]
