# pyright: reportPrivateUsage=false

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from tortoise import Tortoise

import app.metadata.RecordedEpisodeAutomation as RecordedEpisodeAutomationModule
from app.constants import JST
from app.metadata.RecordedEpisodeAutomation import RecordedEpisodeAutomation
from app.metadata.RecordedEpisodeSearch import AIEpisodeCitation, AIEpisodeLookupResult
from app.metadata.RecordedSeriesCandidates import RecordedSeriesAIError
from app.metadata.RecordedSeriesLocks import RECORDED_SERIES_RESOLUTION_LOCK
from app.metadata.RecordedSeriesSettings import (
    RecordedSeriesSettings,
    RecordedSeriesSettingsStore,
)
from app.models.Channel import Channel
from app.models.RecordedEpisode import RecordedEpisodeResolution, SeriesEpisode
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedSeries import RecordedSeriesAIRequest
from app.models.RecordedVideo import RecordedVideo
from app.models.Series import Series
from app.models.SeriesBroadcastPeriod import SeriesBroadcastPeriod
from app.schemas import Genre


ANIME_GENRES: list[Genre] = [
    {'major': 'アニメ・特撮', 'middle': '国内アニメ'},
]


async def InitializeDatabase() -> None:
    """話数自動判定に必要な録画・Episode・AI監査をインメモリDBへ初期化する。"""

    await Tortoise.init(
        db_url='sqlite://:memory:',
        modules={'models': [
            'app.models.Channel',
            'app.models.RecordedEpisode',
            'app.models.RecordedProgram',
            'app.models.RecordedSeries',
            'app.models.RecordedVideo',
            'app.models.Series',
            'app.models.SeriesBroadcastPeriod',
        ]},
        timezone='Asia/Tokyo',
    )
    await Tortoise.generate_schemas()


async def CreateChannel() -> Channel:
    """録画番組スナップショット用の放送局を作成する。"""

    return await Channel.create(
        id='NID4-SID211',
        display_channel_id='bs211',
        network_id=4,
        service_id=211,
        transport_stream_id=1,
        remocon_id=11,
        channel_number='211',
        type='BS',
        name='BS11 テスト',
        jikkyo_force=None,
        is_subchannel=False,
        is_radiochannel=False,
        is_watchable=True,
    )


async def CreateRecordedProgram(
    program_id: int,
    *,
    series: Series,
    channel: Channel,
    episode_number: str | None,
    recorded_status: str = 'Recorded',
) -> RecordedProgram:
    """自動判定テスト用のSeries所属録画と動画を作成する。"""

    day = min(program_id, 28)
    period, _created = await SeriesBroadcastPeriod.get_or_create(
        series=series,
        channel=channel,
        defaults={
            'start_date': datetime(2026, 7, 1, tzinfo=JST).date(),
            'end_date': datetime(2026, 7, 31, tzinfo=JST).date(),
        },
    )
    start_time = datetime(2026, 7, day, 20, 0, tzinfo=JST)
    program = await RecordedProgram.create(
        id=program_id,
        recording_start_margin=0.0,
        recording_end_margin=0.0,
        is_partially_recorded=False,
        channel=channel,
        network_id=channel.network_id,
        service_id=channel.service_id,
        event_id=program_id,
        series=series,
        series_broadcast_period=period,
        title=f'{series.title} {episode_number or "話数不明"}',
        series_title=series.title,
        episode_number=episode_number,
        subtitle=f'副題 {program_id}',
        description='話数自動判定のテスト番組。',
        detail={'番組内容': '検索に渡す番組詳細'},
        start_time=start_time,
        end_time=start_time + timedelta(hours=1),
        duration=3600.0,
        is_free=True,
        genres=ANIME_GENRES,
        primary_audio_type='2/0モード(ステレオ)',
        primary_audio_language='日本語',
        secondary_audio_type=None,
        secondary_audio_language=None,
    )
    await RecordedVideo.create(
        recorded_program=program,
        status=recorded_status,
        file_path=f'/tmp/recorded-episode-automation-{program_id}.ts',
        file_hash=f'automation-hash-{program_id}',
        file_size=1,
        file_created_at=start_time,
        file_modified_at=start_time,
        duration=3600.0,
        container_format='MPEG-TS',
    )
    return program


