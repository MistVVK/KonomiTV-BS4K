import re
from pathlib import Path
from typing import Any, cast

import pytest
import ruamel.yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
# docker-compose.yaml は .gitignore 対象のローカル専用。リポジトリ正本は example のみ。
COMPOSE_FILENAMES = ['docker-compose.example.yaml']
ACP_AUTH_OVERRIDE_EXPECTATIONS = {
    'docker-compose.acp-codex-auth.yaml': (
        '${KONOMITV_BS4K_CODEX_AUTH_FILE:',
        '/run/konomitv-bs4k-host-auth/codex/auth.json',
    ),
    'docker-compose.acp-grok-auth.yaml': (
        '${KONOMITV_BS4K_GROK_AUTH_FILE:',
        '/run/konomitv-bs4k-host-auth/grok/auth.json',
    ),
    'docker-compose.acp-google-adc.yaml': (
        '${KONOMITV_BS4K_GOOGLE_ADC_FILE:',
        '/run/konomitv-bs4k-host-auth/google/application_default_credentials.json',
    ),
}


def _load_compose_service(compose_filename: str) -> dict[str, Any]:
    """
    Compose ファイルから konomitv サービス定義を読み込む。

    Args:
        compose_filename (str): リポジトリ直下の Compose ファイル名

    Returns:
        dict[str, Any]: konomitv サービス定義
    """

    yaml = ruamel.yaml.YAML(typ='safe')
    compose = cast(dict[str, Any], yaml.load(REPOSITORY_ROOT.joinpath(compose_filename).read_text()))
    return cast(dict[str, Any], compose['services']['konomitv'])


def test_final_image_runs_as_non_root_user() -> None:
    """
    final Docker stage が専用 USER を ENTRYPOINT より前に設定していることを検証する。

    Returns:
        None
    """

    dockerfile_lines = (REPOSITORY_ROOT / 'Dockerfile').read_text(encoding='utf-8').splitlines()
    final_stage_index = max(index for index, line in enumerate(dockerfile_lines) if line.startswith('FROM '))
    final_stage_lines = dockerfile_lines[final_stage_index:]
    user_indexes = [index for index, line in enumerate(final_stage_lines) if line.startswith('USER ')]
    entrypoint_index = next(index for index, line in enumerate(final_stage_lines) if line.startswith('ENTRYPOINT '))

    assert len(user_indexes) == 1
    assert user_indexes[0] < entrypoint_index
    assert final_stage_lines[user_indexes[0]] not in {'USER root', 'USER 0', 'USER 0:0'}
    assert any(line == 'ENV HOME=/home/konomitv' for line in final_stage_lines)


@pytest.mark.parametrize('compose_filename', COMPOSE_FILENAMES)
def test_compose_does_not_bind_host_root_or_entire_host_rootfs(compose_filename: str) -> None:
    """
    Compose policy が host / と /host-rootfs 全体の bind mount を禁止していることを検証する。

    Args:
        compose_filename (str): 検査対象の Compose ファイル名

    Returns:
        None
    """

    service = _load_compose_service(compose_filename)
    volumes = cast(list[dict[str, Any] | str], service['volumes'])

    for volume in volumes:
        assert isinstance(volume, dict)
        source = str(volume['source'])
        target = str(volume['target'])

        # host ルートそのもの、および空 source を禁止する
        assert source not in {'', '/'}
        assert source.rstrip('/') != ''

        # /host-rootfs 全体ではなく、必要な部分 path だけを許可する
        ## 実 compose は `/host-rootfs${KONOMITV_RECORDED_FOLDER:-/mnt/TV-Record}` 形式も使う
        if target.startswith('/host-rootfs'):
            assert target.rstrip('/') != '/host-rootfs'
            assert len(target) > len('/host-rootfs')
            assert target[len('/host-rootfs'):] not in {'', '/'}

    # root 実行を既定で禁止する。UID 変数展開後も 0:0 リテラルは拒否する
    user = str(service['user'])
    assert user not in {'0', '0:0', 'root', 'root:root'}
    assert len(cast(list[str], service['group_add'])) >= 1


