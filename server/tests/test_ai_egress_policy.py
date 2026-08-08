"""AI egress 方針 (F-03 / R-08) のユニットテスト。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from app.metadata.ai.ai_egress_policy import (
    AreAIEpisodeWebFetchToolsEnabled,
    AreAIEpisodeWebSearchToolsEnabled,
    BuildOpenCodeEpisodeToolPermissions,
    IsAllowedAIEgressAddress,
    IsBlockedAIEgressHostname,
)
from app.metadata.ai.AIAPIUsageLedger import AIAPIUsageLedger, AIAPIUsageReservation
from app.metadata.ai.AIBackendSettings import AIBackendService
from app.metadata.ai.opencode_backend import OpenCodeBackend
from app.metadata.RecordedEpisodeContext import (
    RECORDED_EPISODE_CONTEXT_VERSION,
    RecordedEpisodeContextFile,
    RecordedEpisodeContextLocalParse,
    RecordedEpisodeContextProgram,
    RecordedEpisodeContextSeries,
    RecordedEpisodeLookupContext,
)


def _service(**overrides: Any) -> AIBackendService:
    base = {
        'service_id': str(uuid4()),
        'service_name': 'DeepSeek Test',
        'opencode_provider_id': 'deepseek',
        'opencode_model_id': 'deepseek-chat',
        'auth_mode': 'ApiKey',
        'billing_mode': 'Metered',
    }
    base.update(overrides)
    return AIBackendService.model_validate(base)


@pytest.fixture(autouse=True)
def _skip_monthly_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """backend unit は DB 無しで回すため台帳を no-op にする。"""

    async def FakeReserve(
        _cls: type[AIAPIUsageLedger],
        service: AIBackendService,
        **_kwargs: Any,
    ) -> AIAPIUsageReservation:
        return AIAPIUsageReservation(
            reservation_id='test',
            service_id=service.service_id,
            year_month='2026-08',
            billing_mode=service.billing_mode,
            skipped=True,
            reserved_total_tokens=0,
            reserved_estimated_cost_usd=Decimal('0'),
        )

    async def FakeSettle(
        _cls: type[AIAPIUsageLedger],
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        return None

    async def FakeRelease(
        _cls: type[AIAPIUsageLedger],
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        return None

    monkeypatch.setattr(AIAPIUsageLedger, 'Reserve', classmethod(FakeReserve))
    monkeypatch.setattr(AIAPIUsageLedger, 'Settle', classmethod(FakeSettle))
    monkeypatch.setattr(AIAPIUsageLedger, 'Release', classmethod(FakeRelease))


def _lookup_context() -> RecordedEpisodeLookupContext:
    return RecordedEpisodeLookupContext(
        pipeline_version=RECORDED_EPISODE_CONTEXT_VERSION,
        series=RecordedEpisodeContextSeries(
            title='series',
            genres=[],
            description='',
            known_episode_count=0,
            known_episode_min=None,
            known_episode_max=None,
            known_episode_sample=[],
        ),
        program=RecordedEpisodeContextProgram(
            title='ep',
            subtitle=None,
            description='',
            detail_items=[],
            broadcast_datetime='2020-01-01',
            channel=None,
            duration_seconds=0.0,
        ),
        local_parse=RecordedEpisodeContextLocalParse(
            legacy_value=None,
            season_number=None,
            episode_number=None,
            unresolved_reason='MissingLegacyValue',
        ),
        neighbors=[],
        file=RecordedEpisodeContextFile(basename=None),
        constraints=[],
    )


def test_episode_search_only_mode_defaults() -> None:
    """検索専用モードは websearch のみ許可し webfetch を拒否する。"""

    assert AreAIEpisodeWebSearchToolsEnabled() is True
    assert AreAIEpisodeWebFetchToolsEnabled() is False
    assert BuildOpenCodeEpisodeToolPermissions() == {
        'websearch': True,
        'webfetch': False,
    }


@pytest.mark.parametrize(
    ('address', 'allowed'),
    [
        ('8.8.8.8', True),
        ('1.1.1.1', True),
        ('127.0.0.1', False),
        ('10.0.0.1', False),
        ('192.168.1.1', False),
        ('172.16.0.1', False),
        ('169.254.1.1', False),
        ('169.254.169.254', False),
        ('::1', False),
        ('::ffff:127.0.0.1', False),
        ('fd00:ec2::254', False),
        ('not-an-ip', False),
    ],
)
def test_allowed_ai_egress_address(address: str, allowed: bool) -> None:
    """loopback / private / link-local / metadata を拒否する。"""

    assert IsAllowedAIEgressAddress(address) is allowed


@pytest.mark.parametrize(
    ('hostname', 'blocked'),
    [
        ('example.com', False),
        ('localhost', True),
        ('foo.localhost', True),
        ('metadata.google.internal', True),
        ('printer.local', True),
        ('host.internal', True),
        ('', True),
    ],
)
def test_blocked_ai_egress_hostname(hostname: str, blocked: bool) -> None:
    """内部用途 hostname を DNS 前に拒否する。"""

    assert IsBlockedAIEgressHostname(hostname) is blocked


def test_opencode_episode_session_passes_search_only_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenCode episode session は tools に websearch のみ True を渡す。"""

    backend = OpenCodeBackend(_service(), api_key='sk-test')
    captured: dict[str, Any] = {}

    class FakeClient:
        async def createSession(self) -> str:
            return 'sess-1'

        async def promptJsonSchema(self, *_args: Any, **kwargs: Any) -> dict[str, Any]:
            captured['tools'] = kwargs.get('tools')
            return {
                'parts': [
                    {
                        'type': 'tool',
                        'tool': 'websearch',
                        'state': {
                            'status': 'completed',
                            'output': {
                                'sources': [
                                    {
                                        'url': 'https://example.com/episode/1',
                                        'title': 'Official episode list',
                                    },
                                ],
                            },
                        },
                    },
                    {
                        'type': 'text',
                        'text': (
                            '{"outcome":"InsufficientEvidence","season_number":null,'
                            '"episode_number":null,"confidence":0.2,'
                            '"rationale_short":"search only"}'
                        ),
                    },
                ],
            }

        async def listMessages(self, _session_id: str) -> list[dict[str, Any]]:
            return []

        async def deleteSession(self, _session_id: str) -> None:
            return None

        async def abortSession(self, _session_id: str) -> None:
            return None

    class NoopProviderLease:
        async def __aenter__(self) -> NoopProviderLease:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def FakeEnsure() -> NoopProviderLease:
        return NoopProviderLease()

    monkeypatch.setattr(backend, '_client', FakeClient())
    monkeypatch.setattr(backend, 'ensureAuthInjected', FakeEnsure)
    result = asyncio.run(backend.lookupEpisode(_lookup_context()))
    assert captured['tools'] == {'websearch': True, 'webfetch': False}
    assert result.web_search_performed is True
    assert result.outcome in {'InsufficientEvidence', 'Resolved', 'NotNumbered'}
