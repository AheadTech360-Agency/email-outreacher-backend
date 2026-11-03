"""The home page: what's left to do in the workspace, and where everyone sits, counted in one read."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session
from dependencies import require_workspace_id

router = APIRouter(prefix="/home", tags=["home"])

# Each one a count of things waiting for someone, and the page they're dealt with on.
TODO = text("""
select
  (select count(*) from outreach.eo_companies where workspace_id = :w and icp_id is null and status <> 'not_fit') as companies_no_icp,
  (select count(*) from outreach.eo_companies where workspace_id = :w and status = 'review') as companies_to_approve,
  (select count(*) from outreach.eo_companies where workspace_id = :w and status = 'found') as companies_to_verify,
  (select count(*) from outreach.eo_companies c where workspace_id = :w and status = 'qualified'
     and not exists (select 1 from outreach.eo_stakeholders s where s.company_id = c.id)) as companies_no_people,
  (select count(*) from outreach.eo_stakeholders where workspace_id = :w and email is null and lead_id is null) as people_no_email,
  (select count(*) from outreach.eo_emails where workspace_id = :w and step = 1 and status in ('draft', 'edited')) as drafts_to_review,
  (select count(*) from outreach.eo_emails where workspace_id = :w and step = 1 and status = 'approved') as approved_not_queued,
  (select count(*) from outreach.eo_quickstart_runs where workspace_id = :w and status = 'waiting') as runs_waiting,
  (select count(*) from outreach.eo_smart_lists where workspace_id = :w and icp_draft is not null and lead_intel_icp_id is null) as draft_icps,
  (select count(distinct lead_id) from outreach.eo_send_events where workspace_id = :w and type = 'replied'
     and occurred_at > now() - interval '14 days') as replies
""")

# Every person in exactly one stage: the furthest their first email got.
STAGES = text("""
with person as (
  select s.id, coalesce(s.lead_intel_icp_id, c.icp_id) as icp_id, s.email,
    (select e.status from outreach.eo_emails e where e.lead_id = s.lead_id and e.step = 1 order by e.created_at desc limit 1) as email_state,
    exists (select 1 from outreach.eo_send_events v where v.lead_id = s.lead_id and v.type = 'replied') as replied
  from outreach.eo_stakeholders s left join outreach.eo_companies c on c.id = s.company_id
  where s.workspace_id = :w
)
select icp_id,
  case
    when replied then 'replied'
    when email_state = 'sent' then 'sent'
    when email_state = 'queued' then 'queued'
    when email_state = 'approved' then 'approved'
    when email_state in ('draft', 'edited') then 'review'
    when email is not null then 'ready'
    else 'no_email'
  end as stage,
  count(*) as n
from person group by 1, 2
""")

# Companies with no ICP, by the smart list they're in (none: not sorted yet), so a whole list gets one at once.
NO_ICP = text("""
select l.id as list_id, l.name, count(*) as n, l.icp_draft is not null as has_draft
from outreach.eo_companies c
left join outreach.eo_smart_list_members m on m.workspace_id = c.workspace_id and m.kind = 'company' and m.member_id = c.id
left join outreach.eo_smart_lists l on l.id = m.list_id
where c.workspace_id = :w and c.icp_id is null and c.status <> 'not_fit'
group by 1, 2, 4 order by 3 desc
""")

COMPANIES = text("""
select icp_id, status, count(*) as n from outreach.eo_companies where workspace_id = :w group by 1, 2
""")


@router.get("")
async def home(workspace_id: UUID = Depends(require_workspace_id), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """What's waiting (todo), where every person sits (stages, overall and per ICP), and companies per ICP."""
    params = {"w": workspace_id}
    todo = dict((await session.execute(TODO, params)).mappings().one())
    by_icp: dict[str, dict[str, Any]] = {}

    def icp(key: UUID | None) -> dict[str, Any]:
        k = str(key) if key else ""
        return by_icp.setdefault(k, {"icpId": k or None, "companies": {}, "people": {}})

    stages: dict[str, int] = {}
    for row in (await session.execute(STAGES, params)).mappings():
        stages[row["stage"]] = stages.get(row["stage"], 0) + row["n"]
        people = icp(row["icp_id"])["people"]
        people[row["stage"]] = people.get(row["stage"], 0) + row["n"]
    for row in (await session.execute(COMPANIES, params)).mappings():
        icp(row["icp_id"])["companies"][row["status"]] = row["n"]
    no_icp = [{"listId": r["list_id"], "name": r["name"], "count": r["n"], "hasDraft": r["has_draft"]}
              for r in (await session.execute(NO_ICP, params)).mappings()]  # fmt: skip
    return {
        "noIcp": no_icp,
        "todo": {_camel(k): int(v) for k, v in todo.items()},
        "stages": stages,
        "icps": sorted(by_icp.values(), key=lambda x: -sum(x["companies"].values())),
    }


def _camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(w.title() for w in rest)
