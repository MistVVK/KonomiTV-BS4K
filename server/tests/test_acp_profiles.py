# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import json
import os
import stat
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import app.metadata.ai.acp_client as AcpClient
import app.metadata.ai.acp_presets as AcpPresets
import app.metadata.ai.acp_profiles as AcpProfiles
import app.metadata.ai.recorded_series_ai as RecordedSeriesAI
from app.metadata.ai.episode_lookup import (
    EpisodeLookupCitation,
    EpisodeLookupResult,
)
from app.metadata.RecordedSeriesCandidates import RecordedSeriesAIError
from app.metadata.RecordedSeriesSettings import RecordedSeriesSettings


def test_codex_profile_generates_only_managed_config_and_isolates_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Codex profile はホスト設定を共有せず、最小 config と隔離 directory だけを作る。"""

    profiles_root = tmp_path / 'profiles'
    monkeypatch.setattr(AcpProfiles, '_ACP_PROFILES_ROOT', profiles_root)

    profile = AcpProfiles.ensure_acp_profile('codex')

    config_path = profile / 'config.toml'
    assert config_path.read_text(encoding='utf-8') == 'cli_auth_credentials_store = "file"\n'
    assert config_path.is_symlink() is False
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700
    for isolated_name in AcpProfiles._ISOLATED_DIRS:
        isolated_path = profile / isolated_name
        assert isolated_path.is_dir()
        assert isolated_path.is_symlink() is False
        assert stat.S_IMODE(isolated_path.stat().st_mode) == 0o700
    assert (profile / 'auth.json').exists() is False
    assert (profile / 'packages').exists() is False
    assert (profile / 'credentials').exists() is False
    assert (profile / 'settings.json').exists() is False


def test_konomitv_bs4k_codex_profile_can_enable_and_then_clear_fast_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Codex Fast 設定は専用 config だけへ書き、無効化時に残さない。"""

    profiles_root = tmp_path / 'profiles'
    monkeypatch.setattr(AcpProfiles, '_ACP_PROFILES_ROOT', profiles_root)

    profile = AcpProfiles.ensure_acp_profile('codex', konomitv_bs4k_fast_mode_enabled=True)
    fast_config = (profile / 'config.toml').read_text(encoding='utf-8')
    assert 'service_tier = "fast"' in fast_config
    assert '[features]' in fast_config
    assert 'fast_mode = true' in fast_config

    AcpProfiles.ensure_acp_profile('codex', konomitv_bs4k_fast_mode_enabled=False)
    assert (profile / 'config.toml').read_text(encoding='utf-8') == (
        'cli_auth_credentials_store = "file"\n'
    )