@pytest.mark.parametrize('compose_filename', COMPOSE_FILENAMES)
def test_compose_keeps_source_tree_read_only_and_uses_explicit_host_mounts(compose_filename: str) -> None:
    """
    source tree を read-only に保ち、host path は必要な個別 mount だけに限定する。

    Args:
        compose_filename (str): 検査対象の Compose ファイル名

    Returns:
        None
    """

    service = _load_compose_service(compose_filename)
    volumes = cast(list[dict[str, Any]], service['volumes'])
    source_tree_mount = next(volume for volume in volumes if volume['target'] == '/code/source-tree/')
    host_mounts = [volume for volume in volumes if str(volume['target']).startswith('/host-rootfs')]

    assert source_tree_mount['read_only'] is True

    # 録画・Capture など 1 つ以上の個別 mount を要求し、件数上限は設けない
    assert len(host_mounts) >= 1
    for volume in host_mounts:
        target = str(volume['target'])
        assert target.startswith('/host-rootfs')
        assert target.rstrip('/') != '/host-rootfs'
        assert len(target) > len('/host-rootfs')
        assert target[len('/host-rootfs'):] not in {'', '/'}
        assert volume.get('bind', {}).get('create_host_path') is False
        # SELinux enforcing host 向けに shared label z を要求する (root 全体の :Z は禁止)
        assert volume.get('bind', {}).get('selinux') == 'z'


@pytest.mark.parametrize('compose_filename', COMPOSE_FILENAMES)
def test_compose_bind_mounts_use_shared_selinux_label_z(compose_filename: str) -> None:
    """
    個別 bind mount が SELinux shared label z を持ち、host root を mount しないことを検証する。

    Args:
        compose_filename (str): 検査対象の Compose ファイル名

    Returns:
        None
    """

    service = _load_compose_service(compose_filename)
    volumes = cast(list[dict[str, Any]], service['volumes'])

    for volume in volumes:
        assert isinstance(volume, dict)
        source = str(volume['source'])
        assert source not in {'', '/'}
        bind = cast(dict[str, Any], volume.get('bind') or {})
        # 全 bind に shared label z（private Z や root 全体 mount は使わない）
        assert bind.get('selinux') == 'z'


@pytest.mark.parametrize('compose_filename', COMPOSE_FILENAMES)
def test_base_compose_does_not_require_or_mount_acp_credentials(compose_filename: str) -> None:
    """認証 backend を使わない基本 Compose は host-auth mount なしで成立する。"""

    service = _load_compose_service(compose_filename)
    volumes = cast(list[dict[str, Any]], service['volumes'])

    assert all(
        str(volume['target']).startswith('/run/konomitv-bs4k-host-auth/') is False
        for volume in volumes
    )


@pytest.mark.parametrize(
    'compose_filename,expected',
    ACP_AUTH_OVERRIDE_EXPECTATIONS.items(),
)
def test_acp_auth_override_mounts_exactly_one_fixed_read_only_file(
    compose_filename: str,
    expected: tuple[str, str],
) -> None:
    """provider override は絶対 path 変数の単一ファイルだけを固定 target へ read-only mount する。"""

    expected_source_prefix, expected_target = expected
    service = _load_compose_service(compose_filename)
    volumes = cast(list[dict[str, Any]], service['volumes'])

    assert len(volumes) == 1
    volume = volumes[0]
    source = str(volume['source'])
    target = str(volume['target'])
    assert source.startswith(expected_source_prefix)
    assert target == expected_target
    assert volume['type'] == 'bind'
    assert volume['read_only'] is True
    assert volume['bind']['create_host_path'] is False
    assert volume['bind']['selinux'] == 'z'
    assert source not in {'/', '/home', '~/.codex', '~/.grok', '~/.config/gcloud'}
    assert target.startswith('/host-rootfs') is False


