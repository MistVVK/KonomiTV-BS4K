from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import ClassVar, Literal, cast

import anyio
from tortoise import transactions

from app import logging
from app.config import Config
from app.constants import BS4K_VERSION, JST
from app.metadata.AnalysisTaskTracker import AnalysisTaskHandle, AnalysisTaskTracker
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
    KonomiTVBS4KLogicalAudioRebuildPreference,
    KonomiTVBS4KLogicalAudioRebuildStrategy,
)
from app.metadata.CMChapterFile import (
    ChapterFingerprint,
    CMChapterPathKind,
    CMChapterPathSelection,
    CMChapterReadResult,
    ReadCMChapterFileAsync,
    SelectCMChapterPath,
)
from app.metadata.CMLogoScanner import CMLogoScanner
from app.metadata.CMLogoSelector import CMLogoSelection, CMLogoSelector
from app.metadata.KonomiTVBS4KChapterFile import (
    BuildKonomiTVBS4KChapterFile,
    CommitKonomiTVBS4KChapterFile,
    GetKonomiTVBS4KChapterPath,
    KonomiTVBS4KChapterConflictError,
    KonomiTVBS4KChapterReadResult,
    ReadKonomiTVBS4KChapterFileAsync,
)
from app.models.CMAnalysis import (
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
from app.utils.HostPath import ToHostPath, ToUserHostPathText


CMAnalysisIntent = Literal['DetectCM', 'CMChapterSync', 'CMDetection', 'CMRegeneration']
CMChapterReadResultType = CMChapterReadResult | KonomiTVBS4KChapterReadResult


class CMAnalysisOrchestrator:
    """録画の既存 chapter 同期と汎用 CM 解析を一貫した状態契約で実行する。"""

    _KONOMITV_BS4K_LOGICAL_AUDIO_REBUILD_STRATEGY_KEY = (
        'konomitv_bs4k_logical_audio_rebuild_strategy'
    )
    _KONOMITV_BS4K_LOGICAL_AUDIO_REBUILD_PREFERENCE_KEY = (
        'konomitv_bs4k_logical_audio_rebuild_preference'
    )
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
        *,
        existing_handle: AnalysisTaskHandle | None = None,
    ) -> RecordedVideoCMAnalysis | None:
        """chapter を優先して同期し、必要な場合だけ新しい解析試行を開始する。

        Args:
            recorded_video_id: 解析対象のRecordedVideo ID。
            intent: 自動同期または明示的な再判定を表す実行意図。
            existing_handle: APIが先にQueuedで永続化した解析履歴。指定時は同じ履歴へ進捗を記録する。

        Returns:
            最新のCM解析状態。対象録画が消えている場合はNone。
        """

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
            # chapter同期で旧pipelineの生成YAMLをPendingへ戻すと、直前の
            # MediaPreparationFailedはerror_codeから消える。更新前の状態と現在入力を
            # ここで照合し、解析semaphore待機をまたいでも再構築方針を失わないようにする。
            logical_audio_rebuild_preference = self._konomiTVBS4KLogicalAudioRebuildPreference(
                state,
                input_fingerprint,
            )
            if logical_audio_rebuild_preference == 'FFmpegAfterPreviousFailure':
                state.runtime_fingerprint = {
                    **(state.runtime_fingerprint or {}),
                    self._KONOMITV_BS4K_LOGICAL_AUDIO_REBUILD_PREFERENCE_KEY: (
                        logical_audio_rebuild_preference
                    ),
                }
            state.input_fingerprint = input_fingerprint

            chapter_selection = await self._selectChapterPath(recorded_path)
            chapter_result = await self._readChapterFile(chapter_selection, recorded_video.duration)
            if chapter_result.status in ('Valid', 'ValidNoCM'):
                if chapter_selection.kind == 'Canonical':
                    assert isinstance(chapter_result, KonomiTVBS4KChapterReadResult)
                    provenance = chapter_result.provenance
                    if provenance is not None and provenance.source == 'Manual':
                        # 手書きYAML、またはKonomiTV-BS4K生成YAMLのchaptersを人が編集した結果は
                        # 利用者入力として常に保護する。
                        return await self._publishChapterResult(
                            recorded_video,
                            state,
                            chapter_result,
                            source='Manual',
                            path_kind='Canonical',
                            input_fingerprint=input_fingerprint,
                            pipeline_version=None,
                            runtime_fingerprint=None,
                            used_logo_id=None,
                        )

                    generated_input_matches = self._isGeneratedChapterForInput(
                        chapter_result,
                        input_fingerprint,
                    )
                    generated_pipeline_matches = self._isGeneratedChapterForInput(
                        chapter_result,
                        input_fingerprint,
                        pipeline_version=GenericCMAnalyzer.ANALYZER_VERSION,
                    )
                    if generated_pipeline_matches:
                        # YAML自身の検証済みgenerator/recording情報を所有権の根拠にする。
                        # これによりSQLiteを失っても生成済み結果をsidecarから復元できる。
                        if intent != 'CMRegeneration':
                            same_published_result = (
                                published_result is not None
                                and published_result.source == 'Generated'
                                and self._sameChapterFingerprint(
                                    published_result.chapter_fingerprint,
                                    chapter_result.fingerprint,
                                )
                            )
                            previous_generated_result = published_result if same_published_result else None
                            generator = provenance.generator if provenance is not None else None
                            return await self._publishChapterResult(
                                recorded_video,
                                state,
                                chapter_result,
                                source='Generated',
                                path_kind='Canonical',
                                input_fingerprint=input_fingerprint,
                                pipeline_version=generator.pipeline_version if generator is not None else None,
                                runtime_fingerprint=(
                                    previous_generated_result.runtime_fingerprint
                                    if previous_generated_result is not None
                                    else None
                                ),
                                used_logo_id=(
                                    previous_generated_result.used_logo_id
                                    if previous_generated_result is not None
                                    else None
                                ),
                            )
                        # 明示的な再生成だけは、自前のYAMLを解析結果で置換する。
                    elif generated_input_matches:
                        # 自前の旧pipeline結果は利用者編集ではないため、新pipelineで置換できる。
                        state = await self._saveMissingChapter(
                            recorded_video,
                            state,
                            chapter_result,
                            chapter_selection.kind,
                            error_code='GeneratedChapterPipelineOutdated',
                        )
                        published_result = None
                        if intent == 'CMChapterSync':
                            return state
                    else:
                        # 録画内容が生成時から変わったYAMLを現在のCM結果として公開しない。
                        state = await self._saveMissingChapter(
                            recorded_video,
                            state,
                            chapter_result,
                            chapter_selection.kind,
                            error_code='GeneratedChapterInputChanged',
                        )
                        published_result = None
                        if intent in ('CMChapterSync', 'CMDetection'):
                            return state
                elif intent != 'CMRegeneration':
                    # 外部.chapter.txtは基本名方式かつ録画との対応が一意な場合だけ採用する。
                    return await self._publishChapterResult(
                        recorded_video,
                        state,
                        chapter_result,
                        source='Existing',
                        path_kind='Legacy',
                        input_fingerprint=input_fingerprint,
                        pipeline_version=None,
                        runtime_fingerprint=None,
                        used_logo_id=None,
                    )
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

            cm_settings = Config().cm_analysis
            if cm_settings.enabled is False:
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
                        cm_settings.logo_directory,
                        chapter_result,
                        chapter_selection.kind,
                        input_fingerprint,
                        logical_audio_rebuild_preference,
                        intent,
                        existing_handle=existing_handle,
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
        logo_directory: Path | None,
        chapter_result: CMChapterReadResultType,
        chapter_path_kind: CMChapterPathKind,
        input_fingerprint: dict[str, int | str],
        logical_audio_rebuild_preference: KonomiTVBS4KLogicalAudioRebuildPreference,
        intent: CMAnalysisIntent,
        *,
        existing_handle: AnalysisTaskHandle | None = None,
    ) -> RecordedVideoCMAnalysis:
        """実CM解析を構造化履歴へ記録しながら実行する。

        Args:
            recorded_video: 解析対象の録画モデル。
            state: 更新対象のCM解析状態。
            logo_directory: 共有ロゴフォルダの実行時パス。未設定時は None。
            chapter_result: 解析開始前に読み取ったchapter状態。
            chapter_path_kind: 採用対象chapterのパス種別。
            input_fingerprint: 録画入力の内容fingerprint。
            logical_audio_rebuild_preference: 状態更新前の失敗履歴から確定した音声再構築方針。
            intent: 自動同期または明示的な再判定を表す実行意図。
            existing_handle: APIが先にQueuedで永続化した解析履歴。

        Returns:
            実解析後のCM解析状態。
        """

        trigger = 'Manual' if intent in ('CMDetection', 'CMRegeneration') else 'Automatic'
        async with AnalysisTaskTracker.track(
            'CMAnalysis',
            recorded_video_id=recorded_video.id,
            title=recorded_video.recorded_program.title,
            trigger=trigger,
            existing_handle=existing_handle,
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
                if logical_audio_rebuild_preference == 'FFmpegAfterPreviousFailure':
                    # 解析開始後にサービス停止・cancelが発生しても次回再解析で方針を
                    # 失わないよう、試行中の状態にも確定済みpreferenceを含める。
                    runtime_fingerprint = {
                        **runtime_fingerprint,
                        self._KONOMITV_BS4K_LOGICAL_AUDIO_REBUILD_PREFERENCE_KEY: (
                            logical_audio_rebuild_preference
                        ),
                    }
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
                    konomitv_bs4k_logical_audio_rebuild_preference=logical_audio_rebuild_preference,
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
                    logo_directory,
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
                if result.konomitv_bs4k_logical_audio_rebuild_strategy is not None:
                    # 固定runtime fingerprintとは別に、この録画入力で実際に採用した
                    # 論理音声再構築方式を保存する。次回の同一入力再解析では、native
                    # crashを起こしたPyAV経路を再試行せずFFmpegを主経路にできる。
                    runtime_fingerprint = {
                        **runtime_fingerprint,
                        self._KONOMITV_BS4K_LOGICAL_AUDIO_REBUILD_STRATEGY_KEY: (
                            result.konomitv_bs4k_logical_audio_rebuild_strategy
                        ),
                    }
                if result.status != 'completed' or result.analyzer_version is None:
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
                current_chapter_result = await self._readChapterFile(
                    current_chapter_selection,
                    recorded_video.duration,
                )
                chapter_unchanged = (
                    current_chapter_selection.kind == chapter_path_kind
                    and current_chapter_result.fingerprint == chapter_result.fingerprint
                )
                if chapter_unchanged is False:
                    if current_chapter_result.status in ('Valid', 'ValidNoCM'):
                        # 解析中に現れた手書き・外部・現行pipelineのsidecarは優先する。
                        # 異なる録画入力や旧pipelineの生成物なら解析結果と混在させず中断する。
                        changed_source: CMResultSource
                        changed_pipeline_version: str | None = None
                        if current_chapter_selection.kind == 'Legacy':
                            changed_source = 'Existing'
                        else:
                            assert isinstance(current_chapter_result, KonomiTVBS4KChapterReadResult)
                            provenance = current_chapter_result.provenance
                            if provenance is not None and provenance.source == 'Manual':
                                changed_source = 'Manual'
                            elif self._isGeneratedChapterForInput(
                                current_chapter_result,
                                current_input_fingerprint,
                                pipeline_version=GenericCMAnalyzer.ANALYZER_VERSION,
                            ):
                                changed_source = 'Generated'
                                changed_pipeline_version = (
                                    provenance.generator.pipeline_version
                                    if provenance is not None and provenance.generator is not None
                                    else None
                                )
                            else:
                                state = await self._saveAttemptFailure(
                                    state,
                                    'Interrupted',
                                    'ChapterChangedDuringAnalysis',
                                    'A generated YAML sidecar for a different recording input or pipeline '
                                    'appeared during analysis.',
                                    input_fingerprint=current_input_fingerprint,
                                    attempt_key=None,
                                    runtime_fingerprint=runtime_fingerprint,
                                    analyzer_version=result.analyzer_version,
                                )
                                await history.finish('Interrupted', error_code=state.error_code)
                                return state
                        state.attempt_key_sha256 = None
                        state = await self._publishChapterResult(
                            recorded_video,
                            state,
                            current_chapter_result,
                            source=changed_source,
                            path_kind=current_chapter_selection.kind,
                            input_fingerprint=current_input_fingerprint,
                            pipeline_version=changed_pipeline_version,
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

                expected_yaml_fingerprint: ChapterFingerprint = (
                    chapter_result.fingerprint
                    if chapter_path_kind == 'Canonical'
                    else {'exists': False}
                )
                generated_at = datetime.now(tz=JST)
                try:
                    generated_yaml = BuildKonomiTVBS4KChapterFile(
                        result.sections,
                        recorded_video.duration,
                        application_version=BS4K_VERSION,
                        pipeline_version=result.analyzer_version,
                        generated_at=generated_at,
                        input_fingerprint=input_fingerprint,
                    )
                except ValueError as ex:
                    state = await self._saveAttemptFailure(
                        state,
                        'Failed',
                        'GeneratedChapterInvalid',
                        str(ex),
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
                    'exists': True,
                    'size': len(generated_yaml),
                    'sha256': hashlib.sha256(generated_yaml).hexdigest(),
                }
                state.used_logo_id = selected_logo.id if selected_logo is not None else None
                state.analyzer_version = result.analyzer_version
                state.attempt_key_sha256 = attempt_key
                await state.save()
                commit_future = asyncio.get_running_loop().run_in_executor(
                    None,
                    partial(
                        CommitKonomiTVBS4KChapterFile,
                        GetKonomiTVBS4KChapterPath(Path(recorded_video.file_path)),
                        result.sections,
                        recorded_video.duration,
                        application_version=BS4K_VERSION,
                        pipeline_version=result.analyzer_version,
                        generated_at=generated_at,
                        input_fingerprint=input_fingerprint,
                        expected_destination_fingerprint=expected_yaml_fingerprint,
                    ),
                )
                try:
                    committed, cancellation_during_commit = await self._awaitCommitFuture(commit_future)
                except KonomiTVBS4KChapterConflictError as ex:
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
        commit_future: asyncio.Future[KonomiTVBS4KChapterReadResult],
    ) -> tuple[KonomiTVBS4KChapterReadResult, bool]:
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

    @classmethod
    def _konomiTVBS4KLogicalAudioRebuildPreference(
        cls,
        state: RecordedVideoCMAnalysis,
        input_fingerprint: Mapping[str, int | str],
    ) -> KonomiTVBS4KLogicalAudioRebuildPreference:
        """同じ録画入力で確認済みのnative crashを再発させない再構築方針を返す。

        Args:
            state: 録画に保存されている直前のCM解析状態。
            input_fingerprint: 今回解析する録画入力のfingerprint。

        Returns:
            初回入力ではPyAV、過去の媒体準備失敗またはfallback成功後はFFmpegを選ぶ方針。
        """

        # ファイルが更新されていれば以前の失敗理由を引き継がず、現在の入力に対して
        # StreamReform準拠assemblerを改めて試す。fingerprintが欠落した旧状態も同様に扱う。
        if state.input_fingerprint != dict(input_fingerprint):
            return 'PyAV'
        previous_strategy = (
            state.runtime_fingerprint.get(cls._KONOMITV_BS4K_LOGICAL_AUDIO_REBUILD_STRATEGY_KEY)
            if state.runtime_fingerprint is not None
            else None
        )
        previous_preference = (
            state.runtime_fingerprint.get(cls._KONOMITV_BS4K_LOGICAL_AUDIO_REBUILD_PREFERENCE_KEY)
            if state.runtime_fingerprint is not None
            else None
        )
        fallback_strategies: tuple[KonomiTVBS4KLogicalAudioRebuildStrategy, ...] = (
            'FFmpegPrimaryAfterPreviousFailure',
            'FFmpegFallbackAfterSignal',
            'FFmpegFallbackAfterReportedFailure',
        )
        if (
            (state.status == 'Failed' and state.error_code == 'MediaPreparationFailed')
            or previous_preference == 'FFmpegAfterPreviousFailure'
            or previous_strategy in fallback_strategies
        ):
            return 'FFmpegAfterPreviousFailure'
        return 'PyAV'

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
        if encoder == 'NVENC':
            return 'cuda:0'
        selected_device = RecordedPlaybackCapabilityProbe.getSelectedDevice(encoder)
        if selected_device is not None:
            return f'vaapi:{selected_device}'
        devices = RecordedPlaybackBackend.discoverRenderDevices(encoder)
        return f'vaapi:{devices[0]}' if devices else None

    async def _selectChapterPath(self, recorded_path: Path) -> CMChapterPathSelection:
        yaml_path = GetKonomiTVBS4KChapterPath(recorded_path)
        if yaml_path.is_file():
            return CMChapterPathSelection(yaml_path, 'Canonical')

        prefix = str(recorded_path.parent / recorded_path.stem)
        rows = cast(
            list[str],
            await RecordedVideo.filter(file_path__startswith=prefix).values_list('file_path', flat=True),
        )
        registered_paths = {Path(path) for path in rows}
        registered_paths.add(recorded_path)
        basic_selection = SelectCMChapterPath(recorded_path, registered_paths)
        if basic_selection is not None:
            return basic_selection
        # YAMLはKonomiTV-BS4Kの唯一の出力先であり、欠落fingerprint/CASの基準にもする。
        return CMChapterPathSelection(yaml_path, 'Canonical')

    @staticmethod
    async def _readChapterFile(
        selection: CMChapterPathSelection,
        duration_sec: float,
    ) -> CMChapterReadResultType:
        """選択したsidecar形式だけを読み込み、旧完全名.chapter.txtは参照しない。"""

        if selection.kind == 'Canonical':
            return await ReadKonomiTVBS4KChapterFileAsync(selection.path, duration_sec)
        return await ReadCMChapterFileAsync(selection.path, duration_sec)

    @staticmethod
    def _isGeneratedChapterForInput(
        chapter_result: KonomiTVBS4KChapterReadResult,
        input_fingerprint: Mapping[str, int | str],
        *,
        pipeline_version: str | None = None,
    ) -> bool:
        """YAMLの生成由来・録画fingerprint・任意のpipeline版を検証する。"""

        provenance = chapter_result.provenance
        if provenance is None or provenance.source != 'Generated' or provenance.recording is None:
            return False
        if (
            pipeline_version is not None
            and (
                provenance.generator is None
                or provenance.generator.pipeline_version != pipeline_version
            )
        ):
            return False
        recording = provenance.recording
        return (
            recording.size == input_fingerprint.get('size')
            and recording.mtime_ns == input_fingerprint.get('mtime_ns')
            and recording.sample_sha256 == input_fingerprint.get('sample_sha256')
        )

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
    def _isPendingMarkerFingerprint(value: Mapping[str, object] | None) -> bool:
        return (
            value is not None
            and set(value) == {'exists', 'size', 'sha256'}
            and value.get('exists') is True
            and isinstance(value.get('sha256'), str)
        )

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
        chapter_result: CMChapterReadResultType,
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
        result: CMChapterReadResultType,
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
        state.error_message = (
            ToUserHostPathText(result.error_message)
            if result.error_message is not None
            else None
        )
        await state.save()
        return state

    @staticmethod
    async def _saveMissingChapter(
        recorded_video: RecordedVideo,
        state: RecordedVideoCMAnalysis,
        result: CMChapterReadResultType,
        path_kind: CMChapterPathKind,
        *,
        error_code: str = 'ChapterMissing',
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
            state.error_code = error_code
            state.error_message = None
            await state.save(using_db=connection)
        return state

    @staticmethod
    async def _saveSkippedState(
        state: RecordedVideoCMAnalysis,
        chapter_result: CMChapterReadResultType,
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
        state.error_message = (
            ToUserHostPathText(error_message)
            if error_message is not None
            else None
        )
        await state.save()
        return state

    @staticmethod
    async def _findMatchedExclusion(recorded_path: Path) -> str | None:
        """除外ディレクトリ設定（実行時パス）に録画が含まれるか照合する。

        Args:
            recorded_path: 照合する録画の実行時パス。

        Returns:
            一致した除外パスのホスト表現。一致しなければ None。
        """

        try:
            resolved_recorded_path = await asyncio.to_thread(recorded_path.resolve)
        except OSError:
            resolved_recorded_path = recorded_path
        # Config 上は実行時パス文字列。照合結果は UI 向けにホスト表現で返す。
        for excluded_text in Config().cm_analysis.excluded_directories:
            configured_path = Path(excluded_text)
            try:
                resolved_excluded_path = await asyncio.to_thread(configured_path.resolve)
            except OSError:
                resolved_excluded_path = configured_path
            for path_candidate in (recorded_path, resolved_recorded_path):
                for excluded_candidate in (configured_path, resolved_excluded_path):
                    if CMAnalysisOrchestrator.isPathWithinDirectory(path_candidate, excluded_candidate):
                        return str(ToHostPath(configured_path))
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
