# pyright: reportPrivateUsage=false

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient
from tortoise import Tortoise

from app.constants import JST
from app.metadata.RecordedSeriesResolver import (
    RecordedSeriesProgramNotFoundError,
    RecordedSeriesResolver,
    RecordedSeriesTargetNotFoundError,
    _buildClusterEvidence,
    _ClusterEvidence,
    _ProgramSnapshot,
)
from app.metadata.RecordedSeriesSettings import RecordedSeriesSettingsStore
from app.metadata.SeriesTitleParser import ParseSeriesTitle, SeriesTitleParseResult
from app.models.Channel import Channel
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedSeries import RecordedSeriesResolution, RecordedSeriesRule
from app.models.RecordedVideo import RecordedVideo
from app.models.Series import Series
from app.models.SeriesBroadcastPeriod import SeriesBroadcastPeriod
from app.routers import RecordedSeriesRouter
from app.schemas import Genre


ANIME_GENRES: list[Genre] = [
    {'major': 'アニメ・特撮', 'middle': '国内アニメ'},
]


def CreateAdminApp() -> FastAPI:
    """管理者認証をテスト用に置き換えたFastAPIアプリを作成する。"""

    async def GetAdminUser() -> object:
        return object()

    app = FastAPI()
    app.include_router(RecordedSeriesRouter.router)
    app.dependency_overrides[RecordedSeriesRouter.GetCurrentAdminUser] = GetAdminUser
    return app


async def InitializeDatabase() -> None:
    """手動シリーズ割当が使用するモデルをインメモリDBへ初期化する。"""

    await Tortoise.init(
        db_url='sqlite://:memory:',
        modules={'models': [
            'app.models.Channel',
            'app.models.RecordedProgram',
            'app.models.RecordedVideo',
            'app.models.RecordedSeries',
            'app.models.Series',
            'app.models.SeriesBroadcastPeriod',
        ]},
        timezone='Asia/Tokyo',
    )
    await Tortoise.generate_schemas()


async def CreateChannel(channel_id: str, service_id: int) -> Channel:
    """録画番組と放送期間に使用するチャンネルを作成する。"""

    return await Channel.create(
        id=channel_id,
        display_channel_id=f'gr{service_id}',
        network_id=4,
        service_id=service_id,
        transport_stream_id=1,
        remocon_id=1,
        channel_number=str(service_id),
        type='GR',
        name=f'テストチャンネル {service_id}',
        jikkyo_force=None,
        is_subchannel=False,
        is_radiochannel=False,
        is_watchable=True,
    )


async def CreateRecordedProgram(
    program_id: int,
    *,
    title: str,
    day: int,
    channel_id: str | None,
    service_id: int | None,
    series: Series | None = None,
    period: SeriesBroadcastPeriod | None = None,
    episode_number: str | None = None,
    subtitle: str | None = None,
    genres: list[Genre] | None = None,
) -> RecordedProgram:
    """シリーズ割当テスト用の解析済み録画を作成する。"""

    start_time = datetime(2026, 7, day, 20, 0, tzinfo=JST)
    program = await RecordedProgram.create(
        id=program_id,
        recording_start_margin=0.0,
        recording_end_margin=0.0,
        is_partially_recorded=False,
        channel_id=channel_id,
        network_id=4 if channel_id is not None else None,
        service_id=service_id,
        event_id=program_id,
        series=series,
        series_broadcast_period=period,
        title=title,
        series_title=series.title if series is not None else None,
        episode_number=episode_number,
        subtitle=subtitle,
        description='手動シリーズ割当のテスト番組。',
        detail={},
        start_time=start_time,
        end_time=start_time + timedelta(hours=1),
        duration=3600.0,
        is_free=True,
        genres=genres or ANIME_GENRES,
        primary_audio_type='2/0モード(ステレオ)',
        primary_audio_language='日本語',
        secondary_audio_type=None,
        secondary_audio_language=None,
    )
    await RecordedVideo.create(
        recorded_program=program,
        status='Recorded',
        file_path=f'/tmp/manual-series-{program_id}.ts',
        file_hash=f'hash-{program_id}',
        file_size=1,
        file_created_at=start_time,
        file_modified_at=start_time,
        duration=3600.0,
        container_format='MPEG-TS',
    )
    return program


