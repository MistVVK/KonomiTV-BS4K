# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient

import app.metadata.ai.KonomiTVBS4KACPCredentials as ACPCredentials
import app.metadata.ai.recorded_series_ai as RecordedSeriesAIModule
from app.routers import AIBackendRouter


def ConfigureCredentialPaths(
    monkeypatch: pytest.MonkeyPatch,
    temporary_directory: Path,
) -> tuple[Path, Path]:
    """固定 host HOME mount と専用 profile をテスト用 directory へ差し替える。

    Args:
        monkeypatch: module 定数をテスト中だけ置き換える pytest fixture。
        temporary_directory: テスト専用の root directory。

    Returns:
        tuple[Path, Path]: host HOME と profile root。
    """

    host_home = temporary_directory / 'host-home'
    profiles_root = temporary_directory / 'profiles'
    host_auth_paths = {
        'codex': host_home / '.codex' / 'auth.json',
        'grok': host_home / '.grok' / 'auth.json',
        'google': host_home / '.config' / 'gcloud' / 'application_default_credentials.json',
    }
    monkeypatch.setattr(ACPCredentials, '_KONOMITV_BS4K_HOST_HOME', host_home)
    monkeypatch.setattr(ACPCredentials, '_KONOMITV_BS4K_HOST_AUTH_PATHS', host_auth_paths)
    monkeypatch.setattr(ACPCredentials, '_KONOMITV_BS4K_ACP_PROFILES_ROOT', profiles_root)
    return host_home, profiles_root


def test_host_auth_paths_use_standard_locations_below_host_home() -> None:
    """追加の Compose 変数なしで、各 CLI の標準資格情報を固定 host HOME から検出する。"""

    assert ACPCredentials._KONOMITV_BS4K_HOST_HOME == Path('/host-home')
    assert ACPCredentials._KONOMITV_BS4K_HOST_AUTH_PATHS == {
        'codex': Path('/host-home/.codex/auth.json'),
        'grok': Path('/host-home/.grok/auth.json'),
        'google': Path('/host-home/.config/gcloud/application_default_credentials.json'),
    }


