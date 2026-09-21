from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from app.metadata.RecordedEpisodeAutomation import (
    RecordedEpisodeAutomation,
    _EpisodeProgramSnapshot,
)


def _Snapshot(*, legacy_episode_number: str | None) -> _EpisodeProgramSnapshot:
    return _EpisodeProgramSnapshot(
        id=11,
        series_id=22,
        series_title='テスト作品',
        series_episode_id=None,
        legacy_episode_number=legacy_episode_number,
        title='テスト作品 第12話',
        subtitle=None,
        description='',
        detail={},
        channel_id=None,
        channel_name=None,
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
    )


class _Query:
    def __init__(self, result: object) -> None:
        self.result = result

    def select_for_update(self) -> _Query:
        return self

    def using_db(self, _db: object) -> _Query:
        return self

    async def first(self) -> object:
        return self.result


def test_resolve_program_local_integer_skips_web_search() -> None:
    """Indexer の整数話数は Automation 実経路で Web 検索せず Local 確定する。"""

    snapshot = _Snapshot(legacy_episode_number='12')
    resolution = MagicMock()
    resolution.id = 3
    resolution.source = 'Local'
    resolution.status = 'Pending'
    resolution.is_legacy_recording = False
    apply = AsyncMock(return_value=True)
    lookup = AsyncMock()

    async def Run() -> None:
        with (
            patch.object(
                RecordedEpisodeAutomation,
                '_loadSnapshot',
                new=AsyncMock(return_value=snapshot),
            ),
            patch(
                'app.metadata.RecordedEpisodeAutomation.BuildRecordedEpisodeLookupContext',
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                'app.metadata.RecordedEpisodeAutomation.BuildEpisodeInputFingerprint',
                return_value='fingerprint',
            ),
            patch.object(
                RecordedEpisodeAutomation,
                '_getOrCreateResolution',
                new=AsyncMock(return_value=resolution),
            ),
            patch.object(RecordedEpisodeAutomation, '_applyResolvedEpisode', apply),
            patch('app.metadata.RecordedEpisodeAutomation.ai_lookup_episode', lookup),
        ):
            result = await RecordedEpisodeAutomation.resolveProgram(11)

        lookup.assert_not_awaited()
        apply.assert_awaited()
        assert apply.await_args.kwargs['source'] == 'Local'
        assert result.status == 'Resolved'
        assert result.source == 'LegacyMetadata'
        assert result.ai_requested is False

    import asyncio
    asyncio.run(Run())


def test_apply_resolved_episode_web_search_binds_bangumi() -> None:
    """Web 検索で話数が確定した Automation 反映経路は bindRecordedProgramById を呼ぶ。"""

    snapshot = _Snapshot(legacy_episode_number=None)
    recorded_program = MagicMock()
    recorded_program.id = snapshot.id
    recorded_program.series_id = snapshot.series_id
    recorded_program.series_title = snapshot.series_title
    recorded_program.title = snapshot.title
    recorded_program.subtitle = snapshot.subtitle
    recorded_program.description = snapshot.description
    recorded_program.detail = snapshot.detail
    recorded_program.channel_id = snapshot.channel_id
    recorded_program.start_time = snapshot.start_time
    recorded_program.save = AsyncMock()
    resolution = MagicMock()
    resolution.source = 'WebSearch'
    resolution.status = 'Pending'
    resolution.save = AsyncMock()
    episode = MagicMock()
    episode.id = 44
    episode.season_number = 1
    bind = AsyncMock()

    async def Run() -> None:
        tx = MagicMock()
        tx.__aenter__ = AsyncMock(return_value=MagicMock())
        tx.__aexit__ = AsyncMock(return_value=False)
        with (
            patch(
                'app.metadata.RecordedEpisodeAutomation.transactions.in_transaction',
                return_value=tx,
            ),
            patch(
                'app.metadata.RecordedEpisodeAutomation.RecordedProgram.filter',
                return_value=_Query(recorded_program),
            ),
            patch(
                'app.metadata.RecordedEpisodeAutomation.RecordedEpisodeResolution.filter',
                return_value=_Query(resolution),
            ),
            patch(
                'app.metadata.RecordedEpisodeAutomation._snapshotFromProgram',
                return_value=snapshot,
            ),
            patch(
                'app.metadata.RecordedEpisodeAutomation.SeriesEpisode.filter',
                return_value=_Query(episode),
            ),
            patch(
                'app.utils.KonomiTVBS4KBangumiClient.KonomiTVBS4KBangumiClient.bindRecordedProgramById',
                bind,
            ),
        ):
            applied = await RecordedEpisodeAutomation._applyResolvedEpisode(
                snapshot=snapshot,
                resolution_id=3,
                season_number=1,
                episode_number=Decimal('12'),
                source='WebSearch',
                provider_fingerprint=None,
                confidence=0.9,
                citations=[],
                ai_model='test',
                write_legacy_value=True,
            )

        assert applied is True
        bind.assert_awaited_with(snapshot.id)

    import asyncio
    asyncio.run(Run())
