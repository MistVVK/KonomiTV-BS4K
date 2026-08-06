# pyright: reportPrivateUsage=false

import asyncio
import time
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from tortoise import Tortoise, transactions
from tortoise.backends.base.client import BaseDBAsyncClient

from app.constants import JST
from app.metadata.RecordedEpisodeResolver import (
    ParsedEpisodeNumber,
    RecordedEpisodeResolver,
)
from app.metadata.RecordedSeriesCandidates import RecordedSeriesAIError
from app.metadata.RecordedSeriesGeneration import (
    AISeriesMetadataResult,
    SeriesMetadataHints,
)
from app.metadata.RecordedSeriesResolver import (
    RECORDED_SERIES_RESOLVER_VERSION,
    RecordedSeriesResolver,
    _buildAIAttemptKey,
    _buildClusterEvidence,
    _ProgramSnapshot,
    _RecordedProgramSnapshotChanged,
)
from app.metadata.RecordedSeriesSettings import (
    RecordedSeriesSettings,
    RecordedSeriesSettingsStore,
)
from app.metadata.SeriesTitleParser import (
    BuildSeriesGroupingKey,
    ParseSeriesTitle,
    SeriesTitleParseResult,
)
from app.models.Channel import Channel
from app.models.Program import Program
from app.models.RecordedEpisode import RecordedEpisodeResolution, SeriesEpisode
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedSeries import (
    RecordedSeriesAIRequest,
    RecordedSeriesResolution,
    RecordedSeriesRule,
)
from app.models.RecordedVideo import RecordedVideo
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


async def InitializeGenerationDatabase() -> None:
    """一括生成の Series / Episode 同時反映に必要なモデルを初期化する。"""

    await Tortoise.init(
        db_url='sqlite://:memory:',
        modules={'models': [
            'app.models.Channel',
            'app.models.RecordedEpisode',
            'app.models.RecordedProgram',
            'app.models.RecordedVideo',
            'app.models.RecordedSeries',
            'app.models.Series',
            'app.models.SeriesBroadcastPeriod',
        ]},
        timezone='Asia/Tokyo',
    )
    await Tortoise.generate_schemas()


async def CreateGenerationChannel() -> Channel:
    """一括生成テスト用の録画チャンネルを作成する。"""

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


async def CreateGenerationProgram(
    program_id: int,
    *,
    title: str = '宇宙兄弟　第3話「弟の覚悟」[新]',
) -> RecordedProgram:
    """一括生成 Resolver が参照する再生可能録画を作成する。"""

    start_time = datetime(2026, 7, min(program_id, 28), 20, 0, tzinfo=JST)
    program = await RecordedProgram.create(
        id=program_id,
        recording_start_margin=0.0,
        recording_end_margin=0.0,
        is_partially_recorded=False,
        channel_id='NID4-SID211',
        network_id=4,
        service_id=211,
        event_id=program_id,
        series_id=None,
        series_broadcast_period_id=None,
        title=title,
        series_title=None,
        episode_number=None,
        subtitle=None,
        description='宇宙飛行士を目指す兄弟を描くアニメ。',
        detail={},
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
        status='Recorded',
        file_path=f'/tmp/series-generation-{program_id}.ts',
        file_hash=f'generation-hash-{program_id}',
        file_size=1,
        file_created_at=start_time,
        file_modified_at=start_time,
        duration=3600.0,
        container_format='MPEG-TS',
    )
    return program


def GenerationSettings(
    *,
    ai_enabled: bool,
    legacy_candidate_selection_enabled: bool = True,
) -> RecordedSeriesSettings:
    """外部接続を mock する Resolver 用設定を返す。"""

    return RecordedSeriesSettings(
        enabled=True,
        ai_enabled=ai_enabled,
        ai_candidate_selection_enabled=legacy_candidate_selection_enabled,
        ai_episode_number_search_enabled=True,
        ai_backend='AcpCodex',
    )


def GeneratedSeriesResult(
    *,
    series_title: str = '宇宙兄弟',
    season_number: int | None = 1,
    episode_number: Decimal | None = Decimal('3'),
    episode_not_numbered: bool = False,
    subtitle: str | None = '弟の覚悟',
    confidence: float = 0.42,
    existing_series_id: int | None = None,
    wikipedia_page_id: int | None = None,
) -> AISeriesMetadataResult:
    """一括生成 backend の検証済み mock 結果を作る。"""

    return AISeriesMetadataResult(
        decision='Series',
        series_title=series_title,
        season_number=season_number,
        episode_number=episode_number,
        episode_not_numbered=episode_not_numbered,
        subtitle=subtitle,
        confidence=confidence,
        existing_series_id=existing_series_id,
        wikipedia_page_id=wikipedia_page_id,
        rationale_short='Synthetic test result.',
        model='openai:test-model',
        prompt_tokens=10,
        completion_tokens=5,
        http_status=200,
        latency_ms=20,
    )


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
            staticmethod(lambda: (
                SimpleNamespace(
                    enabled=True,
                    ai_backend='AcpCodex',
                    acp_model=None,
                    ai_backend_service_id=None,
                ),
                None,
            )),
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
                'app.models.RecordedEpisode',
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

    def BuildAttemptKey(candidate_set_hash: str) -> str:
        return _buildAIAttemptKey(
            resolution_id=10,
            input_fingerprint='1' * 64,
            evidence_hash='2' * 64,
            candidate_set_hash=candidate_set_hash,
            api_base_url='https://example.invalid/v1',
            model='test-model',
            api_key='test-key',
        )

    old_generation = BuildAttemptKey('3' * 64)
    same_generation = BuildAttemptKey('3' * 64)
    new_generation = BuildAttemptKey('4' * 64)

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
                'app.models.RecordedEpisode',
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
    ) -> tuple[list[object], list[object]]:
        return [], []

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
        snapshots = cast(list[dict[str, object]], kwargs['candidate_snapshot'])
        assert len(snapshots) == 1
        hints = snapshots[0]
        wikipedia = cast(list[dict[str, object]], hints['wikipedia'])
        assert wikipedia[0]['page_id'] == 123
        assert kwargs['error_code'] == 'RecentAIAttempt'

    async def ResolveMetadata(**_kwargs: object) -> None:
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
                    ai_backend='AcpCodex',
                    acp_model=None,
                    ai_backend_service_id=None,
                ),
                None,
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
            'app.metadata.RecordedSeriesResolver.ai_resolve_series_metadata',
            ResolveMetadata,
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


