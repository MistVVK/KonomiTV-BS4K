# pyright: reportPrivateUsage=false

import asyncio
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from tortoise import Tortoise

import app.metadata.ai.recorded_series_ai as RecordedSeriesAIModule
import app.metadata.RecordedEpisodeAutomation as RecordedEpisodeAutomationModule
from app.constants import JST
from app.metadata.ai.backends import (
    ConnectionTestCheck,
    ConnectionTestResult,
    EpisodeLookupConnectionChecks,
)
from app.metadata.ai.episode_lookup import EpisodeLookupOutcome, EpisodeLookupResult
from app.metadata.ai.recorded_series_ai import (
    get_episode_lookup_provider_fingerprint,
    has_episode_lookup_capability_proof,
    record_episode_lookup_capability_proof,
    reset_episode_lookup_capability_proofs_for_tests,
)
from app.metadata.AnalysisTaskTracker import AnalysisTaskHandle
from app.metadata.RecordedEpisodeAutomation import (
    RecordedEpisodeAutomation,
    RecordedEpisodeRelookupConflictError,
    RecordedEpisodeRelookupNotFoundError,
)
from app.metadata.RecordedEpisodeContext import (
    BuildEpisodeInputFingerprint,
    BuildRecordedEpisodeLookupContext,
    RecordedEpisodeLookupContext,
)
from app.metadata.RecordedEpisodeResolver import RecordedEpisodeResolver
from app.metadata.RecordedEpisodeSearch import AIEpisodeCitation, AIEpisodeLookupResult
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
    {"major": "アニメ・特撮", "middle": "国内アニメ"},
]


async def InitializeDatabase() -> None:
    """話数自動判定に必要な録画・Episode・AI監査をインメモリDBへ初期化する。"""

    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={
            "models": [
                "app.models.Channel",
                "app.models.RecordedEpisode",
                "app.models.RecordedProgram",
                "app.models.RecordedSeries",
                "app.models.RecordedVideo",
                "app.models.Series",
                "app.models.SeriesBroadcastPeriod",
            ]
        },
        timezone="Asia/Tokyo",
    )
    await Tortoise.generate_schemas()


