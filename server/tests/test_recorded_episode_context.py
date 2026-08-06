# pyright: reportPrivateUsage=false

import asyncio
import copy
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from tortoise import Tortoise

from app.constants import JST
from app.metadata.RecordedEpisodeContext import (
    MAX_CONTEXT_BYTES,
    MAX_EXISTING_EPISODE_SAMPLE,
    MAX_NEIGHBORS_PER_SIDE,
    BuildEpisodeInputFingerprint,
    BuildEpisodeProviderFingerprint,
    BuildRecordedEpisodeLookupContext,
    SerializeEpisodeLookupContext,
)
from app.models.Channel import Channel
from app.models.RecordedEpisode import SeriesEpisode
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedVideo import RecordedVideo
from app.models.Series import Series
from app.models.SeriesBroadcastPeriod import SeriesBroadcastPeriod


async def _initializeDatabase() -> None:
    """rich context に必要なモデルをインメモリ DB へ初期化する。"""

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


async def _createProgram(
    program_id: int,
    *,
    series: Series,
    period: SeriesBroadcastPeriod,
    channel: Channel,
    description: str,
    detail: dict[str, str],
    legacy_episode: str | None = None,
    file_path: str | None = None,
) -> RecordedProgram:
    """context テスト用の再生可能録画を作る。"""

    start_time = datetime(2026, 7, program_id, 20, 0, tzinfo=JST)
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
        title=f'構造化話数テスト {program_id}',
        series_title=series.title,
        episode_number=legacy_episode,
        subtitle=f'副題 {program_id}',
        description=description,
        detail=detail,
        start_time=start_time,
        end_time=start_time + timedelta(hours=1),
        duration=3600.0,
        is_free=True,
        genres=[{'major': 'アニメ・特撮', 'middle': '国内アニメ'}],
        primary_audio_type='2/0モード(ステレオ)',
        primary_audio_language='日本語',
        secondary_audio_type=None,
        secondary_audio_language=None,
    )
    await RecordedVideo.create(
        recorded_program=program,
        status='Recorded',
        file_path=file_path or f'/recordings/context-{program_id}.ts',
        file_hash=f'context-hash-{program_id}',
        file_size=1,
        file_created_at=start_time,
        file_modified_at=start_time,
        duration=3600.0,
        container_format='MPEG-TS',
    )
    return program


