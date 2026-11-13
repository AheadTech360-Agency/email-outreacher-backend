"""Versioned, bounded snapshots of recorded outreach outcomes. No provider syncs."""
import json
from collections import defaultdict
from typing import Literal

import anthropic
from pydantic import Field, ValidationError
from sqlalchemy import text

from models import OUTREACH_SCHEMA

from schemas import ApiModel
from services.claude import cost_usd, error_message
from services.prompts import PLACEHOLDER, render

# One row per saved first email, with deduplicated outcome flags rather than joined event counts.
COHORT = text("""
select e.id::text as id, e.lead_id::text as lead_id, e.sent_at, e.sequence_sent_count as emails_sent,
  array_length(regexp_split_to_array(trim(coalesce(e.subject, '')), '[[:space:]]+'),1) as subject_words,
  l.lead_intel_icp_id::text as icp_id, left(e.subject, 250) as subject,
  left(coalesce(e.edited_body,e.body),1200) as body,
  array_length(regexp_split_to_array(trim(coalesce(e.edited_body,e.body)), '[[:space:]]+'),1) as words,
  left(e.hook,500) as hook, p.source as post_source, p.lead_id is not null as personal_post,
  hi.source as hook_source,
  coalesce(pt.name,'Unknown prompt') || coalesce(' v' || pv.version::text,'') as prompt,
  coalesce(outcome.replied,false) as replied, coalesce(outcome.bounced,false) as bounced,
  coalesce(outcome.unsubscribed,false) as unsubscribed,
  case when f.updated_at <= :as_of then f.rating else null end as rating
from outreach.eo_emails e
join outreach.eo_leads l on l.id=e.lead_id and l.workspace_id=e.workspace_id
left join outreach.eo_posts p on p.id=e.chosen_post_id and p.workspace_id=e.workspace_id
left join outreach.eo_prompt_versions pv on pv.id=e.email_prompt_version_id
left join outreach.eo_prompts pt on pt.id=pv.prompt_id and pt.workspace_id=e.workspace_id
left join outreach.eo_email_feedback f on f.email_id=e.id and f.workspace_id=e.workspace_id
left join lateral (
  select i.source from outreach.eo_insights i where i.workspace_id=e.workspace_id and i.lead_id=e.lead_id
  and i.hook=e.hook and i.created_at<=e.created_at order by i.created_at desc limit 1
) hi on true
left join lateral (
  select bool_or(v.type='replied') as replied, bool_or(v.type='bounced') as bounced,
    bool_or(v.type='unsubscribed') as unsubscribed
  from outreach.eo_send_events v where v.workspace_id=e.workspace_id
  and (v.lead_id=e.lead_id or v.email_id=e.id) and v.occurred_at>=e.sent_at and v.occurred_at<:as_of
) outcome on true
where e.workspace_id=:w and e.step=1 and e.sent_at>=:start and e.sent_at<:end
  and (cast(:icp as uuid) is null or l.lead_intel_icp_id=cast(:icp as uuid))
order by e.sent_at,e.id limit 10001
""".replace("outreach.", f"{OUTREACH_SCHEMA}."))
ACTIVITY = text("""
select count(*) as drafted,
 count(*) filter(where e.status in ('draft','edited')) as awaiting_review,
 count(*) filter(where e.approved_at is not null and e.approved_at<:end) as approved,
 count(*) filter(where e.queued_at is not null and e.queued_at<:end) as queued
from outreach.eo_emails e join outreach.eo_leads l on l.id=e.lead_id and l.workspace_id=e.workspace_id
where e.workspace_id=:w and e.step=1 and e.created_at>=:start and e.created_at<:end
 and (cast(:icp as uuid) is null or l.lead_intel_icp_id=cast(:icp as uuid))
""".replace("outreach.", f"{OUTREACH_SCHEMA}."))


