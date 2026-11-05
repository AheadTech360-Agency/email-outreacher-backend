"""Workspace-shared analysis history. Reading reports never triggers a paid call."""
from datetime import UTC, datetime, timedelta
from uuid import UUID

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from background import current_job
from crud import PromptRepo, ProviderCallRepo
from database import get_session
from dependencies import get_current_user, get_optional_anthropic, get_prompt_repo, get_provider_call_repo, require_manager, require_workspace_id
from models import AnalysisReport
from schemas import ApiModel
from services.auth import CurrentUser
from services.smart_analysis import interpret, snapshot

router = APIRouter(prefix="/analysis", tags=["analysis"])


class AnalysisIn(ApiModel):
    start: AwareDatetime
    end: AwareDatetime
    icp_id: UUID | None = None
    period_key: str = Field(min_length=1, max_length=200)
    timezone: str = Field(default="UTC", max_length=100)

    @model_validator(mode="after")
    def valid_range(self):
        if self.end <= self.start or self.end-self.start > timedelta(days=366):
            raise ValueError("Choose a date range of up to 366 days, with end after start")
        return self


def metadata(row):
    return {'id':str(row.id),'start':row.range_start.isoformat(),'end':row.range_end.isoformat(),
        'createdAt':row.created_at.isoformat(),'filters':row.filters,
        'status':row.payload['status'],'summary':row.payload['narrative']['summary']}


def complete(row):
    return {**metadata(row),'payload':row.payload}


@router.get('/reports')
async def reports(workspace_id: UUID = Depends(require_workspace_id), session: AsyncSession = Depends(get_session),
                  limit: int = Query(50,ge=1,le=100), offset: int = Query(0,ge=0)):
    where = AnalysisReport.workspace_id == workspace_id
    total = await session.scalar(select(func.count()).select_from(AnalysisReport).where(where))
    # Project summaries only: history should not fetch every saved email sample and report payload.
    rows = (await session.execute(select(AnalysisReport.id,AnalysisReport.range_start,AnalysisReport.range_end,
        AnalysisReport.created_at,AnalysisReport.filters,AnalysisReport.payload['status'].astext.label('status'),
        AnalysisReport.payload['narrative']['summary'].astext.label('summary')).where(where)
        .order_by(AnalysisReport.created_at.desc(),AnalysisReport.id.desc()).offset(offset).limit(limit))).mappings()
    return {'items':[{'id':str(row['id']),'start':row['range_start'].isoformat(),'end':row['range_end'].isoformat(),
        'createdAt':row['created_at'].isoformat(),'filters':row['filters'],'status':row['status'],'summary':row['summary']} for row in rows], 'total':total or 0}


@router.get('/reports/{report_id}')
async def get_report(report_id: UUID, workspace_id: UUID = Depends(require_workspace_id), session: AsyncSession = Depends(get_session)):
    row = await session.scalar(select(AnalysisReport).where(AnalysisReport.workspace_id==workspace_id, AnalysisReport.id==report_id))
    if row is None: raise HTTPException(404,'Analysis report not found')
    return complete(row)


@router.post('/refresh', dependencies=[Depends(require_manager)])
async def refresh(body: AnalysisIn, workspace_id: UUID = Depends(require_workspace_id),
                  user: CurrentUser = Depends(get_current_user), session: AsyncSession = Depends(get_session),
                  prompts: PromptRepo = Depends(get_prompt_repo), calls: ProviderCallRepo = Depends(get_provider_call_repo),
                  client: anthropic.AsyncAnthropic | None = Depends(get_optional_anthropic)):
    job = current_job.get()
    if job:
        existing = await session.scalar(select(AnalysisReport).where(AnalysisReport.workspace_id==workspace_id,AnalysisReport.job_id==job.job_id))
        if existing: return complete(existing)
    start, end = body.start.astimezone(UTC), min(body.end.astimezone(UTC),datetime.now(UTC))
    if start>=end: raise HTTPException(422,'Choose a period that has already started')
    data = await snapshot(session,workspace_id,start,end,body.icp_id)
    narrative = {'summary':'Not enough recorded sent-email data to identify what works. Metrics are saved; collect sending and reply outcomes first.', 'findings':[], 'experiments':[], 'limitations':[]}
    status, error, cost, prompt_info = 'metrics_only',None,0,None
    if data['metrics']['recipients']:
        if client is None:
            error = 'AI analysis is unavailable because ANTHROPIC_API_KEY is not configured. Recorded metrics were saved.'
            narrative['summary'] = error
        else:
            version = await prompts.for_icp(workspace_id,'analysis',None)
            if version is None:
                error = 'Set a default Smart analysis prompt in Prompts. Recorded metrics were saved.'
                narrative['summary'] = error
            else:
                prompt_info = {'id':str(version.prompt_id),'version':version.version,'model':version.model}
                result,error,cost,inputs,outputs = await interpret(client,version,{'start':start.isoformat(),'end':end.isoformat(),**data})
                await calls.record(workspace_id,'claude','messages','failed' if error else 'succeeded',cost_usd=cost,
                    action='Smart analysis',model=version.model,input_tokens=inputs,output_tokens=outputs)
                if result is not None: narrative,status = result,'complete'
                else: narrative['summary'] = error or 'Only recorded metrics are available.'
    row = AnalysisReport(workspace_id=workspace_id,created_by=user.id,job_id=job.job_id if job else None,
        range_start=start,range_end=end,filters={'periodKey':body.period_key,'timezone':body.timezone,'icpId':str(body.icp_id) if body.icp_id else None},
        payload={'version':1,'status':status,'error':error,'costUsd':cost,'prompt':prompt_info,'narrative':narrative,**data})
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return complete(row)