async def CreateChannel() -> Channel:
    """録画番組スナップショット用の放送局を作成する。"""

    return await Channel.create(
        id="NID4-SID211",
        display_channel_id="bs211",
        network_id=4,
        service_id=211,
        transport_stream_id=1,
        remocon_id=11,
        channel_number="211",
        type="BS",
        name="BS11 テスト",
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
    recorded_status: str = "Recorded",
) -> RecordedProgram:
    """自動判定テスト用のSeries所属録画と動画を作成する。"""

    day = min(program_id, 28)
    period, _created = await SeriesBroadcastPeriod.get_or_create(
        series=series,
        channel=channel,
        defaults={
            "start_date": datetime(2026, 7, 1, tzinfo=JST).date(),
            "end_date": datetime(2026, 7, 31, tzinfo=JST).date(),
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
        title=f"{series.title} {episode_number or '話数不明'}",
        series_title=series.title,
        episode_number=episode_number,
        subtitle=f"副題 {program_id}",
        description="話数自動判定のテスト番組。",
        detail={"番組内容": "検索に渡す番組詳細"},
        start_time=start_time,
        end_time=start_time + timedelta(hours=1),
        duration=3600.0,
        is_free=True,
        genres=ANIME_GENRES,
        primary_audio_type="2/0モード(ステレオ)",
        primary_audio_language="日本語",
        secondary_audio_type=None,
        secondary_audio_language=None,
    )
    await RecordedVideo.create(
        recorded_program=program,
        status=recorded_status,
        file_path=f"/tmp/recorded-episode-automation-{program_id}.ts",
        file_hash=f"automation-hash-{program_id}",
        file_size=1,
        file_created_at=start_time,
        file_modified_at=start_time,
        duration=3600.0,
        container_format="MPEG-TS",
    )
    return program


def InstallAISettings(
    monkeypatch: pytest.MonkeyPatch,
) -> RecordedSeriesSettings:
    """AI話数検索に必要な設定を、能力証明なしでメモリ上に返す。"""

    # 接続試験未実施を再現し、各検索経路が能力証明へ依存しないことも検証する。
    monkeypatch.setattr(
        RecordedSeriesAIModule,
        'EPISODE_LOOKUP_CAPABILITY_PROOFS_PATH',
        Path(tempfile.mkdtemp()) / 'recorded-series-episode-lookup-proofs.json',
    )
    reset_episode_lookup_capability_proofs_for_tests()

    settings = RecordedSeriesSettings(
        enabled=True,
        ai_enabled=True,
        ai_backend="OpenCode",
        ai_backend_service_id="00000000-0000-4000-8000-000000000001",
    )

    def GetSettingsAndAPIKey(
        cls: type[RecordedSeriesSettingsStore],
    ) -> tuple[RecordedSeriesSettings, str]:
        del cls
        return settings, "test-secret"

    monkeypatch.setattr(
        RecordedSeriesSettingsStore,
        "getSettingsAndAPIKey",
        classmethod(GetSettingsAndAPIKey),
    )
    return settings


def SuccessfulCapabilityProof() -> ConnectionTestResult:
    """話数検索能力の検証に成功した接続試験結果を返す。"""

    passed = ConnectionTestCheck(status='Passed', message='テストで確認済み')
    return ConnectionTestResult(
        success=True,
        latency_ms=1,
        model='test-model',
        message='EpisodeLookup capability verified.',
        checks=EpisodeLookupConnectionChecks(
            backend_connection=passed,
            web_search=passed,
            source_url=passed,
            strict_schema=passed,
            timeout_cancel=ConnectionTestCheck(
                status='NotRun',
                message='個別テスト対象外',
            ),
            permission_policy=ConnectionTestCheck(
                status='NotApplicable',
                message='個別テスト対象外',
            ),
        ),
    )


def CreateAIResult(
    *,
    season_number: int = 1,
    episode_number: Decimal = Decimal("4"),
    confidence: float = 0.95,
    with_citation: bool = True,
    with_source: bool | None = None,
    recovery_attempt_summaries: tuple[str, ...] = (),
) -> AIEpisodeLookupResult:
    """受理可否の境界を指定できるモックWeb検索結果を作成する。"""

    evidence = (
        AIEpisodeCitation(
            url="https://example.com/official/episode",
            title="番組公式話数ページ",
        ),
    )
    citations = evidence if with_citation else ()
    sources = (
        evidence if (with_source if with_source is not None else with_citation) else ()
    )
    return AIEpisodeLookupResult(
        numbered=True,
        season_number=season_number,
        episode_number=episode_number,
        confidence=confidence,
        citations=citations,
        sources=sources,
        model="response-model",
        prompt_tokens=120,
        completion_tokens=12,
        http_status=200,
        latency_ms=45,
        recovery_attempt_summaries=recovery_attempt_summaries,
    )


def CreateOutcomeResult(outcome: EpisodeLookupOutcome) -> EpisodeLookupResult:
    """全 lookup outcome の永続化を検証する共通結果を作成する。"""

    evidence = (
        AIEpisodeCitation(
            url="https://example.com/official/episode",
            title="番組公式話数ページ",
        ),
    )
    if outcome == "Resolved":
        return EpisodeLookupResult(
            outcome=outcome,
            season_number=1,
            episode_number=Decimal("4"),
            confidence=0.95,
            rationale_short="公式の第4話ページと放送日時が一致しました。",
            citations=evidence,
            web_search_performed=True,
            model="response-model",
            prompt_tokens=120,
            completion_tokens=12,
            http_status=200,
            latency_ms=45,
        )
    if outcome in {'NotNumbered', 'NoPublishedNumber', 'InsufficientEvidence'}:
        return EpisodeLookupResult(
            outcome=outcome,
            season_number=2 if outcome == 'NoPublishedNumber' else None,
            episode_number=None,
            confidence=0.95 if outcome != 'InsufficientEvidence' else 0.35,
            rationale_short=(
                '公式情報から話数を付けない番組だと確認しました。'
                if outcome == 'NotNumbered'
                else '公式情報からシーズン2の特別編だと確認しました。'
                if outcome == 'NoPublishedNumber'
                else '番組名は一致しましたが、放送日時を確認できませんでした。'
            ),
            citations=evidence,
            web_search_performed=True,
            model="response-model",
            prompt_tokens=120,
            completion_tokens=12,
            http_status=200,
            latency_ms=45,
        )
    if outcome == "Pending":
        return EpisodeLookupResult(
            outcome=outcome,
            season_number=None,
            episode_number=None,
            confidence=None,
            rationale_short=None,
            citations=(),
            web_search_performed=False,
            model="response-model",
            prompt_tokens=None,
            completion_tokens=None,
            http_status=None,
            latency_ms=0,
        )
    return EpisodeLookupResult(
        outcome=outcome,
        season_number=None,
        episode_number=None,
        confidence=None,
        rationale_short=None,
        citations=(),
        web_search_performed=outcome == "InvalidModelOutput",
        model="response-model",
        prompt_tokens=120,
        completion_tokens=12,
        http_status=429 if outcome == "RateLimited" else None,
        latency_ms=45,
        error_code={
            "SearchFailed": "HTTP500",
            "SearchNotRun": "MissingWebSearchCall",
            "InvalidModelOutput": "InvalidOutputSchema",
            "Disabled": "AIEpisodeNumberSearchIsDisabled",
            "RateLimited": "HTTP429",
            "Cancelled": "Cancelled",
        }[outcome],
        error_message="安全なテスト用エラーメッセージです。",
    )


def test_new_recording_uses_deterministic_episode_before_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新規録画の厳格に解釈できる話数は、AI設定やWeb通信に触れず構造化する。"""

    async def SearchMustNotRun(**_kwargs: object) -> AIEpisodeLookupResult:
        raise AssertionError("Deterministic episode parsing must not call Web Search.")

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule, "ai_lookup_episode", SearchMustNotRun
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="決定論的シリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1,
                series=series,
                channel=channel,
                episode_number="Season 2 #03.5",
            )

            result = await RecordedEpisodeAutomation.resolveProgram(program.id)

            assert result.status == "Resolved"
            assert result.source == "LegacyMetadata"
            assert result.ai_requested is False
            program = await RecordedProgram.get(id=program.id)
            episode = await SeriesEpisode.get(id=program.series_episode_id)
            resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=program.id
            )
            assert episode.series_id == series.id
            assert episode.season_number == 2
            assert episode.episode_number == Decimal("3.5")
            assert resolution.status == "Resolved"
            assert resolution.source == "Local"
            assert resolution.web_search_performed is False
            assert await RecordedSeriesAIRequest.all().count() == 0
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_generated_ai_episode_returns_structured_cache_without_web_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一括生成済み AI Episode は直後の Automation で再検索・再課金しない。"""

    async def SearchMustNotRun(**_kwargs: object) -> AIEpisodeLookupResult:
        raise AssertionError('AI 一括生成済み Episode で Web Search を呼んではいけない')

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule,
        'ai_lookup_episode',
        SearchMustNotRun,
    )
    InstallAISettings(monkeypatch)

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title='一括生成シリーズ',
                description='',
                genres=ANIME_GENRES,
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                30,
                series=series,
                channel=channel,
                episode_number='#3',
            )
            episode = await SeriesEpisode.create(
                series=series,
                season_number=1,
                episode_number=Decimal('3'),
            )
            program.series_episode_id = episode.id
            await program.save(update_fields=['series_episode_id', 'updated_at'])
            await RecordedEpisodeResolution.create(
                recorded_program=program,
                episode=episode,
                status='Resolved',
                source='AI',
                is_legacy_recording=False,
                input_fingerprint='1' * 64,
                confidence=0.42,
                web_search_performed=False,
                ai_model='openai:test-model',
                resolved_at=datetime.now(tz=JST),
            )

            result = await RecordedEpisodeAutomation.resolveProgram(program.id)

            assert result.status == 'Resolved'
            assert result.source == 'StructuredCache'
            assert result.ai_requested is False
            resolution = await RecordedEpisodeResolution.get(recorded_program_id=program.id)
            assert resolution.source == 'AI'
            assert resolution.episode_id == episode.id
            assert resolution.web_search_performed is False
            assert await RecordedSeriesAIRequest.all().count() == 0
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_generated_ai_not_numbered_survives_disabled_web_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一括生成の NotNumbered は BETA 検索無効時も Unknown へ退行しない。"""

    async def SearchMustNotRun(**_kwargs: object) -> AIEpisodeLookupResult:
        raise AssertionError('話数 Web 検索 OFF で検索を呼んではいけない')

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule,
        'ai_lookup_episode',
        SearchMustNotRun,
    )
    settings = InstallAISettings(monkeypatch)
    settings.ai_enabled = False

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title='話数なし一括生成シリーズ',
                description='',
                genres=ANIME_GENRES,
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                31,
                series=series,
                channel=channel,
                episode_number=None,
            )
            resolution = await RecordedEpisodeResolution.create(
                recorded_program=program,
                status='NotNumbered',
                source='AI',
                is_legacy_recording=False,
                input_fingerprint='2' * 64,
                confidence=0.42,
                web_search_performed=False,
                ai_model='openai:test-model',
                resolved_at=datetime.now(tz=JST),
            )

            result = await RecordedEpisodeAutomation.resolveProgram(program.id)

            await resolution.refresh_from_db()
            assert result.status == 'Skipped'
            assert result.source == 'Disabled'
            assert result.ai_requested is False
            assert result.error_code == 'AIIsDisabled'
            assert resolution.status == 'NotNumbered'
            assert resolution.source == 'AI'
            assert resolution.lookup_outcome == 'Disabled'
            assert resolution.error_code == 'AIIsDisabled'
            assert await RecordedSeriesAIRequest.all().count() == 0
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_acp_episode_lookup_uses_the_common_facade_and_persists_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """固定 ACP preset でも OpenAI 互換と同じ lookup 契約を Automation が利用する。"""

    settings = RecordedSeriesSettings(
        enabled=True,
        ai_enabled=True,
        ai_backend="AcpCodex",
    )
    monkeypatch.setattr(
        RecordedSeriesAIModule,
        'EPISODE_LOOKUP_CAPABILITY_PROOFS_PATH',
        Path(tempfile.mkdtemp()) / 'recorded-series-episode-lookup-proofs.json',
    )
    reset_episode_lookup_capability_proofs_for_tests()

    def GetSettingsAndAPIKey(
        cls: type[RecordedSeriesSettingsStore],
    ) -> tuple[RecordedSeriesSettings, None]:
        del cls
        return settings, None

    received_context: RecordedEpisodeLookupContext | None = None

    async def SearchEpisode(
        *,
        program: RecordedEpisodeLookupContext,
        **_kwargs: object,
    ) -> AIEpisodeLookupResult:
        nonlocal received_context
        received_context = program
        return CreateAIResult()

    monkeypatch.setattr(
        RecordedSeriesSettingsStore,
        "getSettingsAndAPIKey",
        classmethod(GetSettingsAndAPIKey),
    )
    monkeypatch.setattr(
        RecordedEpisodeAutomationModule, "ai_lookup_episode", SearchEpisode
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="ACP対応シリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number=None
            )

            result = await RecordedEpisodeAutomation.resolveProgram(program.id)

            resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=program.id
            )
            assert result.status == "Resolved"
            assert result.source == "WebSearch"
            assert result.ai_requested is True
            assert resolution.status == "Resolved"
            assert resolution.lookup_outcome == "Resolved"
            assert resolution.error_code is None
            assert received_context is not None
            assert received_context['pipeline_version'] == '3'
            assert await RecordedSeriesAIRequest.all().count() == 1
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_interrupted_refresh_preserves_confirmed_not_numbered_state() -> None:
    """再起動回収は確定済み NotNumbered を Pending へ戻さず監査だけを中断で閉じる。"""

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="公式話数なし維持シリーズ",
                description="",
                genres=ANIME_GENRES,
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1,
                series=series,
                channel=channel,
                episode_number=None,
            )
            resolution = await RecordedEpisodeResolution.create(
                recorded_program=program,
                status="NotNumbered",
                source="WebSearch",
                lookup_outcome="Pending",
                web_search_performed=True,
                citations=[
                    {
                        "url": "https://example.com/official/unnumbered",
                        "title": "公式番組情報",
                    },
                ],
            )
            request = await RecordedSeriesAIRequest.create(
                resolution_id=None,
                episode_resolution=resolution,
                purpose="EpisodeLookup",
                status="Pending",
                model="openai:test-model",
                input_fingerprint="a" * 64,
                candidate_ids=[],
            )

            await RecordedEpisodeAutomation._recoverInterruptedRequests()

            await resolution.refresh_from_db()
            await request.refresh_from_db()
            assert resolution.status == "NotNumbered"
            assert resolution.source == "WebSearch"
            assert resolution.lookup_outcome == "Cancelled"
            assert resolution.error_code == "AIRequestInterrupted"
            assert resolution.citations == [
                {
                    "url": "https://example.com/official/unnumbered",
                    "title": "公式番組情報",
                },
            ]
            assert request.status == "Failed"
            assert request.error_code == "AIRequestInterrupted"
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_interrupted_unconfirmed_lookup_is_not_automatically_resent() -> None:
    """外部送信済みかもしれない未確定 lookup は Cancelled 回収後に自動再投入しない。"""

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="中断再送防止シリーズ",
                description="",
                genres=ANIME_GENRES,
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1,
                series=series,
                channel=channel,
                episode_number=None,
            )
            resolution = await RecordedEpisodeResolution.create(
                recorded_program=program,
                status="Pending",
                source="WebSearch",
                lookup_outcome="Pending",
                is_legacy_recording=False,
                web_search_performed=False,
                citations=[],
            )
            await RecordedSeriesAIRequest.create(
                resolution_id=None,
                episode_resolution=resolution,
                purpose="EpisodeLookup",
                status="Pending",
                model="openai:test-model",
                input_fingerprint="b" * 64,
                candidate_ids=[],
            )

            await RecordedEpisodeAutomation._recoverInterruptedRequests()
            await resolution.refresh_from_db()
            assert resolution.status == "Unknown"
            assert resolution.lookup_outcome == "Cancelled"
            assert resolution.error_code == "AIRequestInterrupted"

            RecordedEpisodeAutomation._queue = asyncio.Queue()
            RecordedEpisodeAutomation._queued_ids = set()
            RecordedEpisodeAutomation._running_ids = set()
            RecordedEpisodeAutomation._rerun_ids = set()
            await RecordedEpisodeAutomation._enqueueRecoverablePrograms()

            assert RecordedEpisodeAutomation._queue.empty()
            assert RecordedEpisodeAutomation._queued_ids == set()
            assert RecordedEpisodeAutomation._rerun_ids == set()
        finally:
            RecordedEpisodeAutomation._queue = None
            RecordedEpisodeAutomation._queued_ids = set()
            RecordedEpisodeAutomation._running_ids = set()
            RecordedEpisodeAutomation._rerun_ids = set()
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_lookup_receives_bounded_rich_context_without_holding_global_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """facade には path を除いた rich context を渡し、外部待機中は共通 lock を解放する。"""

    InstallAISettings(monkeypatch)
    received_context: RecordedEpisodeLookupContext | None = None

    async def SearchEpisode(
        *,
        program: RecordedEpisodeLookupContext,
        **_kwargs: object,
    ) -> AIEpisodeLookupResult:
        nonlocal received_context
        assert RECORDED_SERIES_RESOLUTION_LOCK.locked() is False
        async with asyncio.timeout(1):
            async with RECORDED_SERIES_RESOLUTION_LOCK:
                pass
        received_context = program
        return CreateAIResult()

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule, "ai_lookup_episode", SearchEpisode
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="rich context シリーズ",
                description="シリーズ説明",
                genres=ANIME_GENRES,
            )
            channel = await CreateChannel()
            await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number="#3"
            )
            target = await CreateRecordedProgram(
                2,
                series=series,
                channel=channel,
                episode_number=None,
            )
            await CreateRecordedProgram(
                3, series=series, channel=channel, episode_number="#5"
            )

            result = await RecordedEpisodeAutomation.resolveProgram(target.id)

            assert result.status == "Resolved"
            assert received_context is not None
            assert received_context["series"]["title"] == series.title
            assert received_context["program"]["detail_items"] == [
                {
                    "name": "番組内容",
                    "value": "検索に渡す番組詳細",
                }
            ]
            assert (
                received_context["local_parse"]["unresolved_reason"]
                == "MissingLegacyValue"
            )
            assert [
                neighbor["relation"] for neighbor in received_context["neighbors"]
            ] == [
                "Previous",
                "Next",
            ]
            assert (
                received_context["file"]["basename"]
                == "recorded-episode-automation-2.ts"
            )
            assert "/tmp/" not in str(received_context)
            assert "test-secret" not in str(received_context)
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_high_confidence_search_source_without_inline_citation_is_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """厳格JSON出力に本文引用がなくても、Web検索元があれば高信頼結果を確定する。"""

    InstallAISettings(monkeypatch)

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        return CreateAIResult(with_citation=False, with_source=True)

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule, "ai_lookup_episode", SearchEpisode
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="検索元根拠シリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number=None
            )

            result = await RecordedEpisodeAutomation.resolveProgram(program.id)

            assert result.status == "Resolved"
            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=program.id
            )
            assert program.series_episode_id is not None
            assert resolution.status == "Resolved"
            assert resolution.citations == [
                {
                    "url": "https://example.com/official/episode",
                    "title": "番組公式話数ページ",
                }
            ]
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

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule, "ai_lookup_episode", SearchEpisode
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="legacyシリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number=None
            )
            await RecordedEpisodeResolution.create(
                recorded_program=program,
                status="NeedsReview",
                source="Migration",
                is_legacy_recording=True,
                error_code="LegacyEpisodeMissing",
            )

            automatic_result = await RecordedEpisodeAutomation.resolveProgram(
                program.id
            )
            assert automatic_result.status == "Skipped"
            assert automatic_result.source == "LegacyRequiresManualBackfill"
            assert automatic_result.ai_requested is False
            assert search_calls == 0
            assert await RecordedSeriesAIRequest.all().count() == 0

            manual_result = await RecordedEpisodeAutomation.resolveProgram(
                program.id,
                allow_legacy_ai=True,
            )
            assert manual_result.status == "Resolved"
            assert manual_result.source == "WebSearch"
            assert manual_result.ai_requested is True
            assert search_calls == 1

            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=program.id
            )
            audit = await RecordedSeriesAIRequest.get(
                episode_resolution_id=resolution.id
            )
            assert program.episode_number == "#4"
            assert resolution.status == "Resolved"
            assert resolution.source == "WebSearch"
            assert resolution.web_search_performed is True
            assert resolution.citations == [
                {
                    "url": "https://example.com/official/episode",
                    "title": "番組公式話数ページ",
                }
            ]
            assert audit.purpose == "EpisodeLookup"
            assert audit.status == "Succeeded"
            assert audit.selected_choice_id == "S1E4"
            assert audit.prompt_tokens == 120
            assert audit.completion_tokens == 12
            assert audit.error_code is None
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_low_confidence_ai_result_with_verified_evidence_is_applied_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """confidence にかかわらず公開 Web 根拠のある数値結果を確定し、再課金しない。"""

    InstallAISettings(monkeypatch)
    search_calls = 0

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        nonlocal search_calls
        search_calls += 1
        return CreateAIResult(
            season_number=3,
            episode_number=Decimal("12.5"),
            confidence=0.45,
            recovery_attempt_summaries=(
                '1:Primary:AcpCodex:service=-:model=test:result=Resolved:adopted:'
                'prompt_tokens=120:completion_tokens=12:latency_ms=45:http_status=200:error=-',
            ),
        )

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule, "ai_lookup_episode", SearchEpisode
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="低信頼シリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number=None
            )

            first_result = await RecordedEpisodeAutomation.resolveProgram(program.id)
            second_result = await RecordedEpisodeAutomation.resolveProgram(program.id)

            assert first_result.status == 'Resolved'
            assert first_result.ai_requested is True
            assert first_result.error_code is None
            assert second_result.status == 'Resolved'
            assert second_result.source == 'StructuredCache'
            assert second_result.ai_requested is False
            assert search_calls == 1

            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=program.id
            )
            audit = await RecordedSeriesAIRequest.get(
                episode_resolution_id=resolution.id
            )
            assert program.series_episode_id is not None
            assert resolution.status == 'Resolved'
            assert resolution.source == "WebSearch"
            assert resolution.proposed_season_number == 3
            assert resolution.proposed_episode_number == Decimal("12.5")
            assert resolution.confidence == 0.45
            assert resolution.web_search_performed is True
            assert resolution.lookup_outcome == 'Resolved'
            assert resolution.error_code is None
            assert resolution.error_message is None
            assert audit.status == 'Succeeded'
            assert audit.selected_choice_id == "S3E12.5"
            assert audit.error_code is None
            assert len(audit.attempt_summaries) == 1
            assert 'result=Resolved' in audit.attempt_summaries[0]
            assert await RecordedSeriesAIRequest.all().count() == 1
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_interrupted_manual_override_closes_only_lookup_audit() -> None:
    """再起動回収は Manual の確定値を保ち、override lookup の Pending だけを閉じる。"""

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="Manual 中断回収シリーズ",
                description="",
                genres=ANIME_GENRES,
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1,
                series=series,
                channel=channel,
                episode_number="#12",
            )
            episode = await SeriesEpisode.create(
                series=series,
                season_number=1,
                episode_number=Decimal("12"),
            )
            program.series_episode_id = episode.id
            await program.save(update_fields=["series_episode_id"])
            resolution = await RecordedEpisodeResolution.create(
                recorded_program=program,
                episode=episode,
                status="Resolved",
                source="Manual",
                lookup_outcome="Pending",
                citations=[{"url": "https://example.com/old", "title": "既存出典"}],
            )
            request = await RecordedSeriesAIRequest.create(
                resolution_id=None,
                episode_resolution=resolution,
                purpose="EpisodeLookup",
                status="Pending",
                model="openai:test-model",
                input_fingerprint="c" * 64,
                candidate_ids=[],
            )

            await RecordedEpisodeAutomation._recoverInterruptedRequests()

            await program.refresh_from_db()
            await resolution.refresh_from_db()
            await request.refresh_from_db()
            assert program.series_episode_id == episode.id
            assert resolution.episode_id == episode.id
            assert resolution.status == "Resolved"
            assert resolution.source == "Manual"
            assert resolution.lookup_outcome == "Cancelled"
            assert resolution.error_code == "AIRequestInterrupted"
            assert resolution.citations == [
                {"url": "https://example.com/old", "title": "既存出典"},
            ]
            assert request.status == "Failed"
            assert request.error_code == "AIRequestInterrupted"
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_audit_status"),
    [
        ("Resolved", "Resolved", "Succeeded"),
        ("NotNumbered", "NotNumbered", "Succeeded"),
        ('NoPublishedNumber', 'NoPublishedNumber', 'Succeeded'),
        ("InsufficientEvidence", "NeedsReview", "Succeeded"),
        ("SearchFailed", "Failed", "Failed"),
        ("SearchNotRun", "NeedsReview", "Rejected"),
        ("InvalidModelOutput", "NeedsReview", "Rejected"),
        ("Disabled", "Unknown", "Failed"),
        ("RateLimited", "Unknown", "Rejected"),
        ("Cancelled", "Pending", "Failed"),
    ],
)
def test_all_lookup_outcomes_are_persisted_with_distinct_resolution_status(
    monkeypatch: pytest.MonkeyPatch,
    outcome: EpisodeLookupOutcome,
    expected_status: str,
    expected_audit_status: str,
) -> None:
    """backend 共通 outcome を潰さず status・理由・監査へ写像する。"""

    InstallAISettings(monkeypatch)

    async def SearchEpisode(**_kwargs: object) -> EpisodeLookupResult:
        return CreateOutcomeResult(outcome)

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule, "ai_lookup_episode", SearchEpisode
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title=f"{outcome} シリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number=None
            )

            await RecordedEpisodeAutomation.resolveProgram(program.id)

            resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=program.id
            )
            audit = await RecordedSeriesAIRequest.get(
                episode_resolution_id=resolution.id
            )
            assert resolution.lookup_outcome == outcome
            assert resolution.status == expected_status
            assert resolution.web_search_performed is (
                outcome
                in {
                    "Resolved",
                    "NotNumbered",
                    'NoPublishedNumber',
                    "InsufficientEvidence",
                    "InvalidModelOutput",
                }
            )
            assert audit.status == expected_audit_status
            if outcome in {
                'Resolved',
                'NotNumbered',
                'NoPublishedNumber',
                'InsufficientEvidence',
            }:
                assert resolution.rationale_short is not None
                assert resolution.error_code is None
                assert resolution.error_message is None
                if outcome == 'NoPublishedNumber':
                    assert resolution.season_number == 2
                    assert resolution.proposed_outcome == 'NoPublishedNumber'
                    assert resolution.episode_id is None
            else:
                assert resolution.rationale_short is None
                assert resolution.error_code is not None
                assert resolution.error_message is not None
                assert "\n" not in resolution.error_message
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_insufficient_evidence_clears_stale_canonical_season(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """要確認へ戻すとき、以前の番号なし正本から残ったシーズンを消去する。"""

    InstallAISettings(monkeypatch)

    async def SearchEpisode(**_kwargs: object) -> EpisodeLookupResult:
        return CreateOutcomeResult('InsufficientEvidence')

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule, 'ai_lookup_episode', SearchEpisode
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title='正本シーズンクリアシリーズ', description='', genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number=None
            )
            await RecordedEpisodeResolution.create(
                recorded_program=program,
                season_number=2,
                status='Pending',
                source='WebSearch',
            )

            await RecordedEpisodeAutomation.resolveProgram(program.id)

            resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=program.id
            )
            assert resolution.status == 'NeedsReview'
            assert resolution.lookup_outcome == 'InsufficientEvidence'
            assert resolution.season_number is None
            assert resolution.proposed_season_number is None
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
        raise RuntimeError("simulated audit finalization failure")

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule, "ai_lookup_episode", SearchEpisode
    )
    monkeypatch.setattr(
        RecordedEpisodeAutomation, "_finishAIRequest", classmethod(FailFinalization)
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="原子保存シリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number=None
            )

            with pytest.raises(
                RuntimeError, match="simulated audit finalization failure"
            ):
                await RecordedEpisodeAutomation.resolveProgram(program.id)

            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=program.id
            )
            audit = await RecordedSeriesAIRequest.get(
                episode_resolution_id=resolution.id
            )
            assert program.series_episode_id is None
            assert await SeriesEpisode.all().count() == 0
            assert resolution.status == "Pending"
            assert resolution.source == "WebSearch"
            assert audit.status == "Pending"
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_stored_proposal_promotion_requires_enabled_ai_and_public_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI有効化後も公開根拠を再検証できる旧提案だけを昇格する。"""

    current_settings = RecordedSeriesSettings(
        enabled=True,
        ai_enabled=False,
    )

    def GetSettings(cls: type[RecordedSeriesSettingsStore]) -> RecordedSeriesSettings:
        del cls
        return current_settings

    monkeypatch.setattr(
        RecordedSeriesSettingsStore, "getSettings", classmethod(GetSettings)
    )

    async def Run() -> None:
        nonlocal current_settings
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="提案昇格シリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number=None
            )
            second_program = await CreateRecordedProgram(
                2,
                series=series,
                channel=channel,
                episode_number=None,
            )
            no_evidence_program = await CreateRecordedProgram(
                3,
                series=series,
                channel=channel,
                episode_number=None,
            )
            private_evidence_program = await CreateRecordedProgram(
                4,
                series=series,
                channel=channel,
                episode_number=None,
            )
            context = await BuildRecordedEpisodeLookupContext(program.id)
            assert context is not None
            await RecordedEpisodeResolution.create(
                recorded_program=program,
                status="NeedsReview",
                source="WebSearch",
                lookup_outcome="InsufficientEvidence",
                input_fingerprint=BuildEpisodeInputFingerprint(context),
                provider_fingerprint="provider-a",
                proposed_season_number=2,
                proposed_episode_number=Decimal("8.5"),
                confidence=0.55,
                web_search_performed=True,
                citations=[{"url": "https://example.com/episode", "title": "出典"}],
                rationale_short="公式の第8.5話と放送日時が一致しました。",
                ai_model="test-model",
                error_code="AcceptancePolicyRejected",
            )
            second_context = await BuildRecordedEpisodeLookupContext(
                second_program.id
            )
            assert second_context is not None
            second_resolution = await RecordedEpisodeResolution.create(
                recorded_program=second_program,
                status="NeedsReview",
                source="WebSearch",
                lookup_outcome="InsufficientEvidence",
                input_fingerprint=BuildEpisodeInputFingerprint(second_context),
                provider_fingerprint="provider-a",
                proposed_season_number=2,
                proposed_episode_number=Decimal("9"),
                confidence=0.35,
                web_search_performed=True,
                citations=[{"url": "https://example.com/episode", "title": "出典"}],
                rationale_short="公式の第9話と放送日時が一致しました。",
                ai_model="test-model",
                error_code=None,
            )
            await RecordedSeriesAIRequest.create(
                resolution_id=None,
                episode_resolution=second_resolution,
                purpose="EpisodeLookup",
                status="Rejected",
                model="test-model",
                input_fingerprint=BuildEpisodeInputFingerprint(second_context),
                candidate_ids=[],
                error_code="AcceptancePolicyRejected",
            )
            no_evidence_context = await BuildRecordedEpisodeLookupContext(
                no_evidence_program.id
            )
            assert no_evidence_context is not None
            no_evidence_resolution = await RecordedEpisodeResolution.create(
                recorded_program=no_evidence_program,
                status='NeedsReview',
                source='WebSearch',
                lookup_outcome='InsufficientEvidence',
                input_fingerprint=BuildEpisodeInputFingerprint(no_evidence_context),
                provider_fingerprint='provider-a',
                proposed_season_number=2,
                proposed_episode_number=Decimal('10'),
                confidence=0.75,
                web_search_performed=True,
                citations=[],
                rationale_short='数値提案はありますが、保存済みの公開根拠がありません。',
                ai_model='test-model',
                error_code='AcceptancePolicyRejected',
            )
            private_evidence_context = await BuildRecordedEpisodeLookupContext(
                private_evidence_program.id
            )
            assert private_evidence_context is not None
            private_evidence_resolution = await RecordedEpisodeResolution.create(
                recorded_program=private_evidence_program,
                status='NeedsReview',
                source='WebSearch',
                lookup_outcome='InsufficientEvidence',
                input_fingerprint=BuildEpisodeInputFingerprint(private_evidence_context),
                provider_fingerprint='provider-a',
                proposed_season_number=2,
                proposed_episode_number=Decimal('11'),
                confidence=0.8,
                web_search_performed=True,
                citations=[{'url': 'http://127.0.0.1/episode', 'title': 'ローカルURL'}],
                rationale_short='公開されていないURLだけが保存されています。',
                ai_model='test-model',
                error_code='AcceptancePolicyRejected',
            )

            assert await RecordedEpisodeAutomation.promoteStoredProposals() == 0
            current_settings = current_settings.model_copy(update={"ai_enabled": True})
            assert await RecordedEpisodeAutomation.promoteStoredProposals() == 2

            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=program.id
            )
            assert program.series_episode_id is not None
            episode = await SeriesEpisode.get(id=program.series_episode_id)
            assert episode.season_number == 2
            assert episode.episode_number == Decimal("8.5")
            assert resolution.status == "Resolved"
            assert resolution.source == "WebSearch"
            assert resolution.rationale_short == "公式の第8.5話と放送日時が一致しました。"
            await second_program.refresh_from_db()
            await second_resolution.refresh_from_db()
            assert second_program.series_episode_id is not None
            second_episode = await SeriesEpisode.get(
                id=second_program.series_episode_id
            )
            assert second_episode.season_number == 2
            assert second_episode.episode_number == Decimal("9")
            assert second_resolution.status == "Resolved"
            assert (
                second_resolution.rationale_short
                == "公式の第9話と放送日時が一致しました。"
            )
            await no_evidence_program.refresh_from_db()
            await no_evidence_resolution.refresh_from_db()
            assert no_evidence_program.series_episode_id is None
            assert no_evidence_resolution.status == 'NeedsReview'
            assert no_evidence_resolution.lookup_outcome == 'InsufficientEvidence'
            await private_evidence_program.refresh_from_db()
            await private_evidence_resolution.refresh_from_db()
            assert private_evidence_program.series_episode_id is None
            assert private_evidence_resolution.status == 'NeedsReview'
            assert private_evidence_resolution.lookup_outcome == 'InsufficientEvidence'
            assert await RecordedSeriesAIRequest.all().count() == 1
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_provider_change_marks_running_episode_for_rerun() -> None:
    """旧providerで実行中の録画は、設定変更後に新providerで再評価する予約を残す。"""

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="provider再試行シリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number=None
            )
            await RecordedEpisodeResolution.create(
                recorded_program=program,
                status="Failed",
                source="WebSearch",
                is_legacy_recording=False,
                provider_fingerprint="provider-a",
                error_code="HTTP500",
            )

            RecordedEpisodeAutomation._queue = asyncio.Queue()
            RecordedEpisodeAutomation._queued_ids = set()
            RecordedEpisodeAutomation._running_ids = {program.id}
            RecordedEpisodeAutomation._rerun_ids = set()
            await RecordedEpisodeAutomation._enqueueRecoverablePrograms(
                provider_fingerprint="provider-b",
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
    )

    def GetSettings(_cls: type[RecordedSeriesSettingsStore]) -> RecordedSeriesSettings:
        return settings

    monkeypatch.setattr(
        RecordedSeriesSettingsStore, "getSettings", classmethod(GetSettings)
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="昇格競合シリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number=None
            )
            context = await BuildRecordedEpisodeLookupContext(program.id)
            assert context is not None
            resolution = await RecordedEpisodeResolution.create(
                recorded_program=program,
                status="Pending",
                source="WebSearch",
                input_fingerprint=BuildEpisodeInputFingerprint(context),
                provider_fingerprint="provider-a",
                web_search_performed=False,
            )

            async with RECORDED_SERIES_RESOLUTION_LOCK:
                promotion_task = asyncio.create_task(
                    RecordedEpisodeAutomation.promoteStoredProposals()
                )
                await asyncio.sleep(0)
                assert promotion_task.done() is False

                resolution.status = "NeedsReview"
                resolution.lookup_outcome = "InsufficientEvidence"
                resolution.proposed_season_number = 3
                resolution.proposed_episode_number = Decimal("7.5")
                resolution.confidence = 0.55
                resolution.web_search_performed = True
                resolution.citations = [
                    {"url": "https://example.com/episode", "title": "出典"}
                ]
                resolution.error_code = "AcceptancePolicyRejected"
                await resolution.save()

            assert await promotion_task == 1
            program = await RecordedProgram.get(id=program.id)
            episode = await SeriesEpisode.get(id=program.series_episode_id)
            assert episode.season_number == 3
            assert episode.episode_number == Decimal("7.5")
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_in_flight_lookup_accepts_low_confidence_with_verified_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部待機中の低 confidence 結果も、Web 根拠があれば固定規則で受理する。"""

    current_settings = InstallAISettings(monkeypatch)
    lookup_started = asyncio.Event()
    finish_lookup = asyncio.Event()

    def GetSettingsAndAPIKey(
        _cls: type[RecordedSeriesSettingsStore],
    ) -> tuple[RecordedSeriesSettings, str]:
        return current_settings, "test-secret"

    def GetSettings(
        _cls: type[RecordedSeriesSettingsStore],
    ) -> RecordedSeriesSettings:
        return current_settings

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        lookup_started.set()
        await finish_lookup.wait()
        return CreateAIResult(
            season_number=4,
            episode_number=Decimal("9.5"),
            confidence=0.45,
        )

    monkeypatch.setattr(
        RecordedSeriesSettingsStore,
        "getSettingsAndAPIKey",
        classmethod(GetSettingsAndAPIKey),
    )
    monkeypatch.setattr(
        RecordedSeriesSettingsStore,
        "getSettings",
        classmethod(GetSettings),
    )
    monkeypatch.setattr(
        RecordedEpisodeAutomationModule,
        "ai_lookup_episode",
        SearchEpisode,
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="受理条件競合シリーズ",
                description="",
                genres=ANIME_GENRES,
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1,
                series=series,
                channel=channel,
                episode_number=None,
            )

            resolve_task = asyncio.create_task(
                RecordedEpisodeAutomation.resolveProgram(program.id)
            )
            await asyncio.wait_for(lookup_started.wait(), timeout=1)

            # 応答前には保存済み提案がないため、旧提案の無課金昇格対象もない。
            assert await RecordedEpisodeAutomation.promoteStoredProposals() == 0
            finish_lookup.set()
            result = await asyncio.wait_for(resolve_task, timeout=1)

            await program.refresh_from_db()
            resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=program.id
            )
            assert result.status == "Resolved"
            assert program.series_episode_id is not None
            assert resolution.status == "Resolved"
            assert resolution.lookup_outcome == "Resolved"
            assert resolution.error_code is None
        finally:
            finish_lookup.set()
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_settings_update_enqueues_without_connection_test_proof_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """設定更新は AI OFF なら保留し、未試験でも AI ON なら再評価をキューへ入れる。"""

    settings = RecordedSeriesSettings(
        enabled=True,
        ai_enabled=False,
        ai_backend="OpenCode",
        ai_backend_service_id="00000000-0000-4000-8000-000000000001",
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
        return settings, "provider-b-key"

    monkeypatch.setattr(RecordedEpisodeAutomation, "start", classmethod(Start))
    monkeypatch.setattr(
        RecordedEpisodeAutomation, "promoteStoredProposals", classmethod(Promote)
    )
    monkeypatch.setattr(
        RecordedEpisodeAutomation,
        "_enqueueRecoverablePrograms",
        classmethod(EnqueueRecoverable),
    )
    monkeypatch.setattr(
        RecordedSeriesSettingsStore,
        "getSettingsAndAPIKey",
        classmethod(GetSettingsAndAPIKey),
    )

    async def Run() -> None:
        async def Wait() -> None:
            await asyncio.Event().wait()

        worker = asyncio.create_task(Wait())
        RecordedEpisodeAutomation._worker_task = worker
        try:
            await RecordedEpisodeAutomation.settingsUpdated()
            assert enqueue_calls == 0
            settings.ai_enabled = True
            await RecordedEpisodeAutomation.settingsUpdated()
            assert enqueue_calls == 1
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            RecordedEpisodeAutomation._worker_task = None

    asyncio.run(Run())


def test_episode_status_counts_only_playable_series_recordings() -> None:
    """状態集計はSeries所属かつ再生可能な録画だけを数え、未作成ResolutionもUnknownへ含める。"""

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="状態集計シリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            status_cases = [
                (2, "Pending"),
                (3, "Unknown"),
                (4, "Resolved"),
                (5, "NotNumbered"),
                (6, "NeedsReview"),
                (7, "Failed"),
            ]
            await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number=None
            )
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
                    source="Local",
                    is_legacy_recording=False,
                )

            unplayable_program = await CreateRecordedProgram(
                8,
                series=series,
                channel=channel,
                episode_number=None,
                recorded_status="Analyzing",
            )
            await RecordedEpisodeResolution.create(
                recorded_program=unplayable_program,
                status="Failed",
                source="Local",
                is_legacy_recording=False,
            )

            status = await RecordedEpisodeAutomation.getStatus()

            assert status["episode_resolved"] == 1
            assert status["episode_unknown"] == 3
            assert status["episode_not_numbered"] == 1
            assert status["episode_needs_review"] == 1
            assert status["episode_failed"] == 1
            assert status["episode_last_run_at"] is not None
            assert status["is_episode_running"] is False
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_manual_backfill_candidate_boundaries() -> None:
    """手動一括は再生可能・非手動判断に限定し、forceで新旧録画の前回結果を再送する。"""

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="一括対象シリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()

            cases = [
                (1, "Pending", "Migration", True, "Recorded"),
                (2, "Unknown", "Local", True, "Recorded"),
                (3, "NeedsReview", "Migration", True, "Recorded"),
                (4, "NeedsReview", "WebSearch", True, "Recorded"),
                (5, "Failed", "WebSearch", True, "Recorded"),
                (6, "NotNumbered", "WebSearch", True, "Recorded"),
                (7, "Resolved", "Migration", True, "Recorded"),
                (8, "Unknown", "Manual", True, "Recorded"),
                (9, "Pending", "Local", False, "Recorded"),
                (10, "Pending", "Migration", True, "Analyzing"),
                (11, "NeedsReview", "WebSearch", False, "Recorded"),
                (12, "Failed", "WebSearch", False, "Recorded"),
                (13, "NotNumbered", "WebSearch", False, "Recorded"),
                (14, "Resolved", "Local", False, "Recorded"),
                (15, "Resolved", "WebSearch", False, "Recorded"),
                (16, "Resolved", "Manual", False, "Recorded"),
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
                    provider_fingerprint="provider-a"
                    if source == "WebSearch"
                    else None,
                )
            await CreateRecordedProgram(
                17,
                series=series,
                channel=channel,
                episode_number=None,
            )

            assert await RecordedEpisodeAutomation._loadBackfillCandidateIDs(
                force=False
            ) == [1, 2, 3, 9, 17]
            assert await RecordedEpisodeAutomation._loadBackfillCandidateIDs(
                force=False,
                provider_fingerprint='provider-a',
            ) == [1, 2, 3, 9, 17]
            assert await RecordedEpisodeAutomation._loadBackfillCandidateIDs(
                force=False,
                provider_fingerprint='provider-b',
            ) == [1, 2, 3, 4, 5, 9, 11, 12, 17]
            assert await RecordedEpisodeAutomation._loadBackfillCandidateIDs(
                force=True
            ) == [
                1,
                2,
                3,
                4,
                5,
                6,
                7,
                9,
                11,
                12,
                13,
                14,
                15,
                17,
            ]
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


@pytest.mark.parametrize(
    ("source", "is_legacy_recording"),
    [
        ("Local", False),
        ("EPG", False),
        ("Migration", True),
        ("Manual", False),
    ],
)
def test_relookup_success_preserves_deterministic_and_manual_episode(
    monkeypatch: pytest.MonkeyPatch,
    source: Literal["Local", "EPG", "Migration", "Manual"],
    is_legacy_recording: bool,
) -> None:
    """通常の force 再検索は Local / EPG / Migration / Manual を自動上書きしない。"""

    InstallAISettings(monkeypatch)

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        return CreateAIResult(season_number=2, episode_number=Decimal("7.5"))

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule, "ai_lookup_episode", SearchEpisode
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title=f"{source} 保持シリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number="#10"
            )
            old_episode = await SeriesEpisode.create(
                series=series,
                season_number=1,
                episode_number=Decimal("10"),
            )
            program.series_episode_id = old_episode.id
            await program.save(update_fields=["series_episode_id"])
            resolution = await RecordedEpisodeResolution.create(
                recorded_program=program,
                episode=old_episode,
                status="Resolved",
                source=source,
                proposed_season_number=1,
                proposed_episode_number=Decimal("10"),
                confidence=1.0,
                is_legacy_recording=is_legacy_recording,
            )

            result = await RecordedEpisodeAutomation.resolveProgram(
                program.id,
                force=True,
                allow_legacy_ai=True,
                override_manual=source == "Manual",
            )

            assert result.status == "Resolved"
            assert result.source == "WebSearchRefreshPreserved"
            assert result.ai_requested is True
            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(id=resolution.id)
            episode = await SeriesEpisode.get(id=program.series_episode_id)
            audit = await RecordedSeriesAIRequest.get(
                episode_resolution_id=resolution.id
            )
            assert episode.id == old_episode.id
            assert episode.season_number == 1
            assert episode.episode_number == Decimal("10")
            assert program.episode_number == "#10"
            assert resolution.episode_id == old_episode.id
            assert resolution.status == "Resolved"
            assert resolution.source == source
            assert resolution.lookup_outcome == "Resolved"
            assert resolution.proposed_season_number == 2
            assert resolution.proposed_episode_number == Decimal("7.5")
            assert resolution.confidence == 0.95
            assert resolution.rationale_short is not None
            assert audit.status == "Succeeded"
            assert audit.selected_choice_id == "S2E7.5"
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


@pytest.mark.parametrize(
    ('source', 'is_legacy_recording'),
    [
        ('Local', False),
        ('EPG', False),
        ('Migration', True),
        ('Manual', False),
    ],
)
def test_single_episode_relookup_applies_accepted_result_as_web_search(
    monkeypatch: pytest.MonkeyPatch,
    source: Literal['Local', 'EPG', 'Migration', 'Manual'],
    is_legacy_recording: bool,
) -> None:
    """単票再検索の受理結果は Manual / 決定論的確定値も AI WebSearch として保存する。"""

    InstallAISettings(monkeypatch)

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        return CreateAIResult(season_number=2, episode_number=Decimal('7.5'))

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule, 'ai_lookup_episode', SearchEpisode
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title=f'{source} 単票適用シリーズ',
                description='',
                genres=ANIME_GENRES,
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number='#10'
            )
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
                source=source,
                proposed_season_number=1,
                proposed_episode_number=Decimal('10'),
                confidence=1.0,
                is_legacy_recording=is_legacy_recording,
                # Manual の場合は手動レーンも事前に埋めて、自動採用後に残ることを検証する。
                manual_episode_id=old_episode.id if source == 'Manual' else None,
                manual_season_number=1 if source == 'Manual' else None,
                manual_episode_number=Decimal('10') if source == 'Manual' else None,
                manual_status='Resolved' if source == 'Manual' else None,
            )

            result = await RecordedEpisodeAutomation.resolveProgram(
                program.id,
                force=True,
                allow_legacy_ai=True,
                override_manual=source == 'Manual',
                apply_accepted_lookup=True,
            )

            assert result.status == 'Resolved'
            assert result.source == 'WebSearch'
            assert result.ai_requested is True
            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(id=resolution.id)
            episode = await SeriesEpisode.get(id=program.series_episode_id)
            audit = await RecordedSeriesAIRequest.get(
                episode_resolution_id=resolution.id
            )
            assert episode.season_number == 2
            assert episode.episode_number == Decimal('7.5')
            assert program.episode_number == 'Season 2 #7.5'
            assert resolution.episode_id == episode.id
            assert resolution.status == 'Resolved'
            assert resolution.source == 'WebSearch'
            assert resolution.lookup_outcome == 'Resolved'
            assert resolution.proposed_season_number == 2
            assert resolution.proposed_episode_number == Decimal('7.5')
            assert resolution.web_search_performed is True
            if source == 'Manual':
                # 単票自動採用後も手動レーンは残る。
                assert resolution.manual_status == 'Resolved'
                assert resolution.manual_season_number == 1
                assert resolution.manual_episode_number == Decimal('10')
                assert resolution.manual_episode_id == old_episode.id
            assert audit.status == 'Succeeded'
            assert audit.selected_choice_id == 'S2E7.5'
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_single_episode_relookup_snapshots_legacy_manual_lane_before_adopt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """manual_* 未設定の旧 Manual 行でも、単票自動採用前に手動レーンへ退避する。"""

    InstallAISettings(monkeypatch)

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        return CreateAIResult(season_number=2, episode_number=Decimal('7.5'))

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule, 'ai_lookup_episode', SearchEpisode
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title='レガシー手動退避シリーズ', description='', genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number='#10'
            )
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
                source='Manual',
                # 移行前の Manual 行を模して manual_* は空のままにする。
                proposed_season_number=None,
                proposed_episode_number=None,
            )

            result = await RecordedEpisodeAutomation.resolveProgram(
                program.id,
                force=True,
                allow_legacy_ai=True,
                override_manual=True,
                apply_accepted_lookup=True,
            )

            assert result.status == 'Resolved'
            assert result.source == 'WebSearch'
            resolution = await RecordedEpisodeResolution.get(id=resolution.id)
            assert resolution.source == 'WebSearch'
            assert resolution.proposed_season_number == 2
            assert resolution.proposed_episode_number == Decimal('7.5')
            assert resolution.manual_status == 'Resolved'
            assert resolution.manual_season_number == 1
            assert resolution.manual_episode_number == Decimal('10')
            assert resolution.manual_episode_id == old_episode.id
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


@pytest.mark.parametrize(
    ('search_outcome', 'audit_status', 'audit_error_code'),
    [
        ("SearchFailed", "Failed", "HTTP500"),
        ("SearchNotRun", "Rejected", "MissingWebSearchCall"),
    ],
)
def test_resolved_web_search_failure_preserves_existing_episode_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
    search_outcome: Literal["SearchFailed", "SearchNotRun"],
    audit_status: str,
    audit_error_code: str,
) -> None:
    """確定済み WebSearch の再検索失敗は Episode・status・出典を維持する。"""

    InstallAISettings(monkeypatch)

    async def SearchEpisode(**_kwargs: object) -> EpisodeLookupResult:
        return CreateOutcomeResult(search_outcome)

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule, "ai_lookup_episode", SearchEpisode
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="再検索保持シリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number="#10"
            )
            old_episode = await SeriesEpisode.create(
                series=series,
                season_number=1,
                episode_number=Decimal("10"),
            )
            program.series_episode_id = old_episode.id
            await program.save(update_fields=["series_episode_id"])
            resolution = await RecordedEpisodeResolution.create(
                recorded_program=program,
                episode=old_episode,
                status="Resolved",
                source="WebSearch",
                proposed_season_number=1,
                proposed_episode_number=Decimal("10"),
                confidence=1.0,
                web_search_performed=True,
                rationale_short="前回の確定理由",
                citations=[
                    {
                        "url": "https://example.com/previous",
                        "title": "前回の検証済み出典",
                    }
                ],
                is_legacy_recording=False,
            )

            await RecordedEpisodeAutomation.resolveProgram(
                program.id,
                force=True,
                allow_legacy_ai=True,
            )

            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(id=resolution.id)
            audit = await RecordedSeriesAIRequest.get(
                episode_resolution_id=resolution.id
            )
            assert program.series_episode_id == old_episode.id
            assert program.episode_number == "#10"
            assert resolution.episode_id == old_episode.id
            assert resolution.status == "Resolved"
            assert resolution.source == "WebSearch"
            assert resolution.lookup_outcome == search_outcome
            assert resolution.proposed_season_number == 1
            assert resolution.proposed_episode_number == Decimal("10")
            assert resolution.confidence == 1.0
            # 失敗時も前回成功の検索実行フラグと出典を両立させる。
            assert resolution.web_search_performed is True
            assert resolution.citations == [
                {
                    "url": "https://example.com/previous",
                    "title": "前回の検証済み出典",
                }
            ]
            assert audit.status == audit_status
            assert audit.error_code == audit_error_code
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_not_numbered_web_search_failure_preserves_confirmed_state_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """確定済み公式話数なしも、再検索中・失敗時に状態と出典を維持する。"""

    InstallAISettings(monkeypatch)

    async def SearchEpisode(**_kwargs: object) -> EpisodeLookupResult:
        in_flight = await RecordedEpisodeResolution.get(recorded_program_id=1)
        assert in_flight.status == "NotNumbered"
        assert in_flight.source == "WebSearch"
        assert in_flight.lookup_outcome == "Pending"
        # in-flight でも前回成功 evidence と web_search フラグは落とさない。
        assert in_flight.web_search_performed is True
        assert in_flight.citations == [
            {
                "url": "https://example.com/previous-unnumbered",
                "title": "前回の検証済み出典",
            }
        ]
        assert in_flight.rationale_short is None
        assert in_flight.error_code is None
        return CreateOutcomeResult("SearchFailed")

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule, "ai_lookup_episode", SearchEpisode
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="公式話数なし保持シリーズ",
                description="",
                genres=ANIME_GENRES,
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1,
                series=series,
                channel=channel,
                episode_number=None,
            )
            resolution = await RecordedEpisodeResolution.create(
                recorded_program=program,
                status="NotNumbered",
                source="WebSearch",
                lookup_outcome="NotNumbered",
                confidence=0.98,
                web_search_performed=True,
                rationale_short="公式サイトで話数表記がないことを確認",
                citations=[
                    {
                        "url": "https://example.com/previous-unnumbered",
                        "title": "前回の検証済み出典",
                    }
                ],
                is_legacy_recording=False,
            )

            result = await RecordedEpisodeAutomation.resolveProgram(
                program.id,
                force=True,
                allow_legacy_ai=True,
            )

            resolution = await RecordedEpisodeResolution.get(id=resolution.id)
            assert result.status == "NotNumbered"
            assert result.source == "WebSearchRefreshPreserved"
            assert resolution.status == "NotNumbered"
            assert resolution.source == "WebSearch"
            assert resolution.lookup_outcome == "SearchFailed"
            assert resolution.confidence == 0.98
            # 失敗 outcome でも前回成功 evidence と web_search フラグを維持する。
            assert resolution.web_search_performed is True
            assert resolution.citations == [
                {
                    "url": "https://example.com/previous-unnumbered",
                    "title": "前回の検証済み出典",
                }
            ]
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_relookup_not_numbered_preserves_deterministic_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公式話数なしの成功結果でも Migration の確定値は自動解除しない。"""

    InstallAISettings(monkeypatch)
    evidence = (
        AIEpisodeCitation(
            url="https://example.com/official/episode",
            title="番組公式話数ページ",
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
            model="response-model",
            prompt_tokens=120,
            completion_tokens=12,
            http_status=200,
            latency_ms=45,
        )

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule, "ai_lookup_episode", SearchEpisode
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="公式話数なし再検索シリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number="#10"
            )
            old_episode = await SeriesEpisode.create(
                series=series,
                season_number=1,
                episode_number=Decimal("10"),
            )
            program.series_episode_id = old_episode.id
            await program.save(update_fields=["series_episode_id"])
            resolution = await RecordedEpisodeResolution.create(
                recorded_program=program,
                episode=old_episode,
                status="Resolved",
                source="Migration",
                proposed_season_number=1,
                proposed_episode_number=Decimal("10"),
                is_legacy_recording=True,
            )

            result = await RecordedEpisodeAutomation.resolveProgram(
                program.id,
                force=True,
                allow_legacy_ai=True,
            )

            assert result.status == "Resolved"
            assert result.source == "WebSearchRefreshPreserved"
            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(id=resolution.id)
            audit = await RecordedSeriesAIRequest.get(
                episode_resolution_id=resolution.id
            )
            assert program.series_episode_id == old_episode.id
            assert program.episode_number == "#10"
            assert resolution.episode_id == old_episode.id
            assert resolution.status == "Resolved"
            assert resolution.source == "Migration"
            assert resolution.lookup_outcome == "NotNumbered"
            assert resolution.proposed_season_number is None
            assert resolution.proposed_episode_number is None
            assert audit.status == "Succeeded"
            assert audit.selected_choice_id == "not-numbered"
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_manual_assignment_wins_while_lookup_is_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部呼び出し中の Manual 確定を応答後に再確認し、AI 結果で上書きしない。"""

    InstallAISettings(monkeypatch)
    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        lookup_started.set()
        await release_lookup.wait()
        return CreateAIResult()

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule, "ai_lookup_episode", SearchEpisode
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="Manual 競合シリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number=None
            )

            lookup_task = asyncio.create_task(
                RecordedEpisodeAutomation.resolveProgram(program.id)
            )
            await asyncio.wait_for(lookup_started.wait(), timeout=1)
            await RecordedEpisodeResolver.assignProgramEpisode(
                program.id,
                expected_series_id=series.id,
                expected_series_episode_id=None,
                decision="Unknown",
            )
            release_lookup.set()
            result = await asyncio.wait_for(lookup_task, timeout=1)

            program = await RecordedProgram.get(id=program.id)
            resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=program.id
            )
            audit = await RecordedSeriesAIRequest.get(
                episode_resolution_id=resolution.id
            )
            assert result.status == "Skipped"
            assert program.series_episode_id is None
            assert resolution.status == "Unknown"
            assert resolution.source == "Manual"
            assert resolution.lookup_outcome is None
            assert audit.status != "Pending"
        finally:
            release_lookup.set()
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_cancellation_is_persisted_and_re_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """明示 cancel は lookup outcome と監査を閉じてから呼び出し元へ伝播する。"""

    InstallAISettings(monkeypatch)
    lookup_started = asyncio.Event()

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        lookup_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule, "ai_lookup_episode", SearchEpisode
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="キャンセルシリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number=None
            )

            lookup_task = asyncio.create_task(
                RecordedEpisodeAutomation.resolveProgram(program.id)
            )
            await asyncio.wait_for(lookup_started.wait(), timeout=1)
            lookup_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(lookup_task, timeout=1)

            resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=program.id
            )
            audit = await RecordedSeriesAIRequest.get(
                episode_resolution_id=resolution.id
            )
            assert resolution.status == "Pending"
            assert resolution.source == "WebSearch"
            assert resolution.lookup_outcome == "Cancelled"
            assert resolution.error_code == "Cancelled"
            assert resolution.error_message is not None
            assert audit.status == "Failed"
            assert audit.error_code == "Cancelled"
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_input_change_after_lookup_closes_pending_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """単票でも使う fingerprint 競合経路は lookup_outcome を Cancelled へ終端化する。"""

    InstallAISettings(monkeypatch)
    lookup_started = asyncio.Event()
    finish_lookup = asyncio.Event()

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        lookup_started.set()
        await finish_lookup.wait()
        return CreateAIResult()

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule,
        "ai_lookup_episode",
        SearchEpisode,
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="入力競合シリーズ",
                description="",
                genres=ANIME_GENRES,
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1,
                series=series,
                channel=channel,
                episode_number=None,
            )

            lookup_task = asyncio.create_task(
                RecordedEpisodeAutomation.resolveProgram(program.id)
            )
            await asyncio.wait_for(lookup_started.wait(), timeout=1)
            program.description = "検索開始後に更新された説明"
            await program.save(update_fields=["description", "updated_at"])
            finish_lookup.set()

            with pytest.raises(
                RecordedEpisodeAutomationModule._RecordedEpisodeSnapshotChanged
            ):
                await asyncio.wait_for(lookup_task, timeout=1)

            resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=program.id
            )
            audit = await RecordedSeriesAIRequest.get(
                episode_resolution_id=resolution.id
            )
            assert resolution.status == "Unknown"
            assert resolution.lookup_outcome == "Cancelled"
            assert resolution.web_search_performed is True
            assert resolution.error_code == "InputChangedAfterRequest"
            assert resolution.error_message is not None
            assert audit.status == "Failed"
            assert audit.error_code == "InputChangedAfterRequest"
        finally:
            finish_lookup.set()
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_manual_override_input_change_closes_pending_lookup_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual override 中の入力競合は確定 Episode を保ち lookup を Cancelled にする。"""

    InstallAISettings(monkeypatch)
    lookup_started = asyncio.Event()
    finish_lookup = asyncio.Event()

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        lookup_started.set()
        await finish_lookup.wait()
        return CreateAIResult()

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule,
        "ai_lookup_episode",
        SearchEpisode,
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="Manual 入力競合シリーズ",
                description="",
                genres=ANIME_GENRES,
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1,
                series=series,
                channel=channel,
                episode_number="#4",
            )
            episode = await SeriesEpisode.create(
                series=series,
                season_number=1,
                episode_number=Decimal("4"),
            )
            program.series_episode_id = episode.id
            await program.save(update_fields=["series_episode_id"])
            resolution = await RecordedEpisodeResolution.create(
                recorded_program=program,
                episode=episode,
                status="Resolved",
                source="Manual",
            )

            lookup_task = asyncio.create_task(
                RecordedEpisodeAutomation.resolveProgram(
                    program.id,
                    force=True,
                    allow_legacy_ai=True,
                    override_manual=True,
                )
            )
            await asyncio.wait_for(lookup_started.wait(), timeout=1)
            program.description = "Manual 確定後に更新された説明"
            await program.save(update_fields=["description", "updated_at"])
            finish_lookup.set()

            with pytest.raises(
                RecordedEpisodeAutomationModule._RecordedEpisodeSnapshotChanged
            ):
                await asyncio.wait_for(lookup_task, timeout=1)

            await program.refresh_from_db()
            await resolution.refresh_from_db()
            audit = await RecordedSeriesAIRequest.get(
                episode_resolution_id=resolution.id
            )
            assert program.series_episode_id == episode.id
            assert resolution.episode_id == episode.id
            assert resolution.status == "Resolved"
            assert resolution.source == "Manual"
            assert resolution.lookup_outcome == "Cancelled"
            assert resolution.error_code == "InputChangedAfterRequest"
            assert audit.status == "Failed"
            assert audit.error_code == "InputChangedAfterRequest"
        finally:
            finish_lookup.set()
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_normal_enqueue_is_deferred_while_single_relookup_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通常 enqueue は同一録画の単票再検索と並走せず、完了後の再評価だけ予約する。"""

    async def Start(_cls: type[RecordedEpisodeAutomation]) -> None:
        return None

    monkeypatch.setattr(RecordedEpisodeAutomation, "start", classmethod(Start))

    async def Run() -> None:
        keep_running = asyncio.Event()

        async def Wait() -> None:
            await keep_running.wait()

        task = asyncio.create_task(Wait())
        RecordedEpisodeAutomation._queue = asyncio.Queue()
        RecordedEpisodeAutomation._queued_ids = set()
        RecordedEpisodeAutomation._running_ids = set()
        RecordedEpisodeAutomation._rerun_ids = set()
        RecordedEpisodeAutomation._relookup_tasks = {77: task}
        try:
            await RecordedEpisodeAutomation.enqueue(77)
            assert RecordedEpisodeAutomation._queue.empty()
            assert RecordedEpisodeAutomation._queued_ids == set()
            assert RecordedEpisodeAutomation._running_ids == set()
            assert RecordedEpisodeAutomation._rerun_ids == {77}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            RecordedEpisodeAutomation._queue = None
            RecordedEpisodeAutomation._queued_ids = set()
            RecordedEpisodeAutomation._running_ids = set()
            RecordedEpisodeAutomation._rerun_ids = set()
            RecordedEpisodeAutomation._relookup_tasks = {}

    asyncio.run(Run())


def test_single_relookup_validates_not_found_stale_and_manual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """単票開始前に再生可能性・楽観 lock・Manual 保護を 404/409 相当で検証する。"""

    InstallAISettings(monkeypatch)

    async def Run() -> None:
        await InitializeDatabase()
        try:
            with pytest.raises(RecordedEpisodeRelookupNotFoundError):
                await RecordedEpisodeAutomation.startRelookup(
                    404,
                    expected_series_id=1,
                    expected_series_episode_id=None,
                )

            series = await Series.create(
                title="単票検証シリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number=None
            )
            with pytest.raises(RecordedEpisodeRelookupConflictError):
                await RecordedEpisodeAutomation.startRelookup(
                    program.id,
                    expected_series_id=series.id + 1,
                    expected_series_episode_id=None,
                )

            RecordedEpisodeAutomation._running_ids = {program.id}
            with pytest.raises(RecordedEpisodeRelookupConflictError):
                await RecordedEpisodeAutomation.startRelookup(
                    program.id,
                    expected_series_id=series.id,
                    expected_series_episode_id=None,
                )
            RecordedEpisodeAutomation._running_ids = set()

            await RecordedEpisodeResolution.create(
                recorded_program=program,
                status="Unknown",
                source="Manual",
                is_legacy_recording=False,
            )
            with pytest.raises(RecordedEpisodeRelookupConflictError):
                await RecordedEpisodeAutomation.startRelookup(
                    program.id,
                    expected_series_id=series.id,
                    expected_series_episode_id=None,
                )
        finally:
            RecordedEpisodeAutomation._running_ids = set()
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_single_relookup_starts_without_connection_test_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """単票再検索は接続試験 proof がない provider でも開始する。"""

    settings = InstallAISettings(monkeypatch)
    # OpenCode では API キーは fingerprint の核にならないため、provider が異なることを
    # service_id の違いで表現して、登録済み proof と fingerprint を不一致にする。
    changed_settings = settings.model_copy(
        update={"ai_backend_service_id": "00000000-0000-4000-8000-000000000002"},
    )

    def GetUnprovenSettingsAndAPIKey(
        _cls: type[RecordedSeriesSettingsStore],
    ) -> tuple[RecordedSeriesSettings, str]:
        return changed_settings, "unproven-episode-lookup-key"

    monkeypatch.setattr(
        RecordedSeriesSettingsStore,
        "getSettingsAndAPIKey",
        classmethod(GetUnprovenSettingsAndAPIKey),
    )

    async def Start(
        _cls: type[object],
        *_args: object,
        **_kwargs: object,
    ) -> AnalysisTaskHandle:
        return cast(
            AnalysisTaskHandle,
            SimpleNamespace(execution=SimpleNamespace(id=321)),
        )

    async def RunRelookup(
        _cls: type[RecordedEpisodeAutomation],
        _handle: AnalysisTaskHandle,
        _recorded_program_id: int,
        *,
        expected_series_id: int,
        expected_series_episode_id: int | None,
        override_manual: bool,
        expected_provider_fingerprint: str,
    ) -> None:
        del (
            expected_series_id,
            expected_series_episode_id,
            override_manual,
            expected_provider_fingerprint,
        )

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule.AnalysisTaskTracker,
        "start",
        classmethod(Start),
    )
    monkeypatch.setattr(
        RecordedEpisodeAutomation,
        "_runRelookup",
        classmethod(RunRelookup),
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="能力証明シリーズ",
                description="",
                genres=ANIME_GENRES,
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1,
                series=series,
                channel=channel,
                episode_number=None,
            )

            accepted = await RecordedEpisodeAutomation.startRelookup(
                program.id,
                expected_series_id=series.id,
                expected_series_episode_id=None,
            )
            assert accepted.execution_id == 321
            assert accepted.reused is False
            await asyncio.gather(*RecordedEpisodeAutomation._relookup_tasks.values())
        finally:
            RecordedEpisodeAutomation._relookup_tasks = {}
            RecordedEpisodeAutomation._relookup_handles = {}
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_episode_backfill_starts_without_connection_test_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一括話数判定も接続試験 proof がない provider で開始する。"""

    settings = InstallAISettings(monkeypatch)
    unproven_settings = settings.model_copy(
        update={"ai_backend_service_id": "00000000-0000-4000-8000-000000000002"},
    )

    def GetUnprovenSettingsAndAPIKey(
        _cls: type[RecordedSeriesSettingsStore],
    ) -> tuple[RecordedSeriesSettings, str]:
        return unproven_settings, "unproven-episode-lookup-key"

    async def StartAutomation(_cls: type[RecordedEpisodeAutomation]) -> None:
        return None

    async def LoadCandidateIDs(
        _cls: type[RecordedEpisodeAutomation],
        *,
        force: bool,
        provider_fingerprint: str | None = None,
    ) -> list[int]:
        del force, provider_fingerprint
        return []

    async def StartTask(
        _cls: type[object],
        *_args: object,
        **_kwargs: object,
    ) -> AnalysisTaskHandle:
        return cast(
            AnalysisTaskHandle,
            SimpleNamespace(execution=SimpleNamespace(id=654)),
        )

    async def RunBackfill(
        _cls: type[RecordedEpisodeAutomation],
        _handle: AnalysisTaskHandle,
        candidate_ids: list[int],
        *,
        force: bool,
        expected_provider_fingerprint: str,
    ) -> None:
        del candidate_ids, force, expected_provider_fingerprint

    monkeypatch.setattr(
        RecordedSeriesSettingsStore,
        "getSettingsAndAPIKey",
        classmethod(GetUnprovenSettingsAndAPIKey),
    )
    monkeypatch.setattr(
        RecordedEpisodeAutomation,
        "start",
        classmethod(StartAutomation),
    )
    monkeypatch.setattr(
        RecordedEpisodeAutomation,
        "_loadBackfillCandidateIDs",
        classmethod(LoadCandidateIDs),
    )
    monkeypatch.setattr(
        RecordedEpisodeAutomationModule.AnalysisTaskTracker,
        "start",
        classmethod(StartTask),
    )
    monkeypatch.setattr(
        RecordedEpisodeAutomation,
        "_runBackfill",
        classmethod(RunBackfill),
    )

    async def Run() -> None:
        try:
            accepted = await RecordedEpisodeAutomation.startBackfill(force=False)
            assert accepted.execution_id == 654
            assert accepted.reused is False
            assert RecordedEpisodeAutomation._backfill_task is not None
            await RecordedEpisodeAutomation._backfill_task
        finally:
            RecordedEpisodeAutomation._backfill_task = None
            RecordedEpisodeAutomation._backfill_handle = None

    asyncio.run(Run())