def source_of(row):
    if row['post_source']:
        return row['post_source'].capitalize() + (' person post' if row['personal_post'] else ' company post')
    source = (row['hook_source'] or '').lower()
    for key in ('linkedin','instagram','website','profile'):
        if key in source:
            return key.capitalize() + ' (other hook)'
    return 'Other / unknown hook' if row['hook'] else 'No recorded hook'


EXAMPLES, BODY_CHARS = 400, 500


def reply_after(row):
    """Which of the five emails the reply came after: emails sent when the sequence stopped on the reply."""
    return max(1, min(5, int(row['emails_sent'] or 1))) if row['replied'] else None


def replies_by_email(rows):
    counts = defaultdict(int)
    for row in rows:
        if row['replied']:
            counts[reply_after(row)] += 1
    return [{'label':f'Email {step}','replies':counts.get(step,0)} for step in range(1,6)]


def metrics(rows):
    n = len(rows)
    replied = sum(bool(row['replied']) for row in rows)
    return {'recipients':n,'replied':replied,'bounced':sum(bool(r['bounced']) for r in rows),
        'unsubscribed':sum(bool(r['unsubscribed']) for r in rows),
        'replyRate':round(100*replied/n,2) if n else None,
        'ratedGood':sum(r['rating']=='good' for r in rows),'ratedBad':sum(r['rating']=='bad' for r in rows)}


def group_rows(rows, key):
    grouped = defaultdict(list)
    for row in rows: grouped[key(row)].append(row)
    return [{'label':label,**metrics(items)} for label,items in sorted(grouped.items(),key=lambda item:-len(item[1]))]


async def snapshot(session, workspace_id, start, end, icp_id):
    params = {'w':workspace_id,'start':start,'end':end,'as_of':end,'icp':str(icp_id) if icp_id else None}
    result = list((await session.execute(COHORT,params)).mappings())
    # A recipient can have several sent drafts. Count them once within each cohort, earliest first email.
    seen, rows = set(), []
    for row in result[:10000]:
        if row['lead_id'] not in seen:
            rows.append(dict(row)); seen.add(row['lead_id'])
    previous_end = start
    previous_start = start-(end-start)
    previous = list((await session.execute(COHORT,{**params,'start':previous_start,'end':previous_end,'as_of':previous_end})).mappings())
    prev_seen, prev_rows = set(), []
    for row in previous[:10000]:
        if row['lead_id'] not in prev_seen:
            prev_rows.append(row);prev_seen.add(row['lead_id'])
    activity = dict((await session.execute(ACTIVITY,params)).mappings().one())
    # Every sent first email (up to EXAMPLES), replies first so none is cut: subject, hook, where the hook came
    # from, length, the outcome and which email the reply came after. Bodies shortened to keep the cost down.
    ordered = sorted(rows, key=lambda r: not r['replied'])[:EXAMPLES]
    examples = [{'emailId':r['id'],'subject':r['subject'],'body':(r['body'] or '')[:BODY_CHARS],'hook':r['hook'],
        'source':source_of(r),'words':r['words'],'replied':r['replied'],'replyAfterEmail':reply_after(r),
        'bounced':r['bounced'],'rating':r['rating'],'icpId':r['icp_id'],'prompt':r['prompt']} for r in ordered]
    limitations = [
        'Replies are any recorded reply, not verified positive replies or revenue. Opens are not tracked: CheapSequencer does not report them.',
        'Reply attribution is to the recipient sequence; follow-ups may have contributed. Source is the chosen hook source, not all fetched platforms.',
        'Only first emails with a recorded sent timestamp enter the recipient cohort. Queued emails and missing send timestamps are excluded.',
        'Outcomes are observed only until the selected range end. Recent cohorts have had less time to reply; comparisons are observational, not randomized.',
        'Recipients recontacted in different periods may appear in both cohorts; these are not independent randomized groups.',
        'Analysis uses the local database only. Missing or delayed webhooks and unsynced sending records reduce coverage.',
        'Review ratings reflect internal opinion, not recipient outcomes. Up to 400 sent emails are shown in full detail, replies first, with bodies shortened to 500 characters.',
        'Historical refreshes use records still present now; saved reports preserve the original snapshot even if source records later change.',
    ]
    if len(result)>10000 or len(previous)>10000: limitations.append('Cohorts are capped at the earliest 10,000 first-email records per period; counts and rates cover that bounded cohort only.')
    if len(rows)<20: limitations.append('Fewer than 20 sent recipients: insufficient evidence to declare a winning strategy.')
    return {'metrics':metrics(rows),'previous':{'start':previous_start.isoformat(),'end':previous_end.isoformat(),**metrics(prev_rows)},
        'activity':{k:int(v) for k,v in activity.items()},'groups':{
        'sources':group_rows(rows,source_of),
        'emailLength':group_rows(rows,lambda r:'Short (up to 75 words)' if (r['words'] or 0)<=75 else 'Medium (76-150 words)' if r['words']<=150 else 'Long (151+ words)'),
        'hooks':group_rows(rows,lambda r:'Hook recorded' if r['hook'] else 'No recorded hook'),
        'subjectLength':group_rows(rows,lambda r:'Short subject (1-3 words)' if (r['subject_words'] or 0)<=3 else 'Medium subject (4-6 words)' if r['subject_words']<=6 else 'Long subject (7+ words)'),
        'icps':group_rows(rows,lambda r:r['icp_id'] or 'No ICP'),
        'prompts':group_rows(rows,lambda r:r['prompt']),
        },'repliesByEmail':replies_by_email(rows),'examples':examples,'limitations':limitations,
        'coverage':{'currentCapped':len(result)>10000,'previousCapped':len(previous)>10000,'firstEmailRecordsAnalyzed':min(len(result),10000)},
        'method':'Unique recipients with step-1 sent_at in [start, end), outcomes before end. Same-length previous window measured to its own end. Draft activity uses created_at and is a separate cohort. Awaiting-review status is current, not historical.'}