@pytest.mark.parametrize(
    'ai_backend,acp_model,expected_audit_model,failure_code',
    [
        ('AcpCodex', 'test-acp-model', 'acp:codex:test-acp-model', None),
        ('AcpCodex', 'test-acp-model', 'acp:codex:test-acp-model', 'HostCLIStartFailed'),
    ],
    ids=['acp-success', 'acp-setup-failure'],
)
def test_ai_generation_audit_uses_backend_prefix_and_closes_failures(
    monkeypatch: pytest.MonkeyPatch,
    ai_backend: str,
    acp_model: str | None,
    expected_audit_model: str,
    failure_code: str | None,
) -> None:
    """候補0件時も backend 付き監査を予約し、ACP セットアップ失敗を終端する。"""

    snapshot = _snapshot(
        10,
        '地域チャンネル▽学生放送コンテスト2025',
        '複数校の応募作品を紹介する特別番組。',
    )
    programs = [snapshot]
    parses = _parseSnapshots(programs)
    evidence = _buildClusterEvidence(programs, parses)
    created_candidate_ids: list[str] = []
    generated_hints: dict[str, object] = {}
    events: list[str] = []
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
        def filter(self, *_args: object, **_kwargs: object) -> 'CountQuery':
            return self

        async def count(self) -> int:
            return 0

    def FilterAIRequests(**_kwargs: object) -> CountQuery:
        return CountQuery()

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
    ) -> tuple[list[object], list[object]]:
        return [], []

    async def SearchWikipedia(_title: str, *, limit: int) -> list[dict[str, object]]:
        assert limit == 5
        return []

    async def CreateAIRequest(**kwargs: object) -> object:
        created_candidate_ids.extend(cast(list[str], kwargs['candidate_ids']))
        assert kwargs['model'] == expected_audit_model
        assert created_candidate_ids == []
        events.append('audit-created')
        return ai_request

    async def ResolveMetadata(**kwargs: object) -> SimpleNamespace:
        generated_hints.update(cast(dict[str, object], kwargs['hints']))
        assert generated_hints['existing_series'] == []
        assert generated_hints['wikipedia'] == []
        if failure_code is not None:
            raise RecordedSeriesAIError(failure_code)
        return SimpleNamespace(
            decision='NotSeries',
            confidence=0.95,
            model=expected_audit_model,
            prompt_tokens=10,
            completion_tokens=5,
            http_status=200,
            latency_ms=20,
        )

    async def FinishAIRequest(
        _cls: type[RecordedSeriesResolver],
        **kwargs: object,
    ) -> None:
        assert kwargs['request'] is ai_request
        if failure_code is None:
            assert kwargs['status'] == 'Succeeded'
            assert kwargs['selected_choice_id'] == 'not-series'
        else:
            assert kwargs['status'] == 'Failed'
            assert cast(RecordedSeriesAIError, kwargs['error']).code == failure_code
        events.append('audit-finished')

    async def ApplyNotSeries(
        _cls: type[RecordedSeriesResolver],
        **kwargs: object,
    ) -> None:
        assert failure_code is None
        assert kwargs['source'] == 'AI'
        assert kwargs['confidence'] == 0.95
        assert kwargs['candidate_snapshot'] == [generated_hints]
        assert kwargs['ai_model'] == expected_audit_model
        events.append('result-applied')

    async def ApplyNeedsReview(
        _cls: type[RecordedSeriesResolver],
        **kwargs: object,
    ) -> None:
        assert failure_code is not None
        assert kwargs['source'] == 'AI'
        assert kwargs['candidate_snapshot'] == [generated_hints]
        assert kwargs['ai_model'] == expected_audit_model
        assert kwargs['error_code'] == failure_code
        events.append('needs-review')

    async def Scenario() -> None:
        monkeypatch.setattr(
            RecordedSeriesSettingsStore,
            'getSettingsAndAPIKey',
            staticmethod(lambda: (
                SimpleNamespace(
                    enabled=True,
                    ai_enabled=True,
                    ai_backend=ai_backend,
                    acp_model=acp_model,
                    ai_backend_service_id=None,
                ),
                None,
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
        monkeypatch.setattr(RecordedSeriesAIRequest, 'filter', staticmethod(FilterAIRequests))
        monkeypatch.setattr(RecordedSeriesAIRequest, 'create', staticmethod(CreateAIRequest))
        monkeypatch.setattr(
            'app.metadata.RecordedSeriesResolver.SearchWikipediaCandidates',
            SearchWikipedia,
        )
        monkeypatch.setattr(
            'app.metadata.RecordedSeriesResolver.ai_resolve_series_metadata',
            ResolveMetadata,
        )
        monkeypatch.setattr(RecordedSeriesResolver, '_finishAIRequest', classmethod(FinishAIRequest))
        monkeypatch.setattr(RecordedSeriesResolver, '_applyNotSeries', classmethod(ApplyNotSeries))
        monkeypatch.setattr(RecordedSeriesResolver, '_applyNeedsReview', classmethod(ApplyNeedsReview))

        result = await RecordedSeriesResolver.resolveProgram(
            snapshot.id,
            snapshots=programs,
            parses=parses,
            cluster_evidence=evidence,
            snapshot_generation=0,
        )

        assert result.status == ('NotSeries' if failure_code is None else 'NeedsReview')
        assert result.source == 'AI'
        assert result.ai_requested is True
        assert events.index('audit-created') < events.index('audit-finished')
        if failure_code is None:
            assert events.index('result-applied') < events.index('audit-finished')
        else:
            assert events.index('audit-finished') < events.index('needs-review')

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
        def filter(self, *_args: object, **_kwargs: object) -> 'CountQuery':
            return self

        async def count(self) -> int:
            return 0

    def FilterAIRequests(**_kwargs: object) -> CountQuery:
        return CountQuery()

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
    ) -> tuple[list[object], list[object]]:
        return [], []

    async def SearchWikipedia(_title: str, *, limit: int) -> list[dict[str, object]]:
        events.append(f'wikipedia:{limit}')
        RecordedSeriesResolver._snapshot_generation += 1
        return [{'page_id': 456, 'title': '候補世代更新テスト', 'extract': '候補本文'}]

    def BuildAttemptKey(**_kwargs: object) -> str:
        raise AssertionError('世代更新後の候補でAI試行キーを作ってはいけない')

    async def CreateAIRequest(**_kwargs: object) -> None:
        raise AssertionError('世代更新後の候補でAI監査を予約してはいけない')

    async def ResolveMetadata(**_kwargs: object) -> None:
        raise AssertionError('世代更新後の候補をAIへ送ってはいけない')

    async def Scenario() -> None:
        monkeypatch.setattr(
            RecordedSeriesSettingsStore,
            'getSettingsAndAPIKey',
            staticmethod(lambda: (
                SimpleNamespace(
                    enabled=True,
                    ai_enabled=True,
                    ai_backend='AcpCodex',
                    acp_model=None,
                    ai_backend_service_id=None,
                ),
                None,
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
        monkeypatch.setattr(RecordedSeriesAIRequest, 'filter', staticmethod(FilterAIRequests))
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
            'app.metadata.RecordedSeriesResolver.ai_resolve_series_metadata',
            ResolveMetadata,
        )

        with pytest.raises(_RecordedProgramSnapshotChanged):
            await RecordedSeriesResolver.resolveProgram(
                snapshot.id,
                snapshots=programs,
                parses=parses,
                cluster_evidence=evidence,
                snapshot_generation=0,
            )

        assert events == ['snapshot', 'wikipedia:5', 'snapshot']

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
    generation_called = False

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
        def filter(self, *_args: object, **_kwargs: object) -> 'CountQuery':
            return self

        async def count(self) -> int:
            return 0

    def FilterAIRequests(**_kwargs: object) -> CountQuery:
        return CountQuery()

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
    ) -> tuple[list[object], list[object]]:
        return [], []

    async def SearchWikipedia(_title: str, *, limit: int) -> list[dict[str, object]]:
        events.append(f'wikipedia:{limit}')
        return [{'page_id': 789, 'title': '監査予約世代更新テスト', 'extract': '候補本文'}]

    async def CreateAIRequest(**kwargs: object) -> FakeAIRequest:
        assert kwargs['model'] == 'acp:codex:test-acp-model'
        events.append('audit-created')
        RecordedSeriesResolver._snapshot_generation += 1
        return ai_request

    async def ResolveMetadata(**_kwargs: object) -> None:
        nonlocal generation_called
        generation_called = True

    async def Scenario() -> None:
        monkeypatch.setattr(
            RecordedSeriesSettingsStore,
            'getSettingsAndAPIKey',
            staticmethod(lambda: (
                SimpleNamespace(
                    enabled=True,
                    ai_enabled=True,
                    ai_backend='AcpCodex',
                    acp_model='test-acp-model',
                    ai_backend_service_id=None,
                ),
                None,
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
        monkeypatch.setattr(RecordedSeriesAIRequest, 'filter', staticmethod(FilterAIRequests))
        monkeypatch.setattr(RecordedSeriesAIRequest, 'create', staticmethod(CreateAIRequest))
        monkeypatch.setattr(
            'app.metadata.RecordedSeriesResolver.SearchWikipediaCandidates',
            SearchWikipedia,
        )
        monkeypatch.setattr(
            'app.metadata.RecordedSeriesResolver.ai_resolve_series_metadata',
            ResolveMetadata,
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
        assert generation_called is False
        assert RecordedSeriesResolver._ai_attempt_keys == {}
        assert events.count('snapshot') == 3
        assert events.index('audit-created') < events.index('audit-failed')

    asyncio.run(Scenario())


def test_generated_hint_id_requires_moderate_title_consistency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既存IDがhints内でも、中程度に食い違う生成名はDB反映前に拒否する。"""

    existing = SimpleNamespace(
        id=42,
        title='料理の鉄人',
        description='既存 Series の説明。',
        wikipedia_page_id=None,
    )

    async def GetSeries(**_kwargs: object) -> SimpleNamespace:
        return existing

    async def Scenario() -> None:
        monkeypatch.setattr(Series, 'get_or_none', staticmethod(GetSeries))
        hints = cast(SeriesMetadataHints, {
            'local_parse': {
                'series_title': '料理の鉄人',
                'season_number': '1',
                'episode_number': '#3',
                'subtitle': '決戦',
            },
            'cluster': {
                'display_title': '料理の鉄人',
                'normalized_key': '料理の鉄人',
                'member_count': 1,
            },
            'existing_series': [{
                'id': 42,
                'title': '料理の鉄人',
                'description': '既存 Series の説明。',
                'wikipedia_page_id': None,
            }],
            'wikipedia': [],
        })
        generated = GeneratedSeriesResult(
            series_title='料理バトル',
            existing_series_id=42,
        )

        with pytest.raises(RecordedSeriesAIError, match='GeneratedSeriesHintTitleMismatch'):
            await RecordedSeriesResolver._reconcileGeneratedSeries(
                generated=generated,
                hints=hints,
                snapshot=_snapshot(42, '料理の鉄人　第3話「決戦」', '料理対決。'),
                available_series=[],
            )

    asyncio.run(Scenario())


def test_generated_null_episode_uses_local_fallback_without_ai_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI話数がnullなら厳格なローカル話数を採用し、AI由来とは記録しない。"""

    created_resolution: dict[str, object] = {}
    recorded_program = SimpleNamespace(
        id=43,
        series_episode_id=None,
        episode_number=None,
    )
    episode = SimpleNamespace(
        id=430,
        season_number=1,
        episode_number=Decimal('3'),
    )

    class ResolutionQuery:
        def using_db(self, _connection: BaseDBAsyncClient) -> 'ResolutionQuery':
            return self

        async def first(self) -> None:
            return None

    def FilterResolution(**_kwargs: object) -> ResolutionQuery:
        return ResolutionQuery()

    async def GetOrCreateEpisode(
        _cls: type[RecordedEpisodeResolver],
        **kwargs: object,
    ) -> SimpleNamespace:
        assert kwargs['series_id'] == 43
        assert kwargs['season_number'] == 1
        assert kwargs['episode_number'] == Decimal('3')
        return episode

    async def CreateResolution(**kwargs: object) -> None:
        created_resolution.update(kwargs)

    async def Scenario() -> None:
        monkeypatch.setattr(RecordedEpisodeResolution, 'filter', staticmethod(FilterResolution))
        monkeypatch.setattr(
            RecordedEpisodeResolution,
            'create',
            staticmethod(CreateResolution),
        )
        monkeypatch.setattr(
            RecordedEpisodeResolver,
            '_getOrCreateEpisode',
            classmethod(GetOrCreateEpisode),
        )

        generated = GeneratedSeriesResult(
            season_number=None,
            episode_number=None,
            subtitle=None,
        )
        allowed = await RecordedEpisodeResolver.applyGeneratedMetadata(
            cast(RecordedProgram, recorded_program),
            target_series_id=43,
            generated=generated,
            input_fingerprint='4' * 64,
            connection=cast(BaseDBAsyncClient, object()),
            local_episode_fallback=ParsedEpisodeNumber(
                season_number=1,
                episode_number=Decimal('3'),
            ),
        )

        assert allowed is True
        assert recorded_program.series_episode_id == episode.id
        assert recorded_program.episode_number == '#3'
        assert created_resolution['status'] == 'Resolved'
        assert created_resolution['source'] == 'Local'
        assert created_resolution['proposed_episode_number'] is None
        assert created_resolution['confidence'] is None
        assert created_resolution['ai_model'] is None

    asyncio.run(Scenario())


def test_generation_change_while_applying_ai_result_closes_audit_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI応答後の入力更新は成功監査にせず、日次利用回数からも除外する。"""

    async def SearchWikipedia(_title: str, *, limit: int) -> list[dict[str, object]]:
        assert limit == 5
        return []

    async def ResolveMetadata(**_kwargs: object) -> AISeriesMetadataResult:
        return GeneratedSeriesResult()

    async def ApplySeries(
        _cls: type[RecordedSeriesResolver],
        **_kwargs: object,
    ) -> Series:
        raise _RecordedProgramSnapshotChanged

    async def Scenario() -> None:
        await InitializeGenerationDatabase()
        try:
            await CreateGenerationChannel()
            program = await CreateGenerationProgram(13)
            settings = GenerationSettings(ai_enabled=True)
            monkeypatch.setattr(
                RecordedSeriesSettingsStore,
                'getSettingsAndAPIKey',
                staticmethod(lambda: (settings, 'test-key')),
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.SearchWikipediaCandidates',
                SearchWikipedia,
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.ai_resolve_series_metadata',
                ResolveMetadata,
            )
            monkeypatch.setattr(RecordedSeriesResolver, '_applySeries', classmethod(ApplySeries))
            monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
            monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)
            monkeypatch.setattr(RecordedSeriesResolver, '_ai_attempt_keys', {})

            with pytest.raises(_RecordedProgramSnapshotChanged):
                await RecordedSeriesResolver.resolveProgram(program.id)

            resolution = await RecordedSeriesResolution.get(recorded_program_id=program.id)
            request = await RecordedSeriesAIRequest.get(resolution_id=resolution.id)
            status = await RecordedSeriesResolver.getStatus()
            assert request.status == 'Failed'
            assert request.error_code == 'InputChangedBeforeApply'
            assert request.selected_choice_id is None
            assert 'ai_requests_today' not in status
            assert status['total'] >= 1
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_ai_on_creates_generated_series_episode_and_reuses_local_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI ON は dirty title を早期採用せず、生成メタデータとローカル key Rule を同時保存する。"""

    generation_calls = 0
    generated = GeneratedSeriesResult(series_title='宇宙兄弟 アニメ版')

    async def SearchWikipedia(_title: str, *, limit: int) -> list[dict[str, object]]:
        assert limit == 5
        return []

    async def ResolveMetadata(**_kwargs: object) -> AISeriesMetadataResult:
        nonlocal generation_calls
        generation_calls += 1
        return generated

    async def Scenario() -> None:
        await InitializeGenerationDatabase()
        try:
            await CreateGenerationChannel()
            first = await CreateGenerationProgram(20)
            # 旧候補選択スイッチは読取互換専用で、AI ON の生成分岐には影響させない。
            settings = GenerationSettings(
                ai_enabled=True,
                legacy_candidate_selection_enabled=False,
            )
            monkeypatch.setattr(
                RecordedSeriesSettingsStore,
                'getSettingsAndAPIKey',
                staticmethod(lambda: (settings, 'test-key')),
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.SearchWikipediaCandidates',
                SearchWikipedia,
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.ai_resolve_series_metadata',
                ResolveMetadata,
            )
            monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
            monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)
            monkeypatch.setattr(RecordedSeriesResolver, '_ai_attempt_keys', {})

            first_result = await RecordedSeriesResolver.resolveProgram(first.id)

            assert first_result.status == 'Resolved'
            assert first_result.source == 'AI'
            assert first_result.ai_requested is True
            await first.refresh_from_db()
            series = await Series.get(id=first.series_id)
            episode = await SeriesEpisode.get(id=first.series_episode_id)
            episode_resolution = await RecordedEpisodeResolution.get(recorded_program_id=first.id)
            rule = await RecordedSeriesRule.get(series_id=series.id)
            local_parse = ParseSeriesTitle(
                first.title,
                first.description,
                first.detail,
                first.genres,
            )
            assert series.title == '宇宙兄弟 アニメ版'
            assert series.title != first.title
            assert first.series_title == series.title
            assert first.episode_number == '#3'
            assert first.subtitle == '弟の覚悟'
            assert episode.season_number == 1
            assert episode.episode_number == Decimal('3')
            assert episode_resolution.status == 'Resolved'
            assert episode_resolution.source == 'AI'
            assert episode_resolution.episode_id == episode.id
            assert episode_resolution.confidence == 0.42
            assert rule.normalized_key == local_parse.normalized_key
            assert rule.normalized_key != BuildSeriesGroupingKey(generated.series_title or '')

            # 同じ dirty EPG 表記の次回録画は、生成 title ではなくローカル key の Rule で合流する。
            second = await CreateGenerationProgram(21)
            second_result = await RecordedSeriesResolver.resolveProgram(second.id)
            await second.refresh_from_db()
            assert second_result.status == 'Resolved'
            assert second_result.source == 'Rule'
            assert second_result.ai_requested is False
            assert second.series_id == series.id
            assert generation_calls == 1
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_ai_on_reuses_hint_id_and_exact_title_without_renaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hints 内 ID と生成 title 完全一致は既存へ合流し、DB 表示名を変更しない。"""

    generated_results: list[AISeriesMetadataResult] = []
    existing_id: int | None = None

    async def SearchWikipedia(_title: str, *, limit: int) -> list[dict[str, object]]:
        assert limit == 5
        return []

    async def ResolveMetadata(**kwargs: object) -> AISeriesMetadataResult:
        hints = cast(dict[str, object], kwargs['hints'])
        existing_hints = cast(list[dict[str, object]], hints['existing_series'])
        assert existing_id is not None
        assert any(hint['id'] == existing_id for hint in existing_hints)
        return generated_results.pop(0)

    async def Scenario() -> None:
        nonlocal existing_id
        await InitializeGenerationDatabase()
        try:
            await CreateGenerationChannel()
            existing = await Series.create(
                title='宇宙兄弟（DB 正本）',
                description='管理画面で確定済みの説明。',
                genres=ANIME_GENRES,
            )
            existing_id = existing.id
            first = await CreateGenerationProgram(22)
            second = await CreateGenerationProgram(23)
            manual_episode_program = await CreateGenerationProgram(26)
            manual_episode = await SeriesEpisode.create(
                series=existing,
                season_number=2,
                episode_number=Decimal('8'),
            )
            manual_episode_program.series_episode_id = manual_episode.id
            manual_episode_program.episode_number = 'S2 #8'
            manual_episode_program.subtitle = '管理者が確定した話名'
            await manual_episode_program.save(update_fields=[
                'series_episode_id',
                'episode_number',
                'subtitle',
                'updated_at',
            ])
            await RecordedEpisodeResolution.create(
                recorded_program=manual_episode_program,
                episode=manual_episode,
                status='Resolved',
                source='Manual',
                is_legacy_recording=False,
                input_fingerprint='2' * 64,
                manual_season_number=2,
                manual_episode_number=Decimal('8'),
                manual_rationale='管理者確定',
                resolved_at=datetime.now(tz=JST),
            )
            generated_results.extend([
                GeneratedSeriesResult(
                    series_title='宇宙兄弟',
                    confidence=0.01,
                    existing_series_id=existing.id,
                ),
                GeneratedSeriesResult(series_title=existing.title),
                GeneratedSeriesResult(series_title=existing.title),
            ])
            settings = GenerationSettings(ai_enabled=True)
            monkeypatch.setattr(
                RecordedSeriesSettingsStore,
                'getSettingsAndAPIKey',
                staticmethod(lambda: (settings, 'test-key')),
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.SearchWikipediaCandidates',
                SearchWikipedia,
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.ai_resolve_series_metadata',
                ResolveMetadata,
            )
            monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
            monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)
            monkeypatch.setattr(RecordedSeriesResolver, '_ai_attempt_keys', {})

            by_id_result = await RecordedSeriesResolver.resolveProgram(first.id)
            by_title_result = await RecordedSeriesResolver.resolveProgram(second.id, force=True)
            manual_episode_result = await RecordedSeriesResolver.resolveProgram(
                manual_episode_program.id,
                force=True,
            )

            await first.refresh_from_db()
            await second.refresh_from_db()
            await manual_episode_program.refresh_from_db()
            await existing.refresh_from_db()
            assert by_id_result.status == 'Resolved'
            assert by_title_result.status == 'Resolved'
            assert manual_episode_result.status == 'Resolved'
            assert first.series_id == existing.id
            assert second.series_id == existing.id
            assert manual_episode_program.series_id == existing.id
            assert first.series_title == existing.title
            assert second.series_title == existing.title
            assert existing.title == '宇宙兄弟（DB 正本）'
            assert existing.description == '管理画面で確定済みの説明。'
            assert manual_episode_program.series_episode_id == manual_episode.id
            assert manual_episode_program.episode_number == 'S2 #8'
            assert manual_episode_program.subtitle == '管理者が確定した話名'
            manual_resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=manual_episode_program.id,
            )
            assert manual_resolution.source == 'Manual'
            assert manual_resolution.episode_id == manual_episode.id
            assert await Series.all().count() == 1
            assert generated_results == []
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


@pytest.mark.parametrize('existing_source', ['Local', 'WebSearch'])
def test_ai_null_metadata_preserves_deterministic_episode_and_subtitle(
    monkeypatch: pytest.MonkeyPatch,
    existing_source: Literal['Local', 'WebSearch'],
) -> None:
    """AI の null は同じSeriesで確定済みの話数・話名を出典にかかわらず上書きしない。"""

    existing_id: int | None = None

    async def SearchWikipedia(_title: str, *, limit: int) -> list[dict[str, object]]:
        assert limit == 5
        return []

    async def ResolveMetadata(**kwargs: object) -> AISeriesMetadataResult:
        hints = cast(dict[str, object], kwargs['hints'])
        existing_hints = cast(list[dict[str, object]], hints['existing_series'])
        assert existing_id is not None
        assert any(hint['id'] == existing_id for hint in existing_hints)
        return GeneratedSeriesResult(
            series_title='宇宙兄弟',
            season_number=None,
            episode_number=None,
            subtitle=None,
            existing_series_id=existing_id,
        )

    async def Scenario() -> None:
        nonlocal existing_id
        await InitializeGenerationDatabase()
        try:
            await CreateGenerationChannel()
            existing = await Series.create(
                title='宇宙兄弟',
                description='既存 Series の説明。',
                genres=ANIME_GENRES,
            )
            existing_id = existing.id
            program = await CreateGenerationProgram(29)
            local_episode = await SeriesEpisode.create(
                series=existing,
                season_number=1,
                episode_number=Decimal('3'),
            )
            program.series_id = existing.id
            program.series_episode_id = local_episode.id
            program.series_title = existing.title
            program.episode_number = '#3'
            program.subtitle = 'ローカル解析済みの話名'
            await program.save(update_fields=[
                'series_id',
                'series_episode_id',
                'series_title',
                'episode_number',
                'subtitle',
                'updated_at',
            ])
            local_resolution = await RecordedEpisodeResolution.create(
                recorded_program=program,
                episode=local_episode,
                status='Resolved',
                source=existing_source,
                input_fingerprint='3' * 64,
                resolved_at=datetime.now(tz=JST),
            )
            settings = GenerationSettings(ai_enabled=True)
            monkeypatch.setattr(
                RecordedSeriesSettingsStore,
                'getSettingsAndAPIKey',
                staticmethod(lambda: (settings, 'test-key')),
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.SearchWikipediaCandidates',
                SearchWikipedia,
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.ai_resolve_series_metadata',
                ResolveMetadata,
            )
            monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
            monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)
            monkeypatch.setattr(RecordedSeriesResolver, '_ai_attempt_keys', {})

            result = await RecordedSeriesResolver.resolveProgram(program.id, force=True)

            await program.refresh_from_db()
            await local_resolution.refresh_from_db()
            assert result.status == 'Resolved'
            assert result.source == 'AI'
            assert program.series_id == existing.id
            assert program.series_episode_id == local_episode.id
            assert program.episode_number == '#3'
            assert program.subtitle == 'ローカル解析済みの話名'
            assert local_resolution.status == 'Resolved'
            assert local_resolution.source == existing_source
            assert local_resolution.episode_id == local_episode.id
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_ai_null_episode_uses_first_local_parse_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """初回AI生成が話数を返さなくても、厳格なローカル解析結果を正本へ保存する。"""

    async def SearchWikipedia(_title: str, *, limit: int) -> list[dict[str, object]]:
        assert limit == 5
        return []

    async def ResolveMetadata(**_kwargs: object) -> AISeriesMetadataResult:
        return GeneratedSeriesResult(
            season_number=None,
            episode_number=None,
            subtitle=None,
        )

    async def Scenario() -> None:
        await InitializeGenerationDatabase()
        try:
            await CreateGenerationChannel()
            program = await CreateGenerationProgram(30)
            settings = GenerationSettings(ai_enabled=True)
            monkeypatch.setattr(
                RecordedSeriesSettingsStore,
                'getSettingsAndAPIKey',
                staticmethod(lambda: (settings, 'test-key')),
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.SearchWikipediaCandidates',
                SearchWikipedia,
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.ai_resolve_series_metadata',
                ResolveMetadata,
            )
            monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
            monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)
            monkeypatch.setattr(RecordedSeriesResolver, '_ai_attempt_keys', {})

            result = await RecordedSeriesResolver.resolveProgram(program.id)

            await program.refresh_from_db()
            resolution = await RecordedEpisodeResolution.get(recorded_program_id=program.id)
            assert result.status == 'Resolved'
            assert program.series_episode_id is not None
            assert program.episode_number == '#3'
            assert program.subtitle == '弟の覚悟'
            assert resolution.status == 'Resolved'
            assert resolution.source == 'Local'
            assert resolution.episode_id == program.series_episode_id
            assert resolution.ai_model is None
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_ai_null_episode_without_local_evidence_is_not_numbered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI の null 話数は Local 証拠がない場合に NotNumbered として保存する。"""

    async def SearchWikipedia(_title: str, *, limit: int) -> list[dict[str, object]]:
        assert limit == 5
        return []

    async def ResolveMetadata(**_kwargs: object) -> AISeriesMetadataResult:
        return GeneratedSeriesResult(
            season_number=None,
            episode_number=None,
            subtitle=None,
        )

    async def Scenario() -> None:
        await InitializeGenerationDatabase()
        try:
            await CreateGenerationChannel()
            program = await CreateGenerationProgram(
                30,
                title='宇宙兄弟「特別編」',
            )
            settings = GenerationSettings(ai_enabled=True)
            monkeypatch.setattr(
                RecordedSeriesSettingsStore,
                'getSettingsAndAPIKey',
                staticmethod(lambda: (settings, 'test-key')),
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.SearchWikipediaCandidates',
                SearchWikipedia,
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.ai_resolve_series_metadata',
                ResolveMetadata,
            )
            monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
            monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)
            monkeypatch.setattr(RecordedSeriesResolver, '_ai_attempt_keys', {})

            result = await RecordedSeriesResolver.resolveProgram(program.id)

            await program.refresh_from_db()
            resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=program.id,
            )
            assert result.status == 'Resolved'
            assert program.series_episode_id is None
            assert program.episode_number is None
            assert resolution.status == 'NotNumbered'
            assert resolution.source == 'AI'
            assert resolution.episode_id is None
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_ai_hint_id_with_moderate_generated_title_mismatch_needs_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hints 内 ID でも生成名が中程度に不一致なら静かな誤合流を防ぐ。"""

    existing_id: int | None = None

    async def SearchWikipedia(_title: str, *, limit: int) -> list[dict[str, object]]:
        assert limit == 5
        return []

    async def ResolveMetadata(**kwargs: object) -> AISeriesMetadataResult:
        hints = cast(dict[str, object], kwargs['hints'])
        existing_hints = cast(list[dict[str, object]], hints['existing_series'])
        assert existing_id is not None
        assert any(hint['id'] == existing_id for hint in existing_hints)
        return GeneratedSeriesResult(
            series_title='料理バトル',
            confidence=0.01,
            existing_series_id=existing_id,
        )

    async def Scenario() -> None:
        nonlocal existing_id
        await InitializeGenerationDatabase()
        try:
            await CreateGenerationChannel()
            existing = await Series.create(
                title='料理の鉄人',
                description='既存 Series の説明。',
                genres=ANIME_GENRES,
            )
            existing_id = existing.id
            program = await CreateGenerationProgram(
                31,
                title='料理の鉄人　第3話「決戦」',
            )
            settings = GenerationSettings(ai_enabled=True)
            monkeypatch.setattr(
                RecordedSeriesSettingsStore,
                'getSettingsAndAPIKey',
                staticmethod(lambda: (settings, 'test-key')),
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.SearchWikipediaCandidates',
                SearchWikipedia,
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.ai_resolve_series_metadata',
                ResolveMetadata,
            )
            monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
            monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)
            monkeypatch.setattr(RecordedSeriesResolver, '_ai_attempt_keys', {})

            result = await RecordedSeriesResolver.resolveProgram(program.id)

            await program.refresh_from_db()
            resolution = await RecordedSeriesResolution.get(recorded_program_id=program.id)
            request = await RecordedSeriesAIRequest.get(resolution_id=resolution.id)
            assert result.status == 'NeedsReview'
            assert result.source == 'AI'
            assert program.series_id is None
            assert resolution.error_code == 'GeneratedSeriesHintTitleMismatch'
            assert request.status == 'Rejected'
            assert request.error_code == 'GeneratedSeriesHintTitleMismatch'
            assert await Series.all().count() == 1
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_ai_on_wikipedia_hint_creates_with_article_title_and_reuses_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wikipedia hint は記事名で作成し、次回は page_id で同じ Series へ合流する。"""

    generation_calls = 0

    async def SearchWikipedia(_title: str, *, limit: int) -> list[dict[str, object]]:
        assert limit == 5
        return [{
            'page_id': 4242,
            'title': '宇宙兄弟',
            'extract': '宇宙飛行士を目指す兄弟を描いた作品。',
        }]

    async def ResolveMetadata(**kwargs: object) -> AISeriesMetadataResult:
        nonlocal generation_calls
        hints = cast(dict[str, object], kwargs['hints'])
        wikipedia_hints = cast(list[dict[str, object]], hints['wikipedia'])
        assert wikipedia_hints[0]['page_id'] == 4242
        generation_calls += 1
        return GeneratedSeriesResult(
            series_title='AI が生成した別表記',
            wikipedia_page_id=4242,
        )

    async def Scenario() -> None:
        await InitializeGenerationDatabase()
        try:
            await CreateGenerationChannel()
            first = await CreateGenerationProgram(27)
            second = await CreateGenerationProgram(
                28,
                title='宇宙兄弟 特別編　第4話「帰還」',
            )
            settings = GenerationSettings(ai_enabled=True)
            monkeypatch.setattr(
                RecordedSeriesSettingsStore,
                'getSettingsAndAPIKey',
                staticmethod(lambda: (settings, 'test-key')),
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.SearchWikipediaCandidates',
                SearchWikipedia,
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.ai_resolve_series_metadata',
                ResolveMetadata,
            )
            monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
            monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)
            monkeypatch.setattr(RecordedSeriesResolver, '_ai_attempt_keys', {})

            first_result = await RecordedSeriesResolver.resolveProgram(first.id)
            # 通常実行では同じ cluster の positive Rule が先に効くため、
            # page_id 再利用分岐そのものは force 再判定で確認する。
            second_result = await RecordedSeriesResolver.resolveProgram(second.id, force=True)

            await first.refresh_from_db()
            await second.refresh_from_db()
            series = await Series.get(id=first.series_id)
            assert first_result.status == 'Resolved'
            assert second_result.status == 'Resolved'
            assert first_result.ai_requested is True
            assert second_result.ai_requested is True
            assert first.series_id == second.series_id
            assert series.title == '宇宙兄弟'
            assert series.wikipedia_page_id == 4242
            assert await Series.all().count() == 1
            assert generation_calls == 2
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_ai_on_unique_high_similarity_title_reuses_existing_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ID・完全一致がなくても、一意な高類似 title は既存 Series へ合流する。"""

    async def SearchWikipedia(_title: str, *, limit: int) -> list[dict[str, object]]:
        assert limit == 5
        return []

    async def ResolveMetadata(**_kwargs: object) -> AISeriesMetadataResult:
        return GeneratedSeriesResult(series_title='宇宙兄弟テレビアニメシリーズ')

    async def Scenario() -> None:
        await InitializeGenerationDatabase()
        try:
            await CreateGenerationChannel()
            existing = await Series.create(
                title='宇宙兄弟テレビアニメシリーズ版',
                description='既存 Series の説明。',
                genres=ANIME_GENRES,
            )
            program = await CreateGenerationProgram(
                27,
                title='宇宙兄弟テレビアニメシリーズ　第3話「弟の覚悟」',
            )
            settings = GenerationSettings(ai_enabled=True)
            monkeypatch.setattr(
                RecordedSeriesSettingsStore,
                'getSettingsAndAPIKey',
                staticmethod(lambda: (settings, 'test-key')),
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.SearchWikipediaCandidates',
                SearchWikipedia,
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.ai_resolve_series_metadata',
                ResolveMetadata,
            )
            monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
            monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)
            monkeypatch.setattr(RecordedSeriesResolver, '_ai_attempt_keys', {})

            result = await RecordedSeriesResolver.resolveProgram(program.id)

            await program.refresh_from_db()
            await existing.refresh_from_db()
            assert result.status == 'Resolved'
            assert result.source == 'AI'
            assert program.series_id == existing.id
            assert program.series_title == existing.title
            assert existing.title == '宇宙兄弟テレビアニメシリーズ版'
            assert await Series.all().count() == 1
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_ai_on_ambiguous_high_similarity_titles_need_review_without_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同率の高類似候補が複数なら誤結合も新規作成もせず NeedsReview にする。"""

    async def SearchWikipedia(_title: str, *, limit: int) -> list[dict[str, object]]:
        assert limit == 5
        return []

    async def ResolveMetadata(**_kwargs: object) -> AISeriesMetadataResult:
        return GeneratedSeriesResult(series_title='宇宙兄弟テレビアニメシリーズ')

    async def Scenario() -> None:
        await InitializeGenerationDatabase()
        try:
            await CreateGenerationChannel()
            await Series.create(
                title='宇宙兄弟テレビアニメシリーズ版',
                description='候補 A。',
                genres=ANIME_GENRES,
            )
            await Series.create(
                title='宇宙兄弟テレビアニメシリーズ編',
                description='候補 B。',
                genres=ANIME_GENRES,
            )
            program = await CreateGenerationProgram(
                28,
                title='宇宙兄弟テレビアニメシリーズ　第3話「弟の覚悟」',
            )
            settings = GenerationSettings(ai_enabled=True)
            monkeypatch.setattr(
                RecordedSeriesSettingsStore,
                'getSettingsAndAPIKey',
                staticmethod(lambda: (settings, 'test-key')),
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.SearchWikipediaCandidates',
                SearchWikipedia,
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.ai_resolve_series_metadata',
                ResolveMetadata,
            )
            monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
            monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)
            monkeypatch.setattr(RecordedSeriesResolver, '_ai_attempt_keys', {})

            result = await RecordedSeriesResolver.resolveProgram(program.id)

            await program.refresh_from_db()
            resolution = await RecordedSeriesResolution.get(recorded_program_id=program.id)
            request = await RecordedSeriesAIRequest.get(resolution_id=resolution.id)
            assert result.status == 'NeedsReview'
            assert result.source == 'AI'
            assert program.series_id is None
            assert resolution.error_code == 'AmbiguousFuzzySeriesMatch'
            assert request.status == 'Rejected'
            assert request.error_code == 'AmbiguousFuzzySeriesMatch'
            assert await Series.all().count() == 2
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_manual_resolution_has_priority_over_ai_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual Resolution は AI ON・入力変更時も再適用し、生成 backend を呼ばない。"""

    async def ResolveMetadata(**_kwargs: object) -> None:
        raise AssertionError('Manual Resolution 適用時は AI を呼んではいけない')

    async def Scenario() -> None:
        await InitializeGenerationDatabase()
        try:
            await CreateGenerationChannel()
            series = await Series.create(
                title='管理者が選んだシリーズ',
                description='Manual decision.',
                genres=ANIME_GENRES,
            )
            program = await CreateGenerationProgram(24)
            await RecordedSeriesResolution.create(
                recorded_program=program,
                input_fingerprint='0' * 64,
                evidence_hash='1' * 64,
                resolver_version=RECORDED_SERIES_RESOLVER_VERSION,
                normalized_title='manual',
                status='Resolved',
                source='Manual',
                series=series,
            )
            settings = GenerationSettings(ai_enabled=True)
            monkeypatch.setattr(
                RecordedSeriesSettingsStore,
                'getSettingsAndAPIKey',
                staticmethod(lambda: (settings, 'test-key')),
            )
            monkeypatch.setattr(
                'app.metadata.RecordedSeriesResolver.ai_resolve_series_metadata',
                ResolveMetadata,
            )
            monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', asyncio.Lock())
            monkeypatch.setattr(RecordedSeriesResolver, '_snapshot_generation', 0)

            result = await RecordedSeriesResolver.resolveProgram(program.id)

            await program.refresh_from_db()
            assert result.status == 'Resolved'
            assert result.source == 'Manual'
            assert result.ai_requested is False
            assert program.series_id == series.id
            assert program.series_title == series.title
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