def WriteCredential(path: Path, value: str) -> None:
    """通常の JSON object 資格情報 fixture を作成する。

    Args:
        path: 作成先。
        value: 内容照合用の非実在 token。

    Returns:
        None
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'token': value}) + '\n', encoding='utf-8')


def CreateAdminApp() -> FastAPI:
    """管理者依存をテスト用に置き換えた FastAPI app を作る。

    Returns:
        FastAPI: AI バックエンド router を登録済みの app。
    """

    async def GetAdminUser() -> object:
        return object()

    app = FastAPI()
    app.include_router(AIBackendRouter.router)
    app.dependency_overrides[AIBackendRouter.GetCurrentAdminUser] = GetAdminUser
    return app


@pytest.mark.parametrize('provider', ['codex', 'grok'])
def test_valid_provider_auth_is_imported_atomically_with_mode_0600(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: ACPCredentials.KonomiTVBS4KACPImportProvider,
) -> None:
    """Codex / Grok の正常な auth.json を専用コピーへ取り込める。"""

    _host_auth_root, profiles_root = ConfigureCredentialPaths(monkeypatch, tmp_path)
    source_path = ACPCredentials._KONOMITV_BS4K_HOST_AUTH_PATHS[provider]
    WriteCredential(source_path, f'{provider}-fixture-token')
    source_before = source_path.read_bytes()
    source_stat_before = source_path.stat()

    imported_at = ACPCredentials.KonomiTVBS4KACPCredentials.importProviderAuth(provider)

    destination_path = profiles_root / provider / 'auth.json'
    marker_path = profiles_root / provider / '.auth-imported.json'
    assert destination_path.read_bytes() == source_before
    assert stat.S_IMODE(destination_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(marker_path.stat().st_mode) == 0o600
    assert source_path.read_bytes() == source_before
    assert source_path.stat().st_mtime_ns == source_stat_before.st_mtime_ns
    status = ACPCredentials.KonomiTVBS4KACPCredentials.getStatus()
    assert getattr(status, f'{provider}_auth_imported') is True
    assert getattr(status, f'{provider}_auth_imported_at') == imported_at


def test_explicit_reimport_replaces_existing_copy_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """明示的な再取り込みだけが、既存コピーを atomic rename で置き換える。"""

    _host_auth_root, profiles_root = ConfigureCredentialPaths(monkeypatch, tmp_path)
    source_path = ACPCredentials._KONOMITV_BS4K_HOST_AUTH_PATHS['codex']
    WriteCredential(source_path, 'old-fixture-token')
    ACPCredentials.KonomiTVBS4KACPCredentials.importProviderAuth('codex')
    destination_path = profiles_root / 'codex' / 'auth.json'
    old_destination = destination_path.read_bytes()
    WriteCredential(source_path, 'new-fixture-token')
    new_source = source_path.read_bytes()
    original_replace = ACPCredentials.os.replace
    observed_auth_replace = False

    def Replace(
        source: str | Path,
        destination: str | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal observed_auth_replace
        observed_destination = (
            destination_path.parent / Path(destination)
            if dst_dir_fd is not None else
            Path(destination)
        )
        observed_source = (
            destination_path.parent / Path(source)
            if src_dir_fd is not None else
            Path(source)
        )
        if observed_destination == destination_path:
            observed_auth_replace = True
            assert destination_path.read_bytes() == old_destination
            assert observed_source.read_bytes() == new_source
            assert stat.S_IMODE(observed_source.stat().st_mode) == 0o600
        original_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(ACPCredentials.os, 'replace', Replace)

    ACPCredentials.KonomiTVBS4KACPCredentials.importProviderAuth('codex')

    assert observed_auth_replace is True
    assert destination_path.read_bytes() == new_source


def test_credential_generation_tracks_import_delete_and_google_adc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """proof 用世代 hash は token refresh では変えず、明示 import ごとに更新する。"""

    ConfigureCredentialPaths(monkeypatch, tmp_path)
    codex_source = ACPCredentials._KONOMITV_BS4K_HOST_AUTH_PATHS['codex']
    google_source = ACPCredentials._KONOMITV_BS4K_HOST_AUTH_PATHS['google']

    assert (
        ACPCredentials.KonomiTVBS4KACPCredentials.getCredentialGeneration(
            'codex'
        )
        == 'missing'
    )
    WriteCredential(codex_source, 'generation-a')
    ACPCredentials.KonomiTVBS4KACPCredentials.importProviderAuth('codex')
    generation_a = (
        ACPCredentials.KonomiTVBS4KACPCredentials.getCredentialGeneration(
            'codex'
        )
    )
    destination_path = ACPCredentials._KONOMITV_BS4K_ACP_PROFILES_ROOT / 'codex' / 'auth.json'
    marker_path = ACPCredentials._KONOMITV_BS4K_ACP_PROFILES_ROOT / 'codex' / '.auth-imported.json'
    assert generation_a == hashlib.sha256(marker_path.read_bytes()).hexdigest()

    # OAuth agent が専用 auth.json の access / refresh token を更新しても、
    # 同じ明示 import 世代の能力証明は維持する。
    WriteCredential(destination_path, 'agent-refreshed-generation-a')
    os.chmod(destination_path, 0o600)
    assert (
        ACPCredentials.KonomiTVBS4KACPCredentials.getCredentialGeneration(
            'codex'
        )
        == generation_a
    )

    WriteCredential(codex_source, 'generation-b')
    ACPCredentials.KonomiTVBS4KACPCredentials.importProviderAuth('codex')
    generation_b = (
        ACPCredentials.KonomiTVBS4KACPCredentials.getCredentialGeneration(
            'codex'
        )
    )
    assert generation_b == hashlib.sha256(marker_path.read_bytes()).hexdigest()
    assert generation_b != generation_a

    ACPCredentials.KonomiTVBS4KACPCredentials.deleteProviderAuth('codex')
    assert (
        ACPCredentials.KonomiTVBS4KACPCredentials.getCredentialGeneration(
            'codex'
        )
        == 'missing'
    )

    WriteCredential(google_source, 'google-generation')
    assert (
        ACPCredentials.KonomiTVBS4KACPCredentials.getCredentialGeneration(
            'google'
        )
        == hashlib.sha256(google_source.read_bytes()).hexdigest()
    )


def test_imported_copy_is_not_overwritten_without_explicit_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """状態取得などの通常処理は更新済みの専用コピーを上書きしない。"""

    ConfigureCredentialPaths(monkeypatch, tmp_path)
    source_path = ACPCredentials._KONOMITV_BS4K_HOST_AUTH_PATHS['grok']
    WriteCredential(source_path, 'host-old-token')
    ACPCredentials.KonomiTVBS4KACPCredentials.importProviderAuth('grok')
    destination_path = ACPCredentials._KONOMITV_BS4K_ACP_PROFILES_ROOT / 'grok' / 'auth.json'
    WriteCredential(destination_path, 'container-refreshed-token')
    os.chmod(destination_path, 0o600)
    refreshed_copy = destination_path.read_bytes()
    WriteCredential(source_path, 'host-relogin-token')

    status = ACPCredentials.KonomiTVBS4KACPCredentials.getStatus()

    assert status.grok_auth_imported is True
    assert destination_path.read_bytes() == refreshed_copy


def test_delete_removes_only_konomitv_bs4k_copy_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """削除は専用コピーだけを対象とし、host HOME mount を変更しない。"""

    ConfigureCredentialPaths(monkeypatch, tmp_path)
    source_path = ACPCredentials._KONOMITV_BS4K_HOST_AUTH_PATHS['codex']
    WriteCredential(source_path, 'host-token-that-must-remain')
    source_before = source_path.read_bytes()
    ACPCredentials.KonomiTVBS4KACPCredentials.importProviderAuth('codex')

    ACPCredentials.KonomiTVBS4KACPCredentials.deleteProviderAuth('codex')
    ACPCredentials.KonomiTVBS4KACPCredentials.deleteProviderAuth('codex')

    profile_dir = ACPCredentials._KONOMITV_BS4K_ACP_PROFILES_ROOT / 'codex'
    assert (profile_dir / 'auth.json').exists() is False
    assert (profile_dir / '.auth-imported.json').exists() is False
    assert source_path.read_bytes() == source_before


def test_delete_unlinks_legacy_auth_symlink_without_touching_its_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """旧 Symlink 形式が残っていてもリンク先を追跡せず directory entry だけを削除する。"""

    ConfigureCredentialPaths(monkeypatch, tmp_path)
    legacy_target = tmp_path / 'legacy-host-auth.json'
    WriteCredential(legacy_target, 'legacy-host-token')
    legacy_before = legacy_target.read_bytes()
    profile_dir = ACPCredentials._KONOMITV_BS4K_ACP_PROFILES_ROOT / 'codex'
    profile_dir.mkdir(parents=True)
    (profile_dir / 'auth.json').symlink_to(legacy_target)

    ACPCredentials.KonomiTVBS4KACPCredentials.deleteProviderAuth('codex')

    assert (profile_dir / 'auth.json').is_symlink() is False
    assert (profile_dir / 'auth.json').exists() is False
    assert legacy_target.read_bytes() == legacy_before


def test_delete_rejects_symlinked_profile_root_without_touching_its_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """専用 profile root が symlink の場合はリンク先の資格情報を削除しない。"""

    ConfigureCredentialPaths(monkeypatch, tmp_path)
    profiles_root = ACPCredentials._KONOMITV_BS4K_ACP_PROFILES_ROOT
    outside_directory = tmp_path / 'outside-profile-root'
    outside_profile = outside_directory / 'codex'
    outside_profile.mkdir(parents=True)
    outside_auth = outside_profile / 'auth.json'
    outside_marker = outside_profile / '.auth-imported.json'
    WriteCredential(outside_auth, 'outside-auth-must-remain')
    outside_marker.write_text('{"imported_at":"2026-07-26T00:00:00+00:00"}', encoding='utf-8')
    profiles_root.symlink_to(outside_directory, target_is_directory=True)

    with pytest.raises(ACPCredentials.KonomiTVBS4KACPCredentialError) as error:
        ACPCredentials.KonomiTVBS4KACPCredentials.deleteProviderAuth('codex')

    assert error.value.code == 'CredentialStorageFailed'
    assert outside_auth.exists() is True
    assert outside_marker.exists() is True
    status = ACPCredentials.KonomiTVBS4KACPCredentials.getStatus()
    assert status.codex_auth_imported is False
    assert status.codex_auth_imported_at is None


def test_import_rejects_symlinked_profile_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """専用 profile root が symlink の場合はリンク先へ資格情報を書き込まない。"""

    ConfigureCredentialPaths(monkeypatch, tmp_path)
    source_path = ACPCredentials._KONOMITV_BS4K_HOST_AUTH_PATHS['codex']
    WriteCredential(source_path, 'profile-root-symlink-fixture')
    profiles_root = ACPCredentials._KONOMITV_BS4K_ACP_PROFILES_ROOT
    outside_directory = tmp_path / 'outside-profile-root'
    outside_directory.mkdir()
    profiles_root.symlink_to(outside_directory, target_is_directory=True)

    with pytest.raises(ACPCredentials.KonomiTVBS4KACPCredentialError) as error:
        ACPCredentials.KonomiTVBS4KACPCredentials.importProviderAuth('codex')

    assert error.value.code == 'CredentialStorageFailed'
    assert (outside_directory / 'codex' / 'auth.json').exists() is False


@pytest.mark.parametrize(
    'invalid_kind',
    ['symlink', 'directory', 'fifo', 'invalid-json', 'json-array', 'oversized', 'device'],
)
def test_import_rejects_unsafe_or_invalid_input_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    """symlink・非通常ファイル・不正 JSON・過大ファイルを内容非公開で拒否する。"""

    ConfigureCredentialPaths(monkeypatch, tmp_path)
    source_path = ACPCredentials._KONOMITV_BS4K_HOST_AUTH_PATHS['codex']
    source_path.parent.mkdir(parents=True, exist_ok=True)
    secret = 'fixture-secret-must-not-appear-in-errors'
    if invalid_kind == 'symlink':
        symlink_target = tmp_path / 'symlink-target.json'
        WriteCredential(symlink_target, secret)
        source_path.symlink_to(symlink_target)
    elif invalid_kind == 'directory':
        source_path.mkdir()
    elif invalid_kind == 'fifo':
        os.mkfifo(source_path)
    elif invalid_kind == 'invalid-json':
        source_path.write_text(f'not-json-{secret}', encoding='utf-8')
    elif invalid_kind == 'json-array':
        source_path.write_text(json.dumps([secret]), encoding='utf-8')
    elif invalid_kind == 'oversized':
        source_path.write_text(
            json.dumps({'token': secret, 'padding': 'x' * ACPCredentials._MAX_CREDENTIAL_FILE_BYTES}),
            encoding='utf-8',
        )
    else:
        monkeypatch.setitem(ACPCredentials._KONOMITV_BS4K_HOST_AUTH_PATHS, 'codex', Path('/dev/null'))

    with pytest.raises(ACPCredentials.KonomiTVBS4KACPCredentialError) as error:
        ACPCredentials.KonomiTVBS4KACPCredentials.importProviderAuth('codex')

    assert error.value.code in {'InvalidHostAuth', 'HostAuthUnavailable'}
    assert secret not in str(error.value)
    assert (ACPCredentials._KONOMITV_BS4K_ACP_PROFILES_ROOT / 'codex' / 'auth.json').exists() is False


def test_google_adc_status_requires_a_readable_json_object(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Google ADC の存在時と不在・破損時をコピーせず正しく判定する。"""

    ConfigureCredentialPaths(monkeypatch, tmp_path)
    google_path = ACPCredentials._KONOMITV_BS4K_HOST_AUTH_PATHS['google']
    assert ACPCredentials.KonomiTVBS4KACPCredentials.getStatus().google_adc_available is False

    WriteCredential(google_path, 'google-fixture-token')
    assert ACPCredentials.KonomiTVBS4KACPCredentials.getStatus().google_adc_available is True
    assert (
        ACPCredentials._KONOMITV_BS4K_ACP_PROFILES_ROOT /
        'codex' /
        'application_default_credentials.json'
    ).exists() is False

    google_path.write_text('broken-json', encoding='utf-8')
    assert ACPCredentials.KonomiTVBS4KACPCredentials.getStatus().google_adc_available is False