def test_gemini_profile_writes_vertex_ai_settings_and_removes_legacy_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Gemini profile は Vertex AI を選んだ settings を実ファイルで固定する。"""

    profiles_root = tmp_path / 'profiles'
    profile = profiles_root / 'gemini'
    profile.mkdir(parents=True)
    legacy_target = tmp_path / 'host-gemini-settings.json'
    legacy_target.write_text('{"legacy": true}\n', encoding='utf-8')
    (profile / 'settings.json').symlink_to(legacy_target)
    monkeypatch.setattr(AcpProfiles, '_ACP_PROFILES_ROOT', profiles_root)

    ensured = AcpProfiles.ensure_acp_profile('gemini')

    assert ensured == profile
    assert (profile / 'settings.json').exists() is False
    settings_path = profile / '.gemini' / 'settings.json'
    assert settings_path.is_file()
    assert settings_path.is_symlink() is False
    assert 'vertex-ai' in settings_path.read_text(encoding='utf-8')
    assert legacy_target.read_text(encoding='utf-8') == '{"legacy": true}\n'


def test_codex_managed_config_replaces_legacy_symlink_without_modifying_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """旧 config.toml symlink は atomic replace し、ホスト側 target を変更しない。"""

    profiles_root = tmp_path / 'profiles'
    profile = profiles_root / 'codex'
    profile.mkdir(parents=True)
    legacy_config = tmp_path / 'host-config.toml'
    legacy_config.write_text('model = "host-specific"\n', encoding='utf-8')
    legacy_before = legacy_config.read_bytes()
    (profile / 'config.toml').symlink_to(legacy_config)
    monkeypatch.setattr(AcpProfiles, '_ACP_PROFILES_ROOT', profiles_root)

    AcpProfiles.ensure_acp_profile('codex')

    assert (profile / 'config.toml').is_symlink() is False
    assert (profile / 'config.toml').read_text(encoding='utf-8') == 'cli_auth_credentials_store = "file"\n'
    assert legacy_config.read_bytes() == legacy_before


def test_acp_profile_rejects_symlink_for_isolated_sessions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """sessions を外部 directory へ向ける symlink は拒否する。"""

    profiles_root = tmp_path / 'profiles'
    profile = profiles_root / 'codex'
    profile.mkdir(parents=True)
    interactive_sessions = tmp_path / 'interactive-sessions'
    interactive_sessions.mkdir()
    (profile / 'sessions').symlink_to(interactive_sessions, target_is_directory=True)
    monkeypatch.setattr(AcpProfiles, '_ACP_PROFILES_ROOT', profiles_root)

    with pytest.raises(AcpProfiles.AcpProfileError, match='must not be a symlink'):
        AcpProfiles.ensure_acp_profile('codex')


def test_acp_profile_rejects_legacy_auth_symlink_until_explicit_reimport(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """旧 auth.json symlink を ACP セッションが追跡しない。"""

    profiles_root = tmp_path / 'profiles'
    profile = profiles_root / 'grok'
    profile.mkdir(parents=True)
    legacy_auth = tmp_path / 'host-auth.json'
    legacy_auth.write_text('{}\n', encoding='utf-8')
    (profile / 'auth.json').symlink_to(legacy_auth)
    monkeypatch.setattr(AcpProfiles, '_ACP_PROFILES_ROOT', profiles_root)

    with pytest.raises(AcpProfiles.AcpProfileError, match='must not be a symlink'):
        AcpProfiles.ensure_acp_profile('grok')


def test_preset_commands_are_fixed_inside_container() -> None:
    """ACP agent は固定プリセットだけから解決し、任意コマンドを受け取らない。"""

    codex_command, codex_args = AcpPresets.resolve_command('AcpCodex')
    assert codex_command == '/usr/local/bin/codex-acp'
    assert codex_args == []


@pytest.mark.parametrize(
    'backend,expected_home_key',
    [
        ('codex', 'CODEX_HOME'),
        ('grok', 'GROK_HOME'),
        ('gemini', 'GEMINI_CLI_HOME'),
    ],
)
def test_provider_environment_uses_only_its_dedicated_profile(
    tmp_path: Path,
    backend: AcpProfiles.KonomiTVBS4KACPProfileBackend,
    expected_home_key: str,
) -> None:
    """各 provider の HOME・sessions・logs は同じ専用 profile 配下に固定する。"""

    profile = tmp_path / backend
    for isolated_name in AcpProfiles._ISOLATED_DIRS:
        (profile / isolated_name).mkdir(parents=True, exist_ok=True)
    environment = AcpProfiles.get_profile_environment(
        backend,
        profile,
        google_cloud_project='fixture-project' if backend == 'gemini' else None,
        google_cloud_location='asia-northeast1' if backend == 'gemini' else None,
    )

    assert environment['HOME'] == str(profile)
    assert environment[expected_home_key] == str(profile)
    assert environment['TMPDIR'] == str(profile / 'tmp')
    assert environment['XDG_CACHE_HOME'] == str(profile / 'cache')
    provider_home_keys = {'CODEX_HOME', 'GROK_HOME', 'GEMINI_CLI_HOME'}
    assert {key for key in provider_home_keys if key in environment} == {expected_home_key}


def test_gemini_environment_uses_read_only_adc_mount_and_vertex_settings(tmp_path: Path) -> None:
    """Gemini へ固定 ADC path と明示 project/location を渡し、profile へコピーしない。"""

    profile = tmp_path / 'gemini'
    profile.mkdir()

    environment = AcpProfiles.get_profile_environment(
        'gemini',
        profile,
        google_cloud_project='fixture-project',
        google_cloud_location='asia-northeast1',
    )

    assert environment['GOOGLE_APPLICATION_CREDENTIALS'] == (
        '/run/konomitv-bs4k-host-auth/google/application_default_credentials.json'
    )
    assert environment['GOOGLE_GENAI_USE_VERTEXAI'] == 'true'
    assert environment['GOOGLE_CLOUD_PROJECT'] == 'fixture-project'
    assert environment['GOOGLE_CLOUD_LOCATION'] == 'asia-northeast1'
    assert list(profile.iterdir()) == []


def test_create_backend_uses_fixed_codex_command_workspace_and_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """プリセット backend は任意 host command/cwd を受けず、専用 profile だけを使う。"""

    profile = tmp_path / 'profile'
    (profile / 'workspace').mkdir(parents=True)

    def EnsureProfile(
        backend: AcpProfiles.KonomiTVBS4KACPProfileBackend,
        *,
        konomitv_bs4k_fast_mode_enabled: bool = False,
    ) -> Path:
        assert backend == 'codex'
        assert konomitv_bs4k_fast_mode_enabled is False
        return profile

    monkeypatch.setattr(AcpProfiles, 'ensure_acp_profile', EnsureProfile)
    settings = RecordedSeriesSettings(
        ai_backend='AcpCodex',
    )

    backend = RecordedSeriesAI._create_backend(settings)

    assert isinstance(backend, RecordedSeriesAI._AcpAdapter)
    assert backend._command == '/usr/local/bin/codex-acp'
    assert backend._cwd == str(profile / 'workspace')
    assert backend._env['HOME'] == str(profile)
    assert backend._env['CODEX_HOME'] == str(profile)
    assert 'OPENAI_API_KEY' not in backend._env


def test_konomitv_bs4k_create_backend_passes_codex_fast_mode_to_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Codex 設定の Fast 有効化を専用 profile 構築へ渡す。"""

    profile = tmp_path / 'profile'
    (profile / 'workspace').mkdir(parents=True)
    captured: dict[str, bool] = {}

    def EnsureProfile(
        backend: AcpProfiles.KonomiTVBS4KACPProfileBackend,
        *,
        konomitv_bs4k_fast_mode_enabled: bool = False,
    ) -> Path:
        assert backend == 'codex'
        captured['konomitv_bs4k_fast_mode_enabled'] = konomitv_bs4k_fast_mode_enabled
        return profile

    monkeypatch.setattr(AcpProfiles, 'ensure_acp_profile', EnsureProfile)
    settings = RecordedSeriesSettings(
        ai_backend='AcpCodex',
        konomitv_bs4k_acp_codex_fast_mode_enabled=True,
    )

    RecordedSeriesAI._create_backend(settings)

    assert captured == {'konomitv_bs4k_fast_mode_enabled': True}