def BuildSnapshot(program: RecordedProgram) -> _ProgramSnapshot:
    """DB上の録画からResolver入力を作成する。"""

    return _ProgramSnapshot(
        id=program.id,
        channel_id=program.channel_id,
        network_id=program.network_id,
        service_id=program.service_id,
        event_id=program.event_id,
        title=program.title,
        description=program.description,
        detail=program.detail,
        genres=program.genres,
        start_time=program.start_time,
    )


def BuildClusterContext(
    programs: list[_ProgramSnapshot],
) -> tuple[dict[int, SeriesTitleParseResult], dict[int, _ClusterEvidence]]:
    """Resolverへ渡すローカル解析とクラスタ証拠を構築する。"""

    parses = {
        program.id: ParseSeriesTitle(
            program.title,
            program.description,
            program.detail,
            program.genres,
        )
        for program in programs
    }
    return parses, _buildClusterEvidence(programs, parses)


def test_assignment_api_accepts_only_the_three_documented_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """APIは既存Series、新規タイトル、NotSeriesの3形だけを受理する。"""

    app = CreateAdminApp()
    assignments: list[tuple[int, str, int | None, str | None]] = []

    async def AssignProgram(
        recorded_program_id: int,
        *,
        decision: str,
        series_id: int | None,
        series_title: str | None,
    ) -> None:
        assignments.append((recorded_program_id, decision, series_id, series_title))

    monkeypatch.setattr(
        RecordedSeriesRouter.RecordedSeriesResolver,
        'assignProgram',
        staticmethod(AssignProgram),
    )

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            by_id = await client.put(
                '/api/recorded-series/programs/10/assignment',
                json={'decision': 'Series', 'series_id': 20},
            )
            by_title = await client.put(
                '/api/recorded-series/programs/11/assignment',
                json={'decision': 'Series', 'series_title': '  新しいシリーズ  '},
            )
            not_series = await client.put(
                '/api/recorded-series/programs/12/assignment',
                json={'decision': 'NotSeries'},
            )
            invalid_bodies = [
                {'decision': 'Series'},
                {'decision': 'Series', 'series_id': 20, 'series_title': '重複'},
                {'decision': 'Series', 'series_title': '   '},
                {'decision': 'NotSeries', 'series_id': 20},
                {'decision': 'NotSeries', 'unexpected': True},
            ]
            invalid_responses = [
                await client.put('/api/recorded-series/programs/13/assignment', json=body)
                for body in invalid_bodies
            ]

        assert by_id.status_code == 204
        assert by_title.status_code == 204
        assert not_series.status_code == 204
        assert by_id.headers['cache-control'] == 'no-store'
        assert all(response.status_code == 422 for response in invalid_responses)

    asyncio.run(Run())
    assert assignments == [
        (10, 'Series', 20, None),
        (11, 'Series', None, '新しいシリーズ'),
        (12, 'NotSeries', None, None),
    ]


def test_assignment_api_returns_404_for_a_missing_target_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """存在しない既存Series指定を明確な404へ変換する。"""

    app = CreateAdminApp()

    async def AssignProgram(*_args: object, **_kwargs: object) -> None:
        raise RecordedSeriesTargetNotFoundError

    monkeypatch.setattr(
        RecordedSeriesRouter.RecordedSeriesResolver,
        'assignProgram',
        staticmethod(AssignProgram),
    )

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.put(
                '/api/recorded-series/programs/10/assignment',
                json={'decision': 'Series', 'series_id': 999},
            )

        assert response.status_code == 404
        assert response.headers['cache-control'] == 'no-store'
        assert response.json()['detail'] == 'Specified series_id was not found.'

    asyncio.run(Run())