def test_import_rolls_back_auth_when_marker_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """marker 書込失敗時は新 auth を残さず、旧 auth/marker 世代へ戻す。"""

    _host_auth_root, profiles_root = ConfigureCredentialPaths(monkeypatch, tmp_path)
    source_path = ACPCredentials._KONOMITV_BS4K_HOST_AUTH_PATHS['codex']
    WriteCredential(source_path, 'old-token')
    ACPCredentials.KonomiTVBS4KACPCredentials.importProviderAuth('codex')
    destination_path = profiles_root / 'codex' / 'auth.json'
    marker_path = profiles_root / 'codex' / '.auth-imported.json'
    previous_auth = destination_path.read_bytes()
    previous_marker = marker_path.read_bytes()

    WriteCredential(source_path, 'new-token-should-not-remain')
    original_write_atomic = ACPCredentials.KonomiTVBS4KACPCredentials._writeAtomic
    write_count = {'value': 0}

    def FailingSecondWrite(cls, destination_path_arg, content):
        write_count['value'] += 1
        # 1 回目: 新 auth、2 回目: 新 marker を失敗させ、以降の rollback 書込は通す。
        if write_count['value'] == 2:
            raise OSError('simulated marker write failure')
        return original_write_atomic(destination_path_arg, content)

    monkeypatch.setattr(
        ACPCredentials.KonomiTVBS4KACPCredentials,
        '_writeAtomic',
        classmethod(FailingSecondWrite),
    )

    with pytest.raises(ACPCredentials.KonomiTVBS4KACPCredentialError) as error:
        ACPCredentials.KonomiTVBS4KACPCredentials.importProviderAuth('codex')

    assert error.value.code == 'CredentialStorageFailed'
    assert destination_path.read_bytes() == previous_auth
    assert marker_path.read_bytes() == previous_marker
    assert b'new-token-should-not-remain' not in destination_path.read_bytes()


