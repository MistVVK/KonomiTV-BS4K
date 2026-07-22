# pyright: reportPrivateUsage=false

import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient
from tortoise import Tortoise

from app import schemas
from app.constants import JST
from app.metadata.RecordedScanTask import RecordedScanTask
from app.metadata.RecordedSeriesResolver import (
    RecordedSeriesResolver,
    _buildRuleKeyHash,
)
from app.metadata.SeriesTitleParser import BuildSeriesGroupingKey
from app.models.Channel import Channel
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedSeries import RecordedSeriesRule
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
    """録画シリーズ管理APIが使用するモデルをインメモリDBへ初期化する。"""

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


async def CreateRecordedProgram(
    program_id: int,
    *,
    series: Series,
    day: int,
    status: str = 'Recorded',
    channel: Channel | None = None,
    period: SeriesBroadcastPeriod | None = None,
) -> RecordedProgram:
    """管理一覧・一括renameのテストに使う録画を作成する。"""

    start_time = datetime(2026, 7, day, 20, 0, tzinfo=JST)
    program = await RecordedProgram.create(
        id=program_id,
        recording_start_margin=0.0,
        recording_end_margin=0.0,
        is_partially_recorded=False,
        channel=channel,
        network_id=channel.network_id if channel is not None else None,
        service_id=channel.service_id if channel is not None else None,
        event_id=program_id,
        series=series,
        series_broadcast_period=period,
        title=f'{series.title} 第{program_id}話',
        series_title=series.title,
        episode_number=str(program_id),
        subtitle=None,
        description='録画シリーズ管理APIのテスト番組。',
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
        status=status,
        file_path=f'/tmp/recorded-series-management-{program_id}.ts',
        file_hash=f'hash-{program_id}',
        file_size=1,
        file_created_at=start_time,
        file_modified_at=start_time,
        duration=3600.0,
        container_format='MPEG-TS',
    )
    return program


