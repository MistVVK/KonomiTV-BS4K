# pyright: reportPrivateUsage=false

import asyncio
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from tortoise import Tortoise, transactions

from app.constants import JST
from app.metadata.RecordedSeriesResolver import (
    RecordedSeriesResolver,
    _buildAIAttemptKey,
    _buildClusterEvidence,
    _ProgramSnapshot,
    _RecordedProgramSnapshotChanged,
)
from app.metadata.RecordedSeriesSettings import RecordedSeriesSettingsStore
from app.metadata.SeriesTitleParser import ParseSeriesTitle, SeriesTitleParseResult
from app.models.Channel import Channel
from app.models.Program import Program
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedSeries import (
    RecordedSeriesAIRequest,
    RecordedSeriesResolution,
    RecordedSeriesRule,
)
from app.models.Series import Series
from app.models.SeriesBroadcastPeriod import SeriesBroadcastPeriod
from app.schemas import Genre


DOCUMENTARY_GENRES: list[Genre] = [
    {'major': 'ドキュメンタリー・教養', 'middle': '歴史・紀行'},
]
ANIME_GENRES: list[Genre] = [
    {'major': 'アニメ・特撮', 'middle': '国内アニメ'},
]


def _snapshot(
    program_id: int,
    title: str,
    description: str,
    *,
    detail: dict[str, str] | None = None,
    genres: list[Genre] | None = None,
    network_id: int | None = 4,
    service_id: int | None = 101,
) -> _ProgramSnapshot:
    """シリーズ判定に必要な最小限の録画スナップショットを作る。"""

    return _ProgramSnapshot(
        id=program_id,
        channel_id=f'NID{network_id}-SID{service_id}' if network_id is not None and service_id is not None else None,
        network_id=network_id,
        service_id=service_id,
        event_id=program_id,
        title=title,
        description=description,
        detail=detail or {},
        genres=genres or DOCUMENTARY_GENRES,
        start_time=datetime(2026, 7, min(program_id, 28), 20, 0, tzinfo=JST),
    )


def _parseSnapshots(programs: list[_ProgramSnapshot]) -> dict[int, SeriesTitleParseResult]:
    """各スナップショットを本番と同じパーサーに通す。"""

    return {
        program.id: ParseSeriesTitle(
            program.title,
            program.description,
            program.detail,
            program.genres,
        )
        for program in programs
    }


def test_same_title_and_same_content_is_not_a_strong_repeat() -> None:
    """同じ回の再放送・重複録画だけでシリーズに昇格しない。"""

    programs = [
        _snapshot(1, '運転席からの風景 東武東上線', '池袋から寄居までを運転席から旅する。'),
        _snapshot(2, '運転席からの風景 東武東上線', '池袋から寄居までを運転席から旅する。'),
    ]

    evidence = _buildClusterEvidence(programs, _parseSnapshots(programs))

    assert evidence[1].member_ids == (1, 2)
    assert evidence[2].member_ids == (1, 2)
    assert evidence[1].is_strong_repeat is False
    assert evidence[2].is_strong_repeat is False


def test_same_title_with_different_content_is_a_strong_repeat() -> None:
    """同じ番組名で内容の異なる2件は、外部APIなしで反復番組と判定できる。"""

    programs = [
        _snapshot(1, '中山秀征の有楽町で逢いまSHOW', '大川栄策が名曲と新曲を熱唱。'),
        _snapshot(2, '中山秀征の有楽町で逢いまSHOW', '有沙瞳がCDデビュー曲を披露。'),
    ]

    evidence = _buildClusterEvidence(programs, _parseSnapshots(programs))

    assert evidence[1].display_title == '中山秀征の有楽町で逢いまSHOW'
    assert evidence[1].member_ids == (1, 2)
    assert evidence[1].is_strong_repeat is True


def test_delimited_common_prefix_builds_a_safe_series_root() -> None:
    """十分長い共通rootが空白境界で裏付けられる場合だけ前方一致で束ねる。"""

    programs = [
        _snapshot(1, '運転席からの風景 東武東上線', '東武東上線を運転席から旅する。'),
        _snapshot(2, '運転席からの風景 JR武蔵野線', 'JR武蔵野線を運転席から旅する。'),
    ]

    evidence = _buildClusterEvidence(programs, _parseSnapshots(programs))

    for program in programs:
        cluster = evidence[program.id]
        assert cluster.display_title == '運転席からの風景'
        assert cluster.member_ids == (1, 2)
        assert cluster.is_strong_repeat is True


def test_prefix_root_does_not_cross_a_word_boundary() -> None:
    """先頭文字が偶然一致する別作品を、共通シリーズとして吸収しない。"""

    programs = [
        _snapshot(1, '魔改造の夜 オフィスチェア対決', '椅子を怪物マシンに改造する。'),
        _snapshot(2, '魔改造の夜明け 技術者たちの朝', '技術者の日常を追う別番組。'),
    ]

    evidence = _buildClusterEvidence(programs, _parseSnapshots(programs))

    assert evidence[1].member_ids == (1,)
    assert evidence[2].member_ids == (2,)
    assert evidence[1].is_strong_repeat is False
    assert evidence[2].is_strong_repeat is False