def InstallAISettings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    daily_limit: int = 20,
    acceptance_mode: str = 'HighConfidenceOnly',
) -> RecordedSeriesSettings:
    """AI話数検索ONの設定とダミーキーをメモリ上で返す。"""

    settings = RecordedSeriesSettings(
        enabled=True,
        ai_enabled=True,
        ai_episode_number_search_enabled=True,
        ai_episode_number_acceptance_mode=acceptance_mode,  # type: ignore[arg-type]
        api_base_url='https://api.example/v1',
        model='test-model',
        daily_ai_request_limit=daily_limit,
    )

    def GetSettingsAndAPIKey(
        cls: type[RecordedSeriesSettingsStore],
    ) -> tuple[RecordedSeriesSettings, str]:
        del cls
        return settings, 'test-secret'

    monkeypatch.setattr(
        RecordedSeriesSettingsStore,
        'getSettingsAndAPIKey',
        classmethod(GetSettingsAndAPIKey),
    )
    return settings


def CreateAIResult(
    *,
    season_number: int = 1,
    episode_number: Decimal = Decimal('4'),
    confidence: float = 0.95,
    with_citation: bool = True,
    with_source: bool | None = None,
) -> AIEpisodeLookupResult:
    """受理可否の境界を指定できるモックWeb検索結果を作成する。"""

    evidence = (
        AIEpisodeCitation(
            url='https://example.com/official/episode',
            title='番組公式話数ページ',
        ),
    )
    citations = evidence if with_citation else ()
    sources = evidence if (with_source if with_source is not None else with_citation) else ()
    return AIEpisodeLookupResult(
        numbered=True,
        season_number=season_number,
        episode_number=episode_number,
        confidence=confidence,
        citations=citations,
        sources=sources,
        model='response-model',
        prompt_tokens=120,
        completion_tokens=12,
        http_status=200,
        latency_ms=45,
    )