def test_import_rolls_back_when_auth_directory_fsync_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """auth replace 後の directory fsync 失敗でも新 auth を残さず旧世代へ戻す。"""

    import stat

    _host_auth_root, profiles_root = ConfigureCredentialPaths(monkeypatch, tmp_path)
    source_path = ACPCredentials._KONOMITV_BS4K_HOST_AUTH_PATHS['codex']
    WriteCredential(source_path, 'old-token')
    ACPCredentials.KonomiTVBS4KACPCredentials.importProviderAuth('codex')
    destination_path = profiles_root / 'codex' / 'auth.json'
    marker_path = profiles_root / 'codex' / '.auth-imported.json'
    previous_auth = destination_path.read_bytes()
    previous_marker = marker_path.read_bytes()

    WriteCredential(source_path, 'new-token-should-not-remain')
    real_fsync = os.fsync
    directory_fsync_failures = {'remaining': 1}

    def FailDirectoryFsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        if stat.S_ISDIR(mode) and directory_fsync_failures['remaining'] > 0:
            directory_fsync_failures['remaining'] -= 1
            raise OSError('simulated directory fsync failure after auth replace')
        real_fsync(fd)

    monkeypatch.setattr(os, 'fsync', FailDirectoryFsync)

    with pytest.raises(ACPCredentials.KonomiTVBS4KACPCredentialError) as error:
        ACPCredentials.KonomiTVBS4KACPCredentials.importProviderAuth('codex')

    assert error.value.code == 'CredentialStorageFailed'
    assert destination_path.read_bytes() == previous_auth
    assert marker_path.read_bytes() == previous_marker
    assert b'new-token-should-not-remain' not in destination_path.read_bytes()


