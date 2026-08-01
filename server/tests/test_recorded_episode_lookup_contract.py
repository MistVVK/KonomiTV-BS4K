# pyright: reportPrivateUsage=false

import asyncio
from decimal import Decimal
from typing import Any

import pytest

import app.metadata.ai.acp_client as AcpClientModule
import app.metadata.ai.openai_compatible as OpenAICompatibleModule
import app.metadata.ai.recorded_series_ai as RecordedSeriesAIModule
from app.metadata.ai.backends import (
    ConnectionTestCheck,
    ConnectionTestResult,
    EpisodeLookupConnectionChecks,
)
from app.metadata.ai.episode_lookup import (
    EpisodeLookupCitation,
    EpisodeLookupOutcome,
    EpisodeLookupResult,
    IsPublicHTTPURL,
    MapLookupOutcomeToResolutionStatus,
)
from app.metadata.ai.openai_compatible import OpenAICompatibleBackend
from app.metadata.RecordedEpisodeSearch import IsEpisodeLookupResultAccepted
from app.metadata.RecordedSeriesCandidates import RecordedSeriesAIError
from app.metadata.RecordedSeriesSettings import RecordedSeriesSettings


def _result(
    outcome: EpisodeLookupOutcome,
    **overrides: Any,
) -> EpisodeLookupResult:
    """outcome ごとの最小 valid 共通結果を作る。"""

    values: dict[str, Any] = {
        'outcome': outcome,
        'season_number': None,
        'episode_number': None,
        'confidence': None,
        'rationale_short': None,
        'citations': (),
        'web_search_performed': False,
        'model': 'test-model',
        'prompt_tokens': None,
        'completion_tokens': None,
        'http_status': None,
        'latency_ms': 1,
        'error_code': None,
        'error_message': None,
        'sources': (),
    }
    if outcome == 'Resolved':
        values.update({
            'season_number': 1,
            'episode_number': Decimal('12'),
            'confidence': 0.86,
            'rationale_short': '公式の第12話あらすじと放送日が一致',
            'web_search_performed': True,
        })
    elif outcome == 'NotNumbered':
        values.update({
            'confidence': 0.91,
            'rationale_short': '公式番組表で話数が付かないことを確認',
            'web_search_performed': True,
        })
    elif outcome == 'InsufficientEvidence':
        values.update({
            'confidence': 0.32,
            'rationale_short': '番組名は一致したが放送日を確認できない',
            'web_search_performed': True,
        })
    elif outcome not in {'Pending'}:
        values.update({
            'error_code': outcome,
            'error_message': '安全な日本語エラーメッセージです。',
        })
        if outcome == 'InvalidModelOutput':
            values['web_search_performed'] = True
    values.update(overrides)
    return EpisodeLookupResult(**values)


@pytest.mark.parametrize(
    ('outcome', 'expected_status'),
    [
        ('Pending', 'Pending'),
        ('Resolved', 'Resolved'),
        ('NotNumbered', 'NotNumbered'),
        ('InsufficientEvidence', 'NeedsReview'),
        ('SearchFailed', 'Failed'),
        ('SearchNotRun', 'NeedsReview'),
        ('InvalidModelOutput', 'NeedsReview'),
        ('Disabled', 'Unknown'),
        ('RateLimited', 'Unknown'),
        ('Cancelled', 'Pending'),
    ],
)
def test_all_lookup_outcomes_satisfy_common_contract_and_status_mapping(
    outcome: EpisodeLookupOutcome,
    expected_status: str,
) -> None:
    """全10 outcome を構築でき、成功時と失敗時の理由契約を区別できる。"""

    result = _result(outcome)

    assert result.outcome == outcome
    assert MapLookupOutcomeToResolutionStatus(outcome) == expected_status
    if outcome in {'Resolved', 'NotNumbered', 'InsufficientEvidence'}:
        assert result.rationale_short is not None
        assert result.error_code is None
        assert result.error_message is None
    elif outcome != 'Pending':
        assert result.rationale_short is None
        assert result.error_code is not None
        assert result.error_message is not None