def test_new_recording_uses_deterministic_episode_before_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新規録画の厳格に解釈できる話数は、AI設定やWeb通信に触れず構造化する。"""

    async def SearchMustNotRun(**_kwargs: object) -> AIEpisodeLookupResult:
        raise AssertionError('Deterministic episode parsing must not call Web Search.')

    monkeypatch.setattr(RecordedEpisodeAutomationModule, 'SearchRecordedEpisodeNumber', SearchMustNotRun)

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(title='決定論的シリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1,
                series=series,
                channel=channel,
                episode_number='Season 2 #03.5',
            )

            result = await RecordedEpisodeAutomation.resolveProgram(program.id)

            assert result.status == 'Resolved'
            assert result.source == 'LegacyMetadata'
            assert result.ai_requested is False
            program = await RecordedProgram.get(id=program.id)
            episode = await SeriesEpisode.get(id=program.series_episode_id)
            resolution = await RecordedEpisodeResolution.get(recorded_program_id=program.id)
            assert episode.series_id == series.id
            assert episode.season_number == 2
            assert episode.episode_number == Decimal('3.5')
            assert resolution.status == 'Resolved'
            assert resolution.source == 'Local'
            assert resolution.web_search_performed is False
            assert await RecordedSeriesAIRequest.all().count() == 0
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_high_confidence_search_source_without_inline_citation_is_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """厳格JSON出力に本文引用がなくても、Web検索元があれば高信頼結果を確定する。"""

    InstallAISettings(monkeypatch, acceptance_mode='HighConfidenceOnly')

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        return CreateAIResult(with_citation=False, with_source=True)

    monkeypatch.setattr(RecordedEpisodeAutomationModule, 'SearchRecordedEpisodeNumber', SearchEpisode)

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(title='検索元根拠シリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel()
            program = await CreateRecordedProgram(1, series=series, channel=channel, episode_number=None)

            result = await RecordedEpisodeAutomation.resolveProgram(program.id)

            assert result.status == 'Resolved'
            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(recorded_program_id=program.id)
            assert program.series_episode_id is not None
            assert resolution.status == 'Resolved'
            assert resolution.citations == [{
                'url': 'https://example.com/official/episode',
                'title': '番組公式話数ページ',
            }]
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_legacy_unknown_requires_explicit_manual_ai_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """legacy録画は通常呼び出しで課金せず、手動一括実行時だけWeb検索する。"""

    InstallAISettings(monkeypatch)
    search_calls = 0

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        nonlocal search_calls
        search_calls += 1
        return CreateAIResult()

    monkeypatch.setattr(RecordedEpisodeAutomationModule, 'SearchRecordedEpisodeNumber', SearchEpisode)

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(title='legacyシリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel()
            program = await CreateRecordedProgram(1, series=series, channel=channel, episode_number=None)
            await RecordedEpisodeResolution.create(
                recorded_program=program,
                status='NeedsReview',
                source='Migration',
                is_legacy_recording=True,
                error_code='LegacyEpisodeMissing',
            )

            automatic_result = await RecordedEpisodeAutomation.resolveProgram(program.id)
            assert automatic_result.status == 'Skipped'
            assert automatic_result.source == 'LegacyRequiresManualBackfill'
            assert automatic_result.ai_requested is False
            assert search_calls == 0
            assert await RecordedSeriesAIRequest.all().count() == 0

            manual_result = await RecordedEpisodeAutomation.resolveProgram(
                program.id,
                allow_legacy_ai=True,
            )
            assert manual_result.status == 'Resolved'
            assert manual_result.source == 'WebSearch'
            assert manual_result.ai_requested is True
            assert search_calls == 1

            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(recorded_program_id=program.id)
            audit = await RecordedSeriesAIRequest.get(episode_resolution_id=resolution.id)
            assert program.episode_number == '#4'
            assert resolution.status == 'Resolved'
            assert resolution.source == 'WebSearch'
            assert resolution.web_search_performed is True
            assert resolution.citations == [{
                'url': 'https://example.com/official/episode',
                'title': '番組公式話数ページ',
            }]
            assert audit.purpose == 'EpisodeLookup'
            assert audit.status == 'Succeeded'
            assert audit.selected_choice_id == 'S1E4'
            assert audit.prompt_tokens == 120
            assert audit.completion_tokens == 12
            assert audit.error_code is None
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_low_confidence_ai_result_is_reviewed_audited_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """高信頼モードで根拠不足の数値提案は保存するが、Episodeへは反映せず再課金もしない。"""

    InstallAISettings(monkeypatch, acceptance_mode='HighConfidenceOnly')
    search_calls = 0

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        nonlocal search_calls
        search_calls += 1
        return CreateAIResult(
            season_number=3,
            episode_number=Decimal('12.5'),
            confidence=0.45,
            with_citation=False,
        )

    monkeypatch.setattr(RecordedEpisodeAutomationModule, 'SearchRecordedEpisodeNumber', SearchEpisode)

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(title='低信頼シリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel()
            program = await CreateRecordedProgram(1, series=series, channel=channel, episode_number=None)

            first_result = await RecordedEpisodeAutomation.resolveProgram(program.id)
            second_result = await RecordedEpisodeAutomation.resolveProgram(program.id)

            assert first_result.status == 'NeedsReview'
            assert first_result.ai_requested is True
            assert first_result.error_code == 'AcceptancePolicyRejected'
            assert second_result.status == 'NeedsReview'
            assert second_result.source == 'Cache'
            assert second_result.ai_requested is False
            assert search_calls == 1

            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(recorded_program_id=program.id)
            audit = await RecordedSeriesAIRequest.get(episode_resolution_id=resolution.id)
            assert program.series_episode_id is None
            assert resolution.status == 'NeedsReview'
            assert resolution.source == 'WebSearch'
            assert resolution.proposed_season_number == 3
            assert resolution.proposed_episode_number == Decimal('12.5')
            assert resolution.confidence == 0.45
            assert resolution.web_search_performed is True
            assert resolution.error_code == 'AcceptancePolicyRejected'
            assert audit.status == 'Rejected'
            assert audit.selected_choice_id == 'S3E12.5'
            assert audit.error_code == 'AcceptancePolicyRejected'
            assert await RecordedSeriesAIRequest.all().count() == 1
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_ai_audit_and_episode_update_roll_back_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """応答後の監査確定に失敗した場合、話数だけを保存せずPending監査と一緒に回収可能に保つ。"""

    InstallAISettings(monkeypatch)

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        return CreateAIResult()

    async def FailFinalization(
        _cls: type[RecordedEpisodeAutomation],
        _request: RecordedSeriesAIRequest,
        **_kwargs: object,
    ) -> None:
        raise RuntimeError('simulated audit finalization failure')

    monkeypatch.setattr(RecordedEpisodeAutomationModule, 'SearchRecordedEpisodeNumber', SearchEpisode)
    monkeypatch.setattr(RecordedEpisodeAutomation, '_finishAIRequest', classmethod(FailFinalization))

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(title='原子保存シリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel()
            program = await CreateRecordedProgram(1, series=series, channel=channel, episode_number=None)

            with pytest.raises(RuntimeError, match='simulated audit finalization failure'):
                await RecordedEpisodeAutomation.resolveProgram(program.id)

            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(recorded_program_id=program.id)
            audit = await RecordedSeriesAIRequest.get(episode_resolution_id=resolution.id)
            assert program.series_episode_id is None
            assert await SeriesEpisode.all().count() == 0
            assert resolution.status == 'Pending'
            assert resolution.source == 'WebSearch'
            assert audit.status == 'Pending'
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_stored_proposal_promotion_requires_enabled_ai_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alwaysでも親・話数スイッチOFF中は昇格せず、有効化後に同じ提案を無課金反映する。"""

    current_settings = RecordedSeriesSettings(
        enabled=True,
        ai_enabled=False,
        ai_episode_number_search_enabled=True,
        ai_episode_number_acceptance_mode='Always',
    )

    def GetSettings(cls: type[RecordedSeriesSettingsStore]) -> RecordedSeriesSettings:
        del cls
        return current_settings

    monkeypatch.setattr(RecordedSeriesSettingsStore, 'getSettings', classmethod(GetSettings))

    async def Run() -> None:
        nonlocal current_settings
        await InitializeDatabase()
        try:
            series = await Series.create(title='提案昇格シリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel()
            program = await CreateRecordedProgram(1, series=series, channel=channel, episode_number=None)
            snapshot = await RecordedEpisodeAutomation._loadSnapshot(program.id)
            assert snapshot is not None
            await RecordedEpisodeResolution.create(
                recorded_program=program,
                status='NeedsReview',
                source='WebSearch',
                input_fingerprint=RecordedEpisodeAutomationModule._buildInputFingerprint(snapshot),
                provider_fingerprint='provider-a',
                proposed_season_number=2,
                proposed_episode_number=Decimal('8.5'),
                confidence=0.55,
                web_search_performed=True,
                citations=[{'url': 'https://example.com/episode', 'title': '出典'}],
                ai_model='test-model',
                error_code='AcceptancePolicyRejected',
            )

            assert await RecordedEpisodeAutomation.promoteStoredProposals() == 0
            current_settings = current_settings.model_copy(update={'ai_enabled': True})
            assert await RecordedEpisodeAutomation.promoteStoredProposals() == 1

            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(recorded_program_id=program.id)
            assert program.series_episode_id is not None
            episode = await SeriesEpisode.get(id=program.series_episode_id)
            assert episode.season_number == 2
            assert episode.episode_number == Decimal('8.5')
            assert resolution.status == 'Resolved'
            assert resolution.source == 'WebSearch'
            assert await RecordedSeriesAIRequest.all().count() == 0
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_provider_change_marks_running_episode_for_rerun() -> None:
    """旧providerで実行中の録画は、設定変更後に新providerで再評価する予約を残す。"""

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(title='provider再試行シリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel()
            program = await CreateRecordedProgram(1, series=series, channel=channel, episode_number=None)
            await RecordedEpisodeResolution.create(
                recorded_program=program,
                status='Failed',
                source='WebSearch',
                is_legacy_recording=False,
                provider_fingerprint='provider-a',
                error_code='HTTP500',
            )

            RecordedEpisodeAutomation._queue = asyncio.Queue()
            RecordedEpisodeAutomation._queued_ids = set()
            RecordedEpisodeAutomation._running_ids = {program.id}
            RecordedEpisodeAutomation._rerun_ids = set()
            await RecordedEpisodeAutomation._enqueueRecoverablePrograms(
                provider_fingerprint='provider-b',
            )

            assert RecordedEpisodeAutomation._rerun_ids == {program.id}
            assert RecordedEpisodeAutomation._queue.empty()
        finally:
            RecordedEpisodeAutomation._queue = None
            RecordedEpisodeAutomation._queued_ids = set()
            RecordedEpisodeAutomation._running_ids = set()
            RecordedEpisodeAutomation._rerun_ids = set()
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_proposal_promotion_waits_for_in_flight_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Always保存時に実行中だった低信頼検索も、完了後の提案を取りこぼさず昇格する。"""

    settings = RecordedSeriesSettings(
        enabled=True,
        ai_enabled=True,
        ai_episode_number_search_enabled=True,
        ai_episode_number_acceptance_mode='Always',
    )

    def GetSettings(_cls: type[RecordedSeriesSettingsStore]) -> RecordedSeriesSettings:
        return settings

    monkeypatch.setattr(RecordedSeriesSettingsStore, 'getSettings', classmethod(GetSettings))

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(title='昇格競合シリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel()
            program = await CreateRecordedProgram(1, series=series, channel=channel, episode_number=None)
            snapshot = await RecordedEpisodeAutomation._loadSnapshot(program.id)
            assert snapshot is not None
            resolution = await RecordedEpisodeResolution.create(
                recorded_program=program,
                status='Pending',
                source='WebSearch',
                input_fingerprint=RecordedEpisodeAutomationModule._buildInputFingerprint(snapshot),
                provider_fingerprint='provider-a',
                web_search_performed=False,
            )

            async with RECORDED_SERIES_RESOLUTION_LOCK:
                promotion_task = asyncio.create_task(RecordedEpisodeAutomation.promoteStoredProposals())
                await asyncio.sleep(0)
                assert promotion_task.done() is False

                resolution.status = 'NeedsReview'
                resolution.proposed_season_number = 3
                resolution.proposed_episode_number = Decimal('7.5')
                resolution.confidence = 0.55
                resolution.web_search_performed = True
                resolution.citations = [{'url': 'https://example.com/episode', 'title': '出典'}]
                resolution.error_code = 'AcceptancePolicyRejected'
                await resolution.save()

            assert await promotion_task == 1
            program = await RecordedProgram.get(id=program.id)
            episode = await SeriesEpisode.get(id=program.series_episode_id)
            assert episode.season_number == 3
            assert episode.episode_number == Decimal('7.5')
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_disabled_settings_update_does_not_enqueue_provider_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider変更と同時にAIをOFFにしても、保存済みWeb提案を再評価キューへ入れない。"""

    settings = RecordedSeriesSettings(
        enabled=True,
        ai_enabled=False,
        ai_episode_number_search_enabled=True,
        ai_episode_number_acceptance_mode='HighConfidenceOnly',
        api_base_url='https://provider-b.example/v1',
        model='provider-b-model',
    )
    enqueue_calls = 0

    async def Start(_cls: type[RecordedEpisodeAutomation]) -> None:
        return None

    async def Promote(_cls: type[RecordedEpisodeAutomation]) -> int:
        return 0

    async def EnqueueRecoverable(
        _cls: type[RecordedEpisodeAutomation],
        *,
        provider_fingerprint: str | None = None,
    ) -> None:
        nonlocal enqueue_calls
        del provider_fingerprint
        enqueue_calls += 1

    def GetSettingsAndAPIKey(
        _cls: type[RecordedSeriesSettingsStore],
    ) -> tuple[RecordedSeriesSettings, str]:
        return settings, 'provider-b-key'

    monkeypatch.setattr(RecordedEpisodeAutomation, 'start', classmethod(Start))
    monkeypatch.setattr(RecordedEpisodeAutomation, 'promoteStoredProposals', classmethod(Promote))
    monkeypatch.setattr(
        RecordedEpisodeAutomation,
        '_enqueueRecoverablePrograms',
        classmethod(EnqueueRecoverable),
    )
    monkeypatch.setattr(
        RecordedSeriesSettingsStore,
        'getSettingsAndAPIKey',
        classmethod(GetSettingsAndAPIKey),
    )

    async def Run() -> None:
        worker = asyncio.create_task(asyncio.Event().wait())
        RecordedEpisodeAutomation._worker_task = worker
        try:
            await RecordedEpisodeAutomation.settingsUpdated()
            assert enqueue_calls == 0
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            RecordedEpisodeAutomation._worker_task = None

    asyncio.run(Run())


def test_episode_search_respects_shared_daily_ai_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同日のSeries候補選択が上限を使い切った場合、Episode検索は予約も送信もしない。"""

    InstallAISettings(monkeypatch, daily_limit=1)

    async def SearchMustNotRun(**_kwargs: object) -> AIEpisodeLookupResult:
        raise AssertionError('Shared daily limit must block Episode Web Search.')

    monkeypatch.setattr(RecordedEpisodeAutomationModule, 'SearchRecordedEpisodeNumber', SearchMustNotRun)

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(title='上限シリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel()
            program = await CreateRecordedProgram(1, series=series, channel=channel, episode_number=None)
            await RecordedSeriesAIRequest.create(
                resolution_id=None,
                episode_resolution_id=None,
                purpose='Resolution',
                status='Succeeded',
                model='candidate-model',
                candidate_ids=['unresolved'],
                selected_choice_id='unresolved',
                error_code=None,
            )

            result = await RecordedEpisodeAutomation.resolveProgram(program.id)

            assert result.status == 'Skipped'
            assert result.source == 'DailyLimit'
            assert result.ai_requested is False
            assert result.error_code == 'DailyAIRequestLimitReached'
            resolution = await RecordedEpisodeResolution.get(recorded_program_id=program.id)
            assert resolution.status == 'Unknown'
            assert resolution.source == 'Local'
            assert resolution.error_code == 'DailyAIRequestLimitReached'
            assert await RecordedSeriesAIRequest.all().count() == 1
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_input_changed_before_request_does_not_consume_daily_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部POST前に入力変更で閉じた監査は共有上限から除外し、次の検索を許可する。"""

    InstallAISettings(monkeypatch, daily_limit=1)
    search_calls = 0

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        nonlocal search_calls
        search_calls += 1
        return CreateAIResult()

    monkeypatch.setattr(RecordedEpisodeAutomationModule, 'SearchRecordedEpisodeNumber', SearchEpisode)

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(title='上限除外シリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel()
            program = await CreateRecordedProgram(1, series=series, channel=channel, episode_number=None)
            await RecordedSeriesAIRequest.create(
                resolution_id=None,
                episode_resolution_id=None,
                purpose='EpisodeLookup',
                status='Failed',
                model='previous-model',
                candidate_ids=[],
                error_code='InputChangedBeforeRequest',
            )

            result = await RecordedEpisodeAutomation.resolveProgram(program.id)

            assert result.status == 'Resolved'
            assert result.source == 'WebSearch'
            assert result.ai_requested is True
            assert search_calls == 1
            assert await RecordedSeriesAIRequest.all().count() == 2
            latest_audit = await RecordedSeriesAIRequest.all().order_by('-id').first()
            assert latest_audit is not None
            assert latest_audit.status == 'Succeeded'
            assert latest_audit.error_code is None
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_episode_status_counts_only_playable_series_recordings() -> None:
    """状態集計はSeries所属かつ再生可能な録画だけを数え、未作成ResolutionもUnknownへ含める。"""

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(title='状態集計シリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel()
            status_cases = [
                (2, 'Pending'),
                (3, 'Unknown'),
                (4, 'Resolved'),
                (5, 'NotNumbered'),
                (6, 'NeedsReview'),
                (7, 'Failed'),
            ]
            await CreateRecordedProgram(1, series=series, channel=channel, episode_number=None)
            for program_id, status in status_cases:
                program = await CreateRecordedProgram(
                    program_id,
                    series=series,
                    channel=channel,
                    episode_number=None,
                )
                await RecordedEpisodeResolution.create(
                    recorded_program=program,
                    status=status,
                    source='Local',
                    is_legacy_recording=False,
                )

            unplayable_program = await CreateRecordedProgram(
                8,
                series=series,
                channel=channel,
                episode_number=None,
                recorded_status='Analyzing',
            )
            await RecordedEpisodeResolution.create(
                recorded_program=unplayable_program,
                status='Failed',
                source='Local',
                is_legacy_recording=False,
            )

            status = await RecordedEpisodeAutomation.getStatus()

            assert status['episode_resolved'] == 1
            assert status['episode_unknown'] == 3
            assert status['episode_not_numbered'] == 1
            assert status['episode_needs_review'] == 1
            assert status['episode_failed'] == 1
            assert status['episode_last_run_at'] is not None
            assert status['is_episode_running'] is False
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_manual_backfill_candidate_boundaries() -> None:
    """手動一括は再生可能・非手動判断に限定し、forceで新旧録画の前回結果を再送する。"""

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(title='一括対象シリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel()

            cases = [
                (1, 'Pending', 'Migration', True, 'Recorded'),
                (2, 'Unknown', 'Local', True, 'Recorded'),
                (3, 'NeedsReview', 'Migration', True, 'Recorded'),
                (4, 'NeedsReview', 'WebSearch', True, 'Recorded'),
                (5, 'Failed', 'WebSearch', True, 'Recorded'),
                (6, 'NotNumbered', 'WebSearch', True, 'Recorded'),
                (7, 'Resolved', 'Migration', True, 'Recorded'),
                (8, 'Unknown', 'Manual', True, 'Recorded'),
                (9, 'Pending', 'Local', False, 'Recorded'),
                (10, 'Pending', 'Migration', True, 'Analyzing'),
                (11, 'NeedsReview', 'WebSearch', False, 'Recorded'),
                (12, 'Failed', 'WebSearch', False, 'Recorded'),
                (13, 'NotNumbered', 'WebSearch', False, 'Recorded'),
                (14, 'Resolved', 'Local', False, 'Recorded'),
                (15, 'Resolved', 'WebSearch', False, 'Recorded'),
                (16, 'Resolved', 'Manual', False, 'Recorded'),
            ]
            for program_id, status, source, is_legacy, recorded_status in cases:
                program = await CreateRecordedProgram(
                    program_id,
                    series=series,
                    channel=channel,
                    episode_number=None,
                    recorded_status=recorded_status,
                )
                await RecordedEpisodeResolution.create(
                    recorded_program=program,
                    status=status,
                    source=source,
                    is_legacy_recording=is_legacy,
                    provider_fingerprint='provider-a' if source == 'WebSearch' else None,
                )

            assert await RecordedEpisodeAutomation._loadBackfillCandidateIDs(force=False) == [1, 2, 3, 9]
            assert await RecordedEpisodeAutomation._loadBackfillCandidateIDs(
                force=False,
                provider_fingerprint='provider-a',
            ) == [1, 2, 3, 9]
            assert await RecordedEpisodeAutomation._loadBackfillCandidateIDs(
                force=False,
                provider_fingerprint='provider-b',
            ) == [1, 2, 3, 4, 5, 9, 11, 12]
            assert await RecordedEpisodeAutomation._loadBackfillCandidateIDs(force=True) == [
                1, 2, 3, 4, 5, 6, 7, 9, 11, 12, 13, 14, 15,
            ]
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_forced_legacy_resolved_search_replaces_existing_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """force付き手動一括は、移行済みの自動話数も受理した再検索結果へ置き換える。"""

    InstallAISettings(monkeypatch)

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        return CreateAIResult(season_number=2, episode_number=Decimal('7.5'))

    monkeypatch.setattr(RecordedEpisodeAutomationModule, 'SearchRecordedEpisodeNumber', SearchEpisode)

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(title='再検索更新シリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel()
            program = await CreateRecordedProgram(1, series=series, channel=channel, episode_number='#10')
            old_episode = await SeriesEpisode.create(
                series=series,
                season_number=1,
                episode_number=Decimal('10'),
            )
            program.series_episode_id = old_episode.id
            await program.save(update_fields=['series_episode_id'])
            resolution = await RecordedEpisodeResolution.create(
                recorded_program=program,
                episode=old_episode,
                status='Resolved',
                source='Migration',
                proposed_season_number=1,
                proposed_episode_number=Decimal('10'),
                is_legacy_recording=True,
            )

            result = await RecordedEpisodeAutomation.resolveProgram(
                program.id,
                force=True,
                allow_legacy_ai=True,
            )

            assert result.status == 'Resolved'
            assert result.source == 'WebSearch'
            assert result.ai_requested is True
            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(id=resolution.id)
            episode = await SeriesEpisode.get(id=program.series_episode_id)
            audit = await RecordedSeriesAIRequest.get(episode_resolution_id=resolution.id)
            assert episode.id != old_episode.id
            assert episode.season_number == 2
            assert episode.episode_number == Decimal('7.5')
            assert program.episode_number == 'Season 2 #7.5'
            assert resolution.episode_id == episode.id
            assert resolution.status == 'Resolved'
            assert resolution.source == 'WebSearch'
            assert resolution.proposed_season_number == 2
            assert resolution.proposed_episode_number == Decimal('7.5')
            assert audit.status == 'Succeeded'
            assert audit.selected_choice_id == 'S2E7.5'
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


@pytest.mark.parametrize(
    ('search_outcome', 'audit_status', 'audit_error_code'),
    [
        ('low-confidence', 'Rejected', 'AcceptancePolicyRejected'),
        ('api-error', 'Failed', 'HTTP500'),
    ],
)
def test_forced_legacy_resolved_search_failure_preserves_existing_episode(
    monkeypatch: pytest.MonkeyPatch,
    search_outcome: str,
    audit_status: str,
    audit_error_code: str,
) -> None:
    """再検索が不受理・失敗なら、既存の確定話数を壊さずAI監査だけを閉じる。"""

    InstallAISettings(monkeypatch)

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        if search_outcome == 'api-error':
            raise RecordedSeriesAIError('HTTP500', http_status=500, latency_ms=30)
        return CreateAIResult(
            season_number=2,
            episode_number=Decimal('7.5'),
            confidence=0.45,
        )

    monkeypatch.setattr(RecordedEpisodeAutomationModule, 'SearchRecordedEpisodeNumber', SearchEpisode)

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(title='再検索保持シリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel()
            program = await CreateRecordedProgram(1, series=series, channel=channel, episode_number='#10')
            old_episode = await SeriesEpisode.create(
                series=series,
                season_number=1,
                episode_number=Decimal('10'),
            )
            program.series_episode_id = old_episode.id
            await program.save(update_fields=['series_episode_id'])
            resolution = await RecordedEpisodeResolution.create(
                recorded_program=program,
                episode=old_episode,
                status='Resolved',
                source='Migration',
                proposed_season_number=1,
                proposed_episode_number=Decimal('10'),
                confidence=1.0,
                is_legacy_recording=True,
            )

            await RecordedEpisodeAutomation.resolveProgram(
                program.id,
                force=True,
                allow_legacy_ai=True,
            )

            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(id=resolution.id)
            audit = await RecordedSeriesAIRequest.get(episode_resolution_id=resolution.id)
            assert program.series_episode_id == old_episode.id
            assert program.episode_number == '#10'
            assert resolution.episode_id == old_episode.id
            assert resolution.status == 'Resolved'
            assert resolution.source == 'Migration'
            assert resolution.proposed_season_number == 1
            assert resolution.proposed_episode_number == Decimal('10')
            assert resolution.confidence == 1.0
            assert audit.status == audit_status
            assert audit.error_code == audit_error_code
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_forced_legacy_resolved_not_numbered_clears_automatic_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受理した公式話数なし判定は、移行由来のEpisodeリンクと旧自動値を解除する。"""

    InstallAISettings(monkeypatch)
    evidence = (
        AIEpisodeCitation(
            url='https://example.com/official/episode',
            title='番組公式話数ページ',
        ),
    )

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        return AIEpisodeLookupResult(
            numbered=False,
            season_number=None,
            episode_number=None,
            confidence=0.95,
            citations=evidence,
            sources=evidence,
            model='response-model',
            prompt_tokens=120,
            completion_tokens=12,
            http_status=200,
            latency_ms=45,
        )

    monkeypatch.setattr(RecordedEpisodeAutomationModule, 'SearchRecordedEpisodeNumber', SearchEpisode)

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(title='公式話数なし再検索シリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel()
            program = await CreateRecordedProgram(1, series=series, channel=channel, episode_number='#10')
            old_episode = await SeriesEpisode.create(
                series=series,
                season_number=1,
                episode_number=Decimal('10'),
            )
            program.series_episode_id = old_episode.id
            await program.save(update_fields=['series_episode_id'])
            resolution = await RecordedEpisodeResolution.create(
                recorded_program=program,
                episode=old_episode,
                status='Resolved',
                source='Migration',
                proposed_season_number=1,
                proposed_episode_number=Decimal('10'),
                is_legacy_recording=True,
            )

            result = await RecordedEpisodeAutomation.resolveProgram(
                program.id,
                force=True,
                allow_legacy_ai=True,
            )

            assert result.status == 'NotNumbered'
            assert result.source == 'WebSearch'
            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(id=resolution.id)
            audit = await RecordedSeriesAIRequest.get(episode_resolution_id=resolution.id)
            assert program.series_episode_id is None
            assert program.episode_number is None
            assert resolution.episode_id is None
            assert resolution.status == 'NotNumbered'
            assert resolution.source == 'WebSearch'
            assert resolution.proposed_season_number is None
            assert resolution.proposed_episode_number is None
            assert audit.status == 'Succeeded'
            assert audit.selected_choice_id == 'not-numbered'
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())