def test_write_atomic_closes_directory_fd_when_temp_unlink_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """一時ファイル削除が失敗しても directory FD を閉じる。"""

    _host_auth_root, profiles_root = ConfigureCredentialPaths(monkeypatch, tmp_path)
    profile_dir = profiles_root / 'codex'
    profile_dir.mkdir(parents=True, exist_ok=True)
    destination_path = profile_dir / 'auth.json'
    before_fd_count = len(os.listdir('/proc/self/fd'))
    real_unlink = os.unlink

    def UnlinkFails(path, *args, dir_fd=None, **kwargs):
        if isinstance(path, str) and path.startswith('.auth.json.'):
            raise PermissionError('simulated unlink failure')
        return real_unlink(path, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(os, 'unlink', UnlinkFails)
    # replace 後の finally で unlink が失敗しても例外は外へ出してよい。FD は閉じること。
    try:
        ACPCredentials.KonomiTVBS4KACPCredentials._writeAtomic(
            destination_path,
            b'{"token":"fd-leak-fixture"}\n',
        )
    except PermissionError:
        pass

    after_fd_count = len(os.listdir('/proc/self/fd'))
    assert after_fd_count <= before_fd_count + 1


def test_admin_api_imports_gets_and_deletes_auth_without_returning_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """管理 API は no-store の状態だけを返し、明示 import / delete を実行する。"""

    ConfigureCredentialPaths(monkeypatch, tmp_path)
    source_path = ACPCredentials._KONOMITV_BS4K_HOST_AUTH_PATHS['codex']
    secret = 'api-fixture-secret-that-must-not-be-returned'
    WriteCredential(source_path, secret)
    source_before = source_path.read_bytes()
    app = CreateAdminApp()

    async def Run():
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            initial_response = await client.get('/api/ai-backends/acp-credentials')
            import_response = await client.post('/api/ai-backends/acp-credentials/codex/import')
            delete_response = await client.delete('/api/ai-backends/acp-credentials/codex')
        return initial_response, import_response, delete_response

    initial_response, import_response, delete_response = asyncio.run(Run())

    assert initial_response.status_code == 200
    assert initial_response.headers['cache-control'] == 'no-store'
    assert initial_response.json()['codex_host_auth_available'] is True
    assert initial_response.json()['codex_auth_imported'] is False
    assert import_response.status_code == 200
    assert import_response.headers['cache-control'] == 'no-store'
    assert import_response.json()['codex_auth_imported'] is True
    assert import_response.json()['codex_auth_imported_at'] is not None
    assert delete_response.status_code == 200
    assert delete_response.headers['cache-control'] == 'no-store'
    assert delete_response.json()['codex_auth_imported'] is False
    assert secret not in initial_response.text
    assert secret not in import_response.text
    assert secret not in delete_response.text
    assert source_path.read_bytes() == source_before


def test_admin_api_rejects_only_the_in_use_provider_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Codex 実行中は Codex import だけを即時拒否し、Grok import は巻き添えにしない。"""

    ConfigureCredentialPaths(monkeypatch, tmp_path)
    WriteCredential(
        ACPCredentials._KONOMITV_BS4K_HOST_AUTH_PATHS['codex'],
        'busy-codex-fixture',
    )
    WriteCredential(
        ACPCredentials._KONOMITV_BS4K_HOST_AUTH_PATHS['grok'],
        'independent-grok-fixture',
    )
    credential_locks = {
        'codex': asyncio.Lock(),
        'grok': asyncio.Lock(),
    }
    monkeypatch.setattr(
        RecordedSeriesAIModule,
        'ACP_CREDENTIAL_OPERATION_LOCKS',
        credential_locks,
    )
    app = CreateAdminApp()

    async def Run():
        await credential_locks['codex'].acquire()
        try:
            async with HTTPXAsyncClient(
                transport=ASGITransport(app=app),
                base_url='http://test',
            ) as client:
                codex_response = await asyncio.wait_for(
                    client.post(
                        '/api/ai-backends/acp-credentials/codex/import'
                    ),
                    timeout=0.5,
                )
                grok_response = await client.post(
                    '/api/ai-backends/acp-credentials/grok/import'
                )
                status_response = await client.get(
                    '/api/ai-backends/acp-credentials'
                )
        finally:
            credential_locks['codex'].release()
        return codex_response, grok_response, status_response

    codex_response, grok_response, status_response = asyncio.run(Run())

    assert codex_response.status_code == 409
    assert codex_response.json() == {
        'detail': 'The ACP authentication is currently in use by an AI operation.',
    }
    assert grok_response.status_code == 200
    assert grok_response.json()['grok_auth_imported'] is True
    assert status_response.json()['codex_auth_in_use'] is True
    assert status_response.json()['grok_auth_in_use'] is False


def test_admin_api_returns_sanitized_error_for_invalid_host_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """不正 JSON の内容・path・例外詳細を import API response へ含めない。"""

    ConfigureCredentialPaths(monkeypatch, tmp_path)
    source_path = ACPCredentials._KONOMITV_BS4K_HOST_AUTH_PATHS['grok']
    source_path.parent.mkdir(parents=True)
    secret = 'broken-api-secret-that-must-not-be-returned'
    source_path.write_text(f'not-json-{secret}', encoding='utf-8')
    app = CreateAdminApp()

    async def Run():
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            return await client.post('/api/ai-backends/acp-credentials/grok/import')

    response = asyncio.run(Run())

    assert response.status_code == 422
    assert response.headers['cache-control'] == 'no-store'
    assert response.json() == {'detail': 'The host authentication file is invalid.'}
    assert secret not in response.text
    assert str(source_path) not in response.text


def test_acp_credential_api_requires_admin_authentication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """状態取得・import・delete は既存の管理者認証を迂回しない。"""

    ConfigureCredentialPaths(monkeypatch, tmp_path)
    app = FastAPI()
    app.include_router(AIBackendRouter.router)

    async def Run():
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            get_response = await client.get('/api/ai-backends/acp-credentials')
            import_response = await client.post('/api/ai-backends/acp-credentials/codex/import')
            delete_response = await client.delete('/api/ai-backends/acp-credentials/codex')
        return get_response, import_response, delete_response

    get_response, import_response, delete_response = asyncio.run(Run())

    assert get_response.status_code == 401
    assert import_response.status_code == 401
    assert delete_response.status_code == 401