@pytest.mark.parametrize(
    'invalid_overrides',
    [
        {'confidence': None},
        {'rationale_short': ' '},
        {'error_code': 'UnexpectedError', 'error_message': '失敗'},
        {'web_search_performed': False},
    ],
)
def test_model_outcome_rejects_inconsistent_values(
    invalid_overrides: dict[str, Any],
) -> None:
    """有効なモデル outcome に confidence・理由・検索証明を必須とする。"""

    with pytest.raises(ValueError):
        _result('Resolved', **invalid_overrides)


@pytest.mark.parametrize(
    'invalid_overrides',
    [
        {'error_code': None},
        {'error_message': None},
        {'error_code': 'unsafe code with spaces'},
        {'error_message': '1行目\n2行目'},
        {'season_number': 1, 'episode_number': Decimal('2')},
        {'confidence': 0.5},
        {'rationale_short': 'モデルの理由'},
    ],
)
def test_failure_outcome_rejects_model_values_or_unsafe_error(
    invalid_overrides: dict[str, Any],
) -> None:
    """transport 系 outcome はモデル値を持たず安全な固定エラーを必須にする。"""

    with pytest.raises(ValueError):
        _result('SearchFailed', **invalid_overrides)


def test_pending_and_search_not_run_are_exclusive_incomplete_states() -> None:
    """Pending と SearchNotRun に完了済み検索情報を混在させない。"""

    with pytest.raises(ValueError):
        _result('Pending', web_search_performed=True)
    with pytest.raises(ValueError):
        _result('SearchNotRun', web_search_performed=True)
    with pytest.raises(ValueError):
        _result('InvalidModelOutput', web_search_performed=False)


def test_failure_with_verified_trace_is_never_accepted_as_episode_result() -> None:
    """検索記録が残る失敗でも Episode への自動反映対象にはしない。"""

    citation = EpisodeLookupCitation(
        url='https://example.com/official/episode',
        title='公式ページ',
    )
    result = _result(
        'SearchFailed',
        citations=(citation,),
        web_search_performed=True,
    )

    assert IsEpisodeLookupResultAccepted(result, 'Always') is False


@pytest.mark.parametrize(
    'url',
    [
        'http://127.0.0.1/result',
        'http://127.1/result',
        'http://0177.0.0.1/result',
        'http://0x7f.0.0.1/result',
        'http://169.254.1/result',
        'http://2130706433/result',
        'http://169.254.169.254/latest/meta-data',
        'https://agent.internal/result',
        'https://localhost/result',
        'https://single-label/result',
        'https://invalid_host.example/result',
        'https://user:password@example.com/result',
        'file:///etc/passwd',
        'https://example.com/\nhttp://127.0.0.1/result',
    ],
)
def test_citation_rejects_non_public_or_credentialed_url(url: str) -> None:
    """正式 citation には public HTTP(S) URL だけを許可する。"""

    assert IsPublicHTTPURL(url) is False
    with pytest.raises(ValueError):
        EpisodeLookupCitation(url=url, title='危険な出典')


def test_citation_normalizes_bounded_public_url_and_title() -> None:
    """public URL と表示題名だけを bounded な citation として保持する。"""

    citation = EpisodeLookupCitation(
        url=' https://example.com/official/episode ',
        title=f' 公式ページ {"題" * 400} ',
    )

    assert citation.url == 'https://example.com/official/episode'
    assert len(citation.title) == 300