def test_acp_profile_setup_failure_is_normalized_for_ai_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """プロファイル構築失敗を Resolver が監査終端できる固定 AI エラーへ変換する。"""

    settings = RecordedSeriesSettings(ai_backend='AcpCodex')

    def FailToEnsureProfile(
        backend: AcpProfiles.KonomiTVBS4KACPProfileBackend,
        *,
        konomitv_bs4k_fast_mode_enabled: bool = False,
    ) -> Path:
        del backend
        del konomitv_bs4k_fast_mode_enabled
        raise AcpProfiles.AcpProfileError('test profile failure')

    monkeypatch.setattr(AcpProfiles, 'ensure_acp_profile', FailToEnsureProfile)

    with pytest.raises(RecordedSeriesAIError) as error:
        RecordedSeriesAI._create_backend(settings)

    assert error.value.code == 'HostCLIStartFailed'


def test_acp_settings_preserve_episode_lookup_and_normalize_fixed_provider_model() -> None:
    """固定 provider でも話数検索を維持し、provider 固有モデルだけを正規化する。"""

    settings = RecordedSeriesSettings(
        ai_backend='AcpGrok',
        ai_episode_number_search_enabled=True,
        # Grok はモデル ID ではなく推論深さで切り替えるため、古い model 指定は捨てる。
        acp_model='grok-4',
        acp_reasoning_effort='High',
    )

    assert settings.ai_episode_number_search_enabled is True
    assert settings.acp_model is None
    assert settings.acp_reasoning_effort == 'High'