def test_epg_match_expands_cross_service_after_same_service_has_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """放送局独自タイトルを、他サービスの一意なEPGから補完する。"""

    snapshot = _snapshot(
        101,
        '星空探検隊 FRIDAY ANIME NIGHT[字][デ]',
        '「謎の信号」',
        genres=ANIME_GENRES,
        network_id=12345,
        service_id=678,
    )
    local_parse = _parseSnapshots([snapshot])[snapshot.id]
    filter_calls: list[dict[str, object]] = []

    class FakeQuery:
        def __init__(self, rows: list[dict[str, object]]) -> None:
            self.rows = rows

        async def values(self, *_fields: str) -> list[dict[str, object]]:
            return self.rows

    cross_service_rows: list[dict[str, object]] = [{
        'id': 'NID1-SID2-EID3',
        'title': '星空探検隊 第4期 #87 [字]',
        'description': '#87 謎の信号',
        'detail': {},
        'genres': ANIME_GENRES,
    }]

    def Filter(*_args: object, **kwargs: object) -> FakeQuery:
        filter_calls.append(kwargs)
        is_same_service_query = kwargs.get('network_id') == 12345 and kwargs.get('service_id') == 678
        return FakeQuery([] if is_same_service_query else cross_service_rows)

    monkeypatch.setattr(Program, 'filter', Filter)

    result = asyncio.run(RecordedSeriesResolver._matchEPG(snapshot, local_parse))

    assert len(filter_calls) >= 2
    assert result is not None
    assert result.program_id == 'NID1-SID2-EID3'
    assert result.parse_result.series_title == '星空探検隊'
    assert result.parse_result.episode_number == 'Season 4 #87'
    assert result.parse_result.subtitle == '謎の信号'


def test_enqueue_deduplicates_waiting_item_but_reruns_running_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """待機中は最新DBを1回読むだけにし、実行中の更新だけを再実行予約する。"""

    async def Scenario() -> None:
        queue: asyncio.Queue[int] = asyncio.Queue()

        async def Start(_cls: type[RecordedSeriesResolver]) -> None:
            return None

        monkeypatch.setattr(
            RecordedSeriesSettingsStore,
            'getSettings',
            staticmethod(lambda: SimpleNamespace(enabled=True)),
        )
        monkeypatch.setattr(RecordedSeriesResolver, 'start', classmethod(Start))
        monkeypatch.setattr(RecordedSeriesResolver, '_queue', queue)
        monkeypatch.setattr(RecordedSeriesResolver, '_queued_ids', {10})
        monkeypatch.setattr(RecordedSeriesResolver, '_running_ids', set())
        monkeypatch.setattr(RecordedSeriesResolver, '_rerun_ids', set())

        await RecordedSeriesResolver.enqueue(10)

        assert queue.empty() is True
        assert RecordedSeriesResolver._rerun_ids == set()

        RecordedSeriesResolver._queued_ids.clear()
        RecordedSeriesResolver._running_ids.add(10)
        await RecordedSeriesResolver.enqueue(10)

        assert queue.empty() is True
        assert RecordedSeriesResolver._rerun_ids == {10}

    asyncio.run(Scenario())