@pytest.mark.parametrize(
    ('error_code', 'http_status', 'expected_outcome', 'expected_web_search'),
    [
        ('MissingWebSearchCall', 200, 'SearchNotRun', False),
        ('InvalidJSON', 200, 'InvalidModelOutput', True),
        ('InvalidOutputSchema', 200, 'InvalidModelOutput', True),
        ('InvalidResponse', 200, 'SearchFailed', False),
        ('InvalidResponseJSON', 200, 'SearchFailed', False),
        ('HTTP429', 429, 'RateLimited', False),
        ('HTTP500', 500, 'SearchFailed', False),
        ('Timeout', None, 'SearchFailed', False),
        ('RedirectRejected', 307, 'SearchFailed', False),
    ],
)
def test_openai_adapter_normalizes_all_provider_failure_classes(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    http_status: int | None,
    expected_outcome: EpisodeLookupOutcome,
    expected_web_search: bool,
) -> None:
    """Responses API の tool/schema/transport 終端を共通 outcome へ写像する。"""

    async def RaiseError(**_kwargs: object) -> EpisodeLookupResult:
        raise RecordedSeriesAIError(error_code, http_status=http_status, latency_ms=25)

    monkeypatch.setattr(OpenAICompatibleModule, 'SearchRecordedEpisodeNumber', RaiseError)
    backend = OpenAICompatibleBackend(
        settings=RecordedSeriesSettings(
            ai_enabled=True,
            ai_episode_number_search_enabled=True,
            api_base_url='https://api.example/v1',
            model='test-model',
        ),
        api_key='test-key',
    )
    result = asyncio.run(backend.lookupEpisode({}))  # type: ignore[arg-type]

    assert result.outcome == expected_outcome
    assert result.web_search_performed is expected_web_search
    assert result.error_code == error_code
    assert result.error_message is not None
    assert result.season_number is None
    assert result.episode_number is None
    assert result.rationale_short is None
    if error_code == 'HTTP429':
        assert result.error_message == 'AI プロバイダーの利用上限に達しました。'


@pytest.mark.parametrize(
    ('lookup_result', 'expected_success', 'expected_source_status', 'expected_schema_status'),
    [
        (
            _result(
                'Resolved',
                citations=(EpisodeLookupCitation(
                    url='https://example.com/official/episode',
                    title='公式ページ',
                ),),
                http_status=200,
            ),
            True,
            'Passed',
            'Passed',
        ),
        (
            _result('InsufficientEvidence', http_status=200),
            False,
            'Failed',
            'Passed',
        ),
        (
            _result(
                'InvalidModelOutput',
                error_code='InvalidOutputSchema',
                http_status=200,
            ),
            False,
            'NotRun',
            'Failed',
        ),
    ],
)
def test_openai_episode_connection_test_returns_six_individual_checks(
    monkeypatch: pytest.MonkeyPatch,
    lookup_result: EpisodeLookupResult,
    expected_success: bool,
    expected_source_status: str,
    expected_schema_status: str,
) -> None:
    """接続試験は出典0件を成功扱いせず、固定6項目を個別返却する。"""

    async def ReturnResult(**_kwargs: object) -> EpisodeLookupResult:
        return lookup_result

    monkeypatch.setattr(OpenAICompatibleModule, 'SearchRecordedEpisodeNumber', ReturnResult)
    backend = OpenAICompatibleBackend(
        settings=RecordedSeriesSettings(
            ai_enabled=True,
            ai_episode_number_search_enabled=True,
            api_base_url='https://api.example/v1',
            model='test-model',
        ),
        api_key='test-key',
    )

    result = asyncio.run(backend.testConnection('EpisodeLookup'))

    assert result.success is expected_success
    assert result.checks is not None
    assert result.checks.backend_connection.status == 'Passed'
    assert result.checks.web_search.status == 'Passed'
    assert result.checks.source_url.status == expected_source_status
    assert result.checks.strict_schema.status == expected_schema_status
    assert result.checks.timeout_cancel.status == 'NotRun'
    assert result.checks.permission_policy.status == 'NotApplicable'