def test_dockerfile_pins_acp_clis_and_does_not_install_google_cloud_cli() -> None:
    """完成イメージへ固定 ACP CLI を導入し、gcloud package を含めない。"""

    import json
    import re

    dockerfile = (REPOSITORY_ROOT / 'Dockerfile').read_text(encoding='utf-8')
    manifest = (REPOSITORY_ROOT / 'docker/acp/manifest.env').read_text(encoding='utf-8')
    package_json = json.loads(
        (REPOSITORY_ROOT / 'docker/acp/package.json').read_text(encoding='utf-8'),
    )
    package_lock = json.loads(
        (REPOSITORY_ROOT / 'docker/acp/package-lock.json').read_text(encoding='utf-8'),
    )

    assert "CODEX_ACP_VERSION='1.1.7'" in manifest
    assert "CODEX_CLI_VERSION='0.145.0'" in manifest
    assert 'GEMINI_CLI' not in manifest
    assert "GROK_NPM_PACKAGE='@xai-official/grok'" in manifest
    assert "GROK_PLATFORM_NPM_PACKAGE='@xai-official/grok-linux-x64'" in manifest
    assert "GROK_BUILD_VERSION='0.2.112'" in manifest
    # 公式 platform package から展開した Grok binary は完全 SHA-256 を build 中に再検証する。
    grok_sha_match = re.search(
        r"GROK_BUILD_LINUX_X86_64_SHA256='([0-9a-f]{64})'",
        manifest,
    )
    assert grok_sha_match is not None
    grok_sha = grok_sha_match.group(1)
    assert grok_sha == 'c2867112f7d89366123fe68a55a23dfb027d3602fc5b5b9cd5c080dacb4a2503'
    assert '"${GROK_BUILD_LINUX_X86_64_SHA256}"' in dockerfile
    assert 'grok-distribution/bin/grok-${GROK_BUILD_VERSION}' in dockerfile
    assert 'sha256sum --check --strict' in dockerfile
    assert 'https://x.ai/cli/grok-' not in dockerfile
    assert 'ADD --checksum' not in dockerfile.split('FROM node:20.16.0 AS acp-builder', 1)[1].split('FROM ', 1)[0]

    # npm integrity の正本は lockfile。未使用の *_NPM_INTEGRITY は持たない
    assert 'NPM_INTEGRITY' not in manifest
    dependencies = package_json['dependencies']
    assert dependencies == {
        '@agentclientprotocol/codex-acp': '1.1.7',
        '@openai/codex': '0.145.0',
        '@xai-official/grok': '0.2.112',
    }
    # exact version のみ（^ や ~ を禁止）
    for version in dependencies.values():
        assert version[0].isdigit()
    lock_packages = package_lock['packages']
    for name, version in dependencies.items():
        entry = lock_packages[f'node_modules/{name}']
        assert entry['version'] == version
        assert isinstance(entry.get('integrity'), str) and entry['integrity'].startswith('sha512-')
        assert entry['resolved'].startswith('https://registry.npmjs.org/')

    # wrapper と実 binary / NOTICE を含む platform tarball の SRI を個別に固定する。
    assert lock_packages['node_modules/@xai-official/grok']['integrity'] == (
        'sha512-dCXAiFHmn3JTOK+vPfCIzzum1GmxPB81NH73yYhqleXx1y/Ks3qjwJ+GeEXmB7eudiap98j9Nj1cDwH4lSuaOw=='
    )
    grok_platform = lock_packages['node_modules/@xai-official/grok-linux-x64']
    assert grok_platform['version'] == '0.2.112'
    assert grok_platform['integrity'] == (
        'sha512-2jD/00EB9xmzDQ89sSdA/CThTVqQQEgxIm/XqGIVuJXr63UST6H/aNdxPUiwz+tMkk8K12Nv6vPYHCO/H+Ae1Q=='
    )
    assert grok_platform['os'] == ['linux']
    assert grok_platform['cpu'] == ['x64']
    assert grok_platform['optional'] is True

    lock_text = (REPOSITORY_ROOT / 'docker/acp/package-lock.json').read_text(encoding='utf-8')
    assert '_authToken' not in lock_text
    assert ':_password' not in lock_text
    assert 'npm.pkg.github.com' not in lock_text

    assert './docker/acp/package.json' in dockerfile
    assert './docker/acp/package-lock.json' in dockerfile
    assert './docker/acp/manifest.env' in dockerfile
    assert './docker/acp/normalize-acp-web-telemetry.mjs' in dockerfile
    assert 'npm ci --omit=dev --no-audit --no-fund' in dockerfile
    assert (
        'node /build/docker/acp/normalize-acp-web-telemetry.mjs /opt/konomitv-bs4k-acp'
        in dockerfile
    )
    assert 'npm install' not in dockerfile.split('FROM node:20.16.0 AS acp-builder', 1)[1].split('FROM ', 1)[0]
    # path / name / version の双方向照合と CLI 実 version の全文一致検査を要求する
    assert 'installed package path/name/version match lockfile' in dockerfile
    assert 'version mismatch' in dockerfile
    assert 'required lock path not installed' in dockerfile
    # pipeline + grep 部分一致は fail-open になるため禁止し、capture + test 全文一致を要求する
    acp_builder_section = dockerfile.split('FROM node:20.16.0 AS acp-builder', 1)[1].split('FROM ', 1)[0]
    assert 'codex-acp --version | grep' not in acp_builder_section
    assert 'codex --version | grep' not in acp_builder_section
    assert 'gemini --version | grep' not in acp_builder_section
    assert 'gemini_cli_version' not in acp_builder_section
    assert 'GEMINI_CLI_VERSION' not in manifest
    assert '@google/gemini-cli' not in dockerfile
    assert 'codex_acp_version="$(/opt/konomitv-bs4k-acp/node_modules/.bin/codex-acp --version)"' in dockerfile
    # 各 CLI の既知の完全1行出力と一致させる（部分一致や pipeline 隠蔽を禁止）
    assert 'test "${codex_acp_version}" = "@agentclientprotocol/codex-acp ${CODEX_ACP_VERSION}"' in dockerfile
    assert 'test "${codex_cli_version}" = "codex-cli ${CODEX_CLI_VERSION}"' in dockerfile
    assert 'test "${grok_version_line}" = "${GROK_BUILD_VERSION_LINE}"' in dockerfile
    assert 'Google Cloud CLI must not be included in the final image.' in dockerfile
    assert 'google-cloud-cli' not in dockerfile
    assert 'google-cloud-sdk' not in dockerfile

    # ACP ライセンス収集: npm generator + Node LICENSE + 最終文書への独立 H2 統合
    assert 'generate-license-document.mjs' in dockerfile
    assert 'assemble-acp-license-section.py' in dockerfile
    assert 'ACP_THIRD_PARTY_LICENSES.md' in dockerfile
    assert '--manifest /tmp/ACP_THIRD_PARTY_LICENSES.md' in dockerfile
    assert '### Node.js 20.16.0' in dockerfile
    assert '### @agentclientprotocol/codex-acp 1.1.7' in dockerfile
    # Grok は推測した公開 source snapshot ではなく、binary と NOTICE が同居する npm package を収録する。
    assert '### @xai-official/grok 0.2.112' in dockerfile
    assert '### @xai-official/grok-linux-x64 0.2.112' in dockerfile
    assert '--grok-source-commit' not in dockerfile
    assert 'GROK_LICENSE_SOURCE_COMMIT' not in manifest
    assert '47348d13ec4508dcfe440e34c6d511bb02998fb2' not in manifest
    licenses_dir = REPOSITORY_ROOT / 'docker/acp/licenses'
    assert list(licenses_dir.glob('grok-build-0.2.112*')) == []
    license_generator = (
        REPOSITORY_ROOT / 'docker/acp/generate-license-document.mjs'
    ).read_text(encoding='utf-8')
    assert '@xai-official/grok@0.2.112' in license_generator
    assert '@xai-official/grok-linux-x64@0.2.112' in license_generator
    assert 'a9a4529af672a2a27496b1623b539bad499e09f1eda1fce34eddc2f38378e8ac' in license_generator
    assert '18814fc44bc1e3d93367dadcacaa010ab2049bcc09704b53315aba4dc2907103' in license_generator
    assert 'npm integrity:' in license_generator
    assert 'APPENDIX: How to apply the Apache License to your work.' in license_generator
    assert 'a3d60eaf32d2fb09c9d82ef39f14bd6e2c0f3ca7de30daa827486c0d6f8b6e9f' in license_generator
    assert (REPOSITORY_ROOT / 'docker/acp/generate-license-document.mjs').is_file()
    telemetry_normalizer = (
        REPOSITORY_ROOT / 'docker/acp/normalize-acp-web-telemetry.mjs'
    ).read_text(encoding='utf-8')
    assert 'expected exactly one unpatched marker' in telemetry_normalizer
    assert 'unexpected pre-patch sha256' in telemetry_normalizer
    assert '0deb6b820dfed8804cd76b16a50210fe12202e5e339b5edaa23f6987f1742e0a' in telemetry_normalizer
    assert 'snippet' not in telemetry_normalizer
    assert '...args,' not in telemetry_normalizer
    assert '@google/gemini-cli' not in telemetry_normalizer
    assert 'rawOutput' in telemetry_normalizer
    assert 'rawInput' in telemetry_normalizer
    assert 'createWebSearchCompleteUpdate' in telemetry_normalizer
    assert (REPOSITORY_ROOT / 'docker/acp/assemble-acp-license-section.py').is_file()


