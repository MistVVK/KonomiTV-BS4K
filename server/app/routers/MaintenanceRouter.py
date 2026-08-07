
import asyncio
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Coroutine
from typing import Annotated, Any, Literal, cast

import anyio
import psutil
from fastapi import APIRouter, Depends, Path, Query, status
from fastapi.exceptions import HTTPException
from fastapi.responses import Response
from sse_starlette.sse import EventSourceResponse

from app import logging, schemas
from app.config import Config
from app.constants import (
    KONOMITV_ACCESS_LOG_PATH,
    KONOMITV_SERVER_LOG_PATH,
    RESTART_REQUIRED_LOCK_PATH,
    THUMBNAILS_DIR,
)
from app.metadata.AnalysisTaskTracker import AnalysisTaskTracker
from app.metadata.CMAnalysisOrchestrator import CMAnalysisOrchestrator
from app.metadata.CMAnalyzer import GenericCMAnalyzer
from app.metadata.RecordedScanTask import RecordedScanTask
from app.metadata.ThumbnailGenerator import ThumbnailGenerator
from app.models.AnalysisTask import AnalysisTaskExecution
from app.models.Channel import Channel
from app.models.CMAnalysis import RecordedVideoCMAnalysis
from app.models.Program import Program
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedVideo import RecordedVideo
from app.models.User import User
from app.routers.UsersRouter import GetCurrentAdminUser


# ルーター
router = APIRouter(
    tags = ['Maintenance'],
    prefix = '/api/maintenance',
)

# 録画フォルダの一括スキャン・メタデータ再解析・CM判定・バックグラウンド解析タスクの asyncio.Task インスタンス
batch_scan_task: asyncio.Task[None] | None = None
metadata_reanalysis_task: asyncio.Task[None] | None = None
cm_detection_task: asyncio.Task[None] | None = None
background_analysis_task: asyncio.Task[None] | None = None