def test_snapshot_generation_change_is_rejected_before_resolution_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共有snapshot取得後の入力更新を、Resolution生成・更新前に拒否する。"""

    program = _snapshot(20, '日本の名峰', '第2話 夏の山道を歩く。')
    programs = [program]
    parses = _parseSnapshots(programs)
    evidence = _buildClusterEvidence(programs, parses)
    resolution_update_attempted = False

    async def GetOrCreateResolution(
        _cls: type[RecordedSeriesResolver],
        _snapshot: _ProgramSnapshot,
        _cluster: object,
    ) -> RecordedSeriesResolution:
        nonlocal resolution_update_attempted
        resolution_update_attempted = True
        raise AssertionError('Resolution更新に進んではいけない')

    async def Scenario() -> None:
        monkeypatch.setattr(
            RecordedSeriesSettingsStore,
            'getSettingsAndAPIKey',
            staticmethod(lambda: (SimpleNamespace(enabled=True), None)),
        )
        monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 5)
        monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
        monkeypatch.setattr(
            RecordedSeriesResolver,
            '_getOrCreateResolution',
            classmethod(GetOrCreateResolution),
        )

        with pytest.raises(_RecordedProgramSnapshotChanged):
            await RecordedSeriesResolver.resolveProgram(
                program.id,
                snapshots=programs,
                parses=parses,
                cluster_evidence=evidence,
                snapshot_generation=4,
            )

    asyncio.run(Scenario())
    assert resolution_update_attempted is False


def test_input_changed_persists_pending_marker_before_queueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """録画入力の更新時は、クラッシュ回復用Pendingマーカーをキューより先に永続化する。"""

    events: list[str] = []

    class RecordingQueue(asyncio.Queue[int]):
        async def put(self, item: int) -> None:
            events.append('queue')
            await super().put(item)

    class ResolutionQuery:
        def __init__(self, is_manual: bool) -> None:
            self.is_manual = is_manual

        def filter(self, *_args: object, **_kwargs: object) -> 'ResolutionQuery':
            return self

        async def update(self, **kwargs: object) -> int:
            if self.is_manual:
                assert kwargs == {'input_fingerprint': '0' * 64}
                return 0
            events.append('pending')
            assert kwargs['status'] == 'Pending'
            return 1

        async def exists(self) -> bool:
            return False

    def Filter(**kwargs: object) -> ResolutionQuery:
        assert kwargs['recorded_program_id'] == 30
        return ResolutionQuery(kwargs.get('source') == 'Manual')

    async def Start(_cls: type[RecordedSeriesResolver]) -> None:
        events.append('start')

    async def Scenario() -> None:
        queue = RecordingQueue()
        monkeypatch.setattr(
            RecordedSeriesSettingsStore,
            'getSettings',
            staticmethod(lambda: SimpleNamespace(enabled=True)),
        )
        monkeypatch.setattr(RecordedSeriesResolution, 'filter', staticmethod(Filter))
        monkeypatch.setattr(RecordedSeriesResolver, 'start', classmethod(Start))
        monkeypatch.setattr(RecordedSeriesResolver, '_queue', queue)
        monkeypatch.setattr(RecordedSeriesResolver, '_queued_ids', set())
        monkeypatch.setattr(RecordedSeriesResolver, '_running_ids', set())
        monkeypatch.setattr(RecordedSeriesResolver, '_rerun_ids', set())

        await RecordedSeriesResolver.enqueue(30, input_changed=True)

        assert events == ['pending', 'start', 'queue']
        assert await queue.get() == 30

    asyncio.run(Scenario())


def test_input_changed_persists_pending_marker_while_automatic_resolution_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """無効化中も入力更新を永続化し、再有効化時の回収対象から漏らさない。"""

    events: list[str] = []

    class ResolutionQuery:
        def __init__(self, is_manual: bool) -> None:
            self.is_manual = is_manual

        def filter(self, *_args: object, **_kwargs: object) -> 'ResolutionQuery':
            return self

        async def update(self, **kwargs: object) -> int:
            if self.is_manual:
                return 0
            events.append('pending')
            assert kwargs['status'] == 'Pending'
            return 1

        async def exists(self) -> bool:
            return False

    def Filter(**kwargs: object) -> ResolutionQuery:
        assert kwargs['recorded_program_id'] == 31
        return ResolutionQuery(kwargs.get('source') == 'Manual')

    async def Start(_cls: type[RecordedSeriesResolver]) -> None:
        raise AssertionError('無効化中にワーカーを起動してはいけない')

    async def Scenario() -> None:
        monkeypatch.setattr(
            RecordedSeriesSettingsStore,
            'getSettings',
            staticmethod(lambda: SimpleNamespace(enabled=False)),
        )
        monkeypatch.setattr(RecordedSeriesResolution, 'filter', staticmethod(Filter))
        monkeypatch.setattr(RecordedSeriesResolver, 'start', classmethod(Start))

        await RecordedSeriesResolver.enqueue(31, input_changed=True)

        assert events == ['pending']

    asyncio.run(Scenario())


def test_pending_ai_recovery_only_closes_the_matching_resolution_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中断したAI監査と同じ候補世代だけNeedsReviewへ閉じ、新しいPendingを壊さない。"""

    async def CreateRecordedProgram(program_id: int) -> RecordedProgram:
        start_time = datetime(2026, 7, program_id, 20, 0, tzinfo=JST)
        return await RecordedProgram.create(
            id=program_id,
            recording_start_margin=0.0,
            recording_end_margin=0.0,
            is_partially_recorded=False,
            channel_id=None,
            network_id=4,
            service_id=101,
            event_id=program_id,
            series_id=None,
            series_broadcast_period_id=None,
            title=f'復旧テスト {program_id}',
            series_title=None,
            episode_number=None,
            subtitle=None,
            description='中断復旧のテスト番組。',
            detail={},
            start_time=start_time,
            end_time=start_time + timedelta(hours=1),
            duration=3600.0,
            is_free=True,
            genres=DOCUMENTARY_GENRES,
            primary_audio_type='1/0モード(モノラル)',
            primary_audio_language='日本語',
            secondary_audio_type=None,
            secondary_audio_language=None,
        )

    async def Scenario() -> None:
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
        try:
            matching_program = await CreateRecordedProgram(1)
            newer_program = await CreateRecordedProgram(2)
            matching_resolution = await RecordedSeriesResolution.create(
                recorded_program=matching_program,
                input_fingerprint='1' * 64,
                evidence_hash='a' * 64,
                resolver_version='test',
                normalized_title='matching',
                status='Pending',
                candidate_set_hash='2' * 64,
            )
            newer_resolution = await RecordedSeriesResolution.create(
                recorded_program=newer_program,
                input_fingerprint='3' * 64,
                evidence_hash='b' * 64,
                resolver_version='test',
                normalized_title='newer',
                status='Pending',
                candidate_set_hash='4' * 64,
            )
            matching_request = await RecordedSeriesAIRequest.create(
                resolution=matching_resolution,
                purpose='Resolution',
                status='Pending',
                model='test-model',
                input_fingerprint='1' * 64,
                candidate_set_hash='2' * 64,
                candidate_ids=['wiki:1'],
            )
            stale_request = await RecordedSeriesAIRequest.create(
                resolution=newer_resolution,
                purpose='Resolution',
                status='Pending',
                model='test-model',
                input_fingerprint='5' * 64,
                candidate_set_hash='6' * 64,
                candidate_ids=['wiki:2'],
            )
            orphaned_request = await RecordedSeriesAIRequest.create(
                resolution=None,
                purpose='Resolution',
                status='Pending',
                model='test-model',
                input_fingerprint='7' * 64,
                candidate_set_hash='8' * 64,
                candidate_ids=['wiki:3'],
            )

            monkeypatch.setattr(
                RecordedSeriesSettingsStore,
                'getSettings',
                staticmethod(lambda: SimpleNamespace(enabled=False)),
            )
            monkeypatch.setattr(RecordedSeriesResolver, '_recovery_lock', asyncio.Lock())
            monkeypatch.setattr(RecordedSeriesResolver, '_pending_recovery_completed', False)

            await RecordedSeriesResolver._recoverPendingResolutions()

            await matching_resolution.refresh_from_db()
            await newer_resolution.refresh_from_db()
            await matching_request.refresh_from_db()
            await stale_request.refresh_from_db()
            await orphaned_request.refresh_from_db()
            assert matching_resolution.status == 'NeedsReview'
            assert matching_resolution.source == 'AI'
            assert matching_resolution.error_code == 'AIRequestInterrupted'
            assert newer_resolution.status == 'Pending'
            assert newer_resolution.source is None
            assert newer_resolution.error_code is None
            assert matching_request.status == 'Failed'
            assert matching_request.error_code == 'AIRequestInterrupted'
            assert stale_request.status == 'Failed'
            assert stale_request.error_code == 'AIRequestInterrupted'
            assert orphaned_request.status == 'Failed'
            assert orphaned_request.error_code == 'AIRequestInterrupted'
            assert RecordedSeriesResolver._pending_recovery_completed is True
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_retry_after_completed_recovery_only_enqueues_pending_resolutions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """起動時回収後の設定変更では実行中AI監査を再度中断扱いにしない。"""

    events: list[str] = []

    async def Recover(_cls: type[RecordedSeriesResolver]) -> None:
        events.append('recover')

    async def EnqueuePending(_cls: type[RecordedSeriesResolver]) -> None:
        events.append('enqueue')

    async def Scenario() -> None:
        monkeypatch.setattr(RecordedSeriesResolver, '_worker_task', object())
        monkeypatch.setattr(RecordedSeriesResolver, '_pending_recovery_completed', True)
        monkeypatch.setattr(RecordedSeriesResolver, '_recoverPendingResolutions', classmethod(Recover))
        monkeypatch.setattr(
            RecordedSeriesResolver,
            '_enqueuePendingResolutionsIfEnabled',
            classmethod(EnqueuePending),
        )

        await RecordedSeriesResolver.retryPendingRecovery()

        assert events == ['enqueue']

    asyncio.run(Scenario())


