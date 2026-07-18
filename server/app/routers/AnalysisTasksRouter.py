from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from fastapi.exceptions import HTTPException
from tortoise.expressions import Q

from app import schemas
from app.models.AnalysisTask import AnalysisTaskExecution
from app.models.CMAnalysis import CMLogoGenerationAttempt
from app.models.User import User
from app.routers.UsersRouter import GetCurrentUser


router = APIRouter(tags=['Analysis Tasks'], prefix='/api/analysis-tasks')


def SerializeExecution(execution: AnalysisTaskExecution, is_admin: bool) -> schemas.AnalysisTaskExecution:
    """DB実行履歴を、権限に応じてエラー本文を伏せたAPI形式へ変換する。"""

    return schemas.AnalysisTaskExecution(
        id=execution.id,
        parent_id=execution.parent_id,
        recorded_video_id=execution.recorded_video_id,
        task_type=execution.task_type,
        status=execution.status,
        trigger=execution.trigger,
        title=execution.title,
        stage=execution.stage,
        progress=execution.progress,
        stage_history=execution.stage_history,
        current_count=execution.current_count,
        total_count=execution.total_count,
        succeeded_count=execution.succeeded_count,
        failed_count=execution.failed_count,
        skipped_count=execution.skipped_count,
        summary=execution.summary,
        error_code=execution.error_code,
        error_message=execution.error_message if is_admin else None,
        started_at=execution.started_at,
        completed_at=execution.completed_at,
        created_at=execution.created_at,
        updated_at=execution.updated_at,
    )


@router.get('/overview', response_model=schemas.AnalysisTaskOverview)
async def AnalysisTaskOverviewAPI(
    current_user: Annotated[User, Depends(GetCurrentUser)],
) -> schemas.AnalysisTaskOverview:
    """マイページ用に実行中ルート処理と直近5件を返す。"""

    active = await AnalysisTaskExecution.filter(
        parent_id=None,
        status__in=['Queued', 'Running'],
    ).order_by('created_at').limit(500)
    recent = await AnalysisTaskExecution.filter(
        parent_id=None,
        status__in=['Succeeded', 'Failed', 'Interrupted', 'Skipped'],
    ).order_by('-completed_at').limit(5)
    return schemas.AnalysisTaskOverview(
        active=[SerializeExecution(item, current_user.is_admin) for item in active],
        recent=[SerializeExecution(item, current_user.is_admin) for item in recent],
    )


@router.get('', response_model=schemas.AnalysisTaskList)
async def AnalysisTasksAPI(
    current_user: Annotated[User, Depends(GetCurrentUser)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 30,
    task_type: Annotated[str | None, Query()] = None,
    task_status: Annotated[
        Literal['Queued', 'Running', 'Succeeded', 'Failed', 'Interrupted', 'Skipped'] | None,
        Query(alias='status'),
    ] = None,
    keyword: Annotated[str | None, Query(max_length=200)] = None,
    started_after: Annotated[datetime | None, Query()] = None,
    started_before: Annotated[datetime | None, Query()] = None,
    parent_id: Annotated[int | None, Query()] = None,
) -> schemas.AnalysisTaskList:
    """構造化履歴を絞り込み、ルートまたは指定親の子処理としてページングする。"""

    query = AnalysisTaskExecution.filter(parent_id=parent_id)
    filters = Q()
    if task_type:
        filters &= Q(task_type=task_type)
    if task_status:
        filters &= Q(status=task_status)
    if keyword:
        filters &= Q(title__icontains=keyword)
    if started_after:
        filters &= Q(started_at__gte=started_after)
    if started_before:
        filters &= Q(started_at__lte=started_before)
    query = query.filter(filters)
    total = await query.count()
    items = await query.order_by('-created_at').offset((page - 1) * page_size).limit(page_size)
    return schemas.AnalysisTaskList(
        total=total,
        page=page,
        page_size=page_size,
        items=[SerializeExecution(item, current_user.is_admin) for item in items],
    )


@router.get('/{execution_id}', response_model=schemas.AnalysisTaskDetail)
async def AnalysisTaskDetailAPI(
    execution_id: int,
    current_user: Annotated[User, Depends(GetCurrentUser)],
) -> schemas.AnalysisTaskDetail:
    """単一履歴の段階、子処理、既存CMロゴ生成試行を返す。"""

    execution = await AnalysisTaskExecution.get_or_none(id=execution_id)
    if execution is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Analysis task was not found')
    children = await AnalysisTaskExecution.filter(parent_id=execution.id).order_by('created_at')
    attempts: list[CMLogoGenerationAttempt] = []
    if execution.cm_logo_generation_batch_id is not None:
        attempts = await CMLogoGenerationAttempt.filter(
            batch_id=execution.cm_logo_generation_batch_id,
        ).order_by('id')
    return schemas.AnalysisTaskDetail(
        execution=SerializeExecution(execution, current_user.is_admin),
        children=[SerializeExecution(item, current_user.is_admin) for item in children],
        logo_attempts=[schemas.AnalysisTaskLogoAttempt.model_validate(item, from_attributes=True) for item in attempts],
    )