def test_relookup_rechecks_the_accepted_provider_before_external_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """202 受付後に設定が変わっても、別 provider を外部実行しない。"""

    accepted_settings = InstallAISettings(monkeypatch)
    accepted_fingerprint = get_episode_lookup_provider_fingerprint(
        accepted_settings,
        "test-secret",
    )
    changed_settings = accepted_settings.model_copy(
        update={"ai_backend_service_id": "00000000-0000-4000-8000-000000000002"},
    )

    def GetChangedSettingsAndAPIKey(
        _cls: type[RecordedSeriesSettingsStore],
    ) -> tuple[RecordedSeriesSettings, str]:
        return changed_settings, "changed-unproven-key"

    async def SearchEpisode(**_kwargs: object) -> AIEpisodeLookupResult:
        raise AssertionError("受付後に変更された provider を呼び出してはならない")

    monkeypatch.setattr(
        RecordedSeriesSettingsStore,
        "getSettingsAndAPIKey",
        classmethod(GetChangedSettingsAndAPIKey),
    )
    monkeypatch.setattr(
        RecordedEpisodeAutomationModule,
        "ai_lookup_episode",
        SearchEpisode,
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="設定 TOCTOU 防止シリーズ",
                description="",
                genres=ANIME_GENRES,
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1,
                series=series,
                channel=channel,
                episode_number=None,
            )

            result = await RecordedEpisodeAutomation.resolveProgram(
                program.id,
                force=True,
                allow_legacy_ai=True,
                expected_series_id=series.id,
                expected_series_episode_id=None,
                expected_provider_fingerprint=accepted_fingerprint,
            )

            resolution = await RecordedEpisodeResolution.get(
                recorded_program_id=program.id
            )
            assert result.ai_requested is False
            assert result.error_code == "AISettingsChangedBeforeRequest"
            assert resolution.lookup_outcome == "SearchNotRun"
            assert resolution.status == "NeedsReview"
            assert resolution.error_code == "AISettingsChangedBeforeRequest"
            assert await RecordedSeriesAIRequest.all().count() == 0
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_runtime_capability_failure_revokes_matching_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """実 lookup で Web tool 未実行なら、接続試験の診断記録を失効する。"""

    settings = InstallAISettings(monkeypatch)
    assert record_episode_lookup_capability_proof(
        settings,
        "test-secret",
        SuccessfulCapabilityProof(),
    ) is True
    assert has_episode_lookup_capability_proof(settings, "test-secret") is True

    async def SearchEpisode(**_kwargs: object) -> EpisodeLookupResult:
        return CreateOutcomeResult("SearchNotRun")

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule,
        "ai_lookup_episode",
        SearchEpisode,
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="実行時能力失効シリーズ",
                description="",
                genres=ANIME_GENRES,
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1,
                series=series,
                channel=channel,
                episode_number=None,
            )

            await RecordedEpisodeAutomation.resolveProgram(program.id)

            assert (
                has_episode_lookup_capability_proof(settings, "test-secret")
                is False
            )
        finally:
            await Tortoise.close_connections()

    asyncio.run(Run())


