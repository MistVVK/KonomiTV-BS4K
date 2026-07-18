from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Literal, cast

import anyio
from tortoise import transactions

from app import logging
from app.config import Config
from app.constants import JST
from app.metadata.AnalysisTaskTracker import AnalysisTaskHandle, AnalysisTaskTracker
from app.metadata.CMAnalysisPaths import ResolveCMHostPath
from app.metadata.CMAnalysisWorkspace import (
    CMAnalysisWorkspace,
    CMAnalysisWorkspaceError,
)
from app.metadata.CMAnalyzer import (
    CMAnalysisStageCallback,
    CMAnalyzer,
    CMAnalyzerRequest,
    CMAnalyzerResult,
    CMInputDescriptor,
    CMInputUnsupportedError,
    GenericCMAnalyzer,
)
from app.metadata.CMChapterFile import (
    ChapterFingerprint,
    CMChapterConflictError,
    CMChapterPathKind,
    CMChapterPathSelection,
    CMChapterReadResult,
    CommitCMChapterFile,
    GetCMChapterPath,
    ReadCMChapterFileAsync,
    SelectCMChapterPath,
)
from app.metadata.CMLogoScanner import CMLogoScanner
from app.metadata.CMLogoSelector import CMLogoSelection, CMLogoSelector
from app.models.CMAnalysis import (
    CMAnalysisExcludedDirectory,
    CMAnalysisSettings,
    CMLogo,
    CMResultSource,
    RecordedVideoCMAnalysis,
    RecordedVideoCMResult,
)
from app.models.RecordedVideo import RecordedVideo
from app.schemas import AudioTrackTimelineEntry
from app.streams.RecordedPlaybackCapabilities import (
    RecordedPlaybackBackend,
    RecordedPlaybackCapabilityProbe,
    RecordedPlaybackEncoder,
)
from app.utils.DriveIOLimiter import DriveIOLimiter


CMAnalysisIntent = Literal['DetectCM', 'CMChapterSync', 'CMDetection', 'CMRegeneration']