def test_acp_adapter_routes_episode_lookup_with_fixed_operation_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ACP adapter は backend 種別と sandbox 条件を EpisodeLookup client へ渡す。"""

    captured_arguments: dict[str, Any] = {}
    citation = EpisodeLookupCitation(
        url='https://example.com/verified-source',
        title='Verified source',
    )

    async def RunLookup(**arguments: Any) -> EpisodeLookupResult:
        captured_arguments.update(arguments)
        return EpisodeLookupResult(
            outcome='Resolved',
            season_number=1,
            episode_number=Decimal('12'),
            confidence=0.9,
            rationale_short='公式情報と一致しました。',
            citations=(citation,),
            web_search_performed=True,
            model='raw-model',
            prompt_tokens=None,
            completion_tokens=None,
            http_status=None,
            latency_ms=10,
            sources=(citation,),
        )

    monkeypatch.setattr(AcpClient, 'run_acp_episode_lookup', RunLookup)
    backend = RecordedSeriesAI._AcpAdapter(
        backend_kind='AcpCodex',
        command='/usr/local/bin/codex-acp',
        args=[],
        env={'HOME': str(tmp_path)},
        timeout_sec=30,
        model='gpt-test',
        reasoning_effort='High',
        cwd=str(tmp_path / 'workspace'),
        profile_dir=str(tmp_path),
        readable_files=(str(tmp_path / 'credential.json'),),
    )

    result = asyncio.run(backend.lookupEpisode({}))  # type: ignore[arg-type]

    assert captured_arguments['backend_kind'] == 'AcpCodex'
    assert captured_arguments['reasoning_effort'] == 'High'
    assert captured_arguments['cwd'] == str(tmp_path / 'workspace')
    assert captured_arguments['profile_dir'] == str(tmp_path)
    assert captured_arguments['readable_files'] == (str(tmp_path / 'credential.json'),)
    assert result.model == 'acp:codex:gpt-test[high]'
    assert result.citations == (citation,)


def test_create_backend_injects_grok_reasoning_effort_cli_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Grok は --reasoning-effort を agent stdio の前に付けて起動する。"""

    profile = tmp_path / 'profile'
    (profile / 'workspace').mkdir(parents=True)

    def EnsureProfile(
        backend: AcpProfiles.KonomiTVBS4KACPProfileBackend,
        *,
        konomitv_bs4k_fast_mode_enabled: bool = False,
    ) -> Path:
        assert backend == 'grok'
        assert konomitv_bs4k_fast_mode_enabled is False
        return profile

    monkeypatch.setattr(AcpProfiles, 'ensure_acp_profile', EnsureProfile)
    settings = RecordedSeriesSettings(
        ai_backend='AcpGrok',
        acp_reasoning_effort='Medium',
    )
    # 未指定時の既定は High。
    assert RecordedSeriesSettings(ai_backend='AcpGrok').acp_reasoning_effort == 'High'

    backend = RecordedSeriesAI._create_backend(settings)

    assert isinstance(backend, RecordedSeriesAI._AcpAdapter)
    assert backend._command == '/usr/local/bin/grok'
    assert backend._args == ['--reasoning-effort', 'medium', 'agent', 'stdio']
    assert backend._reasoning_effort == 'Medium'
    assert backend._audit_model() == 'acp:grok[medium]'
    assert RecordedSeriesAI.get_audit_model(settings) == 'acp:grok[medium]'