def test_single_relookup_returns_accepted_and_reuses_running_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """単票再検索は即時受付 ID を返し、同一録画の実行中 task を再利用する。"""

    InstallAISettings(monkeypatch)
    keep_running = asyncio.Event()

    async def Start(
        _cls: type[object],
        *_args: object,
        **_kwargs: object,
    ) -> AnalysisTaskHandle:
        return cast(
            AnalysisTaskHandle,
            SimpleNamespace(execution=SimpleNamespace(id=321)),
        )

    async def RunRelookup(
        _cls: type[RecordedEpisodeAutomation],
        _handle: AnalysisTaskHandle,
        _recorded_program_id: int,
        *,
        expected_series_id: int,
        expected_series_episode_id: int | None,
        override_manual: bool,
        expected_provider_fingerprint: str,
    ) -> None:
        del (
            expected_series_id,
            expected_series_episode_id,
            override_manual,
            expected_provider_fingerprint,
        )
        await keep_running.wait()

    monkeypatch.setattr(
        RecordedEpisodeAutomationModule.AnalysisTaskTracker,
        "start",
        classmethod(Start),
    )
    monkeypatch.setattr(
        RecordedEpisodeAutomation,
        "_runRelookup",
        classmethod(RunRelookup),
    )

    async def Run() -> None:
        await InitializeDatabase()
        try:
            series = await Series.create(
                title="単票受付シリーズ", description="", genres=ANIME_GENRES
            )
            channel = await CreateChannel()
            program = await CreateRecordedProgram(
                1, series=series, channel=channel, episode_number=None
            )

            first = await RecordedEpisodeAutomation.startRelookup(
                program.id,
                expected_series_id=series.id,
                expected_series_episode_id=None,
            )
            second = await RecordedEpisodeAutomation.startRelookup(
                program.id,
                expected_series_id=series.id,
                expected_series_episode_id=None,
            )

            assert first.execution_id == 321
            assert first.reused is False
            assert second.execution_id == 321
            assert second.reused is True
        finally:
            tasks = list(RecordedEpisodeAutomation._relookup_tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            RecordedEpisodeAutomation._relookup_tasks = {}
            RecordedEpisodeAutomation._relookup_handles = {}
            keep_running.set()
            await Tortoise.close_connections()

    asyncio.run(Run())