def test_next_program_api_returns_the_following_recording_or_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """次番組APIは公開GETで次の録画IDまたは最終話を示すnullを返す。"""

    app = CreateAdminApp()

    async def GetNextProgramID(recorded_program_id: int) -> int | None:
        if recorded_program_id == 10:
            return 11
        if recorded_program_id == 11:
            return None
        raise RecordedSeriesProgramNotFoundError

    monkeypatch.setattr(
        RecordedSeriesRouter.RecordedSeriesResolver,
        'getNextProgramID',
        staticmethod(GetNextProgramID),
    )

    async def Run() -> None:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            following = await client.get('/api/recorded-series/programs/10/next')
            last = await client.get('/api/recorded-series/programs/11/next')
            missing = await client.get('/api/recorded-series/programs/12/next')

        assert following.status_code == 200
        assert following.json() == {'recorded_program_id': 11}
        assert following.headers['cache-control'] == 'no-store'
        assert last.status_code == 200
        assert last.json() == {'recorded_program_id': None}
        assert missing.status_code == 404
        assert missing.headers['cache-control'] == 'no-store'

    asyncio.run(Run())


def test_next_program_uses_stable_recording_order_and_skips_unavailable_programs() -> None:
    """次番組はシリーズ内の開始日時・ID順で、再生可能な録画だけから選ぶ。"""

    async def Scenario() -> None:
        await InitializeDatabase()
        try:
            channel = await CreateChannel('NID4-SID101', 101)
            series = await Series.create(title='連続再生シリーズ', description='テスト', genres=ANIME_GENRES)
            other_series = await Series.create(title='別シリーズ', description='テスト', genres=ANIME_GENRES)
            first = await CreateRecordedProgram(
                20,
                title='連続再生シリーズ 第1話',
                day=1,
                channel_id=channel.id,
                service_id=101,
                series=series,
            )
            same_time = await CreateRecordedProgram(
                21,
                title='連続再生シリーズ 第2話',
                day=1,
                channel_id=channel.id,
                service_id=101,
                series=series,
            )
            unavailable = await CreateRecordedProgram(
                22,
                title='連続再生シリーズ 解析中',
                day=2,
                channel_id=channel.id,
                service_id=101,
                series=series,
            )
            last = await CreateRecordedProgram(
                23,
                title='連続再生シリーズ 第3話',
                day=3,
                channel_id=channel.id,
                service_id=101,
                series=series,
            )
            await CreateRecordedProgram(
                24,
                title='別シリーズ 第1話',
                day=2,
                channel_id=channel.id,
                service_id=101,
                series=other_series,
            )
            standalone = await CreateRecordedProgram(
                25,
                title='単発番組',
                day=4,
                channel_id=channel.id,
                service_id=101,
            )
            unavailable_video = await RecordedVideo.get(recorded_program=unavailable)
            unavailable_video.status = 'Analyzing'
            await unavailable_video.save(update_fields=['status', 'updated_at'])

            assert await RecordedSeriesResolver.getNextProgramID(first.id) == same_time.id
            assert await RecordedSeriesResolver.getNextProgramID(same_time.id) == last.id
            assert await RecordedSeriesResolver.getNextProgramID(last.id) is None
            assert await RecordedSeriesResolver.getNextProgramID(standalone.id) is None
            with pytest.raises(RecordedSeriesProgramNotFoundError):
                await RecordedSeriesResolver.getNextProgramID(999)
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_unexpected_failure_does_not_overwrite_a_manual_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自動判定の想定外失敗はPendingだけを失敗化し、直後の手動確定を保護する。"""

    async def Scenario() -> None:
        await InitializeDatabase()
        try:
            channel = await CreateChannel('NID4-SID101', 101)
            target_series = await Series.create(title='手動保護シリーズ', description='テスト', genres=ANIME_GENRES)
            manual_program = await CreateRecordedProgram(
                30,
                title='手動保護シリーズ 第1話',
                day=1,
                channel_id=channel.id,
                service_id=101,
            )
            automatic_program = await CreateRecordedProgram(
                31,
                title='自動失敗シリーズ 第1話',
                day=2,
                channel_id=channel.id,
                service_id=101,
            )
            monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
            await RecordedSeriesResolver.assignProgram(
                manual_program.id,
                decision='Series',
                series_id=target_series.id,
            )
            await RecordedSeriesResolution.create(
                recorded_program=automatic_program,
                input_fingerprint='a' * 64,
                evidence_hash='b' * 64,
                resolver_version='test',
                normalized_title='自動失敗シリーズ',
                status='Pending',
                source=None,
            )

            await RecordedSeriesResolver._markUnexpectedFailure(manual_program.id, 'Unexpected')
            await RecordedSeriesResolver._markUnexpectedFailure(automatic_program.id, 'Unexpected')

            manual_resolution = await RecordedSeriesResolution.get(recorded_program_id=manual_program.id)
            automatic_resolution = await RecordedSeriesResolution.get(recorded_program_id=automatic_program.id)
            assert manual_resolution.status == 'Resolved'
            assert manual_resolution.source == 'Manual'
            assert manual_resolution.series_id == target_series.id
            assert automatic_resolution.status == 'Failed'
            assert automatic_resolution.source is None
            assert automatic_resolution.error_code == 'Unexpected'
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_legacy_series_canonical_keys_are_backfilled_without_merging_collisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """移行前Seriesは表記揺れの最古行だけを代表とし、新規重複を防ぐ。"""

    async def Scenario() -> None:
        await InitializeDatabase()
        try:
            first = await Series.create(title='星空 探検隊', description='最古', genres=ANIME_GENRES)
            duplicate = await Series.create(title='星空・探検隊', description='表記揺れ', genres=ANIME_GENRES)
            distinct = await Series.create(title='海底探検隊', description='別作品', genres=ANIME_GENRES)
            monkeypatch.setattr(RecordedSeriesResolver, '_canonical_key_backfill_lock', asyncio.Lock())
            monkeypatch.setattr(RecordedSeriesResolver, '_canonical_key_backfill_completed', False)

            await RecordedSeriesResolver._backfillLegacySeriesCanonicalKeys()
            await first.refresh_from_db()
            await duplicate.refresh_from_db()
            await distinct.refresh_from_db()

            assert first.canonical_key is not None
            assert duplicate.canonical_key is None
            assert distinct.canonical_key is not None
            assert first.canonical_key != distinct.canonical_key

            channel = await CreateChannel('NID4-SID102', 102)
            program = await CreateRecordedProgram(
                32,
                title='星空探検隊 第1話',
                day=3,
                channel_id=channel.id,
                service_id=102,
            )
            monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
            await RecordedSeriesResolver.assignProgram(
                program.id,
                decision='Series',
                series_title='星空探検隊',
            )
            await program.refresh_from_db()
            assert program.series_id == first.id

            # canonical keyが他の代表行で使用中でも、管理者が選んだ
            # 重複側SeriesのID自体は再適用時も維持し、UNIQUE違反にしない。
            await RecordedSeriesResolver.assignProgram(
                program.id,
                decision='Series',
                series_id=duplicate.id,
            )
            snapshot = await RecordedSeriesResolver._loadProgramSnapshot(program.id)
            assert snapshot is not None
            parse_result = ParseSeriesTitle(
                snapshot.title,
                snapshot.description,
                snapshot.detail,
                snapshot.genres,
            )
            cluster = _buildClusterEvidence(
                [snapshot],
                {snapshot.id: parse_result},
            )[snapshot.id]
            resolution = await RecordedSeriesResolution.get(recorded_program_id=program.id)
            monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)
            await RecordedSeriesResolver._applySeries(
                snapshot=snapshot,
                parse_result=parse_result,
                cluster=cluster,
                resolution=resolution,
                expected_generation=0,
                source='Manual',
                title=duplicate.title,
                description=duplicate.description,
                confidence=1.0,
                existing_series_id=duplicate.id,
            )
            await program.refresh_from_db()
            await duplicate.refresh_from_db()
            assert program.series_id == duplicate.id
            assert duplicate.canonical_key is None
            assert await Series.all().count() == 3
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_manual_series_assignment_preserves_episode_and_reconciles_periods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手動のSeries変更を原子的に保存し、入力変更後も同じ判断を再適用する。"""

    async def Scenario() -> None:
        await InitializeDatabase()
        try:
            first_channel = await CreateChannel('NID4-SID101', 101)
            await CreateChannel('NID4-SID102', 102)
            old_series = await Series.create(title='旧シリーズ', description='旧', genres=ANIME_GENRES)
            target_series = await Series.create(title='訂正後シリーズ', description='新', genres=ANIME_GENRES)
            old_period = await SeriesBroadcastPeriod.create(
                series=old_series,
                channel=first_channel,
                start_date=datetime(2026, 7, 1, tzinfo=JST).date(),
                end_date=datetime(2026, 7, 3, tzinfo=JST).date(),
            )
            first = await CreateRecordedProgram(
                1,
                title='手動訂正作品 第1話「本当の話名」',
                day=1,
                channel_id=first_channel.id,
                service_id=101,
                series=old_series,
                period=old_period,
                episode_number='保存済み #1',
                subtitle='保存済み話名',
            )
            await CreateRecordedProgram(
                2,
                title='旧シリーズ 第2話',
                day=3,
                channel_id=first_channel.id,
                service_id=101,
                series=old_series,
                period=old_period,
            )

            monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
            monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)
            monkeypatch.setattr(
                RecordedSeriesSettingsStore,
                'getSettingsAndAPIKey',
                staticmethod(lambda: (SimpleNamespace(enabled=True), None)),
            )

            await RecordedSeriesResolver.assignProgram(
                first.id,
                decision='Series',
                series_id=target_series.id,
            )

            await first.refresh_from_db()
            await old_period.refresh_from_db()
            first_target_period_id = first.series_broadcast_period_id
            assert first.series_id == target_series.id
            assert first.series_title == target_series.title
            assert first.episode_number == '保存済み #1'
            assert first.subtitle == '保存済み話名'
            assert old_period.start_date == datetime(2026, 7, 3, tzinfo=JST).date()
            assert old_period.end_date == datetime(2026, 7, 3, tzinfo=JST).date()
            assert first_target_period_id is not None

            resolution = await RecordedSeriesResolution.get(recorded_program_id=first.id)
            rule = await RecordedSeriesRule.get(normalized_key=resolution.normalized_title)
            assert resolution.status == 'Resolved'
            assert resolution.source == 'Manual'
            assert resolution.series_id == target_series.id
            assert rule.source == 'Manual'
            assert rule.decision == 'Series'
            assert rule.series_id == target_series.id

            # force再判定でも同一入力なら手動決定をそのまま保持する。
            snapshot = BuildSnapshot(first)
            parses, evidence = BuildClusterContext([snapshot])
            result = await RecordedSeriesResolver.resolveProgram(
                first.id,
                force=True,
                snapshots=[snapshot],
                parses=parses,
                cluster_evidence=evidence,
                snapshot_generation=0,
            )
            assert result.status == 'Resolved'
            assert result.source == 'Manual'

            # start_time/channelが変化したら保存済みManual series_idを新snapshotへ再適用する。
            moved_start_time = datetime(2026, 7, 5, 20, 0, tzinfo=JST)
            first.channel_id = 'NID4-SID102'
            first.service_id = 102
            first.start_time = moved_start_time
            first.end_time = moved_start_time + timedelta(hours=1)
            await first.save(update_fields=['channel_id', 'service_id', 'start_time', 'end_time'])
            RecordedSeriesResolver._snapshot_generation = 1
            moved_snapshot = BuildSnapshot(first)
            moved_parses, moved_evidence = BuildClusterContext([moved_snapshot])
            moved_result = await RecordedSeriesResolver.resolveProgram(
                first.id,
                force=True,
                snapshots=[moved_snapshot],
                parses=moved_parses,
                cluster_evidence=moved_evidence,
                snapshot_generation=1,
            )

            await first.refresh_from_db()
            moved_period = await SeriesBroadcastPeriod.get(id=first.series_broadcast_period_id)
            await resolution.refresh_from_db()
            assert moved_result.status == 'Resolved'
            assert moved_result.source == 'Manual'
            assert first.series_id == target_series.id
            assert first.episode_number == '保存済み #1'
            assert first.subtitle == '保存済み話名'
            assert moved_period.channel_id == 'NID4-SID102'
            assert moved_period.start_date == moved_start_time.date()
            assert moved_period.end_date == moved_start_time.date()
            assert await SeriesBroadcastPeriod.filter(id=first_target_period_id).exists() is False
            assert resolution.source == 'Manual'
            assert resolution.input_fingerprint != ''

            # title変更とchannel欠落が同時に起きても、desired series_idをNeedsReviewへ保持する。
            first.title = '以前と一致しない完全変更タイトル'
            first.channel_id = None
            first.service_id = None
            await first.save(update_fields=['title', 'channel_id', 'service_id'])
            RecordedSeriesResolver._snapshot_generation = 2
            unavailable_snapshot = BuildSnapshot(first)
            unavailable_parses, unavailable_evidence = BuildClusterContext([unavailable_snapshot])
            unavailable_result = await RecordedSeriesResolver.resolveProgram(
                first.id,
                force=True,
                snapshots=[unavailable_snapshot],
                parses=unavailable_parses,
                cluster_evidence=unavailable_evidence,
                snapshot_generation=2,
            )
            await first.refresh_from_db()
            await resolution.refresh_from_db()
            assert unavailable_result.status == 'NeedsReview'
            assert unavailable_result.source == 'Manual'
            assert first.series_id is None
            assert first.series_broadcast_period_id is None
            assert first.episode_number == '保存済み #1'
            assert first.subtitle == '保存済み話名'
            assert resolution.status == 'NeedsReview'
            assert resolution.source == 'Manual'
            assert resolution.series_id == target_series.id
            assert resolution.error_code == 'ManualChannelUnavailable'

            # 同じchannel欠落fingerprintの再実行をResolved cacheとして誤って返さない。
            repeated_unavailable_result = await RecordedSeriesResolver.resolveProgram(
                first.id,
                force=True,
                snapshots=[unavailable_snapshot],
                parses=unavailable_parses,
                cluster_evidence=unavailable_evidence,
                snapshot_generation=2,
            )
            assert repeated_unavailable_result.status == 'NeedsReview'
            assert repeated_unavailable_result.source == 'Manual'

            # 旧Ruleと一致しない変更後タイトルのままでも、channel復帰時は個別Manualを復元する。
            first.channel_id = 'NID4-SID102'
            first.service_id = 102
            await first.save(update_fields=['channel_id', 'service_id'])
            RecordedSeriesResolver._snapshot_generation = 3
            restored_snapshot = BuildSnapshot(first)
            restored_parses, restored_evidence = BuildClusterContext([restored_snapshot])
            restored_result = await RecordedSeriesResolver.resolveProgram(
                first.id,
                force=True,
                snapshots=[restored_snapshot],
                parses=restored_parses,
                cluster_evidence=restored_evidence,
                snapshot_generation=3,
            )
            await first.refresh_from_db()
            await resolution.refresh_from_db()
            assert restored_result.status == 'Resolved'
            assert restored_result.source == 'Manual'
            assert first.series_id == target_series.id
            assert first.series_broadcast_period_id is not None
            assert first.episode_number == '保存済み #1'
            assert first.subtitle == '保存済み話名'
            assert resolution.status == 'Resolved'
            assert resolution.source == 'Manual'
            assert resolution.series_id == target_series.id
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_manual_rule_beats_force_and_hard_standalone_but_never_creates_a_half_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual ruleを同名録画へ再利用し、channel不明ならSeriesを付けず要確認にする。"""

    async def Scenario() -> None:
        await InitializeDatabase()
        try:
            channel = await CreateChannel('NID4-SID101', 101)
            target_series = await Series.create(title='手動優先作品', description='新', genres=ANIME_GENRES)
            assigned = await CreateRecordedProgram(
                10,
                title='劇場版「作品A」',
                day=1,
                channel_id=channel.id,
                service_id=101,
            )
            same_title = await CreateRecordedProgram(
                11,
                title='劇場版「作品B」',
                day=2,
                channel_id=channel.id,
                service_id=101,
            )
            no_channel = await CreateRecordedProgram(
                12,
                title='劇場版「作品C」',
                day=3,
                channel_id=None,
                service_id=None,
            )

            monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
            monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)
            monkeypatch.setattr(
                RecordedSeriesSettingsStore,
                'getSettingsAndAPIKey',
                staticmethod(lambda: (SimpleNamespace(enabled=True), None)),
            )
            await RecordedSeriesResolver.assignProgram(
                assigned.id,
                decision='Series',
                series_id=target_series.id,
            )

            snapshots = [BuildSnapshot(same_title)]
            parses, evidence = BuildClusterContext(snapshots)
            assert parses[same_title.id].is_hard_standalone is True
            same_result = await RecordedSeriesResolver.resolveProgram(
                same_title.id,
                force=True,
                snapshots=snapshots,
                parses=parses,
                cluster_evidence=evidence,
                snapshot_generation=0,
            )
            await same_title.refresh_from_db()
            same_resolution = await RecordedSeriesResolution.get(recorded_program_id=same_title.id)
            assert same_result.status == 'Resolved'
            assert same_result.source == 'Manual'
            assert same_title.series_id == target_series.id
            assert same_resolution.source == 'Manual'

            no_channel_snapshots = [BuildSnapshot(no_channel)]
            no_channel_parses, no_channel_evidence = BuildClusterContext(no_channel_snapshots)
            no_channel_result = await RecordedSeriesResolver.resolveProgram(
                no_channel.id,
                force=True,
                snapshots=no_channel_snapshots,
                parses=no_channel_parses,
                cluster_evidence=no_channel_evidence,
                snapshot_generation=0,
            )
            await no_channel.refresh_from_db()
            no_channel_resolution = await RecordedSeriesResolution.get(recorded_program_id=no_channel.id)
            assert no_channel_result.status == 'NeedsReview'
            assert no_channel_result.source == 'Manual'
            assert no_channel.series_id is None
            assert no_channel.series_broadcast_period_id is None
            assert no_channel_resolution.source == 'Manual'
            assert no_channel_resolution.error_code == 'ManualChannelUnavailable'

            # Manual positive ruleの参照先が消えても、自動推測へフォールバックせず要確認にする。
            missing_target_rule = await RecordedSeriesRule.get(source='Manual')
            missing_target_rule.series_id = None
            await missing_target_rule.save(update_fields=['series_id', 'updated_at'])
            missing_target = await CreateRecordedProgram(
                14,
                title='劇場版「作品E」',
                day=5,
                channel_id=channel.id,
                service_id=101,
            )
            missing_target_snapshots = [BuildSnapshot(missing_target)]
            missing_target_parses, missing_target_evidence = BuildClusterContext(missing_target_snapshots)
            missing_target_result = await RecordedSeriesResolver.resolveProgram(
                missing_target.id,
                force=True,
                snapshots=missing_target_snapshots,
                parses=missing_target_parses,
                cluster_evidence=missing_target_evidence,
                snapshot_generation=0,
            )
            missing_target_resolution = await RecordedSeriesResolution.get(
                recorded_program_id=missing_target.id,
            )
            assert missing_target_result.status == 'NeedsReview'
            assert missing_target_result.source == 'Manual'
            assert missing_target_resolution.error_code == 'ManualSeriesNoLongerExists'

            # Manual NotSeriesはevidence_hashやforceに関係なく同じ正規化keyへ再利用する。
            await RecordedSeriesResolver.assignProgram(assigned.id, decision='NotSeries')
            other_episode = await CreateRecordedProgram(
                13,
                title='劇場版「作品D」',
                day=4,
                channel_id=channel.id,
                service_id=101,
            )
            other_snapshots = [BuildSnapshot(other_episode)]
            other_parses, other_evidence = BuildClusterContext(other_snapshots)
            other_result = await RecordedSeriesResolver.resolveProgram(
                other_episode.id,
                force=True,
                snapshots=other_snapshots,
                parses=other_parses,
                cluster_evidence=other_evidence,
                snapshot_generation=0,
            )
            await other_episode.refresh_from_db()
            not_series_rule = await RecordedSeriesRule.get(source='Manual')
            assert other_result.status == 'NotSeries'
            assert other_result.source == 'Manual'
            assert other_episode.series_id is None
            assert not_series_rule.decision == 'NotSeries'
            assert not_series_rule.source == 'Manual'
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_input_change_durably_marks_manual_resolution_without_losing_its_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """入力更新をクラッシュ回収できるsentinelにしつつManual決定を保持する。"""

    async def Scenario() -> None:
        await InitializeDatabase()
        try:
            channel = await CreateChannel('NID4-SID101', 101)
            target_series = await Series.create(title='耐久化シリーズ', description='新', genres=ANIME_GENRES)
            manual_program = await CreateRecordedProgram(
                30,
                title='耐久化シリーズ 第1話',
                day=1,
                channel_id=channel.id,
                service_id=101,
            )
            automatic_program = await CreateRecordedProgram(
                31,
                title='自動判定更新 第1話',
                day=2,
                channel_id=channel.id,
                service_id=101,
            )
            await RecordedSeriesResolution.create(
                recorded_program=automatic_program,
                input_fingerprint='a' * 64,
                evidence_hash='b' * 64,
                resolver_version='old',
                normalized_title='自動判定更新',
                status='Resolved',
                source=None,
            )

            monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
            await RecordedSeriesResolver.assignProgram(
                manual_program.id,
                decision='Series',
                series_id=target_series.id,
            )
            original_manual_resolution = await RecordedSeriesResolution.get(
                recorded_program_id=manual_program.id,
            )

            async def Start(_cls: type[RecordedSeriesResolver]) -> None:
                return None

            queue: asyncio.Queue[int] = asyncio.Queue()
            monkeypatch.setattr(
                RecordedSeriesSettingsStore,
                'getSettings',
                staticmethod(lambda: SimpleNamespace(enabled=True)),
            )
            monkeypatch.setattr(RecordedSeriesResolver, 'start', classmethod(Start))
            monkeypatch.setattr(RecordedSeriesResolver, '_queue', queue)
            monkeypatch.setattr(RecordedSeriesResolver, '_queued_ids', set())
            monkeypatch.setattr(RecordedSeriesResolver, '_running_ids', set())
            monkeypatch.setattr(RecordedSeriesResolver, '_rerun_ids', set())
            monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)

            await RecordedSeriesResolver.enqueue(manual_program.id, input_changed=True)
            await RecordedSeriesResolver.enqueue(automatic_program.id, input_changed=True)

            manual_resolution = await RecordedSeriesResolution.get(recorded_program_id=manual_program.id)
            automatic_resolution = await RecordedSeriesResolution.get(
                recorded_program_id=automatic_program.id,
            )
            assert manual_resolution.status == 'Resolved'
            assert manual_resolution.source == 'Manual'
            assert manual_resolution.series_id == target_series.id
            assert manual_resolution.input_fingerprint == '0' * 64
            assert manual_resolution.evidence_hash == original_manual_resolution.evidence_hash
            assert automatic_resolution.status == 'Pending'
            assert automatic_resolution.source is None
            assert automatic_resolution.input_fingerprint == '0' * 64

            queued_ids = {await queue.get(), await queue.get()}
            assert queued_ids == {manual_program.id, automatic_program.id}

            # プロセス再起動相当でもPendingとManual sentinelの両方を回収する。
            recovery_queue: asyncio.Queue[int] = asyncio.Queue()
            RecordedSeriesResolver._queue = recovery_queue
            RecordedSeriesResolver._queued_ids = set()
            RecordedSeriesResolver._running_ids = set()
            await RecordedSeriesResolver._enqueuePendingResolutionsIfEnabled()
            recovered_ids = {await recovery_queue.get(), await recovery_queue.get()}
            assert recovered_ids == {manual_program.id, automatic_program.id}
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_title_assignment_reuses_the_same_canonical_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """タイトル指定は正規化canonical keyを使ってSeriesを重複作成しない。"""

    async def Scenario() -> None:
        await InitializeDatabase()
        try:
            channel = await CreateChannel('NID4-SID101', 101)
            first = await CreateRecordedProgram(
                20,
                title='候補A 第1話',
                day=1,
                channel_id=channel.id,
                service_id=101,
            )
            second = await CreateRecordedProgram(
                21,
                title='候補B 第1話',
                day=2,
                channel_id=channel.id,
                service_id=101,
            )
            monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())

            await RecordedSeriesResolver.assignProgram(
                first.id,
                decision='Series',
                series_title=' 新規シリーズ ',
            )
            await RecordedSeriesResolver.assignProgram(
                second.id,
                decision='Series',
                series_title='新規シリーズ',
            )

            await first.refresh_from_db()
            await second.refresh_from_db()
            assert first.series_id == second.series_id
            assert await Series.filter(title='新規シリーズ').count() == 1
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())