def test_openai_episode_connection_test_reports_observed_timeout_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """実際に timeout outcome を受けた場合だけ timeout check を確認済みにする。"""

    async def RaiseTimeout(**_kwargs: object) -> EpisodeLookupResult:
        raise RecordedSeriesAIError('Timeout', latency_ms=25)

    monkeypatch.setattr(OpenAICompatibleModule, 'SearchRecordedEpisodeNumber', RaiseTimeout)
    backend = OpenAICompatibleBackend(
        settings=RecordedSeriesSettings(
            ai_enabled=True,
            ai_episode_number_search_enabled=True,
            api_base_url='https://api.example/v1',
            model='test-model',
        ),
        api_key='test-key',
    )

    result = asyncio.run(backend.testConnection('EpisodeLookup'))

    assert result.success is False
    assert result.checks is not None
    assert result.checks.backend_connection.status == 'Failed'
    assert result.checks.web_search.status == 'NotRun'
    assert result.checks.timeout_cancel.status == 'Passed'


def test_openai_episode_connection_test_distinguishes_search_not_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """backend 応答成功と Web 検索未実行を別の check として返す。"""

    async def MissingSearch(**_kwargs: object) -> EpisodeLookupResult:
        return _result(
            'SearchNotRun',
            error_code='MissingWebSearchCall',
            http_status=200,
        )

    monkeypatch.setattr(OpenAICompatibleModule, 'SearchRecordedEpisodeNumber', MissingSearch)
    backend = OpenAICompatibleBackend(
        settings=RecordedSeriesSettings(
            ai_enabled=True,
            ai_episode_number_search_enabled=True,
            api_base_url='https://api.example/v1',
            model='test-model',
        ),
        api_key='test-key',
    )

    result = asyncio.run(backend.testConnection('EpisodeLookup'))

    assert result.success is False
    assert result.checks is not None
    assert result.checks.backend_connection.status == 'Passed'
    assert result.checks.web_search.status == 'Failed'
    assert result.checks.source_url.status == 'NotRun'
    assert result.checks.strict_schema.status == 'NotRun'


@pytest.mark.parametrize('success', [True, False])
def test_acp_adapter_does_not_overstate_unmeasured_safety_checks(
    monkeypatch: pytest.MonkeyPatch,
    success: bool,
) -> None:
    """ACP の flat result では timeout/cancel・permission を確認済みと偽らない。"""

    async def TestEpisodeLookup(**_kwargs: object) -> ConnectionTestResult:
        return ConnectionTestResult(
            success=success,
            latency_ms=10,
            model='test-model',
            message='ACP 権限 policy まで確認しました。',
        )

    monkeypatch.setattr(
        AcpClientModule,
        'run_acp_episode_lookup_connection_test',
        TestEpisodeLookup,
    )
    backend = RecordedSeriesAIModule._AcpAdapter(
        backend_kind='AcpCodex',
        command='/bin/false',
        args=[],
        env={},
        timeout_sec=30,
        model='test-model',
        cwd='/tmp',
        profile_dir='/tmp',
    )

    result = asyncio.run(backend.testConnection('EpisodeLookup'))

    assert result.success is success
    assert result.checks is not None
    if success:
        assert result.checks.backend_connection.status == 'Passed'
        assert result.checks.web_search.status == 'Passed'
        assert result.checks.source_url.status == 'Passed'
        assert result.checks.strict_schema.status == 'Passed'
        assert '権限 policy' not in result.message
    else:
        assert result.checks.backend_connection.status == 'NotRun'
        assert result.checks.web_search.status == 'NotRun'
    assert result.checks.timeout_cancel.status == 'NotRun'
    assert result.checks.permission_policy.status == 'NotRun'