def test_ai_attempt_key_distinguishes_candidate_set_generation() -> None:
    """同じ録画入力でも候補集合が更新された場合は、過去の試行cacheと衝突しない。"""

    common = {
        'resolution_id': 10,
        'input_fingerprint': '1' * 64,
        'evidence_hash': '2' * 64,
        'api_base_url': 'https://example.invalid/v1',
        'model': 'test-model',
        'api_key': 'test-key',
    }
    old_generation = _buildAIAttemptKey(candidate_set_hash='3' * 64, **common)
    same_generation = _buildAIAttemptKey(candidate_set_hash='3' * 64, **common)
    new_generation = _buildAIAttemptKey(candidate_set_hash='4' * 64, **common)

    assert old_generation == same_generation
    assert old_generation != new_generation


def test_reconcile_broadcast_period_shrinks_and_moves_preserved_series() -> None:
    """再分類で期間を縮め、保持したSeriesが別チャンネルへ移るとき空期間を残さない。"""

    async def CreateProgram(
        program_id: int,
        day: int,
        series: Series,
        period: SeriesBroadcastPeriod,
    ) -> RecordedProgram:
        start_time = datetime(2026, 7, day, 20, 0, tzinfo=JST)
        return await RecordedProgram.create(
            id=program_id,
            recording_start_margin=0.0,
            recording_end_margin=0.0,
            is_partially_recorded=False,
            channel_id='NID4-SID101',
            network_id=4,
            service_id=101,
            event_id=program_id,
            series=series,
            series_broadcast_period=period,
            title=f'期間再計算 {program_id}',
            series_title=series.title,
            episode_number=f'#{program_id}',
            subtitle=None,
            description='放送期間再計算のテスト。',
            detail={},
            start_time=start_time,
            end_time=start_time + timedelta(hours=1),
            duration=3600.0,
            is_free=True,
            genres=DOCUMENTARY_GENRES,
            primary_audio_type='1/0モード(モノラル)',
            primary_audio_language='日本語',
            secondary_audio_type=None,
            secondary_audio_language=None,
        )

    async def Scenario() -> None:
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
        try:
            channel = await Channel.create(
                id='NID4-SID101',
                display_channel_id='gr011',
                network_id=4,
                service_id=101,
                transport_stream_id=1,
                remocon_id=1,
                channel_number='011',
                type='GR',
                name='テストチャンネル',
                jikkyo_force=None,
                is_subchannel=False,
                is_radiochannel=False,
                is_watchable=True,
            )
            await Channel.create(
                id='NID4-SID102',
                display_channel_id='gr012',
                network_id=4,
                service_id=102,
                transport_stream_id=1,
                remocon_id=1,
                channel_number='012',
                type='GR',
                name='テストチャンネル2',
                jikkyo_force=None,
                is_subchannel=False,
                is_radiochannel=False,
                is_watchable=True,
            )
            series = await Series.create(
                title='期間再計算シリーズ',
                description='テスト',
                genres=DOCUMENTARY_GENRES,
            )
            period = await SeriesBroadcastPeriod.create(
                series=series,
                channel=channel,
                start_date=datetime(2026, 7, 1, tzinfo=JST).date(),
                end_date=datetime(2026, 7, 3, tzinfo=JST).date(),
            )
            first = await CreateProgram(1, 1, series, period)
            last = await CreateProgram(2, 3, series, period)

            async with transactions.in_transaction() as connection:
                first.series_id = None
                first.series_broadcast_period_id = None
                await first.save(update_fields=['series_id', 'series_broadcast_period_id'], using_db=connection)
                await RecordedSeriesResolver._reconcileBroadcastPeriod(period.id, connection=connection)
            await period.refresh_from_db()
            assert period.start_date == datetime(2026, 7, 3, tzinfo=JST).date()
            assert period.end_date == datetime(2026, 7, 3, tzinfo=JST).date()

            moved_start_time = datetime(2026, 7, 5, 20, 0, tzinfo=JST)
            last.channel_id = 'NID4-SID102'
            last.service_id = 102
            last.start_time = moved_start_time
            last.end_time = moved_start_time + timedelta(hours=1)
            await last.save(update_fields=['channel_id', 'service_id', 'start_time', 'end_time'])
            snapshot = _ProgramSnapshot(
                id=last.id,
                channel_id=last.channel_id,
                network_id=last.network_id,
                service_id=last.service_id,
                event_id=last.event_id,
                title=last.title,
                description=last.description,
                detail=last.detail,
                genres=last.genres,
                start_time=last.start_time,
            )
            resolution = await RecordedSeriesResolution.create(
                recorded_program=last,
                input_fingerprint='9' * 64,
                evidence_hash='8' * 64,
                resolver_version='test',
                normalized_title='period-test',
                status='Pending',
            )
            RecordedSeriesResolver._snapshot_generation = 0
            await RecordedSeriesResolver._applyNeedsReview(
                snapshot=snapshot,
                resolution=resolution,
                expected_generation=0,
                source='Local',
                error_code='AIIsDisabled',
                clear_series=False,
            )
            await last.refresh_from_db()
            moved_period = await SeriesBroadcastPeriod.get(id=last.series_broadcast_period_id)
            assert last.series_id == series.id
            assert moved_period.channel_id == 'NID4-SID102'
            assert moved_period.start_date == moved_start_time.date()
            assert moved_period.end_date == moved_start_time.date()
            assert await SeriesBroadcastPeriod.filter(id=period.id).exists() is False
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_zero_ai_request_limit_skips_daily_count_and_checks_recent_attempt_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0は無制限として日次集計を省略し、候補構築後の直近AI試行だけを抑止する。"""

    snapshot = _snapshot(10, '候補構築テスト', 'シリーズか単発か曖昧な番組。')
    programs = [snapshot]
    parses = _parseSnapshots(programs)
    evidence = _buildClusterEvidence(programs, parses)
    events: list[str] = []
    recent_attempt_key = 'recent-attempt'

    class FakeResolution:
        id = 10
        input_fingerprint = ''
        evidence_hash = ''
        resolver_version = ''
        normalized_title = ''
        status = 'NeedsReview'
        source: str | None = None
        error_code: str | None = None
        error_message: str | None = None

        async def save(self, **_kwargs: object) -> None:
            events.append('resolution-saved')

    async def LoadCurrentSnapshot(
        _cls: type[RecordedSeriesResolver],
        _recorded_program_id: int,
    ) -> _ProgramSnapshot:
        return snapshot

    async def GetOrCreateResolution(
        _cls: type[RecordedSeriesResolver],
        _snapshot: _ProgramSnapshot,
        _cluster: object,
    ) -> FakeResolution:
        return FakeResolution()

    async def GetRule(**_kwargs: object) -> None:
        return None

    async def MatchEPG(
        _cls: type[RecordedSeriesResolver],
        _snapshot: _ProgramSnapshot,
        _local_parse: SeriesTitleParseResult,
    ) -> None:
        return None

    async def FindExistingSeries(
        _cls: type[RecordedSeriesResolver],
        _title: str,
    ) -> list[object]:
        return []

    async def SearchWikipedia(_title: str, *, limit: int) -> list[dict[str, object]]:
        events.append(f'wikipedia:{limit}')
        return [{'page_id': 123, 'title': '候補構築テスト', 'extract': '候補本文'}]

    def BuildAttemptKey(**kwargs: object) -> str:
        events.append('attempt-key')
        assert kwargs['candidate_set_hash'] is not None
        return recent_attempt_key

    async def ApplyNeedsReview(
        _cls: type[RecordedSeriesResolver],
        **kwargs: object,
    ) -> None:
        events.append('attempt-cache')
        assert kwargs['candidate_set_hash'] is not None
        candidates = cast(list[dict[str, object]], kwargs['candidate_snapshot'])
        assert any(candidate['choice_id'] == 'wiki:123' for candidate in candidates)
        assert kwargs['error_code'] == 'RecentAIAttempt'

    async def SelectCandidate(**_kwargs: object) -> None:
        raise AssertionError('直近試行cacheの一致時はAIへ再送してはいけない')

    async def CreateAIRequest(**_kwargs: object) -> None:
        raise AssertionError('直近試行cacheの一致時は監査行を追加してはいけない')

    def CountAIRequests(**_kwargs: object) -> None:
        raise AssertionError('0指定時は日次集計してはいけない')

    async def Scenario() -> None:
        monkeypatch.setattr(
            RecordedSeriesSettingsStore,
            'getSettingsAndAPIKey',
            staticmethod(lambda: (
                SimpleNamespace(
                    enabled=True,
                    ai_enabled=True,
                    api_base_url='https://example.invalid/v1',
                    model='test-model',
                    daily_ai_request_limit=0,
                ),
                'test-key',
            )),
        )
        monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)
        monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
        monkeypatch.setattr(RecordedSeriesResolver, '_ai_attempt_keys', {recent_attempt_key: time.monotonic()})
        monkeypatch.setattr(RecordedSeriesResolver, '_loadProgramSnapshot', classmethod(LoadCurrentSnapshot))
        monkeypatch.setattr(RecordedSeriesResolver, '_getOrCreateResolution', classmethod(GetOrCreateResolution))
        monkeypatch.setattr(RecordedSeriesRule, 'get_or_none', staticmethod(GetRule))
        monkeypatch.setattr(RecordedSeriesResolver, '_matchEPG', classmethod(MatchEPG))
        monkeypatch.setattr(RecordedSeriesResolver, '_findExistingSeriesCandidates', classmethod(FindExistingSeries))
        monkeypatch.setattr(
            RecordedSeriesAIRequest,
            'filter',
            staticmethod(CountAIRequests),
        )
        monkeypatch.setattr(RecordedSeriesAIRequest, 'create', staticmethod(CreateAIRequest))
        monkeypatch.setattr(
            'app.metadata.RecordedSeriesResolver.SearchWikipediaCandidates',
            SearchWikipedia,
        )
        monkeypatch.setattr(
            'app.metadata.RecordedSeriesResolver._buildAIAttemptKey',
            BuildAttemptKey,
        )
        monkeypatch.setattr(
            'app.metadata.RecordedSeriesResolver.SelectRecordedSeriesCandidate',
            SelectCandidate,
        )
        monkeypatch.setattr(RecordedSeriesResolver, '_applyNeedsReview', classmethod(ApplyNeedsReview))

        result = await RecordedSeriesResolver.resolveProgram(
            snapshot.id,
            snapshots=programs,
            parses=parses,
            cluster_evidence=evidence,
            snapshot_generation=0,
        )

        assert result.status == 'Skipped'
        assert result.source == 'AIAttemptCache'
        assert result.ai_requested is False
        assert events.index('wikipedia:5') < events.index('attempt-key') < events.index('attempt-cache')

    asyncio.run(Scenario())


def test_ai_can_resolve_without_existing_or_wikipedia_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部候補が0件でも、固定済みのローカル・単発・未解決候補だけをAIへ渡す。"""

    snapshot = _snapshot(
        10,
        '地域チャンネル▽学生放送コンテスト2025',
        '複数校の応募作品を紹介する特別番組。',
    )
    programs = [snapshot]
    parses = _parseSnapshots(programs)
    evidence = _buildClusterEvidence(programs, parses)
    created_candidate_ids: list[str] = []
    selected_candidates: list[dict[str, object]] = []
    ai_request = object()

    class FakeResolution:
        id = 10
        input_fingerprint = ''
        evidence_hash = ''
        resolver_version = ''
        normalized_title = ''
        status = 'NeedsReview'
        source: str | None = None
        error_code: str | None = None
        error_message: str | None = None

        async def save(self, **_kwargs: object) -> None:
            return None

    class CountQuery:
        def filter(self, **_kwargs: object) -> 'CountQuery':
            return self

        async def count(self) -> int:
            return 0

    async def LoadCurrentSnapshot(
        _cls: type[RecordedSeriesResolver],
        _recorded_program_id: int,
    ) -> _ProgramSnapshot:
        return snapshot

    async def GetOrCreateResolution(
        _cls: type[RecordedSeriesResolver],
        _snapshot: _ProgramSnapshot,
        _cluster: object,
    ) -> FakeResolution:
        return FakeResolution()

    async def GetRule(**_kwargs: object) -> None:
        return None

    async def MatchEPG(
        _cls: type[RecordedSeriesResolver],
        _snapshot: _ProgramSnapshot,
        _local_parse: SeriesTitleParseResult,
    ) -> None:
        return None

    async def FindExistingSeries(
        _cls: type[RecordedSeriesResolver],
        _title: str,
    ) -> list[object]:
        return []

    async def SearchWikipedia(_title: str, *, limit: int) -> list[dict[str, object]]:
        assert limit == 5
        return []

    async def CreateAIRequest(**kwargs: object) -> object:
        created_candidate_ids.extend(cast(list[str], kwargs['candidate_ids']))
        assert len(created_candidate_ids) == 3
        assert created_candidate_ids[0].startswith('local:')
        assert created_candidate_ids[1:] == ['standalone', 'unresolved']
        return ai_request

    async def SelectCandidate(**kwargs: object) -> SimpleNamespace:
        selected_candidates.extend(cast(list[dict[str, object]], kwargs['candidates']))
        assert len(selected_candidates) == 3
        assert cast(str, selected_candidates[0]['choice_id']).startswith('local:')
        assert [candidate['kind'] for candidate in selected_candidates] == [
            'Local',
            'Standalone',
            'Unresolved',
        ]
        assert [candidate['choice_id'] for candidate in selected_candidates] == created_candidate_ids
        return SimpleNamespace(choice_id='standalone', confidence=0.95, model='test-model')

    async def FinishAIRequest(
        _cls: type[RecordedSeriesResolver],
        **kwargs: object,
    ) -> None:
        assert kwargs['request'] is ai_request
        assert kwargs['status'] == 'Succeeded'
        assert kwargs['selected_choice_id'] == 'standalone'

    async def ApplyNotSeries(
        _cls: type[RecordedSeriesResolver],
        **kwargs: object,
    ) -> None:
        assert kwargs['source'] == 'AI'
        assert kwargs['confidence'] == 0.95
        assert kwargs['candidate_snapshot'] == selected_candidates
        assert kwargs['ai_model'] == 'test-model'

    async def Scenario() -> None:
        monkeypatch.setattr(
            RecordedSeriesSettingsStore,
            'getSettingsAndAPIKey',
            staticmethod(lambda: (
                SimpleNamespace(
                    enabled=True,
                    ai_enabled=True,
                    api_base_url='https://example.invalid/v1',
                    model='test-model',
                    daily_ai_request_limit=20,
                ),
                'test-key',
            )),
        )
        monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)
        monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
        monkeypatch.setattr(RecordedSeriesResolver, '_ai_attempt_keys', {})
        monkeypatch.setattr(RecordedSeriesResolver, '_loadProgramSnapshot', classmethod(LoadCurrentSnapshot))
        monkeypatch.setattr(RecordedSeriesResolver, '_getOrCreateResolution', classmethod(GetOrCreateResolution))
        monkeypatch.setattr(RecordedSeriesRule, 'get_or_none', staticmethod(GetRule))
        monkeypatch.setattr(RecordedSeriesResolver, '_matchEPG', classmethod(MatchEPG))
        monkeypatch.setattr(RecordedSeriesResolver, '_findExistingSeriesCandidates', classmethod(FindExistingSeries))
        monkeypatch.setattr(RecordedSeriesAIRequest, 'filter', staticmethod(lambda **_kwargs: CountQuery()))
        monkeypatch.setattr(RecordedSeriesAIRequest, 'create', staticmethod(CreateAIRequest))
        monkeypatch.setattr(
            'app.metadata.RecordedSeriesResolver.SearchWikipediaCandidates',
            SearchWikipedia,
        )
        monkeypatch.setattr(
            'app.metadata.RecordedSeriesResolver.SelectRecordedSeriesCandidate',
            SelectCandidate,
        )
        monkeypatch.setattr(RecordedSeriesResolver, '_finishAIRequest', classmethod(FinishAIRequest))
        monkeypatch.setattr(RecordedSeriesResolver, '_applyNotSeries', classmethod(ApplyNotSeries))

        result = await RecordedSeriesResolver.resolveProgram(
            snapshot.id,
            snapshots=programs,
            parses=parses,
            cluster_evidence=evidence,
            snapshot_generation=0,
        )

        assert result.status == 'NotSeries'
        assert result.source == 'AI'
        assert result.ai_requested is True

    asyncio.run(Scenario())


