from __future__ import annotations

import asyncio
import json
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from ruamel.yaml import YAML
from starlette.requests import Request

import app.config as config_module
import app.routers.SettingsRouter as settings_router_module
import app.utils.HostPath as host_path_module
from app.config import HostServerSettings


def _PrepareDockerPaths(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """
    対象9項目のホストパスに対応する一時Dockerファイルシステムを作る。

    Args:
        tmp_path (Path): pytestの一時ディレクトリ。

    Returns:
        tuple[Path, dict[str, str]]: Docker内部ルートとホスト表現の対象パス。
    """

    docker_root = tmp_path / 'host-rootfs'
    host_paths = {
        'recorded_folder': '/mnt/TV-Record',
        'exclude_scan_path': '/mnt/TV-Record/host-rootfs/Temp',
        'recorded_fmp4_cache_folder': '/mnt/KonomiTV-Cache',
        'upload_folder': '/mnt/TV-Capture',
        'cm_logo_directory': '/mnt/CM-Logos',
        'cm_excluded_directory': '/mnt/TV-Record/CM-Exclude',
        'debug_mode_ts_path': '/mnt/Test/debug.ts',
        'server_certificate': '/mnt/Certificates/server.crt',
        'server_private_key': '/mnt/Certificates/server.key',
        'compatibility_certificate': '/mnt/Certificates/compatibility.crt',
        'compatibility_private_key': '/mnt/Certificates/compatibility.key',
    }
    for key in (
        'recorded_folder',
        'exclude_scan_path',
        'recorded_fmp4_cache_folder',
        'upload_folder',
        'cm_logo_directory',
        'cm_excluded_directory',
    ):
        (docker_root / host_paths[key].removeprefix('/')).mkdir(parents=True, exist_ok=True)
    for key in (
        'debug_mode_ts_path',
        'server_certificate',
        'server_private_key',
        'compatibility_certificate',
        'compatibility_private_key',
    ):
        internal_path = docker_root / host_paths[key].removeprefix('/')
        internal_path.parent.mkdir(parents=True, exist_ok=True)
        internal_path.write_text(key, encoding='utf-8')
    return docker_root, host_paths


def _BuildHostSettings(host_paths: dict[str, str], *, legacy_prefix: str = '') -> HostServerSettings:
    """
    対象9項目をすべて設定した外部向けモデルを作る。

    Args:
        host_paths (dict[str, str]): 項目名からホスト絶対パスへの対応。
        legacy_prefix (str): 旧形式入力を再現する先頭接頭辞。

    Returns:
        HostServerSettings: API・保存用の外部設定。
    """

    data: dict[str, Any] = HostServerSettings().model_dump(mode='json')
    data['server'].update({
        'https_mode': 'certificate',
        'custom_https_certificate': legacy_prefix + host_paths['server_certificate'],
        'custom_https_private_key': legacy_prefix + host_paths['server_private_key'],
    })
    data['compatibility_api'].update({
        'enabled': True,
        'https_mode': 'certificate',
        'custom_https_certificate': legacy_prefix + host_paths['compatibility_certificate'],
        'custom_https_private_key': legacy_prefix + host_paths['compatibility_private_key'],
    })
    data['tv']['debug_mode_ts_path'] = legacy_prefix + host_paths['debug_mode_ts_path']
    data['video'].update({
        'recorded_folders': [legacy_prefix + host_paths['recorded_folder']],
        'exclude_scan_paths': [legacy_prefix + host_paths['exclude_scan_path']],
        'recorded_fmp4_cache_folder': legacy_prefix + host_paths['recorded_fmp4_cache_folder'],
    })
    data['capture']['upload_folders'] = [legacy_prefix + host_paths['upload_folder']]
    data['cm_analysis'].update({
        'enabled': True,
        'logo_directory': legacy_prefix + host_paths['cm_logo_directory'],
        'excluded_directories': [legacy_prefix + host_paths['cm_excluded_directory']],
    })
    return HostServerSettings.model_validate(data, context={'bypass_validation': True})


def _AssertHostSettingsPaths(settings: HostServerSettings, host_paths: dict[str, str]) -> None:
    """
    外部設定の対象9項目がすべてホスト表現であることを確認する。

    Args:
        settings (HostServerSettings): 検証対象の外部設定。
        host_paths (dict[str, str]): 期待するホスト絶対パス。

    Returns:
        None
    """

    assert settings.video.recorded_folders == [Path(host_paths['recorded_folder'])]
    assert settings.video.exclude_scan_paths == [host_paths['exclude_scan_path']]
    assert settings.video.recorded_fmp4_cache_folder == Path(host_paths['recorded_fmp4_cache_folder'])
    assert settings.capture.upload_folders == [Path(host_paths['upload_folder'])]
    assert settings.cm_analysis.logo_directory == Path(host_paths['cm_logo_directory'])
    assert settings.cm_analysis.excluded_directories == [host_paths['cm_excluded_directory']]
    assert settings.tv.debug_mode_ts_path == Path(host_paths['debug_mode_ts_path'])
    assert settings.server.custom_https_certificate == Path(host_paths['server_certificate'])
    assert settings.server.custom_https_private_key == Path(host_paths['server_private_key'])
    assert settings.compatibility_api.custom_https_certificate == Path(host_paths['compatibility_certificate'])
    assert settings.compatibility_api.custom_https_private_key == Path(host_paths['compatibility_private_key'])
    serialized_paths = [
        *settings.video.recorded_folders,
        *settings.video.exclude_scan_paths,
        settings.video.recorded_fmp4_cache_folder,
        *settings.capture.upload_folders,
        settings.cm_analysis.logo_directory,
        *settings.cm_analysis.excluded_directories,
        settings.tv.debug_mode_ts_path,
        settings.server.custom_https_certificate,
        settings.server.custom_https_private_key,
        settings.compatibility_api.custom_https_certificate,
        settings.compatibility_api.custom_https_private_key,
    ]
    assert all(
        path is None or str(path).startswith('/host-rootfs') is False
        for path in serialized_paths
    )


def test_all_server_paths_round_trip_between_host_and_docker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """対象9項目をホスト表現からDocker内部表現へ1回だけ変換し、元へ戻す。"""

    docker_root, host_paths = _PrepareDockerPaths(tmp_path)
    monkeypatch.setattr('app.utils.GetPlatformEnvironment', lambda: 'Linux-Docker')
    monkeypatch.setattr(host_path_module, 'DOCKER_HOST_ROOT', docker_root)
    host_settings = _BuildHostSettings(host_paths)

    internal_settings = host_settings.toServerSettings(bypass_validation=True)

    assert internal_settings.video.recorded_folders == [docker_root / 'mnt/TV-Record']
    assert internal_settings.video.exclude_scan_paths == [str(docker_root / 'mnt/TV-Record/host-rootfs/Temp')]
    assert internal_settings.video.recorded_fmp4_cache_folder == docker_root / 'mnt/KonomiTV-Cache'
    assert internal_settings.capture.upload_folders == [docker_root / 'mnt/TV-Capture']
    assert internal_settings.cm_analysis.logo_directory == docker_root / 'mnt/CM-Logos'
    assert internal_settings.cm_analysis.excluded_directories == [
        str(docker_root / 'mnt/TV-Record/CM-Exclude'),
    ]
    assert internal_settings.tv.debug_mode_ts_path == docker_root / 'mnt/Test/debug.ts'
    assert internal_settings.server.custom_https_certificate == docker_root / 'mnt/Certificates/server.crt'
    assert internal_settings.server.custom_https_private_key == docker_root / 'mnt/Certificates/server.key'
    assert (
        internal_settings.compatibility_api.custom_https_certificate
        == docker_root / 'mnt/Certificates/compatibility.crt'
    )
    assert (
        internal_settings.compatibility_api.custom_https_private_key
        == docker_root / 'mnt/Certificates/compatibility.key'
    )
    _AssertHostSettingsPaths(HostServerSettings.fromServerSettings(internal_settings), host_paths)


def test_legacy_prefixed_input_is_normalized_before_save(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """旧接頭辞付き入力を受理しても、config.yamlにはホストパスだけを保存する。"""

    docker_root, host_paths = _PrepareDockerPaths(tmp_path)
    monkeypatch.setattr('app.utils.GetPlatformEnvironment', lambda: 'Linux-Docker')
    monkeypatch.setattr(host_path_module, 'DOCKER_HOST_ROOT', docker_root)
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('{}\n', encoding='utf-8')
    monkeypatch.setattr(config_module, '_CONFIG_YAML_PATH', config_path)
    host_settings = _BuildHostSettings(host_paths, legacy_prefix=str(docker_root))

    _AssertHostSettingsPaths(host_settings, host_paths)
    internal_settings = host_settings.toServerSettings(bypass_validation=True)
    config_module.SaveConfig(host_settings)
    saved_config = YAML().load(config_path.read_text(encoding='utf-8'))

    assert str(internal_settings.video.recorded_folders[0]).count(str(docker_root)) == 1
    assert saved_config['video']['recorded_folders'] == [host_paths['recorded_folder']]
    assert saved_config['video']['exclude_scan_paths'] == [host_paths['exclude_scan_path']]
    assert saved_config['video']['recorded_fmp4_cache_folder'] == host_paths['recorded_fmp4_cache_folder']
    assert saved_config['capture']['upload_folders'] == [host_paths['upload_folder']]
    assert saved_config['cm_analysis']['logo_directory'] == host_paths['cm_logo_directory']
    assert saved_config['cm_analysis']['excluded_directories'] == [host_paths['cm_excluded_directory']]
    assert saved_config['tv']['debug_mode_ts_path'] == host_paths['debug_mode_ts_path']
    assert saved_config['server']['custom_https_certificate'] == host_paths['server_certificate']
    assert saved_config['server']['custom_https_private_key'] == host_paths['server_private_key']
    assert (
        saved_config['compatibility_api']['custom_https_certificate']
        == host_paths['compatibility_certificate']
    )
    assert (
        saved_config['compatibility_api']['custom_https_private_key']
        == host_paths['compatibility_private_key']
    )


def test_load_config_keeps_yaml_host_paths_and_builds_internal_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """LoadConfigはホスト表現のYAMLから内部設定だけを生成する。"""

    docker_root, host_paths = _PrepareDockerPaths(tmp_path)
    monkeypatch.setattr('app.utils.GetPlatformEnvironment', lambda: 'Linux-Docker')
    monkeypatch.setattr(host_path_module, 'DOCKER_HOST_ROOT', docker_root)
    config_path = tmp_path / 'config.yaml'
    config_yaml = StringIO()
    YAML().dump(_BuildHostSettings(host_paths).model_dump(mode='json'), config_yaml)
    config_path.write_text(config_yaml.getvalue(), encoding='utf-8')
    monkeypatch.setattr(config_module, '_CONFIG_YAML_PATH', config_path)
    monkeypatch.setattr(config_module, '_CONFIG', None)

    internal_settings = config_module.LoadConfig(bypass_validation=True)

    assert internal_settings.video.recorded_folders == [docker_root / 'mnt/TV-Record']
    assert HostServerSettings.fromServerSettings(internal_settings).video.recorded_folders == [
        Path(host_paths['recorded_folder']),
    ]
    loaded_yaml = YAML().load(config_path.read_text(encoding='utf-8'))
    assert loaded_yaml['video']['recorded_folders'] == [host_paths['recorded_folder']]
    assert loaded_yaml['video']['exclude_scan_paths'] == [host_paths['exclude_scan_path']]


def test_server_settings_get_api_returns_only_host_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Docker内部設定からのGETレスポンスは対象9項目をすべてホスト表現で返す。"""

    docker_root, host_paths = _PrepareDockerPaths(tmp_path)
    monkeypatch.setattr('app.utils.GetPlatformEnvironment', lambda: 'Linux-Docker')
    monkeypatch.setattr(host_path_module, 'DOCKER_HOST_ROOT', docker_root)
    internal_settings = _BuildHostSettings(host_paths).toServerSettings(bypass_validation=True)
    monkeypatch.setattr(settings_router_module, 'Config', lambda: internal_settings)

    response = asyncio.run(settings_router_module.ServerSettingsAPI())

    _AssertHostSettingsPaths(response, host_paths)


def test_server_settings_put_api_validates_host_paths_before_saving(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PUTはホストパスを内部パスで検証してから、外部モデルのまま保存へ渡す。"""

    docker_root, host_paths = _PrepareDockerPaths(tmp_path)
    monkeypatch.setattr('app.utils.GetPlatformEnvironment', lambda: 'Linux-Docker')
    monkeypatch.setattr(host_path_module, 'DOCKER_HOST_ROOT', docker_root)
    host_settings = _BuildHostSettings(host_paths)
    original_to_server_settings = HostServerSettings.toServerSettings
    monkeypatch.setattr(
        HostServerSettings,
        'toServerSettings',
        lambda self, bypass_validation=False: original_to_server_settings(self, bypass_validation=True),
    )
    saved_settings: list[HostServerSettings] = []
    monkeypatch.setattr(settings_router_module, 'SaveConfig', saved_settings.append)

    asyncio.run(settings_router_module.ServerSettingsUpdateAPI(host_settings, object()))  # type: ignore[arg-type]

    assert saved_settings == [host_settings]


def test_server_settings_put_error_does_not_expose_internal_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """二重接頭辞を拒否する422本文へ内部接頭辞や入力値を転載しない。"""

    monkeypatch.setattr('app.utils.GetPlatformEnvironment', lambda: 'Linux-Docker')
    settings = HostServerSettings()
    settings.video.recorded_folders = [Path('/host-rootfs/host-rootfs/mnt/TV-Record')]

    with pytest.raises(HTTPException) as error:
        asyncio.run(settings_router_module.ServerSettingsUpdateAPI(settings, object()))  # type: ignore[arg-type]

    assert error.value.status_code == 422
    assert '/host-rootfs' not in str(error.value.detail)


def test_host_server_settings_rejects_double_prefix_during_model_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部モデルは二重接頭辞を内部変換まで保持せず、構築時点で拒否する。"""

    monkeypatch.setattr('app.utils.GetPlatformEnvironment', lambda: 'Linux-Docker')

    with pytest.raises(config_module.ValidationError) as error:
        HostServerSettings.model_validate(
            {
                'video': {
                    'recorded_folders': ['/host-rootfs/host-rootfs/mnt/TV-Record'],
                },
            },
            context={'bypass_validation': True},
        )

    sanitized_errors = settings_router_module._SanitizeValidationErrors(error.value)
    assert sanitized_errors[0]['loc'] == []
    assert '/host-rootfs' not in str(sanitized_errors)


def test_server_settings_put_validation_error_keeps_field_location_without_internal_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """内部の実在検証に失敗したPUTは、入力値を隠したまま該当項目を返す。"""

    docker_root = tmp_path / 'host-rootfs'
    docker_root.mkdir()
    monkeypatch.setattr('app.utils.GetPlatformEnvironment', lambda: 'Linux-Docker')
    monkeypatch.setattr(host_path_module, 'DOCKER_HOST_ROOT', docker_root)
    settings = HostServerSettings.model_validate(
        {
            'video': {
                'recorded_folders': ['/mnt/does-not-exist'],
            },
        },
        context={'bypass_validation': True},
    )
    original_to_server_settings = HostServerSettings.toServerSettings
    monkeypatch.setattr(
        HostServerSettings,
        'toServerSettings',
        lambda self, bypass_validation=False: original_to_server_settings(self, bypass_validation=True),
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(settings_router_module.ServerSettingsUpdateAPI(settings, object()))  # type: ignore[arg-type]

    assert error.value.status_code == 422
    assert isinstance(error.value.detail, list)
    assert error.value.detail[0]['loc'] == ['body', 'video', 'recorded_folders', 0]
    assert error.value.detail[0]['msg'] == 'Path does not point to a directory'
    assert '/host-rootfs' not in str(error.value.detail)


def test_server_settings_body_validation_error_omits_input_value() -> None:
    """FastAPIのBody型エラーから旧内部パスを含むinput値を除く。"""

    request = Request({
        'type': 'http',
        'method': 'PUT',
        'scheme': 'https',
        'path': '/api/settings/server',
        'raw_path': b'/api/settings/server',
        'query_string': b'',
        'headers': [],
        'client': None,
        'server': ('testserver', 443),
    })
    exception = RequestValidationError([
        {
            'type': 'list_type',
            'loc': ('body', 'video', 'recorded_folders'),
            'msg': 'Input should be a valid list',
            'input': '/host-rootfs/mnt/TV-Record',
        },
    ])

    response = asyncio.run(
        settings_router_module.HostPathRequestValidationErrorHandler(request, exception),
    )
    response_body = json.loads(response.body)

    assert response.status_code == 422
    assert 'input' not in response_body['detail'][0]
    assert '/host-rootfs' not in response.body.decode('utf-8')


def test_non_docker_server_paths_remain_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """非Docker環境では外部・内部設定のパス表現を変えない。"""

    recorded_folder = tmp_path / 'recorded'
    recorded_folder.mkdir()
    host_settings = HostServerSettings.model_validate(
        {'video': {'recorded_folders': [str(recorded_folder)]}},
        context={'bypass_validation': True},
    )
    monkeypatch.setattr('app.utils.GetPlatformEnvironment', lambda: 'Linux')

    internal_settings = host_settings.toServerSettings(bypass_validation=True)

    assert internal_settings.video.recorded_folders == [recorded_folder]
    assert HostServerSettings.fromServerSettings(internal_settings).video.recorded_folders == [recorded_folder]


def test_empty_exclude_scan_paths_keep_existing_ignore_behavior() -> None:
    """WebUIやconfig.yamlの未入力行は、従来どおり録画スキャン除外から取り除く。"""

    settings = HostServerSettings.model_validate({
        'video': {
            'exclude_scan_paths': [
                '',
                '   ',
                '  /mnt/TV-Record/Temp  ',
            ],
        },
    }, context={'bypass_validation': True})

    assert settings.video.exclude_scan_paths == ['/mnt/TV-Record/Temp']
