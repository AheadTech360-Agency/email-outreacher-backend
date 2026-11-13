"""Guards for the rule: never modify Lead Intel's tables or data."""

import re
from pathlib import Path
from types import SimpleNamespace

from database import to_async_url
from lead_intel import lead_intel_metadata
from models import OUTREACH_SCHEMA, TABLE_PREFIX, Base

ROOT = Path(__file__).resolve().parents[1]
LEAD_INTEL_TABLES = {t.name for t in lead_intel_metadata.sorted_tables} | {
    "runs", "run_grid_cells", "findings", "exclusions", "suppression_hashes", "merge_snapshots",
    "provider_calls", "outcomes", "page_audits", "page_checklists", "job_runs", "user_roles",
}  # fmt: skip


def test_our_models_live_only_in_outreach_schema() -> None:
    assert Base.metadata.tables, "expected at least one outreach table"
    for table in Base.metadata.tables.values():
        assert table.schema == OUTREACH_SCHEMA
        assert table.name.startswith(TABLE_PREFIX), f"{table.name} must start with {TABLE_PREFIX}"
        assert table.name not in LEAD_INTEL_TABLES
        for fk in table.foreign_keys:
            target = fk.column.table
            assert target.schema == OUTREACH_SCHEMA and target.name.startswith(TABLE_PREFIX), (
                f"{table.name} references {target.fullname}; foreign keys may only point at our own eo_ tables"
            )


def test_lead_intel_tables_are_not_in_alembic_metadata() -> None:
    assert not set(Base.metadata.tables) & {f"public.{t}" for t in LEAD_INTEL_TABLES}


def test_migrations_only_touch_outreach_schema() -> None:
    for path in (ROOT / "migrations" / "versions").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for schema in re.findall(r"schema\s*=\s*['\"](\w+)['\"]", source):
            assert schema == OUTREACH_SCHEMA, f"{path.name} targets schema {schema}"
        for call in re.findall(r"op\.\w+\(\s*['\"](\w+)['\"]", source):
            assert call not in LEAD_INTEL_TABLES, f"{path.name} touches Lead Intel table {call}"
        assert "public." not in source, f"{path.name} mentions the public schema"


def test_migrations_only_create_eo_tables() -> None:
    for path in (ROOT / "migrations" / "versions").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for name in re.findall(r"op\.(?:create_table|drop_table|add_column|alter_column|drop_column|create_index|drop_index)\(\s*['\"](\w+)['\"]", source):
            assert name.startswith(TABLE_PREFIX), f"{path.name} touches {name}, which isn't an {TABLE_PREFIX} table"
    assert 'version_table="eo_alembic_version"' in (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")


def test_lead_intel_repo_never_writes() -> None:
    source = (ROOT / "lead_intel.py").read_text(encoding="utf-8")
    for word in ("insert(", "update(", "delete(", ".commit(", "INSERT ", "UPDATE ", "DELETE ", "ALTER ", "DROP "):
        assert word not in source


def test_alembic_filters_out_everything_but_outreach() -> None:
    source = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
    # Load just the two filter functions, without running the migration at the bottom of env.py.
    namespace: dict = {"OUTREACH_SCHEMA": OUTREACH_SCHEMA}
    for name in ("include_name", "include_object"):
        block = re.search(rf"def {name}\(.*?(?=\n\n\n)", source, re.S).group(0)
        exec(block, namespace)

    assert namespace["include_name"]("outreach", "schema", {}) is True
    assert namespace["include_name"]("public", "schema", {}) is False
    assert namespace["include_name"](None, "schema", {}) is False
    public_table = SimpleNamespace(schema=None, name="businesses")
    assert namespace["include_object"](public_table, "businesses", "table", True, None) is False
    assert namespace["include_object"](SimpleNamespace(schema="public", name="icps"), "icps", "table", True, None) is False
    assert namespace["include_object"](SimpleNamespace(schema="outreach", name="eo_leads"), "eo_leads", "table", False, None) is True

    # In Coldbrew's own database the eo_ tables are in `public` (DB_SCHEMA=public), next to Lead Intel's: Alembic
    # sees ours and never Lead Intel's, so it can never propose changing or dropping them.
    namespace["OUTREACH_SCHEMA"] = "public"
    assert namespace["include_object"](SimpleNamespace(schema="public", name="eo_leads"), "eo_leads", "table", False, None) is True
    for lead_intel in ("icps", "businesses", "field_metadata", "identity_keys"):
        assert namespace["include_object"](SimpleNamespace(schema="public", name=lead_intel), lead_intel, "table", True, None) is False


def test_supabase_url_is_converted_for_asyncpg() -> None:
    url, args = to_async_url("postgres://u:p@db.example.supabase.co:5432/postgres?sslmode=require")
    assert url == "postgresql+asyncpg://u:p@db.example.supabase.co:5432/postgres"
    assert args == {"ssl": "require"}


# ---- The agreed writes: adding, editing and deleting ICPs in Lead Intel (icps table only) ----

APP_FILES = [p for p in ROOT.glob("**/*.py") if not {".venv", "tests", "migrations"} & set(p.parts)]


def test_the_only_lead_intel_writes_are_inserting_updating_and_deleting_icps() -> None:
    source = (ROOT / "lead_intel_writes.py").read_text(encoding="utf-8")
    assert source.count("insert(") == 1 and "insert(icps)" in source
    assert source.count("update(") == 1 and "update(icps)" in source
    assert source.count("delete(") == 1 and "delete(icps)" in source
    # The delete only happens while no business is in the ICP, checked in the same statement.
    assert "~exists().where(businesses.c.icp_id == icp_id)" in source
    for word in ("UPDATE ", "DELETE ", "ALTER ", "DROP ", "TRUNCATE"):
        assert word not in source, f"lead_intel_writes.py must never {word.strip('( ')}"
    assert "from lead_intel import businesses, icps\n" in source, "it may only write the icps table (businesses is read)"


def test_nothing_else_writes_lead_intel_tables() -> None:
    names = "|".join(t.name for t in lead_intel_metadata.sorted_tables)
    write = re.compile(rf"\b(insert|update|delete)\(\s*({names})\b")
    offenders = [
        f"{p.relative_to(ROOT)}: {m.group(0)}"
        for p in APP_FILES
        for m in write.finditer(p.read_text(encoding="utf-8"))
        if not (p.name == "lead_intel_writes.py" and m.group(0).replace(" ", "") in ("insert(icps", "update(icps", "delete(icps"))
    ]
    assert not offenders, offenders
