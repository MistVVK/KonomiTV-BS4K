from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import app.config as config_module
import app.routers.CMAnalysisRouter as cm_router_module
import app.utils.HostPath as host_path_module
from app import schemas
from app.config import HostServerSettings
from app.metadata.CMAnalysisPaths import ValidateCMLogoDirectory


def test_cm_logo_directory_uses_runtime_path_only_for_file_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CM共有ロゴはホストパスを保持し、読み書き検証時だけDocker内部へ変換する。"""

    docker_root = tmp_path / 'host-rootfs'
    monkeypatch.setattr('app.utils.GetPlatformEnvironment', lambda: 'Linux-Docker')
    monkeypatch.setattr(host_path_module, 'DOCKER_HOST_ROOT', docker_root)

    runtime_path = ValidateCMLogoDirectory(Path('/mnt/CM-Logos'))

    assert runtime_path == docker_root / 'mnt/CM-Logos'
    assert runtime_path.is_dir()


def test_cm_settings_get_api_returns_host_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CM設定GETはconfig.yaml由来の共有ロゴと除外パスをホスト表現で返す。"""

    docker_root = tmp_path / 'host-rootfs'
    (docker_root / 'mnt/CM-Logos').mkdir(parents=True)
    (docker_root / 'mnt/TV-Record/Temp').mkdir(parents=True)
    monkeypatch.setattr('app.utils.GetPlatformEnvironment', lambda: 'Linux-Docker')
    monkeypatch.setattr(host_path_module, 'DOCKER_HOST_ROOT', docker_root)

    host_settings = HostServerSettings.model_validate(
        {
            **HostServerSettings().model_dump(mode='json'),
            'cm_analysis': {
                'enabled': True,
                'logo_directory': '/mnt/CM-Logos',
                'excluded_directories': [
                    '/mnt/TV-Record/Temp',
                    '/mnt/host-rootfs/archive',
                ],
            },
        },
        context={'bypass_validation': True},
    )
    internal = host_settings.toServerSettings(bypass_validation=True)
    monkeypatch.setattr(cm_router_module, 'Config', lambda: internal)

    response = asyncio.run(cm_router_module.CMAnalysisSettingsAPI())

    assert response.enabled is True
    assert response.logo_directory == '/mnt/CM-Logos'
    assert response.excluded_directories == [
        '/mnt/TV-Record/Temp',
        '/mnt/host-rootfs/archive',
    ]


def test_cm_settings_update_saves_only_host_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CM設定PUTは旧入力を受理してもconfig.yamlへホストパスだけを保存する。"""

    docker_root = tmp_path / 'host-rootfs'
    (docker_root / 'mnt/TV-Record/Temp').mkdir(parents=True)
    monkeypatch.setattr('app.utils.GetPlatformEnvironment', lambda: 'Linux-Docker')
    monkeypatch.setattr(host_path_module, 'DOCKER_HOST_ROOT', docker_root)

    async def RunInline(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(cm_router_module.asyncio, 'to_thread', RunInline)

    config_path = tmp_path / 'config.yaml'
    config_path.write_text('{}\n', encoding='utf-8')
    monkeypatch.setattr(config_module, '_CONFIG_YAML_PATH', config_path)
    monkeypatch.setattr(config_module, '_CONFIG', HostServerSettings().toServerSettings(bypass_validation=True))
    monkeypatch.setattr(cm_router_module, 'Config', config_module.Config)

    saved: list[HostServerSettings] = []

    def FakeSaveAndApply(config: HostServerSettings, *, bypass_validation: bool = True) -> object:
        del bypass_validation
        saved.append(config)
        return config.toServerSettings(bypass_validation=True)

    monkeypatch.setattr(cm_router_module, 'SaveConfigAndApply', FakeSaveAndApply)
    legacy_prefix = str(docker_root)

    asyncio.run(cm_router_module.CMAnalysisSettingsUpdateAPI(
        schemas.CMAnalysisSettingsUpdate(
            enabled=True,
            logo_directory=f'{legacy_prefix}/mnt/CM-Logos',
            excluded_directories=[
                f'{legacy_prefix}/mnt/TV-Record/Temp',
                '/mnt/TV-Record/host-rootfs/Archive',
            ],
        ),
        object(),  # type: ignore[arg-type]
    ))

    assert len(saved) == 1
    cm = saved[0].cm_analysis
    assert cm.enabled is True
    assert cm.logo_directory == Path('/mnt/CM-Logos')
    assert cm.excluded_directories == [
        '/mnt/TV-Record/Temp',
        '/mnt/TV-Record/host-rootfs/Archive',
    ]