def test_grok_adapter_maps_common_operation_schema_to_cli_argument() -> None:
    """Grok 固有処理は共通 schema を固定 CLI option へ写像するだけにする。"""

    base_args = ['--reasoning-effort', 'high', 'agent', 'stdio']
    backend = RecordedSeriesAI._AcpAdapter(
        backend_kind='AcpGrok',
        command='/usr/local/bin/grok',
        args=base_args,
        env={},
        timeout_sec=30,
    )

    candidate_args = backend._operation_args('CandidateSelection')
    episode_args = backend._operation_args('EpisodeLookup')

    assert candidate_args[:1] == ['--json-schema']
    assert candidate_args[2:] == base_args
    assert episode_args[:1] == ['--json-schema']
    assert episode_args[2:] == base_args
    candidate_schema = json.loads(candidate_args[1])
    episode_schema = json.loads(episode_args[1])
    assert candidate_schema['additionalProperties'] is False
    assert set(candidate_schema['required']) == {'choice_id', 'confidence'}
    assert episode_schema['additionalProperties'] is False
    assert set(episode_schema['required']) == {
        'outcome',
        'season_number',
        'episode_number',
        'confidence',
        'rationale_short',
    }
    # 呼び出しごとに新しい引数を構築し、固定 preset 自体は変更しない。
    assert backend._args == base_args


def test_codex_splits_composite_model_and_applies_defaults() -> None:
    """Codex は連結 ID を分解し、未指定時は Luna + Medium を既定にする。"""

    split_settings = RecordedSeriesSettings(
        ai_backend='AcpCodex',
        acp_model='gpt-5.6-luna[medium]',
    )
    assert split_settings.acp_model == 'gpt-5.6-luna'
    assert split_settings.acp_reasoning_effort == 'Medium'

    # 明示 effort は括弧側より優先する。
    preferred = RecordedSeriesSettings(
        ai_backend='AcpCodex',
        acp_model='gpt-5.6-terra[low]',
        acp_reasoning_effort='High',
    )
    assert preferred.acp_model == 'gpt-5.6-terra'
    assert preferred.acp_reasoning_effort == 'High'

    defaults = RecordedSeriesSettings(ai_backend='AcpCodex')
    assert defaults.acp_model == 'gpt-5.6-luna'
    assert defaults.acp_reasoning_effort == 'Medium'
    assert RecordedSeriesAI.get_audit_model(defaults) == 'acp:codex:gpt-5.6-luna[medium]'


def test_konomitv_bs4k_codex_ultra_is_reserved_for_sol_and_fast_mode_is_codex_only() -> None:
    """Ultra は Sol だけに残し、Fast 設定は Codex 以外へ持ち越さない。"""

    non_sol = RecordedSeriesSettings(
        ai_backend='AcpCodex',
        acp_model='gpt-5.6-terra',
        acp_reasoning_effort='Ultra',
        konomitv_bs4k_acp_codex_fast_mode_enabled=True,
    )
    assert non_sol.acp_reasoning_effort == 'Max'
    assert non_sol.konomitv_bs4k_acp_codex_fast_mode_enabled is True

    legacy_non_sol = RecordedSeriesSettings(
        ai_backend='AcpCodex',
        acp_model='gpt-5.6-luna[ultra]',
    )
    assert legacy_non_sol.acp_model == 'gpt-5.6-luna'
    assert legacy_non_sol.acp_reasoning_effort == 'Max'

    sol = RecordedSeriesSettings(
        ai_backend='AcpCodex',
        acp_model='gpt-5.6-sol',
        acp_reasoning_effort='Ultra',
    )
    assert sol.acp_reasoning_effort == 'Ultra'

    grok = RecordedSeriesSettings(
        ai_backend='AcpGrok',
        konomitv_bs4k_acp_codex_fast_mode_enabled=True,
    )
    assert grok.konomitv_bs4k_acp_codex_fast_mode_enabled is False




def test_acp_imported_auth_requires_mode_0600(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """専用 auth.json の mode が緩んだ場合は ACP セッションを開始しない。"""

    profiles_root = tmp_path / 'profiles'
    profile = profiles_root / 'codex'
    profile.mkdir(parents=True)
    auth_path = profile / 'auth.json'
    auth_path.write_text('{}\n', encoding='utf-8')
    os.chmod(auth_path, 0o644)
    monkeypatch.setattr(AcpProfiles, '_ACP_PROFILES_ROOT', profiles_root)

    with pytest.raises(AcpProfiles.AcpProfileError, match='0600 regular file'):
        AcpProfiles.ensure_acp_profile('codex')