def test_acp_lookup_holds_credential_generation_lock_until_backend_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """proof 再照合から ACP 終了まで credential import/delete lock を保持する。"""

    monkeypatch.setattr(
        RecordedSeriesAIModule,
        'ACP_CREDENTIAL_OPERATION_LOCK',
        asyncio.Lock(),
    )
    settings = RecordedSeriesSettings(ai_backend='AcpCodex')
    passed = ConnectionTestCheck(status='Passed', message='verified')
    connection_result = ConnectionTestResult(
        success=True,
        latency_ms=1,
        model='test-model',
        message='verified',
        checks=EpisodeLookupConnectionChecks(
            backend_connection=passed,
            web_search=passed,
            source_url=passed,
            strict_schema=passed,
            timeout_cancel=ConnectionTestCheck(
                status='NotRun',
                message='not run',
            ),
            permission_policy=ConnectionTestCheck(
                status='Passed',
                message='verified',
            ),
        ),
    )
    expected_fingerprint = (
        RecordedSeriesAIModule.get_episode_lookup_provider_fingerprint(
            settings,
            None,
        )
    )
    assert (
        RecordedSeriesAIModule.record_episode_lookup_capability_proof(
            settings,
            None,
            connection_result,
            tested_provider_fingerprint=expected_fingerprint,
        )
        is True
    )
    lookup_started = asyncio.Event()
    finish_lookup = asyncio.Event()
    mutation_entered = asyncio.Event()

    class FakeBackend:
        async def lookupEpisode(self, _program: object) -> EpisodeLookupResult:
            lookup_started.set()
            await finish_lookup.wait()
            return _result(
                'Resolved',
                citations=(
                    EpisodeLookupCitation(
                        url='https://example.com/episode-12',
                        title='公式',
                    ),
                ),
                sources=(
                    EpisodeLookupCitation(
                        url='https://example.com/episode-12',
                        title='公式',
                    ),
                ),
            )

    def CreateBackend(
        _settings: RecordedSeriesSettings,
        *,
        api_key: str | None = None,
    ) -> FakeBackend:
        assert api_key is None
        return FakeBackend()

    monkeypatch.setattr(
        RecordedSeriesAIModule,
        '_create_backend',
        CreateBackend,
    )

    async def MutateCredential() -> None:
        async with RecordedSeriesAIModule.ACP_CREDENTIAL_OPERATION_LOCK:
            mutation_entered.set()

    async def Run() -> None:
        lookup_task = asyncio.create_task(
            RecordedSeriesAIModule.lookup_episode(
                program={},  # type: ignore[arg-type]
                settings=settings,
                expected_provider_fingerprint=expected_fingerprint,
            )
        )
        await asyncio.wait_for(lookup_started.wait(), timeout=1)
        mutation_task = asyncio.create_task(MutateCredential())
        await asyncio.sleep(0)
        assert mutation_entered.is_set() is False
        finish_lookup.set()
        result = await asyncio.wait_for(lookup_task, timeout=1)
        await asyncio.wait_for(mutation_task, timeout=1)
        assert result.outcome == 'Resolved'
        assert mutation_entered.is_set() is True

    asyncio.run(Run())


def test_automatic_acp_lookup_rejects_a_changed_credential_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通常 enqueue も永続 fingerprint と異なる ACP credential では起動しない。"""

    generation = 'generation-a'

    def GetCredentialGeneration(
        _cls: type[object],
        _provider: object,
    ) -> str:
        return generation

    monkeypatch.setattr(
        RecordedSeriesAIModule.KonomiTVBS4KACPCredentials,
        'getCredentialGeneration',
        classmethod(GetCredentialGeneration),
    )
    settings = RecordedSeriesSettings(ai_backend='AcpCodex')
    expected_fingerprint = (
        RecordedSeriesAIModule.get_episode_lookup_provider_fingerprint(
            settings,
            None,
        )
    )
    generation = 'generation-b'

    def CreateBackend(*_args: object, **_kwargs: object) -> object:
        raise AssertionError('変更後 credential で backend を起動してはならない')

    monkeypatch.setattr(
        RecordedSeriesAIModule,
        '_create_backend',
        CreateBackend,
    )

    async def Run() -> None:
        with pytest.raises(RecordedSeriesAIError) as error:
            await RecordedSeriesAIModule.lookup_episode(
                program={},  # type: ignore[arg-type]
                settings=settings,
                expected_provider_fingerprint=expected_fingerprint,
                require_capability_proof=False,
            )
        assert error.value.code == 'EpisodeLookupCapabilityNotVerified'

    asyncio.run(Run())