def test_generation_change_while_building_wikipedia_candidates_stops_before_ai_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MediaWiki待機中の入力更新は、AI監査の予約や外部POSTより前に拒否する。"""

    snapshot = _snapshot(11, '候補世代更新テスト', 'シリーズか単発か曖昧な番組。')
    programs = [snapshot]
    parses = _parseSnapshots(programs)
    evidence = _buildClusterEvidence(programs, parses)
    events: list[str] = []

    class FakeResolution:
        id = 11
        input_fingerprint = ''
        evidence_hash = ''
        resolver_version = ''
        normalized_title = ''
        status = 'NeedsReview'
        source: str | None = None
        error_code: str | None = None
        error_message: str | None = None

        async def save(self, **_kwargs: object) -> None:
            return None

    class CountQuery:
        def filter(self, **_kwargs: object) -> 'CountQuery':
            return self

        async def count(self) -> int:
            return 0

    async def LoadCurrentSnapshot(
        _cls: type[RecordedSeriesResolver],
        _recorded_program_id: int,
    ) -> _ProgramSnapshot:
        events.append('snapshot')
        return snapshot

    async def GetOrCreateResolution(
        _cls: type[RecordedSeriesResolver],
        _snapshot: _ProgramSnapshot,
        _cluster: object,
    ) -> FakeResolution:
        return FakeResolution()

    async def GetRule(**_kwargs: object) -> None:
        return None

    async def MatchEPG(
        _cls: type[RecordedSeriesResolver],
        _snapshot: _ProgramSnapshot,
        _local_parse: SeriesTitleParseResult,
    ) -> None:
        return None

    async def FindExistingSeries(
        _cls: type[RecordedSeriesResolver],
        _title: str,
    ) -> list[object]:
        return []

    async def SearchWikipedia(_title: str, *, limit: int) -> list[dict[str, object]]:
        events.append(f'wikipedia:{limit}')
        RecordedSeriesResolver._snapshot_generation += 1
        return [{'page_id': 456, 'title': '候補世代更新テスト', 'extract': '候補本文'}]

    def BuildAttemptKey(**_kwargs: object) -> str:
        raise AssertionError('世代更新後の候補でAI試行キーを作ってはいけない')

    async def CreateAIRequest(**_kwargs: object) -> None:
        raise AssertionError('世代更新後の候補でAI監査を予約してはいけない')

    async def SelectCandidate(**_kwargs: object) -> None:
        raise AssertionError('世代更新後の候補をAIへ送ってはいけない')

    async def Scenario() -> None:
        monkeypatch.setattr(
            RecordedSeriesSettingsStore,
            'getSettingsAndAPIKey',
            staticmethod(lambda: (
                SimpleNamespace(
                    enabled=True,
                    ai_enabled=True,
                    api_base_url='https://example.invalid/v1',
                    model='test-model',
                    daily_ai_request_limit=20,
                ),
                'test-key',
            )),
        )
        monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)
        monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
        monkeypatch.setattr(RecordedSeriesResolver, '_ai_attempt_keys', {})
        monkeypatch.setattr(RecordedSeriesResolver, '_loadProgramSnapshot', classmethod(LoadCurrentSnapshot))
        monkeypatch.setattr(RecordedSeriesResolver, '_getOrCreateResolution', classmethod(GetOrCreateResolution))
        monkeypatch.setattr(RecordedSeriesRule, 'get_or_none', staticmethod(GetRule))
        monkeypatch.setattr(RecordedSeriesResolver, '_matchEPG', classmethod(MatchEPG))
        monkeypatch.setattr(RecordedSeriesResolver, '_findExistingSeriesCandidates', classmethod(FindExistingSeries))
        monkeypatch.setattr(RecordedSeriesAIRequest, 'filter', staticmethod(lambda **_kwargs: CountQuery()))
        monkeypatch.setattr(RecordedSeriesAIRequest, 'create', staticmethod(CreateAIRequest))
        monkeypatch.setattr(
            'app.metadata.RecordedSeriesResolver.SearchWikipediaCandidates',
            SearchWikipedia,
        )
        monkeypatch.setattr(
            'app.metadata.RecordedSeriesResolver._buildAIAttemptKey',
            BuildAttemptKey,
        )
        monkeypatch.setattr(
            'app.metadata.RecordedSeriesResolver.SelectRecordedSeriesCandidate',
            SelectCandidate,
        )

        with pytest.raises(_RecordedProgramSnapshotChanged):
            await RecordedSeriesResolver.resolveProgram(
                snapshot.id,
                snapshots=programs,
                parses=parses,
                cluster_evidence=evidence,
                snapshot_generation=0,
            )

        assert events == ['snapshot', 'wikipedia:5']

    asyncio.run(Scenario())


def test_generation_change_while_creating_ai_audit_closes_it_before_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI監査予約中の入力更新は失敗監査へ閉じ、外部POSTせず再実行へ戻す。"""

    snapshot = _snapshot(12, '監査予約世代更新テスト', 'シリーズか単発か曖昧な番組。')
    programs = [snapshot]
    parses = _parseSnapshots(programs)
    evidence = _buildClusterEvidence(programs, parses)
    events: list[str] = []
    select_called = False

    class FakeResolution:
        id = 12
        input_fingerprint = ''
        evidence_hash = ''
        resolver_version = ''
        normalized_title = ''
        status = 'NeedsReview'
        source: str | None = None
        error_code: str | None = None
        error_message: str | None = None
        candidate_set_hash: str | None = None
        candidate_snapshot: list[object] | None = None
        ai_model: str | None = None

        async def save(self, **_kwargs: object) -> None:
            events.append('resolution-saved')

    class FakeAIRequest:
        status = 'Pending'
        model = 'test-model'
        selected_choice_id: str | None = None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        http_status: int | None = None
        latency_ms: int | None = None
        error_code: str | None = None

        async def save(self, **_kwargs: object) -> None:
            events.append('audit-failed')

    class CountQuery:
        def filter(self, **_kwargs: object) -> 'CountQuery':
            return self

        async def count(self) -> int:
            return 0

    ai_request = FakeAIRequest()

    async def LoadCurrentSnapshot(
        _cls: type[RecordedSeriesResolver],
        _recorded_program_id: int,
    ) -> _ProgramSnapshot:
        events.append('snapshot')
        return snapshot

    async def GetOrCreateResolution(
        _cls: type[RecordedSeriesResolver],
        _snapshot: _ProgramSnapshot,
        _cluster: object,
    ) -> FakeResolution:
        return FakeResolution()

    async def GetRule(**_kwargs: object) -> None:
        return None

    async def MatchEPG(
        _cls: type[RecordedSeriesResolver],
        _snapshot: _ProgramSnapshot,
        _local_parse: SeriesTitleParseResult,
    ) -> None:
        return None

    async def FindExistingSeries(
        _cls: type[RecordedSeriesResolver],
        _title: str,
    ) -> list[object]:
        return []

    async def SearchWikipedia(_title: str, *, limit: int) -> list[dict[str, object]]:
        events.append(f'wikipedia:{limit}')
        return [{'page_id': 789, 'title': '監査予約世代更新テスト', 'extract': '候補本文'}]

    async def CreateAIRequest(**_kwargs: object) -> FakeAIRequest:
        events.append('audit-created')
        RecordedSeriesResolver._snapshot_generation += 1
        return ai_request

    async def SelectCandidate(**_kwargs: object) -> None:
        nonlocal select_called
        select_called = True

    async def Scenario() -> None:
        monkeypatch.setattr(
            RecordedSeriesSettingsStore,
            'getSettingsAndAPIKey',
            staticmethod(lambda: (
                SimpleNamespace(
                    enabled=True,
                    ai_enabled=True,
                    api_base_url='https://example.invalid/v1',
                    model='test-model',
                    daily_ai_request_limit=20,
                ),
                'test-key',
            )),
        )
        monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)
        monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
        monkeypatch.setattr(RecordedSeriesResolver, '_ai_attempt_keys', {})
        monkeypatch.setattr(RecordedSeriesResolver, '_loadProgramSnapshot', classmethod(LoadCurrentSnapshot))
        monkeypatch.setattr(RecordedSeriesResolver, '_getOrCreateResolution', classmethod(GetOrCreateResolution))
        monkeypatch.setattr(RecordedSeriesRule, 'get_or_none', staticmethod(GetRule))
        monkeypatch.setattr(RecordedSeriesResolver, '_matchEPG', classmethod(MatchEPG))
        monkeypatch.setattr(RecordedSeriesResolver, '_findExistingSeriesCandidates', classmethod(FindExistingSeries))
        monkeypatch.setattr(RecordedSeriesAIRequest, 'filter', staticmethod(lambda **_kwargs: CountQuery()))
        monkeypatch.setattr(RecordedSeriesAIRequest, 'create', staticmethod(CreateAIRequest))
        monkeypatch.setattr(
            'app.metadata.RecordedSeriesResolver.SearchWikipediaCandidates',
            SearchWikipedia,
        )
        monkeypatch.setattr(
            'app.metadata.RecordedSeriesResolver.SelectRecordedSeriesCandidate',
            SelectCandidate,
        )

        with pytest.raises(_RecordedProgramSnapshotChanged):
            await RecordedSeriesResolver.resolveProgram(
                snapshot.id,
                snapshots=programs,
                parses=parses,
                cluster_evidence=evidence,
                snapshot_generation=0,
            )

        assert ai_request.status == 'Failed'
        assert ai_request.error_code == 'InputChangedBeforeRequest'
        assert select_called is False
        assert RecordedSeriesResolver._ai_attempt_keys == {}
        assert events.count('snapshot') == 3
        assert events.index('audit-created') < events.index('audit-failed')

    asyncio.run(Scenario())
