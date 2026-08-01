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
from app.utils.HostPath import ToUserHostPathText


router = APIRouter(tags=['Analysis Tasks'], prefix='/api/analysis-tasks')


def SerializeExecution(execution: AnalysisTaskExecution, is_admin: bool) -> schemas.AnalysisTaskExecution:
    """DB実行履歴を、権限に応じてエラー本文を伏せたAPI形式へ変換する。"""

    # 保存済みパスを優先し、未移行の旧履歴だけ関連録画から補完する。
    raw_file_path = execution.file_path
    if raw_file_path is None:
        recorded_video = execution.recorded_video
        if recorded_video is not None and recorded_video.file_path:
            raw_file_path = recorded_video.file_path
    file_path = ToUserHostPathText(raw_file_path) if raw_file_path else None

    return schemas.AnalysisTaskExecution(
        id=execution.id,
        parent_id=execution.parent_id,
        recorded_video_id=execution.recorded_video_id,
        task_type=execution.task_type,
        status=execution.status,
        trigger=execution.trigger,
        title=execution.title,
        file_path=file_path,
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
    """ナビゲーションとマイページ用に実行中処理と直近5件を返す。"""

    active = (
        await AnalysisTaskExecution.filter(
            parent_id=None,
            status__in=['Queued', 'Running'],
        )
        .prefetch_related('recorded_video')
        .order_by('created_at')
        .limit(500)
    )
    active_children: list[AnalysisTaskExecution] = []
    if active:
        # 一括処理のルートにはファイルパスがないため、現在動作中の子処理も返す。
        # 完了済みの子処理は履歴詳細 API に任せ、3秒間隔の概要ポーリングが肥大化しないようにする。
        active_children = (
            await AnalysisTaskExecution.filter(
                parent_id__in=[item.id for item in active],
                status__in=['Queued', 'Running'],
            )
            .prefetch_related('recorded_video')
            .order_by('created_at')
            .limit(500)
        )
    recent = (
        await AnalysisTaskExecution.filter(
            parent_id=None,
            status__in=['Succeeded', 'Failed', 'Interrupted', 'Skipped'],
        )
        .prefetch_related('recorded_video')
        .order_by('-completed_at')
        .limit(5)
    )
    return schemas.AnalysisTaskOverview(
        active=[SerializeExecution(item, current_user.is_admin) for item in active],
        active_children=[SerializeExecution(item, current_user.is_admin) for item in active_children],
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
        # 番組名だけでなく、失敗時にファイル名だけで並ぶ履歴もパスから探せるようにする。
        filters &= Q(title__icontains=keyword) | Q(file_path__icontains=keyword)
    if started_after:
        filters &= Q(started_at__gte=started_after)
    if started_before:
        filters &= Q(started_at__lte=started_before)
    query = query.filter(filters)
    total = await query.count()
    items = await (
        query.prefetch_related('recorded_video')
        .order_by('-created_at')
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
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

    execution = await AnalysisTaskExecution.get_or_none(
        id=execution_id
    ).prefetch_related(
        'recorded_video',
    )
    if execution is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail='Analysis task was not found'
        )
    children = await (
        AnalysisTaskExecution.filter(parent_id=execution.id)
        .prefetch_related('recorded_video')
        .order_by('created_at')
    )
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
