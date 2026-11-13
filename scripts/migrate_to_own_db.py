"""Move Coldbrew from Lead Intel's shared database into its own Supabase project (2026-11-13).

The old database (DATABASE_URL) is only ever read, in one read-only snapshot. Everything is written to the new one
(NEW_DATABASE_URL), in its default `public` schema:
  - Coldbrew's eo_ tables, exactly as they are today (their definitions dumped from the old database, `outreach.`
    becoming `public.`), the migration history included (eo_alembic_version);
  - the four Lead Intel tables Coldbrew still reads (icps, businesses, field_metadata, identity_keys), with their types,
    keys, indexes and the links among them; not Lead Intel's other tables, nor its row security rules (they rely on
    Lead Intel's own functions);
  - the sign-ins (auth.users and auth.identities), passwords included, so nobody has to reset theirs.
Every table in `public` gets row level security on, with no rules: Supabase's Data API (anyone with the publishable
key) then reads nothing, while the backend, which owns the tables, is unaffected.

    python scripts/migrate_to_own_db.py build    # the structure, once (refuses if eo_ tables are there already)
    python scripts/migrate_to_own_db.py copy     # empty the new database's copies, then copy everything again
    python scripts/migrate_to_own_db.py verify   # every table's row count, old against new

`copy` can run again at the switch-over, with the backend and worker paused, for the final copy.
"""

import asyncio
import io
import os
import re
import subprocess
import sys
from pathlib import Path

import asyncpg
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
WORK = Path(r"D:\email-outreacher-backups") / "migration"
LEAD_INTEL = ("icps", "businesses", "field_metadata", "identity_keys")
# Links among the four Lead Intel tables that move; the others point at Lead Intel tables that stay behind.
KEEP_FKS = ("businesses_category_rule_id_fkey", "field_metadata_business_id_businesses_id_fk", "identity_keys_business_id_businesses_id_fk")


def env() -> dict[str, str]:
    out = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if m := re.match(r"^([A-Z_]+)=(.*)$", line.strip()):
            out[m.group(1)] = m.group(2).strip().strip('"')
    return out


E = env()
OLD, NEW = make_url(E["DATABASE_URL"]), make_url(E["NEW_DATABASE_URL"])
assert OLD.host != NEW.host, "the new database must be a different one"


async def connect(url, **kw) -> asyncpg.Connection:
    return await asyncpg.connect(host=url.host, port=url.port, user=url.username, password=url.password,
                                 database=url.database or "postgres", ssl="require", statement_cache_size=0, **kw)  # fmt: skip


def old_ip() -> str:
    """The old pooler's address, from a public resolver (this machine's DNS times out on it, and so does Docker's)."""
    out = subprocess.run(["nslookup", OLD.host, "1.1.1.1"], capture_output=True, text=True).stdout
    ips = re.findall(r"(\d+\.\d+\.\d+\.\d+)", out.split("Name:")[-1])
    return ips[0]


def pg(tool: str, *args: str) -> None:
    """pg_dump or pg_restore 17 in Docker, reading the old database (session pooler) and writing into WORK."""
    cmd = ["docker", "run", "--rm", "--add-host", f"{OLD.host}:{old_ip()}", "-e", "PGPASSWORD", "-v", f"{WORK.as_posix()}:/work",
           "postgres:17", tool, *args]  # fmt: skip
    proc = subprocess.run(cmd, env={"PGPASSWORD": OLD.password or "", "MSYS_NO_PATHCONV": "1", "PATH": os.environ["PATH"]},
                          capture_output=True, text=True)  # fmt: skip
    if proc.returncode:
        sys.exit(f"{tool} failed: {proc.stderr.strip()[-800:]}")


OLD_ARGS = ["-h", OLD.host, "-p", "5432", "-U", OLD.username, "-d", OLD.database or "postgres", "--no-owner", "--no-privileges"]


def sql_file(path: Path) -> str:
    """A plain pg_dump file as SQL a driver can run: psql's own commands (\\restrict, \\unrestrict) dropped."""
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("\\"))


async def build() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    new = await connect(NEW)
    if await new.fetchval("select count(*) from pg_tables where schemaname = 'public' and tablename like 'eo\\_%'"):
        sys.exit("The new database already has eo_ tables: build runs once. Use `copy` to copy the data again.")

    # Coldbrew's tables, as they are today, in public.
    pg("pg_dump", *OLD_ARGS, "--schema-only", "-n", "outreach", "-f", "/work/outreach_schema.sql")
    ddl = sql_file(WORK / "outreach_schema.sql")
    ddl = re.sub(r"(?m)^CREATE SCHEMA outreach;$", "", ddl).replace("outreach.eo_", "public.eo_")
    assert "outreach." not in ddl.replace("-- Name", ""), "a reference to the old schema would be left"

    # The four Lead Intel tables: types, tables, keys, indexes, and the links among them.
    pg("pg_dump", *OLD_ARGS, "-Fc", "-n", "public", "-f", "/work/public.dump")
    pg("pg_restore", "-l", "/work/public.dump", "-f", "/work/public.toc")
    keep = []
    for line in (WORK / "public.toc").read_text(encoding="utf-8").splitlines():
        if line.startswith(";"):
            continue
        if (re.search(r" TYPE public ", line)
                or re.search(rf" TABLE public ({'|'.join(LEAD_INTEL)}) ", line)
                or re.search(rf" CONSTRAINT public ({'|'.join(LEAD_INTEL)}) ", line) and " FK CONSTRAINT " not in line
                or re.search(rf" INDEX public ({'|'.join(LEAD_INTEL)})_", line)
                or (" FK CONSTRAINT " in line and any(fk in line for fk in KEEP_FKS))):  # fmt: skip
            keep.append(line)
    (WORK / "lead_intel.list").write_text("\n".join(keep) + "\n", encoding="utf-8")
    pg("pg_restore", "--schema-only", "--no-owner", "--no-privileges", "-L", "/work/lead_intel.list", "-f", "/work/lead_intel.sql", "/work/public.dump")
    lead_intel = sql_file(WORK / "lead_intel.sql")

    async with new.transaction():
        await new.execute(lead_intel)  # first: businesses links to icps, and nothing of ours to theirs
        await new.execute(ddl)
        # Row level security on, no rules, on every table in public: the Data API reads nothing, the backend (owner) all.
        for t in await new.fetch("select tablename from pg_tables where schemaname = 'public'"):
            await new.execute(f'alter table public."{t["tablename"]}" enable row level security')
    tables = await new.fetchval("select count(*) from pg_tables where schemaname = 'public'")
    print(f"built: {tables} tables in public ({len(keep)} Lead Intel definitions kept), row level security on for all")
    await new.close()