def test_management_list_is_lightweight_searchable_and_includes_orphans() -> None:
    """管理一覧は孤立Seriesを含め、再生可能な録画だけを集計してページングする。"""

    async def Scenario() -> None:
        await InitializeDatabase()
        try:
            first = await Series.create(
                id=1,
                title='作品A',
                description='検索対象の説明',
                genres=ANIME_GENRES,
                wikipedia_page_id=101,
            )
            second = await Series.create(id=2, title='作品B', description='説明B', genres=ANIME_GENRES)
            await Series.create(id=3, title='孤立作品', description='録画なし', genres=ANIME_GENRES)
            await CreateRecordedProgram(1, series=first, day=3)
            await CreateRecordedProgram(2, series=first, day=1)
            await CreateRecordedProgram(3, series=first, day=2, status='Analyzing')
            await CreateRecordedProgram(4, series=second, day=4)

            app = CreateAdminApp()
            async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
                first_page = await client.get('/api/recorded-series/series?page=1&page_size=2')
                search = await client.get('/api/recorded-series/series?query=検索対象&page=1&page_size=30')
                detail = await client.get(f'/api/recorded-series/series/{first.id}')

            assert first_page.status_code == 200
            first_page_body = first_page.json()
            assert first_page_body['total'] == 3
            assert first_page_body['page'] == 1
            assert first_page_body['page_size'] == 2
            assert [item['id'] for item in first_page_body['items']] == [3, 2]
            assert first_page_body['items'][0]['recorded_program_count'] == 0
            assert first_page_body['items'][0]['first_recorded_at'] is None
            assert first_page_body['items'][0]['last_recorded_at'] is None

            assert search.status_code == 200
            assert search.json()['total'] == 1
            item = search.json()['items'][0]
            assert item['id'] == first.id
            assert item['wikipedia_page_id'] == 101
            assert item['recorded_program_count'] == 2
            assert item['first_recorded_at'].startswith('2026-07-01T20:00:00')
            assert item['last_recorded_at'].startswith('2026-07-03T20:00:00')
            assert 'broadcast_periods' not in item
            assert detail.status_code == 200
            assert detail.json() == item
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_management_update_keeps_canonical_alias_and_syncs_all_recordings() -> None:
    """renameは旧canonical/Ruleを保持し、Seriesと全所属録画の表示名だけを一括同期する。"""

    async def Scenario() -> None:
        await InitializeDatabase()
        try:
            old_key = _buildRuleKeyHash(BuildSeriesGroupingKey('旧EPG作品名'))
            series = await Series.create(
                title='旧EPG作品名',
                description='旧説明',
                genres=ANIME_GENRES,
                canonical_key=old_key,
            )
            first = await CreateRecordedProgram(10, series=series, day=1)
            second = await CreateRecordedProgram(11, series=series, day=2, status='Analyzing')
            old_rule = await RecordedSeriesRule.create(
                key_hash=old_key,
                normalized_key=BuildSeriesGroupingKey('旧EPG作品名'),
                display_title='旧EPG作品名',
                decision='Series',
                series=series,
                wikipedia_page_id=None,
                source='Manual',
                confidence=1.0,
                evidence_hash='a' * 64,
                resolver_version='test',
            )

            app = CreateAdminApp()
            async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
                response = await client.put(
                    f'/api/recorded-series/series/{series.id}',
                    json={
                        'title': '  正式な作品名  ',
                        'description': '  更新後の説明  ',
                        'expected_title': '旧EPG作品名',
                        'expected_description': '旧説明',
                    },
                )

            assert response.status_code == 204
            await series.refresh_from_db()
            await first.refresh_from_db()
            await second.refresh_from_db()
            await old_rule.refresh_from_db()
            assert series.title == '正式な作品名'
            assert series.description == '更新後の説明'
            assert series.canonical_key == old_key
            assert first.series_title == '正式な作品名'
            assert second.series_title == '正式な作品名'
            assert old_rule.key_hash == old_key
            assert old_rule.display_title == '旧EPG作品名'
            assert old_rule.series_id == series.id
            new_key = _buildRuleKeyHash(BuildSeriesGroupingKey('正式な作品名'))
            alias_rule = await RecordedSeriesRule.get(key_hash=new_key)
            assert alias_rule.normalized_key == BuildSeriesGroupingKey('正式な作品名')
            assert alias_rule.display_title == '正式な作品名'
            assert alias_rule.decision == 'Series'
            assert alias_rule.series_id == series.id
            assert alias_rule.source == 'Manual'
            assert await RecordedSeriesRule.all().count() == 2
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_management_update_rejects_series_and_rule_title_collisions_atomically() -> None:
    """別Series・単発Ruleが新タイトルを所有するrenameは元データを変えず409にする。"""

    async def Scenario() -> None:
        await InitializeDatabase()
        try:
            target = await Series.create(
                title='変更前',
                description='元説明',
                genres=ANIME_GENRES,
                canonical_key=_buildRuleKeyHash(BuildSeriesGroupingKey('変更前')),
            )
            program = await CreateRecordedProgram(20, series=target, day=1)
            await Series.create(
                title='衝突・シリーズ',
                description='別Series',
                genres=ANIME_GENRES,
                canonical_key=None,
            )
            rule_title = '単発判定済み'
            await RecordedSeriesRule.create(
                key_hash=_buildRuleKeyHash(BuildSeriesGroupingKey(rule_title)),
                normalized_key=BuildSeriesGroupingKey(rule_title),
                display_title=rule_title,
                decision='NotSeries',
                series_id=None,
                wikipedia_page_id=None,
                source='Manual',
                confidence=1.0,
                evidence_hash='b' * 64,
                resolver_version='test',
            )

            app = CreateAdminApp()
            async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
                series_conflict = await client.put(
                    f'/api/recorded-series/series/{target.id}',
                    json={
                        'title': '衝突 シリーズ',
                        'description': '変えてはいけない',
                        'expected_title': '変更前',
                        'expected_description': '元説明',
                    },
                )
                rule_conflict = await client.put(
                    f'/api/recorded-series/series/{target.id}',
                    json={
                        'title': rule_title,
                        'description': '変えてはいけない',
                        'expected_title': '変更前',
                        'expected_description': '元説明',
                    },
                )

            assert series_conflict.status_code == 409
            assert rule_conflict.status_code == 409
            await target.refresh_from_db()
            await program.refresh_from_db()
            assert target.title == '変更前'
            assert target.description == '元説明'
            assert program.series_title == '変更前'
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_management_description_update_allows_preexisting_normalized_conflicts() -> None:
    """既存重複・逆向きRuleがあっても、分類キーを変えない説明更新は拒否しない。"""

    async def Scenario() -> None:
        await InitializeDatabase()
        try:
            title = '重複・シリーズ'
            normalized_title = BuildSeriesGroupingKey(title)
            target = await Series.create(
                title=title,
                description='更新前',
                genres=ANIME_GENRES,
                canonical_key=_buildRuleKeyHash(normalized_title),
            )
            duplicate = await Series.create(
                title='重複 シリーズ',
                description='移行前の重複',
                genres=ANIME_GENRES,
                canonical_key=None,
            )
            program = await CreateRecordedProgram(30, series=target, day=1)
            await RecordedSeriesRule.create(
                key_hash=_buildRuleKeyHash(normalized_title),
                normalized_key=normalized_title,
                display_title=duplicate.title,
                decision='Series',
                series=duplicate,
                wikipedia_page_id=None,
                source='Manual',
                confidence=1.0,
                evidence_hash='c' * 64,
                resolver_version='test',
            )

            app = CreateAdminApp()
            async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
                response = await client.put(
                    f'/api/recorded-series/series/{target.id}',
                    json={
                        'title': target.title,
                        'description': '説明だけ更新',
                        'expected_title': title,
                        'expected_description': '更新前',
                    },
                )

            assert response.status_code == 204
            await target.refresh_from_db()
            await program.refresh_from_db()
            assert target.description == '説明だけ更新'
            assert program.series_title == title
            assert await RecordedSeriesRule.all().count() == 1
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_management_update_rejects_stale_editor_without_rollback() -> None:
    """別タブの先行更新後は古い表示メタデータを409にし、Seriesと録画名を巻き戻さない。"""

    async def Scenario() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title='更新前',
                description='更新前の説明',
                genres=ANIME_GENRES,
                canonical_key=_buildRuleKeyHash(BuildSeriesGroupingKey('更新前')),
            )
            program = await CreateRecordedProgram(40, series=series, day=1)
            app = CreateAdminApp()
            async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
                first = await client.put(
                    f'/api/recorded-series/series/{series.id}',
                    json={
                        'title': '先行更新',
                        'description': '先行更新の説明',
                        'expected_title': '更新前',
                        'expected_description': '更新前の説明',
                    },
                )
                stale = await client.put(
                    f'/api/recorded-series/series/{series.id}',
                    json={
                        'title': '古い画面の名前',
                        'description': '巻き戻してはいけない',
                        'expected_title': '更新前',
                        'expected_description': '更新前の説明',
                    },
                )
                latest = await client.get(f'/api/recorded-series/series/{series.id}')
                retry = await client.put(
                    f'/api/recorded-series/series/{series.id}',
                    json={
                        'title': '入力を保持した再更新',
                        'description': '入力を保持した再更新の説明',
                        'expected_title': latest.json()['title'],
                        'expected_description': latest.json()['description'],
                    },
                )

            assert first.status_code == 204
            assert stale.status_code == 409
            assert stale.json()['detail'] == 'Recorded series metadata was updated by another request.'
            assert latest.status_code == 200
            assert latest.json()['title'] == '先行更新'
            assert retry.status_code == 204
            await series.refresh_from_db()
            await program.refresh_from_db()
            assert series.title == '入力を保持した再更新'
            assert series.description == '入力を保持した再更新の説明'
            assert program.series_title == '入力を保持した再更新'
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_management_update_ignores_unrelated_series_timestamp_changes() -> None:
    """録画追加などによるupdated_at変更だけでは、表示メタデータ編集を競合扱いしない。"""

    async def Scenario() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title='時刻だけ更新',
                description='編集前の説明',
                genres=ANIME_GENRES,
            )
            series.updated_at += timedelta(minutes=1)
            await series.save(update_fields=['updated_at'])

            app = CreateAdminApp()
            async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
                response = await client.put(
                    f'/api/recorded-series/series/{series.id}',
                    json={
                        'title': '時刻だけ更新',
                        'description': '編集後の説明',
                        'expected_title': '時刻だけ更新',
                        'expected_description': '編集前の説明',
                    },
                )

            assert response.status_code == 204
            await series.refresh_from_db()
            assert series.description == '編集後の説明'
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_management_rename_promotes_existing_same_series_rule_to_manual_alias() -> None:
    """改名先Ruleが同じSeries向けでも、管理者指定の現行Manual aliasへ昇格する。"""

    async def Scenario() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title='改名前',
                description='説明',
                genres=ANIME_GENRES,
            )
            renamed_title = '管理者が決めた名前'
            renamed_key = BuildSeriesGroupingKey(renamed_title)
            rule = await RecordedSeriesRule.create(
                key_hash=_buildRuleKeyHash(renamed_key),
                normalized_key=renamed_key,
                display_title='古い自動判定名',
                decision='Series',
                series=series,
                wikipedia_page_id=None,
                source='Local',
                confidence=0.5,
                evidence_hash='d' * 64,
                resolver_version='legacy-version',
            )

            app = CreateAdminApp()
            async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
                response = await client.put(
                    f'/api/recorded-series/series/{series.id}',
                    json={
                        'title': renamed_title,
                        'description': '説明',
                        'expected_title': '改名前',
                        'expected_description': '説明',
                    },
                )

            assert response.status_code == 204
            await rule.refresh_from_db()
            assert rule.display_title == renamed_title
            assert rule.source == 'Manual'
            assert rule.confidence == 1.0
            assert rule.resolver_version != 'legacy-version'
            assert rule.series_id == series.id
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_recorded_scan_update_does_not_restore_stale_series_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解析開始後の改名を、Scannerが保持する古いモデルから巻き戻さない。"""

    async def Scenario() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title='スキャン前の名前',
                description='説明',
                genres=ANIME_GENRES,
            )
            program = await CreateRecordedProgram(50, series=series, day=1)
            stale_program = await RecordedProgram.get(id=program.id)
            video = await RecordedVideo.get(recorded_program_id=program.id)
            video.recorded_program = stale_program
            video_schema = schemas.RecordedVideo.model_validate(video, from_attributes=True)
            program_schema = schemas.RecordedProgram(
                recorded_video=video_schema,
                recording_start_margin=stale_program.recording_start_margin,
                recording_end_margin=stale_program.recording_end_margin,
                is_partially_recorded=stale_program.is_partially_recorded,
                channel=None,
                network_id=stale_program.network_id,
                service_id=stale_program.service_id,
                event_id=stale_program.event_id,
                series_id=stale_program.series_id,
                series_broadcast_period_id=stale_program.series_broadcast_period_id,
                title='再解析後の番組タイトル',
                series_title=stale_program.series_title,
                episode_number=stale_program.episode_number,
                subtitle=stale_program.subtitle,
                description='再解析後の番組説明',
                detail=stale_program.detail,
                start_time=stale_program.start_time,
                end_time=stale_program.end_time,
                duration=stale_program.duration,
                is_free=stale_program.is_free,
                genres=stale_program.genres,
                primary_audio_type=stale_program.primary_audio_type,
                primary_audio_language=stale_program.primary_audio_language,
                secondary_audio_type=stale_program.secondary_audio_type,
                secondary_audio_language=stale_program.secondary_audio_language,
                created_at=stale_program.created_at,
                updated_at=stale_program.updated_at,
            )

            await Series.filter(id=series.id).update(title='管理画面で改名後')
            await RecordedProgram.filter(id=program.id).update(series_title='管理画面で改名後')

            async def GetTestGitCommit() -> str:
                return 'test-commit'

            monkeypatch.setattr('app.metadata.RecordedScanTask.GetGitCommit', GetTestGitCommit)
            scan_task = object.__new__(RecordedScanTask)
            save_recorded_metadata = getattr(scan_task, '_RecordedScanTask__saveRecordedMetadataToDB')
            await save_recorded_metadata(program_schema, video, 'Unchanged')

            await program.refresh_from_db()
            assert program.title == '再解析後の番組タイトル'
            assert program.description == '再解析後の番組説明'
            assert program.series_id == series.id
            assert program.series_title == '管理画面で改名後'
            assert program.episode_number == '50'
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_management_update_returns_immediately_while_resolver_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolver lock使用中は待機せず503を返し、後から更新をcommitしない。"""

    async def Scenario() -> None:
        await InitializeDatabase()
        busy_lock = asyncio.Lock()
        await busy_lock.acquire()
        monkeypatch.setattr(RecordedSeriesResolver, '_resolve_lock', busy_lock)
        try:
            series = await Series.create(
                title='判定中',
                description='変更前',
                genres=ANIME_GENRES,
                canonical_key=_buildRuleKeyHash(BuildSeriesGroupingKey('判定中')),
            )
            app = CreateAdminApp()
            async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
                response = await client.put(
                    f'/api/recorded-series/series/{series.id}',
                    json={
                        'title': '遅延更新',
                        'description': '反映してはいけない',
                        'expected_title': '判定中',
                        'expected_description': '変更前',
                    },
                )

            assert response.status_code == 503
            assert response.headers['retry-after'] == '5'
            await series.refresh_from_db()
            assert series.title == '判定中'
            assert series.description == '変更前'
        finally:
            busy_lock.release()
            await Tortoise.close_connections()

    asyncio.run(Scenario())