class AnalysisFinding(ApiModel):
    title: str = Field(max_length=200)
    category: Literal['email','hook','source','icp','prompt','other']
    confidence: Literal['low','medium','high']
    observation: str = Field(max_length=2000)
    evidence: str = Field(max_length=2000)
    recommendation: str = Field(max_length=2000)
    example_email_ids: list[str] = Field(default_factory=list,max_length=5)


class AnalystReport(ApiModel):
    summary: str = Field(max_length=5000)
    findings: list[AnalysisFinding] = Field(default_factory=list,max_length=15)
    experiments: list[str] = Field(default_factory=list,max_length=10)
    limitations: list[str] = Field(default_factory=list,max_length=15)


async def interpret(client, version, data):
    variables = {'analysis.data':json.dumps(data,ensure_ascii=False)}
    user = render(version.user_prompt,variables)
    if 'analysis.data' not in {m.group(1) for m in PLACEHOLDER.finditer(version.user_prompt)}:
        user += '\n\nRecorded data:\n'+variables['analysis.data']
    try:
        response = await client.messages.parse(model=version.model,max_tokens=version.max_tokens,
            system=render(version.system_prompt,variables),output_config={'effort':'medium'},
            output_format=AnalystReport,messages=[{'role':'user','content':user}])
    except anthropic.APIError as exc: return None,error_message(exc),0,0,0
    except (ValueError,ValidationError): return None,'The AI report could not be read. Metrics were saved; refresh to try again.',0,0,0
    inputs, outputs = response.usage.input_tokens,response.usage.output_tokens
    cost = cost_usd(version.model,inputs,outputs)
    if response.stop_reason in ('refusal','max_tokens') or response.parsed_output is None:
        return None,'AI analysis was incomplete or declined. Recorded metrics are saved.',cost,inputs,outputs
    result = response.parsed_output.model_dump(mode='json',by_alias=True)
    allowed = {example['emailId'] for example in data['examples']}
    for finding in result['findings']:
        finding['exampleEmailIds'] = [id for id in finding['exampleEmailIds'] if id in allowed]
        if data['metrics']['recipients'] < 20:
            finding['confidence'] = 'low'
    return result,None,cost,inputs,outputs
