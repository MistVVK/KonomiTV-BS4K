import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from tortoise import Tortoise, transactions

from app.constants import JST
from app.metadata.RecordedEpisodeResolver import (
    FormatEpisodeNumber,
    ParseLegacyEpisodeNumber,
    RecordedEpisodeAssignmentStaleError,
    RecordedEpisodeCrossSeriesError,
    RecordedEpisodeResolver,
)
from app.models.Channel import Channel
from app.models.RecordedEpisode import RecordedEpisodeResolution, SeriesEpisode
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedVideo import RecordedVideo
from app.models.Series import Series
from app.models.SeriesBroadcastPeriod import SeriesBroadcastPeriod
from app.schemas import Genre


ANIME_GENRES: list[Genre] = [
    {'major': 'アニメ・特撮', 'middle': '国内アニメ'},
]


async def InitializeDatabase() -> None:
    """構造化Episodeと録画Series一式をインメモリDBへ初期化する。"""

    await Tortoise.init(
        db_url='sqlite://:memory:',
        modules={'models': [
            'app.models.Channel',
            'app.models.RecordedEpisode',
            'app.models.RecordedProgram',
            'app.models.RecordedVideo',
            'app.models.Series',
            'app.models.SeriesBroadcastPeriod',
        ]},
        timezone='Asia/Tokyo',
    )
    await Tortoise.generate_schemas()