def test_opencode_runtime_is_pinned_and_binary_only_in_final_image() -> None:
    """opencode-ai 1.18.13 が固定導入され、final は SEA バイナリのみを持つ。"""

    import json

    dockerfile = (REPOSITORY_ROOT / 'Dockerfile').read_text(encoding='utf-8')
    manifest = (REPOSITORY_ROOT / 'docker/opencode/manifest.env').read_text(encoding='utf-8')
    package_json = json.loads(
        (REPOSITORY_ROOT / 'docker/opencode/package.json').read_text(encoding='utf-8'),
    )
    package_lock = json.loads(
        (REPOSITORY_ROOT / 'docker/opencode/package-lock.json').read_text(encoding='utf-8'),
    )

    assert "OPENCODE_VERSION='1.18.13'" in manifest
    assert "OPENCODE_PLATFORM_PACKAGE='opencode-linux-x64'" in manifest
    assert package_json['dependencies'] == {'opencode-ai': '1.18.13'}
    assert package_lock['packages']['node_modules/opencode-ai']['version'] == '1.18.13'
    assert package_lock['packages']['node_modules/opencode-linux-x64']['version'] == '1.18.13'
    assert package_lock['packages']['node_modules/opencode-linux-x64'].get('optional') is True

    assert 'FROM node:20.16.0 AS opencode-builder' in dockerfile
    opencode_section = dockerfile.split(
        'FROM node:20.16.0 AS opencode-builder', 1,
    )[1].split('FROM ', 1)[0]
    assert 'npm ci --omit=dev --no-audit --no-fund' in opencode_section
    assert 'npm install' not in opencode_section
    assert 'opencode --version | grep' not in opencode_section
    assert 'test "${opencode_version}" = "${OPENCODE_VERSION}"' in opencode_section
    # final はバイナリ + ライセンスのみ。node_modules 丸ごとは禁止。
    assert 'COPY --from=opencode-builder /opt/konomitv-bs4k-opencode/dist/opencode /usr/local/bin/opencode' in dockerfile
    # node_modules ツリー全体の COPY は禁止（dist 配下のみ）。
    assert 'COPY --from=opencode-builder /opt/konomitv-bs4k-opencode/node_modules' not in dockerfile
    assert 'COPY --from=opencode-builder /opt/konomitv-bs4k-opencode/ /' not in dockerfile
    assert 'test ! -e /opt/konomitv-bs4k-opencode' in dockerfile
    assert 'test "${opencode_version}" = \'1.18.13\'' in dockerfile
    assert '## OpenCode Runtime Dependencies' in dockerfile
    assert '### opencode-ai 1.18.13' in dockerfile
    assert (REPOSITORY_ROOT / 'docker/opencode/opencode.json').is_file()
    assert (REPOSITORY_ROOT / 'docker/opencode/assemble-opencode-license-section.py').is_file()
    config = (REPOSITORY_ROOT / 'docker/opencode/opencode.json').read_text(encoding='utf-8')
    assert 'recorded-series-generate' in config
    assert 'recorded-series-episode' in config
    assert '"bash": "deny"' in config
    assert '"webfetch": "allow"' in config


