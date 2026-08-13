# pyright: reportPrivateUsage=false

import asyncio
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

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
from app.metadata.RecordedEpisodeAutomation import (
    RecordedEpisodeAutomation,
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