def test_rich_context_is_bounded_secret_free_and_fingerprint_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB から前後録画・既知話数・legacy 解析を bounded に組み立てる。"""

    environment_secret = 'must-not-enter-recorded-episode-context'
    monkeypatch.setenv('OPENAI_API_KEY', environment_secret)

    async def Scenario() -> None:
        await _initializeDatabase()
        try:
            channel = await Channel.create(
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
            series = await Series.create(
                title='構造化話数テスト',
                description=f'シリーズ先頭-{"説" * 2500}-シリーズ末尾',
                genres=[{'major': 'アニメ・特撮', 'middle': '国内アニメ'}],
            )
            period = await SeriesBroadcastPeriod.create(
                series=series,
                channel=channel,
                start_date=datetime(2026, 7, 1, tzinfo=JST).date(),
                end_date=datetime(2026, 7, 31, tzinfo=JST).date(),
            )
            episodes = [
                await SeriesEpisode.create(
                    series=series,
                    season_number=1,
                    episode_number=Decimal(index * 10),
                )
                for index in range(14)
            ]
            programs = [
                await _createProgram(
                    program_id,
                    series=series,
                    period=period,
                    channel=channel,
                    description=(
                        f'番組説明先頭-{"詳" * 6000}-番組説明末尾'
                        if program_id == 5
                        else '近傍録画'
                    ),
                    detail=(
                        {
                            f'項目{index:02d}': f'値先頭-{index}-{"値" * 1800}-値末尾-{index}'
                            for index in range(20)
                        }
                        if program_id == 5
                        else {}
                    ),
                    legacy_episode='#10' if program_id == 5 else None,
                    file_path=(
                        '/private/acp/profile-real-path/target-episode.ts'
                        if program_id == 5
                        else None
                    ),
                )
                for program_id in range(1, 10)
            ]
            for program, episode in zip(programs, episodes, strict=False):
                program.series_episode = episode
                await program.save(update_fields=['series_episode_id'])
            # 対象だけは legacy parse を context に残すため構造化リンクを外す。
            target = programs[4]
            target.series_episode = None
            await target.save(update_fields=['series_episode_id'])

            context = await BuildRecordedEpisodeLookupContext(target.id)
            assert context is not None
            serialized = SerializeEpisodeLookupContext(context)

            assert len(serialized.encode('utf-8')) <= MAX_CONTEXT_BYTES
            assert len(context['neighbors']) == MAX_NEIGHBORS_PER_SIDE * 2
            assert [neighbor['relation'] for neighbor in context['neighbors']] == [
                'Previous',
                'Previous',
                'Previous',
                'Next',
                'Next',
                'Next',
            ]
            assert len(context['series']['known_episode_sample']) == MAX_EXISTING_EPISODE_SAMPLE
            assert context['series']['known_episode_min'] == 'S1E0'
            assert context['series']['known_episode_max'] == 'S1E130'
            assert context['local_parse'] == {
                'legacy_value': '#10',
                'season_number': 1,
                'episode_number': '10',
                'unresolved_reason': 'Parsed',
            }
            assert context['file']['basename'] == 'target-episode.ts'
            assert context['program']['description'].startswith('番組説明先頭-')
            assert context['program']['description'].endswith('-番組説明末尾')
            assert context['program']['detail_items'][0]['value'].startswith('値先頭-0-')
            assert context['program']['detail_items'][0]['value'].endswith('-値末尾-0')
            assert '/private/' not in serialized
            assert 'profile-real-path' not in serialized
            assert environment_secret not in serialized

            structured_context = await BuildRecordedEpisodeLookupContext(programs[3].id)
            assert structured_context is not None
            assert structured_context['local_parse'] == {
                'legacy_value': None,
                'season_number': 1,
                'episode_number': '30',
                'unresolved_reason': 'AlreadyStructured',
            }

            fingerprint = BuildEpisodeInputFingerprint(context)
            assert fingerprint == BuildEpisodeInputFingerprint(context)
            changed_context = copy.deepcopy(context)
            changed_context['program']['title'] = '変更後の番組名'
            assert BuildEpisodeInputFingerprint(changed_context) != fingerprint
        finally:
            await Tortoise.close_connections()

    asyncio.run(Scenario())


def test_provider_fingerprint_separates_backend_endpoint_model_and_key() -> None:
    """provider fingerprint v2 は backend 固有値を区別し秘密値自体を保持しない。"""

    base = BuildEpisodeProviderFingerprint(
        backend_kind='OpenCode',
        effective_model='model-a',
        endpoint_identifier='https://api.example/v1',
        api_key='provider-secret',
    )

    assert len(base) == 64
    assert 'provider-secret' not in base
    assert base == BuildEpisodeProviderFingerprint(
        backend_kind=' OpenCode ',
        effective_model=' model-a ',
        endpoint_identifier=' https://api.example/v1 ',
        api_key=' provider-secret ',
    )
    assert base != BuildEpisodeProviderFingerprint(
        backend_kind='AcpCodex',
        effective_model='model-a',
        endpoint_identifier='AcpCodex:recorded-series-profile',
        api_key='provider-secret',
    )
    assert base != BuildEpisodeProviderFingerprint(
        backend_kind='OpenCode',
        effective_model='model-b',
        endpoint_identifier='https://api.example/v1',
        api_key='provider-secret',
    )
    assert base != BuildEpisodeProviderFingerprint(
        backend_kind='OpenCode',
        effective_model='model-a',
        endpoint_identifier='https://other.example/v1',
        api_key='provider-secret',
    )
    assert base != BuildEpisodeProviderFingerprint(
        backend_kind='OpenCode',
        effective_model='model-a',
        endpoint_identifier='https://api.example/v1',
        api_key='different-secret',
    )