def test_acp_subprocess_is_always_started_through_hardened_landlock_launcher() -> None:
    """ACP provider は追加 privilege なしの root-owned Landlock launcher だけを経由する。"""

    dockerfile = (REPOSITORY_ROOT / 'Dockerfile').read_text(encoding='utf-8')
    sandbox_source = (
        REPOSITORY_ROOT / 'docker/acp/KonomiTVBS4KACPSandbox.c'
    ).read_text(encoding='utf-8')
    acp_client = (
        REPOSITORY_ROOT / 'server/app/metadata/ai/acp_client.py'
    ).read_text(encoding='utf-8')

    assert 'KonomiTVBS4KACPSandbox.c' in dockerfile
    assert '-fstack-protector-strong' in dockerfile
    assert '-Wl,-z,relro,-z,now -pie' in dockerfile
    assert '/usr/local/libexec/konomitv-bs4k-acp-sandbox' in dockerfile
    assert (
        """test "$(stat -c '%U:%G:%a' /usr/local/libexec/konomitv-bs4k-acp-sandbox)" = 'root:root:755'"""
        in dockerfile
    )
    assert "_ACP_SANDBOX_LAUNCHER = '/usr/local/libexec/konomitv-bs4k-acp-sandbox'" in acp_client
    assert 'asyncio.create_subprocess_exec(\n        _ACP_SANDBOX_LAUNCHER,' in acp_client

    assert '#define ACP_SANDBOX_MINIMUM_ABI 6' in sandbox_source
    assert 'PR_SET_NO_NEW_PRIVS' in sandbox_source
    assert 'LandlockRestrictSelf' in sandbox_source
    assert 'LANDLOCK_ACCESS_FS_TRUNCATE' in sandbox_source
    assert 'LANDLOCK_SCOPE_SIGNAL' in sandbox_source
    assert 'RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS' in sandbox_source
    assert 'entry_stat.st_nlink != 1' in sandbox_source
    assert 'execv(arguments.command_argv[0], arguments.command_argv)' in sandbox_source
    assert 'ACP sandbox setup failed.' in sandbox_source
    assert 'fallback' in sandbox_source

    for compose_filename in COMPOSE_FILENAMES:
        service = _load_compose_service(compose_filename)
        assert service.get('privileged') is not True
        assert service.get('cap_add') in (None, [])
        assert service.get('security_opt') in (None, [])
        volumes = cast(list[dict[str, Any]], service['volumes'])
        assert all('/var/run/docker.sock' not in str(volume) for volume in volumes)


@pytest.mark.parametrize('ignore_filename', ['.dockerignore', '.gitignore'])
def test_authentication_files_are_excluded_from_build_context_and_git(ignore_filename: str) -> None:
    """認証 directory と既知の JSON filename を Docker context / Git から除外する。"""

    ignore_content = REPOSITORY_ROOT.joinpath(ignore_filename).read_text(encoding='utf-8')

    assert '**/.codex/' in ignore_content
    assert '**/.grok/' in ignore_content
    assert '**/.config/gcloud/' in ignore_content
    assert '**/auth.json' in ignore_content
    assert '**/application_default_credentials.json' in ignore_content


def test_local_docker_compose_yaml_is_gitignored() -> None:
    """実機用 docker-compose.yaml は Git 管理外とし、example をテンプレート正本にする。"""

    gitignore = (REPOSITORY_ROOT / '.gitignore').read_text(encoding='utf-8')
    assert re.search(r'(?m)^docker-compose\.yaml$', gitignore) is not None
    # 追跡済みのままだと ignore が効かないため、index からも外れていること
    assert (REPOSITORY_ROOT / 'docker-compose.example.yaml').is_file()