async def tables(conn: asyncpg.Connection, schema: str) -> list[str]:
    rows = await conn.fetch("select tablename from pg_tables where schemaname = $1 and tablename like 'eo\\_%' order by 1", schema)
    return [r["tablename"] for r in rows]


async def copy() -> None:
    old, new = await connect(OLD), await connect(NEW)
    ours = await tables(old, "outreach")
    missing = set(ours) - set(await tables(new, "public"))
    if missing:
        sys.exit(f"Run build first: the new database lacks {sorted(missing)}")
    pairs = [("outreach", t, t) for t in ours] + [("public", t, t) for t in LEAD_INTEL]
    async with old.transaction(isolation="repeatable_read", readonly=True):  # one consistent snapshot, never a write
        async with new.transaction():
            # Links and triggers off while loading (the inbound-email trigger would otherwise run on copied rows).
            await new.execute("set local session_replication_role = replica")
            await new.execute("truncate " + ", ".join(f'public."{t}"' for _, _, t in pairs))
            total = 0
            for schema, src, dst in pairs:
                cols = [r["attname"] for r in await new.fetch(
                    "select attname from pg_attribute where attrelid = $1::regclass and attnum > 0 and not attisdropped and attgenerated = '' order by attnum",
                    f'public."{dst}"')]  # fmt: skip
                collist = ", ".join(f'"{c}"' for c in cols)
                buf = bytearray()

                async def sink(chunk: bytes, buf=buf) -> None:
                    buf.extend(chunk)

                await old.copy_from_query(f'select {collist} from {schema}."{src}"', output=sink, format="binary")
                await new.copy_to_table(dst, schema_name="public", source=io.BytesIO(bytes(buf)), columns=cols, format="binary")
                n = await new.fetchval(f'select count(*) from public."{dst}"')
                total += n
            # Sign-ins, passwords included (only the columns both projects' auth has, and never generated ones).
            for table in ("users", "identities"):
                cols_old = {r["attname"] for r in await old.fetch(
                    "select attname from pg_attribute where attrelid = $1::regclass and attnum > 0 and not attisdropped", f"auth.{table}")}
                cols = [r["attname"] for r in await new.fetch(
                    "select attname from pg_attribute where attrelid = $1::regclass and attnum > 0 and not attisdropped and attgenerated = '' order by attnum",
                    f"auth.{table}") if r["attname"] in cols_old]  # fmt: skip
                collist = ", ".join(f'"{c}"' for c in cols)
                ids = [r["id"] for r in await old.fetch(f"select id from auth.{table}")]
                await new.execute(f"delete from auth.{table} where id = any($1::uuid[])", ids)
                buf = bytearray()

                async def sink2(chunk: bytes, buf=buf) -> None:
                    buf.extend(chunk)

                await old.copy_from_query(f"select {collist} from auth.{table}", output=sink2, format="binary")
                await new.copy_to_table(table, schema_name="auth", source=io.BytesIO(bytes(buf)), columns=cols, format="binary")
    print(f"copied: {len(pairs)} tables, {total} rows, and the sign-ins")
    await old.close()
    await new.close()


async def verify() -> bool:
    old, new = await connect(OLD), await connect(NEW)
    ours = await tables(old, "outreach")
    bad = []
    for schema, t in [("outreach", t) for t in ours] + [("public", t) for t in LEAD_INTEL] + [("auth", "users"), ("auth", "identities")]:
        a = await old.fetchval(f'select count(*) from {schema}."{t}"')
        b = await new.fetchval(f'select count(*) from {"auth" if schema == "auth" else "public"}."{t}"')
        if a != b:
            bad.append((f"{schema}.{t}", a, b))
    version = (await old.fetchval("select version_num from outreach.eo_alembic_version"), await new.fetchval("select version_num from public.eo_alembic_version"))
    no_rls = await new.fetch("select tablename from pg_tables where schemaname = 'public' and not rowsecurity")
    print(f"verified {len(ours) + len(LEAD_INTEL) + 2} tables: mismatches {bad or 'none'}; migration version old/new {version};"
          f" tables in public without row level security: {[r['tablename'] for r in no_rls] or 'none'}")
    await old.close()
    await new.close()
    return not bad and version[0] == version[1] and not no_rls


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else ""
    if step == "build":
        asyncio.run(build())
    elif step == "copy":
        asyncio.run(copy())
    elif step == "verify":
        sys.exit(0 if asyncio.run(verify()) else 1)
    else:
        sys.exit(__doc__)