class CMAnalysisOrchestrator:
    """録画の既存 chapter 同期と汎用 CM 解析を一貫した状態契約で実行する。"""

    _analysis_semaphore: ClassVar[asyncio.Semaphore] = asyncio.Semaphore(1)
    _recording_locks: ClassVar[dict[int, asyncio.Lock]] = {}
    _recording_lock_users: ClassVar[dict[int, int]] = {}
    _recording_locks_guard: ClassVar[asyncio.Lock] = asyncio.Lock()

    def __init__(self, analyzer: CMAnalyzer | None = None) -> None:
        # 既存 chapter の同期は解析ランタイムがなくても動作する。
        self.analyzer = analyzer or GenericCMAnalyzer()

    @staticmethod
    def _buildAnalyzerStageCallback(history: AnalysisTaskHandle) -> CMAnalysisStageCallback:
        """Analyzerの詳細stageを進捗が後退しない形で実行履歴へ橋渡しする。

        Args:
            history: 更新対象のCM解析履歴。

        Returns:
            Analyzer requestへ渡す非同期stage callback。
        """

        highest_progress = max(0.0, min(1.0, history.execution.progress or 0.0))
        current_stage = history.execution.stage
        update_lock = asyncio.Lock()

        async def UpdateStage(stage: str, progress: float | None) -> None:
            """同時通知も直列化し、stage切替または同一stageの進捗更新を保存する。

            Args:
                stage: Analyzerが開始した処理段階。
                progress: 計画進捗値。``None`` では直前値を維持する。

            Returns:
                None
            """

            nonlocal current_stage, highest_progress
            async with update_lock:
                if progress is not None:
                    highest_progress = max(highest_progress, max(0.0, min(1.0, progress)))
                if stage == current_stage:
                    await history.setProgress(highest_progress)
                    return
                await history.setStage(stage, highest_progress)
                current_stage = stage

        return UpdateStage

    @classmethod
    @asynccontextmanager
    async def _recordingLock(cls, recorded_video_id: int) -> AsyncGenerator[None, None]:
        """同じ録画に対する scanner/API/watcher の競合を直列化する。"""

        async with cls._recording_locks_guard:
            lock = cls._recording_locks.setdefault(recorded_video_id, asyncio.Lock())
            cls._recording_lock_users[recorded_video_id] = cls._recording_lock_users.get(recorded_video_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            async with cls._recording_locks_guard:
                remaining = cls._recording_lock_users[recorded_video_id] - 1
                if remaining == 0:
                    cls._recording_lock_users.pop(recorded_video_id, None)
                    cls._recording_locks.pop(recorded_video_id, None)
                else:
                    cls._recording_lock_users[recorded_video_id] = remaining

    async def run(
        self,
        recorded_video_id: int,
        intent: CMAnalysisIntent = 'DetectCM',
    ) -> RecordedVideoCMAnalysis | None:
        """chapter を優先して同期し、必要な場合だけ新しい解析試行を開始する。"""

        async with self._recordingLock(recorded_video_id):
            recorded_video = await RecordedVideo.get_or_none(id=recorded_video_id).prefetch_related(
                'recorded_program__channel',
            )
            if recorded_video is None:
                return None
            state, _ = await RecordedVideoCMAnalysis.get_or_create(recorded_video_id=recorded_video.id)
            published_result = await RecordedVideoCMResult.get_or_none(recorded_video_id=recorded_video.id)
            recorded_path = Path(recorded_video.file_path)
            try:
                input_fingerprint = await self.buildInputFingerprint(recorded_path)
            except OSError as ex:
                return await self._saveAttemptFailure(
                    state,
                    'Failed',
                    type(ex).__name__,
                    str(ex),
                    input_fingerprint=None,
                    attempt_key=None,
                )
            saved_input_fingerprint = state.input_fingerprint
            state.input_fingerprint = input_fingerprint

            chapter_selection = await self._selectChapterPath(recorded_path)
            chapter_result = await ReadCMChapterFileAsync(chapter_selection.path, recorded_video.duration)
            if chapter_result.status in ('Valid', 'ValidNoCM'):
                published_generated_identity = (
                    published_result is not None
                    and published_result.source == 'Generated'
                    and self._sameChapterFingerprint(published_result.chapter_fingerprint, chapter_result.fingerprint)
                )
                pending_generated_identity = (
                    state.chapter_source == 'Generated'
                    and state.chapter_path_kind == 'Canonical'
                    and chapter_selection.kind == 'Canonical'
                    and self._isRecoverablePendingState(state)
                    and self._samePendingChapterContent(state.chapter_fingerprint, chapter_result.fingerprint)
                )
                if (
                    pending_generated_identity
                    and published_generated_identity is False
                    and self._sameInputFingerprint(saved_input_fingerprint, input_fingerprint)
                ):
                    # sidecar配置後・結果transaction前の停止を、durableな生成予定hashから回復する。
                    return await self._publishChapterResult(
                        recorded_video,
                        state,
                        chapter_result,
                        source='Generated',
                        path_kind='Canonical',
                        input_fingerprint=input_fingerprint,
                        pipeline_version=state.analyzer_version,
                        runtime_fingerprint=state.runtime_fingerprint,
                        used_logo_id=state.used_logo_id,
                    )
                generated_chapter_identity = published_generated_identity or pending_generated_identity
                # 外部chapterは常に保護する。KonomiTV生成chapterは公開結果として保持したまま
                # runtime/logo/streamを含むattempt keyを計算し、同一keyなら状態を変更せず返す。
                if generated_chapter_identity is False:
                    source: CMResultSource
                    if chapter_selection.kind == 'Legacy':
                        source = 'LegacyImported'
                    else:
                        source = 'Existing'
                    return await self._publishChapterResult(
                        recorded_video,
                        state,
                        chapter_result,
                        source=source,
                        path_kind=chapter_selection.kind,
                        input_fingerprint=input_fingerprint,
                        pipeline_version=None,
                        runtime_fingerprint=None,
                        used_logo_id=None,
                    )
                # 明示的な置換許可がない個別・一括再判定では、自前chapterもそのまま保持する。
                if intent == 'CMDetection':
                    return state
            elif chapter_result.status in ('Invalid', 'IOError'):
                # 壊れた sidecar も暗黙に置き換えない。公開済み結果は保持して試行状態だけ失敗にする。
                return await self._saveChapterError(state, chapter_result, chapter_selection.kind)
            else:
                # 公開結果を裏付けるsidecarが消えた場合は、same-key判定より先に失効させる。
                # DetectCMはそのまま再生成へ進み、watcher同期はPendingへ戻して終了する。
                unverified_migrated_result = (
                    published_result is not None and published_result.verified is False
                )
                pending_marker_without_sidecar = (
                    published_result is None
                    and state.chapter_source == 'Generated'
                    and state.chapter_path_kind == 'Canonical'
                    and self._isRecoverablePendingState(state)
                    and self._isPendingMarkerFingerprint(state.chapter_fingerprint)
                )
                if pending_marker_without_sidecar:
                    # 配置前にcommitが失敗したmarkerだけを片付け、決定的なFailed+keyは保持する。
                    # 次のAutomatic実行は同じkeyなら再解析せず、runtime/input変更時だけ再試行する。
                    state.chapter_source = None
                    state.chapter_fingerprint = chapter_result.fingerprint
                    state.chapter_path_kind = chapter_selection.kind
                    await state.save()
                elif (
                    unverified_migrated_result is False
                    and (
                        intent == 'CMChapterSync'
                        or published_result is not None
                        or state.chapter_source is not None
                    )
                ):
                    state = await self._saveMissingChapter(
                        recorded_video,
                        state,
                        chapter_result,
                        chapter_selection.kind,
                    )
                    published_result = None
                if intent == 'CMChapterSync' and unverified_migrated_result is False:
                    return state

            settings, _ = await CMAnalysisSettings.get_or_create(id=1, defaults={'enabled': False})
            if settings.enabled is False:
                return await self._saveSkippedState(
                    state,
                    chapter_result,
                    chapter_selection.kind,
                    'Pending',
                    'CMAnalysisDisabled',
                    None,
                )
            matched_exclusion = await self._findMatchedExclusion(recorded_path)
            if matched_exclusion is not None:
                return await self._saveSkippedState(
                    state,
                    chapter_result,
                    chapter_selection.kind,
                    'Excluded',
                    'ExcludedDirectory',
                    matched_exclusion,
                )

            async with self._analysis_semaphore:
                async with DriveIOLimiter.getSemaphore(anyio.Path(recorded_path)):
                    return await self._runTrackedAnalysis(
                        recorded_video,
                        state,
                        settings,
                        chapter_result,
                        chapter_selection.kind,
                        input_fingerprint,
                        intent,
                    )

    async def syncIfChapterChanged(self, recorded_video_id: int) -> RecordedVideoCMAnalysis | None:
        """採用対象 chapter の軽量 fingerprint が変わった場合だけ同期する。"""

        recorded_video = await RecordedVideo.get_or_none(id=recorded_video_id)
        if recorded_video is None:
            return None
        state = await RecordedVideoCMAnalysis.get_or_none(recorded_video_id=recorded_video_id)
        if state is None:
            return await self.run(recorded_video_id, 'DetectCM')
        selection = await self._selectChapterPath(Path(recorded_video.file_path))

        def GetLightweightFingerprint() -> dict[str, int | bool]:
            try:
                stat = selection.path.stat()
            except FileNotFoundError:
                return {'exists': False}
            return {
                'exists': True,
                'size': stat.st_size,
                'mtime_ns': stat.st_mtime_ns,
                'device': stat.st_dev,
                'inode': stat.st_ino,
            }

        current = await asyncio.to_thread(GetLightweightFingerprint)
        saved = state.chapter_fingerprint or {}
        if state.chapter_path_kind == selection.kind and all(saved.get(key) == value for key, value in current.items()):
            return state
        return await self.run(recorded_video_id, 'CMChapterSync')

    async def _runTrackedAnalysis(
        self,
        recorded_video: RecordedVideo,
        state: RecordedVideoCMAnalysis,
        settings: CMAnalysisSettings,
        chapter_result: CMChapterReadResult,
        chapter_path_kind: CMChapterPathKind,
        input_fingerprint: dict[str, int | str],
        intent: CMAnalysisIntent,
    ) -> RecordedVideoCMAnalysis:
        trigger = 'Manual' if intent in ('CMDetection', 'CMRegeneration') else 'Automatic'
        async with AnalysisTaskTracker.track(
            'CMAnalysis',
            recorded_video_id=recorded_video.id,
            title=recorded_video.recorded_program.title,
            trigger=trigger,
        ) as history:
            workspace: CMAnalysisWorkspace | None = None
            attempt_key: str | None = None
            runtime_fingerprint: dict[str, object] | None = None
            result_published = False
            try:
                source_path = Path(recorded_video.file_path)
                await history.setStage('LogoCatalogScanning', 0.02)
                await CMLogoScanner.scan()
                logo_selection = CMLogoSelection(status='Missing')
                runtime_fingerprint = self._runtimeFingerprint()
                hardware_device, hardware_environment = self._resolveHardwareDecodeContext()
                await history.setStage('ProbingMedia', 0.05)
                analyzer_request = CMAnalyzerRequest(
                    recorded_file_path=source_path,
                    # probeはworkspaceへ書き込まない。実解析前に作成した専用pathへ差し替える。
                    work_directory=source_path.parent / CMAnalysisWorkspace.ROOT_DIRECTORY_NAME,
                    service_id=recorded_video.recorded_program.service_id,
                    logo_paths=(logo_selection.runtime_paths if logo_selection.status == 'Selected' else ()),
                    hardware_device=hardware_device,
                    hardware_environment=hardware_environment,
                    duration_seconds=recorded_video.duration,
                    has_variable_video_format=recorded_video.has_video_stream_changes,
                )
                descriptor: CMInputDescriptor | None = None
                try:
                    descriptor = await self.analyzer.resolveInputDescriptor(analyzer_request)
                    descriptor = replace(
                        descriptor,
                        has_variable_audio_stream=self._hasVariableSelectedAudioStream(
                            recorded_video.audio_track_timeline,
                            descriptor.audio_stream_index,
                        ),
                    )
                except CMInputUnsupportedError as ex:
                    attempt_key = self.buildAttemptKey(
                        input_fingerprint,
                        runtime_fingerprint,
                        logo_selection,
                        service_id=recorded_video.recorded_program.service_id,
                        has_variable_video_format=recorded_video.has_video_stream_changes,
                    )
                    if (
                        intent not in ('CMDetection', 'CMRegeneration')
                        and self._isStableAutomaticResult(state, attempt_key)
                    ):
                        await history.finish('Skipped', error_code='AttemptKeyUnchanged')
                        return state
                    state = await self._saveAttemptFailure(
                        state,
                        'Unsupported',
                        ex.code,
                        ex.message,
                        input_fingerprint=input_fingerprint,
                        attempt_key=attempt_key,
                        runtime_fingerprint=runtime_fingerprint,
                        analyzer_version=GenericCMAnalyzer.ANALYZER_VERSION,
                    )
                    await history.finish('Failed', error_code=state.error_code)
                    return state
                except (OSError, ValueError, json.JSONDecodeError) as ex:
                    attempt_key = self.buildAttemptKey(
                        input_fingerprint,
                        runtime_fingerprint,
                        logo_selection,
                        service_id=recorded_video.recorded_program.service_id,
                        has_variable_video_format=recorded_video.has_video_stream_changes,
                    )
                    if (
                        intent not in ('CMDetection', 'CMRegeneration')
                        and self._isStableAutomaticResult(state, attempt_key)
                    ):
                        await history.finish('Skipped', error_code='AttemptKeyUnchanged')
                        return state
                    state = await self._saveAttemptFailure(
                        state,
                        'Failed',
                        'MediaProbeFailed',
                        str(ex),
                        input_fingerprint=input_fingerprint,
                        attempt_key=attempt_key,
                        runtime_fingerprint=runtime_fingerprint,
                        analyzer_version=GenericCMAnalyzer.ANALYZER_VERSION,
                    )
                    await history.finish('Failed', error_code=state.error_code, error_message=state.error_message)
                    return state

                logo_selection = await CMLogoSelector.select(
                    recorded_video,
                    settings,
                    resolved_video_size=(descriptor.width, descriptor.height) if descriptor is not None else None,
                )

                # TS は実 program ID が要求 SID と一致すると確認できた場合だけ、その SID 用ロゴを使う。
                # program 概念のない一般コンテナでは録画メタデータの SID を引き続き利用できる。
                if (
                    descriptor is not None
                    and analyzer_request.service_id is not None
                    and 'mpegts' in (descriptor.format_name or '').split(',')
                    and descriptor.program_id != analyzer_request.service_id
                ):
                    logo_selection = CMLogoSelection(status='Missing')
                analyzer_request = replace(
                    analyzer_request,
                    logo_paths=(logo_selection.runtime_paths if logo_selection.status == 'Selected' else ()),
                    input_descriptor=descriptor,
                )
                attempt_key = self.buildAttemptKey(
                    input_fingerprint,
                    runtime_fingerprint,
                    logo_selection,
                    descriptor=descriptor,
                    service_id=recorded_video.recorded_program.service_id,
                    has_variable_video_format=recorded_video.has_video_stream_changes,
                )
                if (
                    intent not in ('CMDetection', 'CMRegeneration')
                    and self._isStableAutomaticResult(state, attempt_key)
                ):
                    await history.finish('Skipped', error_code='AttemptKeyUnchanged')
                    return state
                if logo_selection.status == 'Ambiguous':
                    result = await self._saveAttemptFailure(
                        state,
                        'Failed',
                        'AmbiguousLogoSelection',
                        None,
                        input_fingerprint=input_fingerprint,
                        attempt_key=attempt_key,
                        runtime_fingerprint=runtime_fingerprint,
                    )
                    await history.finish('Failed', error_code=result.error_code)
                    return result

                # 安定skip・probe・ロゴ曖昧判定を通過した解析だけ、録画と同じFSへ大容量workspaceを作る。
                try:
                    workspace = await CMAnalysisWorkspace.create(
                        source_path,
                        recorded_video.id,
                        max(recorded_video.duration, descriptor.duration_seconds),
                    )
                except CMAnalysisWorkspaceError as ex:
                    state = await self._saveAttemptFailure(
                        state,
                        'Failed',
                        ex.error_code,
                        str(ex),
                        input_fingerprint=input_fingerprint,
                        attempt_key=attempt_key,
                        runtime_fingerprint=runtime_fingerprint,
                        analyzer_version=GenericCMAnalyzer.ANALYZER_VERSION,
                    )
                    await history.finish(
                        'Failed',
                        error_code=state.error_code,
                        error_message=state.error_message,
                    )
                    return state
                analyzer_request = replace(
                    analyzer_request,
                    work_directory=workspace.path,
                    stage_callback=self._buildAnalyzerStageCallback(history),
                )
                result = await self._analyze(
                    state,
                    analyzer_request,
                    input_fingerprint,
                    attempt_key,
                    runtime_fingerprint,
                )
                if result.status != 'completed' or result.chapter_file is None:
                    state = await self._saveStructuredAnalyzerFailure(
                        state,
                        result,
                        input_fingerprint,
                        attempt_key,
                        runtime_fingerprint,
                    )
                    await history.finish(
                        'Interrupted' if state.status == 'Interrupted' else 'Failed',
                        error_code=state.error_code,
                        error_message=state.error_message,
                    )
                    return state

                await history.setStage('Committing', 0.95)
                current_input_fingerprint = await self.buildInputFingerprint(Path(recorded_video.file_path))
                if current_input_fingerprint != input_fingerprint:
                    state = await self._saveAttemptFailure(
                        state,
                        'Interrupted',
                        'InputChangedDuringAnalysis',
                        None,
                        input_fingerprint=current_input_fingerprint,
                        attempt_key=None,
                        runtime_fingerprint=runtime_fingerprint,
                        analyzer_version=result.analyzer_version,
                    )
                    await history.finish('Interrupted', error_code=state.error_code)
                    return state

                current_chapter_selection = await self._selectChapterPath(Path(recorded_video.file_path))
                current_chapter_result = await ReadCMChapterFileAsync(
                    current_chapter_selection.path,
                    recorded_video.duration,
                )
                chapter_unchanged = (
                    current_chapter_selection.kind == chapter_path_kind
                    and current_chapter_result.fingerprint == chapter_result.fingerprint
                )
                if chapter_unchanged is False:
                    if current_chapter_result.status in ('Valid', 'ValidNoCM'):
                        # 解析中に現れた正常chapterは利用者入力として優先し、解析結果を破棄する。
                        state.attempt_key_sha256 = None
                        state = await self._publishChapterResult(
                            recorded_video,
                            state,
                            current_chapter_result,
                            source=(
                                'LegacyImported'
                                if current_chapter_selection.kind == 'Legacy'
                                else 'Existing'
                            ),
                            path_kind=current_chapter_selection.kind,
                            input_fingerprint=current_input_fingerprint,
                            pipeline_version=None,
                            runtime_fingerprint=None,
                            used_logo_id=None,
                        )
                        await history.finish('Skipped', error_code='ChapterChangedDuringAnalysis')
                        return state
                    if current_chapter_result.status in ('Invalid', 'IOError'):
                        state = await self._saveChapterError(
                            state,
                            current_chapter_result,
                            current_chapter_selection.kind,
                        )
                        await history.finish('Failed', error_code=state.error_code)
                        return state
                    state = await self._saveAttemptFailure(
                        state,
                        'Interrupted',
                        'ChapterChangedDuringAnalysis',
                        None,
                        input_fingerprint=current_input_fingerprint,
                        attempt_key=None,
                        runtime_fingerprint=runtime_fingerprint,
                        analyzer_version=result.analyzer_version,
                    )
                    await history.finish('Interrupted', error_code=state.error_code)
                    return state

                expected_canonical_fingerprint: ChapterFingerprint = (
                    chapter_result.fingerprint
                    if chapter_path_kind == 'Canonical'
                    else {'exists': False}
                )
                generated_chapter = await ReadCMChapterFileAsync(result.chapter_file, recorded_video.duration)
                if generated_chapter.status not in ('Valid', 'ValidNoCM'):
                    state = await self._saveAttemptFailure(
                        state,
                        'Failed',
                        'GeneratedChapterInvalid',
                        generated_chapter.error_message,
                        input_fingerprint=input_fingerprint,
                        attempt_key=attempt_key,
                        runtime_fingerprint=runtime_fingerprint,
                        analyzer_version=result.analyzer_version,
                    )
                    await history.finish('Failed', error_code=state.error_code)
                    return state
                selected_logo = self._matchSelectedLogo(logo_selection, result.matched_logo)
                # ファイル配置とDB transactionの間で停止しても所有権を回復できるよう、
                # 生成予定の内容hashを先にdurable保存する（配置実体のfingerprintは公開時に更新）。
                state.chapter_source = 'Generated'
                state.chapter_path_kind = 'Canonical'
                state.chapter_fingerprint = {
                    key: value
                    for key, value in generated_chapter.fingerprint.items()
                    if key in ('exists', 'size', 'sha256')
                }
                state.used_logo_id = selected_logo.id if selected_logo is not None else None
                state.analyzer_version = result.analyzer_version
                state.attempt_key_sha256 = attempt_key
                await state.save()
                commit_future = asyncio.get_running_loop().run_in_executor(
                    None,
                    CommitCMChapterFile,
                    result.chapter_file,
                    GetCMChapterPath(Path(recorded_video.file_path)),
                    recorded_video.duration,
                    expected_canonical_fingerprint,
                )
                try:
                    committed, cancellation_during_commit = await self._awaitCommitFuture(commit_future)
                except CMChapterConflictError as ex:
                    state.chapter_source = None
                    state = await self._saveAttemptFailure(
                        state,
                        'Interrupted',
                        'ChapterChangedDuringCommit',
                        str(ex),
                        input_fingerprint=current_input_fingerprint,
                        attempt_key=None,
                        runtime_fingerprint=runtime_fingerprint,
                        analyzer_version=result.analyzer_version,
                    )
                    await history.finish('Interrupted', error_code=state.error_code)
                    return state
                except (OSError, ValueError) as ex:
                    state = await self._saveAttemptFailure(
                        state,
                        'Failed',
                        'ChapterCommitFailed',
                        str(ex),
                        input_fingerprint=input_fingerprint,
                        attempt_key=attempt_key,
                        runtime_fingerprint=runtime_fingerprint,
                        analyzer_version=result.analyzer_version,
                    )
                    await history.finish('Failed', error_code=state.error_code, error_message=state.error_message)
                    return state

                state.attempt_key_sha256 = attempt_key
                published = await self._publishChapterResult(
                    recorded_video,
                    state,
                    committed,
                    source='Generated',
                    path_kind='Canonical',
                    input_fingerprint=input_fingerprint,
                    pipeline_version=result.analyzer_version,
                    runtime_fingerprint=runtime_fingerprint,
                    used_logo_id=selected_logo.id if selected_logo is not None else None,
                )
                result_published = True
                if selected_logo is not None:
                    try:
                        selected_logo.last_used_at = datetime.now(tz=JST)
                        await selected_logo.save(update_fields=['last_used_at', 'updated_at'])
                    except Exception as ex:
                        logging.warning('CM result was published, but logo usage metadata could not be updated:', exc_info=ex)
                try:
                    await history.finish('Succeeded', summary={
                        'cm_section_count': len(recorded_video.cm_sections or []),
                        'used_logo_id': published.used_logo_id,
                        'decode_mode': result.decode_mode,
                        'analysis_fps': result.analysis_fps,
                    })
                except Exception as ex:
                    logging.warning('CM result was published, but task history could not be finalized:', exc_info=ex)
                if cancellation_during_commit:
                    raise asyncio.CancelledError
                return published
            except asyncio.CancelledError:
                if result_published:
                    raise
                await self._saveAttemptFailure(
                    state,
                    'Interrupted',
                    'Interrupted',
                    None,
                    input_fingerprint=input_fingerprint,
                    attempt_key=attempt_key,
                    runtime_fingerprint=runtime_fingerprint,
                )
                await history.finish('Interrupted', error_code='Interrupted')
                raise
            except Exception as ex:
                logging.error(f'{recorded_video.file_path}: CM analyzer failed:', exc_info=ex)
                state = await self._saveAttemptFailure(
                    state,
                    'Interrupted',
                    type(ex).__name__,
                    str(ex),
                    input_fingerprint=input_fingerprint,
                    attempt_key=None,
                    runtime_fingerprint=runtime_fingerprint,
                )
                try:
                    await history.finish('Interrupted', error_code=state.error_code, error_message=state.error_message)
                except Exception as history_ex:
                    logging.warning('CM task history could not be finalized after an interruption:', exc_info=history_ex)
                return state
            finally:
                if workspace is not None:
                    try:
                        await workspace.cleanup()
                    except Exception as ex:
                        # workspace残骸は起動時回収できるため、公開済み結果や本来の失敗を上書きしない。
                        logging.warning('[CMAnalysisWorkspace] Failed to clean the completed workspace:', exc_info=ex)

    async def _analyze(
        self,
        state: RecordedVideoCMAnalysis,
        request: CMAnalyzerRequest,
        input_fingerprint: dict[str, int | str],
        attempt_key: str,
        runtime_fingerprint: dict[str, object],
    ) -> CMAnalyzerResult:
        """放送種別やcodecを推測せず、登録ファイルをそのまま汎用解析器へ渡す。"""

        state.status = 'Analyzing'
        state.input_fingerprint = input_fingerprint
        state.attempt_key_sha256 = attempt_key
        state.runtime_fingerprint = runtime_fingerprint
        state.started_at = datetime.now(tz=JST)
        state.finished_at = None
        state.completed_at = None
        state.error_code = None
        state.error_message = None
        state.matched_exclusion_path = None
        await state.save()

        return await self.analyzer.analyze(request)

    @staticmethod
    async def _awaitCommitFuture(
        commit_future: asyncio.Future[CMChapterReadResult],
    ) -> tuple[CMChapterReadResult, bool]:
        """executor commitをキャンセルから保護し、完了をjoinしてから要求有無と結果を返す。"""

        cancellation_requested = False
        while commit_future.done() is False:
            try:
                # wait() のキャンセルは集合内 Future へ伝播しない。shield の一時 Future を
                # 作り直す方式を避け、executor の完了通知を確実に一つの Future で受ける。
                await asyncio.wait((commit_future,))
            except asyncio.CancelledError:
                # run_in_executor Futureはall_tasks()の一括cancel対象外。録画lock/tempを
                # 解放する前にworkerを必ずjoinし、sidecarの後着を防ぐ。
                cancellation_requested = True
                current_task = asyncio.current_task()
                if current_task is not None:
                    current_task.uncancel()
        return commit_future.result(), cancellation_requested

    @staticmethod
    def _matchSelectedLogo(selection: CMLogoSelection, matched_name: str | None) -> CMLogo | None:
        if matched_name is None:
            return None
        return next(
            (
                logo
                for logo, runtime_path in zip(selection.logos, selection.runtime_paths, strict=True)
                if runtime_path.name == matched_name
            ),
            None,
        )

    def _runtimeFingerprint(self) -> dict[str, object]:
        return self.analyzer.runtimeFingerprint

    @staticmethod
    def buildAttemptKey(
        input_fingerprint: Mapping[str, int | str],
        runtime_fingerprint: Mapping[str, object],
        logo_selection: CMLogoSelection,
        *,
        descriptor: CMInputDescriptor | None = None,
        service_id: int | None = None,
        has_variable_video_format: bool = False,
    ) -> str:
        """入力・ランタイム・ロゴ選択を束ねた決定的な自動再試行キーを返す。"""

        payload = {
            'input': dict(input_fingerprint),
            'runtime': dict(runtime_fingerprint),
            'service_id': service_id,
            'has_variable_video_format': has_variable_video_format,
            'descriptor': descriptor.toJSON() if descriptor is not None else None,
            'logo_selection': logo_selection.status,
            'logos': [
                {'id': logo.id, 'sha256': logo.file_hash, 'path': str(path)}
                for logo, path in zip(logo_selection.logos, logo_selection.runtime_paths, strict=True)
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8'),
        ).hexdigest()

    @staticmethod
    def _hasVariableSelectedAudioStream(
        audio_timeline: list[AudioTrackTimelineEntry],
        selected_stream_index: int | None,
    ) -> bool:
        """選択音声streamが別音声の存在区間で消える、途中交代を検出する。"""

        if selected_stream_index is None or len(audio_timeline) == 0:
            return False
        selected_was_observed = False
        alternative_interval_was_observed = False
        for interval in audio_timeline:
            try:
                interval_duration = float(interval['end_time']) - float(interval['start_time'])
            except (KeyError, TypeError, ValueError):
                continue
            if interval_duration <= 1.0:
                continue
            stream_indexes = {
                int(stream_index)
                for track in interval.get('tracks', [])
                if (stream_index := track.get('stream_index')) is not None
            }
            if selected_stream_index in stream_indexes:
                selected_was_observed = True
            elif len(stream_indexes) > 0:
                alternative_interval_was_observed = True
        # 対応付け不能な旧タイムラインは推測で拒否せず、実probe結果を使う。
        return selected_was_observed and alternative_interval_was_observed

    @staticmethod
    def _isStableAutomaticResult(state: RecordedVideoCMAnalysis, attempt_key: str) -> bool:
        return state.status in ('Completed', 'Failed', 'Unsupported') and state.attempt_key_sha256 == attempt_key

    @staticmethod
    def _resolveHardwareDecodeContext() -> tuple[str | None, dict[str, str] | None]:
        """設定済みGPUを高速化候補にする。入力codecや放送種別には依存しない。"""

        general = Config().general
        configured = (general.encoder, general.encoder_bs4k)
        for encoder in dict.fromkeys(configured):
            device = CMAnalysisOrchestrator._resolveHardwareDecodeDevice(encoder)
            if device is None:
                continue
            try:
                return device, RecordedPlaybackBackend.getEnvironment(encoder)
            except (OSError, RuntimeError):
                continue
        return None, None

    @staticmethod
    def _resolveHardwareDecodeDevice(encoder: RecordedPlaybackEncoder) -> str | None:
        if encoder == 'FFmpeg':
            return None
        if encoder == 'NVEncC':
            return 'cuda:0'
        selected_device = RecordedPlaybackCapabilityProbe.getSelectedDevice(encoder)
        if selected_device is not None:
            return f'vaapi:{selected_device}'
        devices = RecordedPlaybackBackend.discoverRenderDevices(encoder)
        return f'vaapi:{devices[0]}' if devices else None

    async def _selectChapterPath(self, recorded_path: Path) -> CMChapterPathSelection:
        prefix = str(recorded_path.parent / recorded_path.stem)
        rows = cast(
            list[str],
            await RecordedVideo.filter(file_path__startswith=prefix).values_list('file_path', flat=True),
        )
        registered_paths = {Path(path) for path in rows}
        registered_paths.add(recorded_path)
        return SelectCMChapterPath(recorded_path, registered_paths)

    @staticmethod
    def _sameChapterFingerprint(
        left: Mapping[str, object] | None,
        right: Mapping[str, object] | None,
    ) -> bool:
        if left is None or right is None:
            return False
        # 内容だけでなく配置実体も一致した時だけ、自前生成chapterの所有権が継続しているとみなす。
        # device/inode導入前の保存値は size/mtime/hash で互換判定し、新しい保存値では置換も検出する。
        required_keys = ('sha256', 'size', 'mtime_ns')
        if any(left.get(key) != right.get(key) for key in required_keys):
            return False
        if not isinstance(left.get('sha256'), str):
            return False
        for key in ('device', 'inode'):
            if key in left and left.get(key) != right.get(key):
                return False
        return True

    @staticmethod
    def _samePendingChapterContent(
        left: Mapping[str, object] | None,
        right: Mapping[str, object] | None,
    ) -> bool:
        """commit直前に保存した生成予定hashと、配置済みsidecarの内容を照合する。"""

        if not CMAnalysisOrchestrator._isPendingMarkerFingerprint(left) or right is None:
            return False
        assert left is not None
        return left.get('sha256') == right.get('sha256') and left.get('size') == right.get('size')

    @staticmethod
    def _isPendingMarkerFingerprint(value: Mapping[str, object] | None) -> bool:
        return (
            value is not None
            and set(value) == {'exists', 'size', 'sha256'}
            and value.get('exists') is True
            and isinstance(value.get('sha256'), str)
        )

    @staticmethod
    def _sameInputFingerprint(
        left: Mapping[str, object] | None,
        right: Mapping[str, object] | None,
    ) -> bool:
        """pending結果は解析時と現在の録画内容が完全一致する場合だけ回復する。"""

        return left is not None and right is not None and dict(left) == dict(right)

    @staticmethod
    def _isRecoverablePendingState(state: RecordedVideoCMAnalysis) -> bool:
        return (
            state.status in ('Analyzing', 'Interrupted')
            or (state.status == 'Failed' and state.error_code == 'ChapterCommitFailed')
        )

    @staticmethod
    async def _publishChapterResult(
        recorded_video: RecordedVideo,
        state: RecordedVideoCMAnalysis,
        chapter_result: CMChapterReadResult,
        *,
        source: CMResultSource,
        path_kind: CMChapterPathKind,
        input_fingerprint: dict[str, int | str],
        pipeline_version: str | None,
        runtime_fingerprint: dict[str, object] | None,
        used_logo_id: int | None,
    ) -> RecordedVideoCMAnalysis:
        """検証済み chapter・公開結果・録画 CM 区間を一つのtransactionで確定する。"""

        now = datetime.now(tz=JST)
        async with transactions.in_transaction() as connection:
            recorded_video.cm_sections = list(chapter_result.sections)
            await recorded_video.save(using_db=connection, update_fields=['cm_sections', 'updated_at'])

            state.status = 'Completed'
            state.chapter_source = 'Generated' if source == 'Generated' else 'Existing'
            state.chapter_path_kind = path_kind
            state.input_fingerprint = input_fingerprint
            state.chapter_fingerprint = chapter_result.fingerprint
            state.chapter_last_read_at = now
            state.used_logo_id = used_logo_id
            state.analyzer_version = pipeline_version
            state.runtime_fingerprint = runtime_fingerprint
            if source != 'Generated':
                state.attempt_key_sha256 = None
            state.completed_at = now
            state.finished_at = now
            state.error_code = None
            state.error_message = None
            state.matched_exclusion_path = None
            await state.save(using_db=connection)

            result = await RecordedVideoCMResult.filter(
                recorded_video_id=recorded_video.id,
            ).using_db(connection).first()
            if result is None:
                result = await RecordedVideoCMResult.create(
                    recorded_video_id=recorded_video.id,
                    source=source,
                    verified=True,
                    input_fingerprint=input_fingerprint,
                    chapter_fingerprint=chapter_result.fingerprint,
                    chapter_path_kind=path_kind,
                    pipeline_version=pipeline_version,
                    runtime_fingerprint=runtime_fingerprint,
                    used_logo_id=used_logo_id,
                    published_at=now,
                    using_db=connection,
                )
            else:
                result.source = source
                result.verified = True
                result.input_fingerprint = input_fingerprint
                result.chapter_fingerprint = chapter_result.fingerprint
                result.chapter_path_kind = path_kind
                result.pipeline_version = pipeline_version
                result.runtime_fingerprint = runtime_fingerprint
                result.used_logo_id = used_logo_id
                result.published_at = now
                await result.save(using_db=connection)
        return state

    @staticmethod
    async def _saveChapterError(
        state: RecordedVideoCMAnalysis,
        result: CMChapterReadResult,
        path_kind: CMChapterPathKind,
    ) -> RecordedVideoCMAnalysis:
        state.status = 'Failed'
        state.chapter_path_kind = path_kind
        state.chapter_fingerprint = result.fingerprint
        state.chapter_last_read_at = datetime.now(tz=JST)
        state.attempt_key_sha256 = None
        state.finished_at = datetime.now(tz=JST)
        state.completed_at = None
        state.error_code = result.error_code or result.status
        state.error_message = result.error_message
        await state.save()
        return state

    @staticmethod
    async def _saveMissingChapter(
        recorded_video: RecordedVideo,
        state: RecordedVideoCMAnalysis,
        result: CMChapterReadResult,
        path_kind: CMChapterPathKind,
    ) -> RecordedVideoCMAnalysis:
        """chapter削除を公開結果へ反映し、次の解析が可能なPendingへ戻す。"""

        now = datetime.now(tz=JST)
        async with transactions.in_transaction() as connection:
            recorded_video.cm_sections = None
            await recorded_video.save(using_db=connection, update_fields=['cm_sections', 'updated_at'])
            await RecordedVideoCMResult.filter(recorded_video_id=recorded_video.id).using_db(connection).delete()
            state.status = 'Pending'
            state.chapter_source = None
            state.chapter_path_kind = path_kind
            state.chapter_fingerprint = result.fingerprint
            state.chapter_last_read_at = now
            state.used_logo_id = None
            state.attempt_key_sha256 = None
            state.completed_at = None
            state.finished_at = now
            state.error_code = 'ChapterMissing'
            state.error_message = None
            await state.save(using_db=connection)
        return state

    @staticmethod
    async def _saveSkippedState(
        state: RecordedVideoCMAnalysis,
        chapter_result: CMChapterReadResult,
        path_kind: CMChapterPathKind,
        status: Literal['Pending', 'Excluded'],
        error_code: str,
        matched_exclusion: str | None,
    ) -> RecordedVideoCMAnalysis:
        state.status = status
        state.chapter_path_kind = path_kind
        state.chapter_fingerprint = chapter_result.fingerprint
        state.completed_at = None
        state.finished_at = datetime.now(tz=JST)
        state.error_code = error_code
        state.error_message = None
        state.matched_exclusion_path = matched_exclusion
        await state.save()
        return state

    @staticmethod
    async def _saveStructuredAnalyzerFailure(
        state: RecordedVideoCMAnalysis,
        result: CMAnalyzerResult,
        input_fingerprint: dict[str, int | str],
        attempt_key: str,
        runtime_fingerprint: dict[str, object],
    ) -> RecordedVideoCMAnalysis:
        status: Literal['Failed', 'Unsupported', 'Interrupted']
        if result.status == 'unsupported':
            status = 'Unsupported'
        elif result.status == 'interrupted':
            status = 'Interrupted'
        else:
            status = 'Failed'
        return await CMAnalysisOrchestrator._saveAttemptFailure(
            state,
            status,
            result.error_code or result.status,
            result.error_message,
            input_fingerprint=input_fingerprint,
            attempt_key=attempt_key,
            runtime_fingerprint=runtime_fingerprint,
            analyzer_version=result.analyzer_version,
        )

    @staticmethod
    async def _saveAttemptFailure(
        state: RecordedVideoCMAnalysis,
        status: Literal['Failed', 'Unsupported', 'Interrupted'],
        error_code: str,
        error_message: str | None,
        *,
        input_fingerprint: dict[str, int | str] | None,
        attempt_key: str | None,
        runtime_fingerprint: dict[str, object] | None = None,
        analyzer_version: str | None = None,
    ) -> RecordedVideoCMAnalysis:
        """公開済み結果を変更せず、最新試行だけを終端状態へ更新する。"""

        state.status = status
        if input_fingerprint is not None:
            state.input_fingerprint = input_fingerprint
        state.attempt_key_sha256 = attempt_key
        state.runtime_fingerprint = runtime_fingerprint
        state.analyzer_version = analyzer_version
        state.completed_at = None
        state.finished_at = datetime.now(tz=JST)
        state.error_code = error_code
        state.error_message = error_message
        await state.save()
        return state

    @staticmethod
    async def _findMatchedExclusion(recorded_path: Path) -> str | None:
        try:
            resolved_recorded_path = await asyncio.to_thread(recorded_path.resolve)
        except OSError:
            resolved_recorded_path = recorded_path
        for excluded in await CMAnalysisExcludedDirectory.filter(enabled=True):
            configured_path = Path(excluded.path)
            for path_candidate in (recorded_path, resolved_recorded_path):
                for excluded_candidate in (
                    configured_path,
                    ResolveCMHostPath(configured_path),
                    Path(excluded.resolved_path),
                ):
                    if CMAnalysisOrchestrator.isPathWithinDirectory(path_candidate, excluded_candidate):
                        return excluded.path
        return None

    @staticmethod
    def isPathWithinDirectory(path: Path, directory: Path) -> bool:
        try:
            path.relative_to(directory)
            return True
        except ValueError:
            return False

    @staticmethod
    async def buildInputFingerprint(recorded_path: Path) -> dict[str, int | str]:
        """statに加えて先頭・中央・末尾を採取し、内容変更に追従する軽量fingerprintを作る。"""

        def Build() -> dict[str, int | str]:
            stat_before = recorded_path.stat()
            digest = hashlib.sha256()
            sample_size = 64 * 1024
            with recorded_path.open('rb') as file:
                for position in dict.fromkeys((0, max(0, stat_before.st_size // 2), max(0, stat_before.st_size - sample_size))):
                    file.seek(position)
                    digest.update(position.to_bytes(8, 'little', signed=False))
                    digest.update(file.read(sample_size))
            stat_after = recorded_path.stat()
            if (
                stat_before.st_size != stat_after.st_size
                or stat_before.st_mtime_ns != stat_after.st_mtime_ns
            ):
                raise OSError('The recorded file changed while its CM fingerprint was being calculated.')
            return {
                'size': stat_after.st_size,
                'mtime_ns': stat_after.st_mtime_ns,
                'sample_sha256': digest.hexdigest(),
            }

        return await asyncio.to_thread(Build)

    @staticmethod
    async def markInterruptedAtStartup() -> int:
        """異常終了時にAnalyzingのまま残った最新試行だけをInterruptedへ移す。"""

        return await RecordedVideoCMAnalysis.filter(status='Analyzing').update(
            status='Interrupted',
            finished_at=datetime.now(tz=JST),
            error_code='ServerRestarted',
            error_message=None,
        )