async def CreateChannel(channel_id: str, service_id: int) -> Channel:
    """異なる放送局の重複録画を作るためのチャンネルを登録する。"""

    return await Channel.create(
        id=channel_id,
        display_channel_id=f'bs{service_id}',
        network_id=4,
        service_id=service_id,
        transport_stream_id=1,
        remocon_id=1,
        channel_number=str(service_id),
        type='BS',
        name=f'テスト局 {service_id}',
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
    day: int,
) -> RecordedProgram:
    """Episode移行・手動訂正テスト用の再生可能録画を作成する。"""

    period, _created = await SeriesBroadcastPeriod.get_or_create(
        series=series,
        channel=channel,
        defaults={
            'start_date': datetime(2026, 7, day, tzinfo=JST).date(),
            'end_date': datetime(2026, 7, day, tzinfo=JST).date(),
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
        description='構造化Episodeのテスト番組。',
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
        file_path=f'/tmp/recorded-episode-{program_id}.ts',
        file_hash=f'episode-hash-{program_id}',
        file_size=1,
        file_created_at=start_time,
        file_modified_at=start_time,
        duration=3600.0,
        container_format='MPEG-TS',
    )
    return program


@pytest.mark.parametrize(
    ('legacy', 'season_number', 'episode_number'),
    [
        ('#07', 1, Decimal('7')),
        ('＃12.5', 1, Decimal('12.5')),
        ('Season 2 #01', 2, Decimal('1')),
        ('Season 四 第八十七話', 4, Decimal('87')),
        ('第十話', 1, Decimal('10')),
        ('第百二十三回', 1, Decimal('123')),
        ('100', 1, Decimal('100')),
    ],
)
def test_parse_legacy_episode_number_accepts_only_strict_single_numbers(
    legacy: str,
    season_number: int,
    episode_number: Decimal,
) -> None:
    """既知の単一話表記をシーズンとDecimal話数へ正規化できる。"""

    assert ParseLegacyEpisodeNumber(legacy) is not None
    parsed = ParseLegacyEpisodeNumber(legacy)
    assert parsed is not None
    assert parsed.season_number == season_number
    assert parsed.episode_number == episode_number


@pytest.mark.parametrize(
    'legacy',
    [
        None,
        '',
        '#1-2',
        '#1/#2',
        '総集編',
        '第十百話',
        '二三',
        '#1.2345',
        'Season 2',
        'Season 2147483648 #1',
    ],
)
def test_parse_legacy_episode_number_rejects_ambiguous_or_invalid_values(legacy: str | None) -> None:
    """範囲・複数話・不正な漢数字・固定精度外を推測で採用しない。"""

    assert ParseLegacyEpisodeNumber(legacy) is None


@pytest.mark.parametrize(
    ('season_number', 'episode_number', 'expected'),
    [
        (1, Decimal('10'), '#10'),
        (1, Decimal('100'), '#100'),
        (1, Decimal('12.500'), '#12.5'),
        (2, Decimal('10'), 'Season 2 #10'),
    ],
)
def test_format_episode_number_preserves_integer_trailing_zeroes(
    season_number: int,
    episode_number: Decimal,
    expected: str,
) -> None:
    """小数の余分な0だけを除き、10話・100話の桁は失わない。"""

    assert FormatEpisodeNumber(season_number, episode_number) == expected


def test_legacy_backfill_shares_duplicates_and_is_idempotent() -> None:
    """MX/BS11の同一話を同じEpisodeへ結び、旧表記を保ったまま一度だけ移行する。"""

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(title='重複シリーズ', description='', genres=ANIME_GENRES)
            mx = await CreateChannel('NID4-SID101', 101)
            bs11 = await CreateChannel('NID4-SID211', 211)
            first = await CreateRecordedProgram(1, series=series, channel=mx, episode_number='#10', day=1)
            second = await CreateRecordedProgram(2, series=series, channel=bs11, episode_number='第十話', day=2)
            unknown = await CreateRecordedProgram(3, series=series, channel=mx, episode_number=None, day=3)
            for program in (first, second, unknown):
                await RecordedEpisodeResolution.create(
                    recorded_program=program,
                    status='Pending',
                    source='Migration',
                    is_legacy_recording=True,
                )
            assert await RecordedEpisodeResolver.backfillLegacyEpisodes() == (2, 1)
            assert await RecordedEpisodeResolver.backfillLegacyEpisodes() == (0, 0)

            first = await RecordedProgram.get(id=first.id)
            second = await RecordedProgram.get(id=second.id)
            unknown_resolution = await RecordedEpisodeResolution.get(recorded_program_id=unknown.id)
            assert first.series_episode_id == second.series_episode_id
            assert first.episode_number == '#10'
            assert second.episode_number == '第十話'
            assert await SeriesEpisode.filter(series_id=series.id).count() == 1
            assert unknown_resolution.status == 'NeedsReview'
            assert unknown_resolution.error_code == 'LegacyEpisodeMissing'
            assert unknown_resolution.is_legacy_recording is True
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_legacy_backfill_isolates_one_broken_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """1件の予期しない移行失敗を要確認へ閉じ、後続の既存録画を処理し続ける。"""

    original_backfill = RecordedEpisodeResolver._backfillLegacyEpisode

    async def BackfillWithOneFailure(
        cls: type[RecordedEpisodeResolver],
        resolution_id: int,
    ) -> tuple[int, int]:
        del cls
        if resolution_id == 1:
            raise RuntimeError('simulated per-row failure')
        return await original_backfill(resolution_id)

    monkeypatch.setattr(
        RecordedEpisodeResolver,
        '_backfillLegacyEpisode',
        classmethod(BackfillWithOneFailure),
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(title='行分離シリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel('NID4-SID101', 101)
            first = await CreateRecordedProgram(1, series=series, channel=channel, episode_number='#1', day=1)
            second = await CreateRecordedProgram(2, series=series, channel=channel, episode_number='#2', day=2)
            for program in (first, second):
                await RecordedEpisodeResolution.create(
                    id=program.id,
                    recorded_program=program,
                    status='Pending',
                    source='Migration',
                    is_legacy_recording=True,
                )

            assert await RecordedEpisodeResolver.backfillLegacyEpisodes() == (1, 1)
            first_resolution = await RecordedEpisodeResolution.get(id=1)
            second_resolution = await RecordedEpisodeResolution.get(id=2)
            assert first_resolution.status == 'NeedsReview'
            assert first_resolution.error_code == 'LegacyBackfillFailed'
            assert second_resolution.status == 'Resolved'
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_manual_assignment_validates_stale_and_cross_series_updates() -> None:
    """手動割当はdual-writeし、古い画面と別SeriesのEpisodeを拒否する。"""

    async def Run() -> None:
        await InitializeDatabase()
        try:
            first_series = await Series.create(title='第1シリーズ', description='', genres=ANIME_GENRES)
            second_series = await Series.create(title='第2シリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel('NID4-SID101', 101)
            program = await CreateRecordedProgram(
                1,
                series=first_series,
                channel=channel,
                episode_number=None,
                day=1,
            )
            cross_series_episode = await SeriesEpisode.create(
                series=second_series,
                season_number=1,
                episode_number=Decimal('1'),
            )

            await RecordedEpisodeResolver.assignProgramEpisode(
                program.id,
                expected_series_id=first_series.id,
                expected_series_episode_id=None,
                decision='StructuredEpisode',
                season_number=2,
                episode_number=Decimal('10'),
            )
            program = await RecordedProgram.get(id=program.id)
            assert program.episode_number == 'Season 2 #10'
            assert program.series_episode_id is not None
            resolution = await RecordedEpisodeResolution.get(recorded_program_id=program.id)
            assert resolution.status == 'Resolved'
            assert resolution.source == 'Manual'
            assert resolution.manual_status == 'Resolved'
            assert resolution.manual_season_number == 2
            assert resolution.manual_episode_number == Decimal('10')
            assert resolution.manual_episode_id == program.series_episode_id

            with pytest.raises(RecordedEpisodeAssignmentStaleError):
                await RecordedEpisodeResolver.assignProgramEpisode(
                    program.id,
                    expected_series_id=first_series.id,
                    expected_series_episode_id=None,
                    decision='Unknown',
                )
            with pytest.raises(RecordedEpisodeCrossSeriesError):
                await RecordedEpisodeResolver.assignProgramEpisode(
                    program.id,
                    expected_series_id=first_series.id,
                    expected_series_episode_id=program.series_episode_id,
                    decision='ExistingEpisode',
                    episode_id=cross_series_episode.id,
                )

            await RecordedEpisodeResolver.assignProgramEpisode(
                program.id,
                expected_series_id=first_series.id,
                expected_series_episode_id=program.series_episode_id,
                decision='Unknown',
            )
            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(recorded_program_id=program.id)
            assert program.series_episode_id is None
            assert program.episode_number is None
            assert resolution.status == 'Unknown'
            assert resolution.source == 'Manual'
            assert resolution.manual_status == 'Unknown'
            assert resolution.manual_episode_id is None
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_manual_no_published_number_keeps_optional_season_without_creating_episode() -> None:
    """公開話数なしはシーズンだけを正本化し、架空の SeriesEpisode を作らない。"""

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title='特別編シリーズ',
                description='',
                genres=ANIME_GENRES,
            )
            channel = await CreateChannel('NID4-SID301', 301)
            program = await CreateRecordedProgram(
                1,
                series=series,
                channel=channel,
                episode_number=None,
                day=1,
            )

            await RecordedEpisodeResolver.assignProgramEpisode(
                program.id,
                expected_series_id=series.id,
                expected_series_episode_id=None,
                decision='NoPublishedNumber',
                season_number=3,
            )

            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(recorded_program_id=program.id)
            assert program.series_episode_id is None
            assert program.episode_number is None
            assert resolution.status == 'NoPublishedNumber'
            assert resolution.source == 'Manual'
            assert resolution.season_number == 3
            assert resolution.manual_status == 'NoPublishedNumber'
            assert resolution.manual_season_number == 3
            assert resolution.manual_episode_number is None
            assert await SeriesEpisode.filter(series_id=series.id).count() == 0
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_manual_assignment_preserves_ai_lane_and_adopt_ai_keeps_manual_lane() -> None:
    """手動保存は AI レーンを消しず、AdoptAI は手動レーンを残して正本だけ切り替える。"""

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title='デュアルレーンシリーズ', description='', genres=ANIME_GENRES
            )
            channel = await CreateChannel('NID4-SID201', 201)
            program = await CreateRecordedProgram(
                1,
                series=series,
                channel=channel,
                episode_number='#3',
                day=1,
            )
            ai_episode = await SeriesEpisode.create(
                series=series,
                season_number=1,
                episode_number=Decimal('3'),
            )
            program.series_episode_id = ai_episode.id
            await program.save(update_fields=['series_episode_id', 'updated_at'])
            await RecordedEpisodeResolution.create(
                recorded_program=program,
                episode=ai_episode,
                status='Resolved',
                source='WebSearch',
                lookup_outcome='Resolved',
                proposed_outcome='Resolved',
                proposed_season_number=1,
                proposed_episode_number=Decimal('3'),
                confidence=0.91,
                web_search_performed=True,
                rationale_short='AI 提案の根拠',
                citations=[{'url': 'https://example.com/ai', 'title': 'AI 出典'}],
                ai_model='test-model',
            )

            await RecordedEpisodeResolver.assignProgramEpisode(
                program.id,
                expected_series_id=series.id,
                expected_series_episode_id=ai_episode.id,
                decision='StructuredEpisode',
                season_number=2,
                episode_number=Decimal('10'),
            )
            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=program.id
            )
            assert resolution.source == 'Manual'
            assert resolution.status == 'Resolved'
            assert resolution.manual_status == 'Resolved'
            assert resolution.manual_season_number == 2
            assert resolution.manual_episode_number == Decimal('10')
            # AI レーンは手動保存後も残る。
            assert resolution.proposed_season_number == 1
            assert resolution.proposed_episode_number == Decimal('3')
            assert resolution.web_search_performed is True
            assert resolution.citations == [
                {'url': 'https://example.com/ai', 'title': 'AI 出典'}
            ]
            assert resolution.rationale_short == 'AI 提案の根拠'
            assert resolution.lookup_outcome == 'Resolved'
            assert program.episode_number == 'Season 2 #10'

            await RecordedEpisodeResolver.assignProgramEpisode(
                program.id,
                expected_series_id=series.id,
                expected_series_episode_id=program.series_episode_id,
                decision='AdoptAI',
            )
            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=program.id
            )
            assert resolution.source == 'WebSearch'
            assert resolution.status == 'Resolved'
            assert resolution.proposed_season_number == 1
            assert resolution.proposed_episode_number == Decimal('3')
            # 手動レーンは AI 採用後も残る。
            assert resolution.manual_status == 'Resolved'
            assert resolution.manual_season_number == 2
            assert resolution.manual_episode_number == Decimal('10')
            episode = await SeriesEpisode.get(id=program.series_episode_id)
            assert episode.season_number == 1
            assert episode.episode_number == Decimal('3')
            # Season 1 は #N 形式へ省略される。
            assert program.episode_number == '#3'
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_series_move_upserts_the_same_number_in_the_target_series() -> None:
    """Series移動では元Episodeを参照せず、移動先Seriesの同一番号へ付け替える。"""

    async def Run() -> None:
        await InitializeDatabase()
        try:
            first_series = await Series.create(title='移動元', description='', genres=ANIME_GENRES)
            second_series = await Series.create(title='移動先', description='', genres=ANIME_GENRES)
            channel = await CreateChannel('NID4-SID101', 101)
            program = await CreateRecordedProgram(
                1,
                series=first_series,
                channel=channel,
                episode_number='#12.5',
                day=1,
            )
            await RecordedEpisodeResolver.assignProgramEpisode(
                program.id,
                expected_series_id=first_series.id,
                expected_series_episode_id=None,
                decision='StructuredEpisode',
                season_number=1,
                episode_number=Decimal('12.5'),
            )
            original_episode_id = (await RecordedProgram.get(id=program.id)).series_episode_id

            async with transactions.in_transaction() as connection:
                program = await RecordedProgram.filter(id=program.id).using_db(connection).get()
                await RecordedEpisodeResolver.synchronizeSeriesAssignment(
                    program,
                    target_series_id=second_series.id,
                    connection=connection,
                )
                program.series_id = second_series.id
                await program.save(update_fields=['series_id', 'updated_at'], using_db=connection)

            program = await RecordedProgram.get(id=program.id)
            target_episode = await SeriesEpisode.get(id=program.series_episode_id)
            assert program.series_episode_id != original_episode_id
            assert target_episode.series_id == second_series.id
            assert target_episode.season_number == 1
            assert target_episode.episode_number == Decimal('12.5')
            resolution = await RecordedEpisodeResolution.get(recorded_program_id=program.id)
            assert resolution.episode_id == target_episode.id
            assert resolution.source == 'Manual'

            async with transactions.in_transaction() as connection:
                program = await RecordedProgram.filter(id=program.id).using_db(connection).get()
                await RecordedEpisodeResolver.synchronizeSeriesAssignment(
                    program,
                    target_series_id=None,
                    connection=connection,
                )
            program = await RecordedProgram.get(id=program.id)
            assert program.series_episode_id is None
            resolution = await RecordedEpisodeResolution.get(recorded_program_id=program.id)
            assert resolution.status == 'Unknown'
            assert resolution.error_code == 'ProgramNotInSeries'
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_series_move_invalidates_web_search_episode_context() -> None:
    """Web検索由来の話数・根拠は別Seriesへ移植せず、移動先で再判定可能に戻す。"""

    async def Run() -> None:
        await InitializeDatabase()
        try:
            first_series = await Series.create(title='検索時シリーズ', description='', genres=ANIME_GENRES)
            second_series = await Series.create(title='訂正後シリーズ', description='', genres=ANIME_GENRES)
            channel = await CreateChannel('NID4-SID101', 101)
            program = await CreateRecordedProgram(
                1,
                series=first_series,
                channel=channel,
                episode_number='#4',
                day=1,
            )
            episode = await SeriesEpisode.create(
                series=first_series,
                season_number=1,
                episode_number=Decimal('4'),
            )
            program.series_episode_id = episode.id
            await program.save(update_fields=['series_episode_id', 'updated_at'])
            resolution = await RecordedEpisodeResolution.create(
                recorded_program=program,
                episode=episode,
                status='Resolved',
                source='WebSearch',
                provider_fingerprint='old-provider',
                proposed_season_number=1,
                proposed_episode_number=Decimal('4'),
                confidence=0.95,
                web_search_performed=True,
                citations=[{'url': 'https://example.com/episode-4', 'title': '旧シリーズ第4話'}],
                ai_model='old-model',
            )

            async with transactions.in_transaction() as connection:
                program = await RecordedProgram.filter(id=program.id).using_db(connection).get()
                await RecordedEpisodeResolver.synchronizeSeriesAssignment(
                    program,
                    target_series_id=second_series.id,
                    connection=connection,
                )
                program.series_id = second_series.id
                await program.save(update_fields=['series_id', 'updated_at'], using_db=connection)

            program = await RecordedProgram.get(id=program.id)
            await resolution.refresh_from_db()
            assert program.series_episode_id is None
            assert program.episode_number is None
            assert resolution.episode_id is None
            assert resolution.status == 'Pending'
            assert resolution.source == 'Local'
            assert resolution.provider_fingerprint is None
            assert resolution.proposed_episode_number is None
            assert resolution.citations == []
            assert resolution.ai_model is None
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())