@router.get(
    '/logs/{log_type}',
    summary = 'サーバーログストリーミング API',
    response_class = Response,
    responses = {
        status.HTTP_200_OK: {
            'description': 'サーバーログまたはアクセスログが随時配信されるイベントストリーム。',
            'content': {'text/event-stream': {}},
        }
    }
)
def LogStreamAPI(
    log_type: Annotated[Literal['server', 'access'], Path(description='ログの種類。server: サーバーログ、access: アクセスログ')],
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """
    サーバーログまたはアクセスログを Server-Sent Events で随時配信する。

    イベントには、
    - 初回にログファイルの先頭から現在の最新行までのすべての行を送信する **initial_log_update**
    - リアルタイムに追加されたログを送信する **log_update**
    の2種類がある。

    初回接続時にはログファイルの先頭から現在の最新行までのすべての行が initial_log_update イベントで一括送信され、<br>
    その後ログに更新があれば log_update イベントで1行ずつ送信される。

    ファイル I/O を伴うため敢えて同期関数として実装している。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    # ログファイルのパスを決定
    log_path = KONOMITV_SERVER_LOG_PATH if log_type == 'server' else KONOMITV_ACCESS_LOG_PATH

    # ログファイルが存在しない場合はエラー
    if not log_path.exists():
        logging.error(f'[MaintenanceRouter][LogStreamAPI] Log file not found: {log_path}')
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail = f'Log file not found: {log_path}',
        )

    # ログの変更を監視し、変更があればログ行をイベントストリームとして出力する
    def generator():
        """イベントストリームを出力するジェネレーター"""

        # ファイルを開く
        ## ログファイルは基本 UTF-8 だが、稀に外部プロセス由来の文字化けや別エンコーディングが混入し、
        ## UTF-8 としてデコードできないバイト列が含まれることがある
        ## その場合でもログストリームの配信を継続できるよう、errors='replace' でデコード不能なバイトは
        ## 置換文字 (U+FFFD) に置き換えて読み取る
        with open(log_path, encoding='utf-8', errors='replace') as f:
            # 初回接続時に全ての行を送信
            all_lines = [line.rstrip('\n') for line in f.readlines() if line.strip()]  # 空行は除外
            yield {
                'event': 'initial_log_update',
                'data': json.dumps(all_lines, ensure_ascii=False),
            }

            # ファイルの現在位置を記録
            current_position = f.tell()

            # 継続的に新しい行を監視
            while True:
                # ファイルが更新されたかチェック
                f.seek(0, os.SEEK_END)
                if f.tell() > current_position:
                    # ファイルが更新された場合、前回の位置に戻る
                    f.seek(current_position)

                    # 新しい行を読み込む
                    for line in f:
                        line = line.rstrip('\n')
                        if line:  # 空行は送信しない
                            yield {
                                'event': 'log_update',
                                'data': json.dumps(line, ensure_ascii=False),
                            }

                    # 現在位置を更新
                    current_position = f.tell()

                # 少し待機
                time.sleep(0.5)

    # EventSourceResponse でイベントストリームを配信する
    return EventSourceResponse(generator())


@router.post(
    '/update-database',
    summary = 'データベース更新 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def UpdateDatabaseAPI(
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """
    データベースに保存されている、チャンネル情報・番組情報・Twitter アカウント情報などの外部 API に依存するデータをすべて更新する。<br>
    即座に外部 API からのデータ更新を反映させたい場合に利用する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    del current_user
    await Channel.update()
    # 実況機能が無効な間は、手動 DB 更新からも実況サービスへ接続しない
    if Config().general.jikkyo_enabled is True:
        await Channel.updateJikkyoStatus()
    await Program.update(multiprocess=True)


@router.post(
    '/run-batch-scan',
    summary = '録画フォルダ一括スキャン API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def BatchScanAPI(
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """
    録画フォルダ内の全 TS ファイルをスキャンし、メタデータを解析して DB に永続化する。<br>
    追加・変更があったファイルのみメタデータを解析し、DB に永続化する。<br>
    存在しない録画ファイルに対応するレコードを一括削除する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    del current_user
    global batch_scan_task

    async def BatchScan():
        global batch_scan_task
        logging.info('Manual batch scan of recording folders has started.')
        try:
            async with AnalysisTaskTracker.track('BatchScan', trigger='Maintenance') as history:
                await history.setStage('Scanning')
                # 一括スキャンを実行
                await RecordedScanTask().runBatchScan()

            # 一括スキャンが完了した
            logging.info('Manual batch scan of recording folders has finished.')
        finally:
            batch_scan_task = None  # 再度新しいタスクを作成できるように None にする

    # タスクが実行中でない場合、新しくタスクを作成して実行
    ## asyncio.create_task() で実行することで、API への HTTP コネクションが切断されてもタスクが継続される
    if batch_scan_task is None:
        batch_scan_task = asyncio.create_task(BatchScan())
        # タスクの実行が完了するまで待機
        await batch_scan_task
    else:
        logging.warning('[MaintenanceRouter][BatchScanAPI] Batch scan of recording folders is already running.')
        raise HTTPException(
            status_code = status.HTTP_429_TOO_MANY_REQUESTS,
            detail = 'Batch scan of recording folders is already running',
        )


@router.post(
    '/reanalyze-all-recorded-videos',
    summary = '全録画ファイルメタデータ再解析 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def ReanalyzeAllRecordedVideosAPI(
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """
    データベースに登録されているすべての録画ファイルのメタデータを強制的に再解析する。<br>
    録画ごとの処理順序を保ちつつ、別録画の解析パイプラインは上限付きで並行実行する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    del current_user
    global metadata_reanalysis_task

    async def ReanalyzeAllRecordedVideos():
        global metadata_reanalysis_task
        logging.info('Manual metadata reanalysis of all recorded videos has started.')

        try:
            file_paths = cast(
                list[str],
                await RecordedVideo.all().order_by('id').values_list('file_path', flat=True),
            )
            total = len(file_paths)
            completed_count = 0
            missing_count = 0
            completed_count_lock = asyncio.Lock()

            pipeline_semaphore = asyncio.Semaphore(RecordedScanTask.BATCH_PIPELINE_CONCURRENCY)
            async with AnalysisTaskTracker.track(
                'BatchMetadataReanalysis',
                trigger='Maintenance',
                total_count=total,
            ) as history:
                await history.setStage('Processing', 0.0)

                async def ReanalyzeRecordedVideo(index: int, file_path_str: str) -> None:
                    nonlocal completed_count, missing_count
                    file_path = anyio.Path(file_path_str)
                    if not await file_path.is_file():
                        logging.warning(f'{file_path}: File not found. Skipping metadata reanalysis...')
                        is_missing = True
                    else:
                        is_missing = False
                        logging.info(f'{file_path}: Reanalyzing metadata... ({index}/{total})')
                        async with pipeline_semaphore:
                            await RecordedScanTask().processRecordedFile(
                                file_path=file_path,
                                analysis_request='MetadataReanalysis',
                            )
                    async with completed_count_lock:
                        completed_count += 1
                        missing_count += int(is_missing)
                        await history.setCounts(current=completed_count, total=total)

                await asyncio.gather(*(
                    ReanalyzeRecordedVideo(index, file_path_str)
                    for index, file_path_str in enumerate(file_paths, start=1)
                ))
                child_statuses = cast(
                    list[str],
                    await AnalysisTaskExecution.filter(parent_id=history.execution.id).values_list('status', flat=True),
                )
                await history.setCounts(
                    current=total,
                    total=total,
                    succeeded=child_statuses.count('Succeeded'),
                    failed=child_statuses.count('Failed'),
                    skipped=missing_count + child_statuses.count('Skipped'),
                )

            logging.info('Manual metadata reanalysis of all recorded videos has finished.')
        finally:
            metadata_reanalysis_task = None

    if metadata_reanalysis_task is None:
        metadata_reanalysis_task = asyncio.create_task(ReanalyzeAllRecordedVideos())
        await metadata_reanalysis_task
    else:
        logging.warning('[MaintenanceRouter][ReanalyzeAllRecordedVideosAPI] Metadata reanalysis is already running.')
        raise HTTPException(
            status_code = status.HTTP_429_TOO_MANY_REQUESTS,
            detail = 'Metadata reanalysis of all recorded videos is already running',
        )


@router.post(
    '/detect-cm-sections-for-all-recorded-videos',
    summary = '全録画ファイル CM 区間再判定 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def DetectCMSectionsForAllRecordedVideosAPI(
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
    replace_existing_chapter: Annotated[
        bool,
        Query(
            description=(
                'KonomiTV-BS4K自動解析YAMLを再解析して置換する。また、基本名方式の外部.chapter.txtは保持したまま'
                'KonomiTV-BS4K YAMLを新規生成する。手動編集YAMLは保護する。'
            ),
        ),
    ] = False,
):
    """
    登録済み録画を再判定し、明示指定時だけKonomiTV-BS4K自動解析YAMLを再生成する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    del current_user
    global cm_detection_task

    async def DetectCMSectionsForAllRecordedVideos() -> None:
        global cm_detection_task
        logging.info('Manual CM section detection of all recorded videos has started.')
        try:
            video_rows = await RecordedVideo.filter(status='Recorded').order_by('id').values(
                'id',
                'file_path',
                'duration',
            )
            total = len(video_rows)
            completed_count = 0
            failed_count = 0
            skipped_count = 0
            count_lock = asyncio.Lock()
            async with AnalysisTaskTracker.track(
                'BatchCMAnalysis',
                trigger='Maintenance',
                total_count=total,
            ) as history:
                await history.setStage('Processing', 0.0)

                async def DetectRecordedVideoCM(index: int, video_row: dict[str, Any]) -> None:
                    nonlocal completed_count, failed_count, skipped_count
                    file_path = anyio.Path(video_row['file_path'])
                    item_failed = False
                    item_skipped = False
                    try:
                        if not await file_path.is_file():
                            logging.warning(f'{file_path}: File not found. Skipping CM section detection...')
                            item_skipped = True
                        else:
                            logging.info(f'{file_path}: Detecting CM sections... ({index}/{total})')
                            if replace_existing_chapter:
                                result = await CMAnalysisOrchestrator().run(video_row['id'], 'CMRegeneration')
                            else:
                                await RecordedScanTask().processRecordedFile(
                                    file_path=file_path,
                                    analysis_request='CMDetection',
                                )
                                result = await RecordedVideoCMAnalysis.get_or_none(recorded_video_id=video_row['id'])
                            item_failed = result is not None and result.status in ('Failed', 'Unsupported', 'Interrupted')
                            item_skipped = result is not None and result.status in ('Pending', 'Excluded')
                    except Exception:
                        item_failed = True
                        raise
                    finally:
                        async with count_lock:
                            completed_count += 1
                            failed_count += int(item_failed)
                            skipped_count += int(item_skipped)
                            await history.setCounts(
                                current=completed_count,
                                total=total,
                                succeeded=completed_count - failed_count - skipped_count,
                                failed=failed_count,
                                skipped=skipped_count,
                            )

                await asyncio.gather(*(
                    DetectRecordedVideoCM(index, video_row)
                    for index, video_row in enumerate(video_rows, start=1)
                ))
            logging.info('Manual CM section detection of all recorded videos has finished.')
        finally:
            cm_detection_task = None

    if cm_detection_task is not None:
        logging.warning('[MaintenanceRouter][DetectCMSectionsForAllRecordedVideosAPI] CM section detection is already running.')
        raise HTTPException(
            status_code = status.HTTP_429_TOO_MANY_REQUESTS,
            detail = 'CM section detection of all recorded videos is already running',
        )

    cm_detection_task = asyncio.create_task(DetectCMSectionsForAllRecordedVideos())
    await cm_detection_task


@router.post(
    '/run-background-analysis',
    summary = 'バックグラウンド解析タスク手動実行 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def BackgroundAnalysisAPI(
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """
    CM 区間情報が未解析の録画ファイルに対して CM 区間情報を解析し、<br>
    サムネイルが未生成の録画ファイルに対してサムネイルを生成する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    del current_user
    global background_analysis_task

    async def BackgroundAnalysis():
        global background_analysis_task
        logging.info('Manual background analysis has started.')

        try:
            # CM 区間情報やサムネイルが未生成の録画ファイルを取得
            ## 再生開始位置はオンデマンドで解決できるため、重い key_frames は取得しない
            video_rows = await RecordedVideo.filter(status='Recorded').values(
                'id',
                'recorded_program_id',
                'file_path',
                'file_hash',
                'duration',
                'cm_sections',
            )
            cm_states = {
                row['recorded_video_id']: row
                for row in await RecordedVideoCMAnalysis.all().values(
                    'recorded_video_id',
                    'status',
                    'analyzer_version',
                    'chapter_source',
                )
            }

            # 各録画ファイルに対して直列にバックグラウンド解析タスクを実行
            ## HDD は並列アクセスが遅いため、随時直列に実行していった方が結果的に早いことが多い
            ## すべて直列なので ProcessLimiter や DriveIOLimiter での制限は掛けていない
            async with AnalysisTaskTracker.track(
                'BackgroundAnalysis',
                trigger='Maintenance',
                total_count=len(video_rows),
            ) as history:
                await history.setStage('Processing', 0.0)
                for index, video_row in enumerate(video_rows, start=1):
                    file_path = anyio.Path(video_row['file_path'])
                    try:
                        if not await file_path.is_file():
                            logging.warning(f'{file_path}: File not found. Skipping...')
                            continue

                        # CM 区間検出とサムネイル生成を同時に実行
                        tasks: list[Coroutine[Any, Any, object]] = []

                        # 公開結果の有無とは独立して、未完了・失敗・旧pipelineの試行だけを候補にする。
                        # Failed/UnsupportedもOrchestrator側のattempt keyが同じなら解析器を再起動しない。
                        cm_state = cm_states.get(video_row['id'])
                        should_check_cm = (
                            video_row['cm_sections'] is None
                            or cm_state is None
                            or cm_state['status'] in ('Pending', 'Interrupted', 'Failed', 'Unsupported')
                            or cm_state['chapter_source'] == 'Generated'
                            or (
                                cm_state['status'] == 'Completed'
                                and cm_state['analyzer_version'] not in (None, GenericCMAnalyzer.ANALYZER_VERSION)
                            )
                        )
                        if should_check_cm:
                            tasks.append(CMAnalysisOrchestrator().run(video_row['id'], 'DetectCM'))

                        thumbnail_tile_path = anyio.Path(str(THUMBNAILS_DIR)) / f'{video_row["file_hash"]}_tile.webp'
                        thumbnail_path = anyio.Path(str(THUMBNAILS_DIR)) / f'{video_row["file_hash"]}.webp'
                        if (not await thumbnail_tile_path.is_file()) or (not await thumbnail_path.is_file()):
                            db_recorded_program = await RecordedProgram.all() \
                                .select_related('recorded_video') \
                                .select_related('channel') \
                                .get_or_none(id=video_row['recorded_program_id'])
                            if db_recorded_program is not None:
                                recorded_program = schemas.RecordedProgram.model_validate(
                                    db_recorded_program,
                                    from_attributes=True,
                                )

                                async def GenerateThumbnail() -> None:
                                    async with AnalysisTaskTracker.track(
                                        'ThumbnailGeneration',
                                        recorded_video_id=recorded_program.recorded_video.id,
                                        title=recorded_program.title,
                                        trigger='Maintenance',
                                    ) as thumbnail_history:
                                        await thumbnail_history.setStage('Generating')
                                        await ThumbnailGenerator.fromRecordedProgram(recorded_program).generateAndSave()

                                tasks.append(GenerateThumbnail())

                        if tasks:
                            await asyncio.gather(*tasks)

                    except Exception as ex:
                        logging.error(f'{file_path}: Error in background analysis:', exc_info=ex)
                    finally:
                        await history.setCounts(current=index, total=len(video_rows))

            # すべての録画ファイルのバックグラウンド解析が完了した
            logging.info('Manual background analysis has finished processing all recorded files.')
        finally:
            background_analysis_task = None  # 再度新しいタスクを作成できるように None にする

    # タスクが実行中でない場合、新しくタスクを作成して実行
    ## asyncio.create_task() で実行することで、API への HTTP コネクションが切断されてもタスクが継続される
    if background_analysis_task is None:
        background_analysis_task = asyncio.create_task(BackgroundAnalysis())
        # タスクの実行が完了するまで待機
        await background_analysis_task
    else:
        logging.warning('[MaintenanceRouter][BackgroundAnalysisAPI] Background analysis task is already running.')
        raise HTTPException(
            status_code = status.HTTP_429_TOO_MANY_REQUESTS,
            detail = 'Background analysis task is already running',
        )


@router.post(
    '/restart',
    summary = 'サーバー再起動 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
def ServerRestartAPI(
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """
    KonomiTV-BS4K サーバーを再起動する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    def Restart():

        # シグナルの送信対象の PID
        ## --reload フラグが付与されている場合のみ、Reloader の起動元である親プロセスの PID を利用する
        target_process = psutil.Process(os.getpid())
        if '--reload' in sys.argv:
            parent_process = target_process.parent()
            if parent_process is not None:
                target_process = parent_process

        # 現在の Uvicorn サーバーを終了する
        target_process.send_signal(signal.SIGINT)

        # Uvicorn 終了後に再起動が必要であることを示すロックファイルを作成する
        # Uvicorn 終了後、KonomiTV.py でロックファイルの存在が確認され、もし存在していればサーバー再起動が行われる
        RESTART_REQUIRED_LOCK_PATH.touch(exist_ok=True)

    # バックグラウンドでサーバー再起動を開始
    threading.Thread(target=Restart).start()


@router.post(
    '/shutdown',
    summary = 'サーバー終了 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
def ServerShutdownAPI(
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """
    KonomiTV-BS4K サーバーを終了する。<br>
    なお、PM2 環境 / Docker 環境ではサーバー終了後に自動的にプロセスが再起動されるため、事実上 /api/maintenance/restart と等価。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    def Shutdown():

        # シグナルの送信対象の PID
        ## --reload フラグが付与されている場合のみ、Reloader の起動元である親プロセスの PID を利用する
        target_process = psutil.Process(os.getpid())
        if '--reload' in sys.argv:
            parent_process = target_process.parent()
            if parent_process is not None:
                target_process = parent_process

        # 現在の Uvicorn サーバーを終了する
        target_process.send_signal(signal.SIGINT)

    # バックグラウンドでサーバー終了を開始
    threading.Thread(target=Shutdown).start()
