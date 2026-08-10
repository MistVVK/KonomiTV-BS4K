import re
from pathlib import Path
from typing import Any, cast

import pytest
import ruamel.yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
# 公開・Main と Development は独立した完全な Compose とし、同じホストパス契約を保つ。
COMPOSE_FILENAMES = ['compose.yaml', 'compose.development.yaml']


def _GetDockerfileStageLines(stage_name: str) -> list[str]:
    """Dockerfile から指定した名前付き stage の行だけを返す。

    Args:
        stage_name (str): `FROM ... AS` で指定された stage 名。

    Returns:
        list[str]: stage の FROM から次の FROM 直前までの行。
    """

    dockerfile_lines = (REPOSITORY_ROOT / 'Dockerfile').read_text(encoding='utf-8').splitlines()
    stage_start_index = next(
        index
        for index, line in enumerate(dockerfile_lines)
        if re.search(rf'\sAS\s+{re.escape(stage_name)}$', line, flags=re.IGNORECASE)
    )
    stage_end_index = next(
        (
            index for index in range(stage_start_index + 1, len(dockerfile_lines))
            if dockerfile_lines[index].startswith('FROM ')
        ),
        len(dockerfile_lines),
    )
    return dockerfile_lines[stage_start_index:stage_end_index]


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


def test_public_compose_uses_runtime_target_and_public_identifiers() -> None:
    """公開 Compose が一般配布用の識別子と runtime target を正本にする。

    Returns:
        None
    """

    yaml = ruamel.yaml.YAML(typ='safe')
    compose = cast(dict[str, Any], yaml.load((REPOSITORY_ROOT / 'compose.yaml').read_text()))
    service = cast(dict[str, Any], compose['services']['konomitv'])
    build = cast(dict[str, Any], service['build'])

    assert compose['name'] == 'konomitv-bs4k'
    assert service['image'] == 'konomitv-bs4k'
    assert service['container_name'] == 'KonomiTV-BS4K'
    assert build['context'] == '.'
    assert build['target'] == 'runtime'


def test_runtime_image_runs_as_non_root_user() -> None:
    """
    公開用 runtime stage が専用 USER を ENTRYPOINT より前に設定していることを検証する。

    Returns:
        None
    """

    runtime_stage_lines = _GetDockerfileStageLines('runtime')
    user_indexes = [index for index, line in enumerate(runtime_stage_lines) if line.startswith('USER ')]
    entrypoint_index = next(index for index, line in enumerate(runtime_stage_lines) if line.startswith('ENTRYPOINT '))

    assert len(user_indexes) == 1
    assert user_indexes[0] < entrypoint_index
    assert runtime_stage_lines[user_indexes[0]] not in {'USER root', 'USER 0', 'USER 0:0'}
    assert any(line == 'ENV HOME=/home/konomitv' for line in runtime_stage_lines)


def test_dockerfile_separates_runtime_and_verification_targets() -> None:
    """検証をruntimeのbuild依存にでき、開発依存とテストを完成imageへ持ち込まない構造を検証する。

    Returns:
        None
    """

    dockerfile = (REPOSITORY_ROOT / 'Dockerfile').read_text(encoding='utf-8')
    runtime_stage = '\n'.join(_GetDockerfileStageLines('runtime'))
    client_verify_stage = '\n'.join(_GetDockerfileStageLines('client-verify'))
    server_verify_stage = '\n'.join(_GetDockerfileStageLines('server-verify'))
    verify_stage = '\n'.join(_GetDockerfileStageLines('verify'))
    verified_runtime_stage = '\n'.join(_GetDockerfileStageLines('verified-runtime'))

    assert 'RUN --mount=type=bind,source=server' in runtime_stage
    assert 'rm -rf /code/server/tests' in runtime_stage
    assert 'test ! -e /code/server/tests' in runtime_stage
    assert 'poetry install --with dev' not in runtime_stage
    assert 'yarn lint:check' not in runtime_stage

    assert client_verify_stage.startswith('FROM client-builder AS client-verify')
    assert 'yarn lint:check' in client_verify_stage
    assert 'yarn typecheck' in client_verify_stage
    assert 'yarn test' in client_verify_stage
    assert 'yarn test:licenses' in client_verify_stage

    assert server_verify_stage.startswith('FROM runtime AS server-verify')
    assert 'apt-get install -y --fix-broken' in server_verify_stage
    assert 'apt-get install -y --no-install-recommends build-essential' in server_verify_stage
    assert 'command -v cc >/dev/null' in server_verify_stage
    assert 'poetry install --with dev --no-root' in server_verify_stage
    assert 'poetry run task lint-check' in server_verify_stage
    assert 'poetry run task test' in server_verify_stage

    assert verify_stage.startswith('FROM server-verify AS verify')
    assert 'COPY --from=client-verify' in verify_stage
    assert verified_runtime_stage.startswith('FROM runtime AS verified-runtime')
    assert 'type=bind,from=verify' in verified_runtime_stage
    assert 'COPY --from=verify' not in verified_runtime_stage
    assert dockerfile.rstrip().endswith('FROM runtime AS default-runtime')


def test_dockerignore_excludes_host_state_but_keeps_verification_sources() -> None:
    """巨大なホスト状態を除外しつつ、verifyに必要なテストをcontextへ残す。

    Returns:
        None
    """

    dockerignore = (REPOSITORY_ROOT / '.dockerignore').read_text(encoding='utf-8')

    assert '.env' in dockerignore
    assert 'compose.override.yaml' in dockerignore
    assert '.git/' in dockerignore
    assert 'client/dist/' in dockerignore
    assert 'server/logs/*' in dockerignore
    assert 'server/cutover-backups/' in dockerignore
    assert 'server/tests/' not in dockerignore
    assert 'client/scripts/*.test.mjs' not in dockerignore


@pytest.mark.parametrize('compose_filename', COMPOSE_FILENAMES)
def test_compose_mounts_host_root_for_host_path_compatibility(compose_filename: str) -> None:
    """config.yaml のホスト絶対パスをそのまま使えるよう、host / 全体を mount する。

    Args:
        compose_filename (str): 検査対象の Compose ファイル名

    Returns:
        None
    """

    service = _load_compose_service(compose_filename)
    volumes = cast(list[dict[str, Any]], service['volumes'])
    host_root_mounts = [volume for volume in volumes if str(volume['target']).rstrip('/') == '/host-rootfs']

    assert len(host_root_mounts) == 1
    assert host_root_mounts[0]['type'] == 'bind'
    assert host_root_mounts[0]['source'] == '/'
    assert host_root_mounts[0].get('read_only') is not True
    assert all(
        str(volume['target']).startswith('/host-rootfs/') is False
        for volume in volumes
        if volume is not host_root_mounts[0]
    )

    # root 実行を既定で禁止する。UID 変数展開後も 0:0 リテラルは拒否する
    user = str(service['user'])
    assert user not in {'0', '0:0', 'root', 'root:root'}
    assert len(cast(list[str], service['group_add'])) >= 1


@pytest.mark.parametrize('compose_filename', COMPOSE_FILENAMES)
def test_compose_keeps_source_tree_and_host_home_read_only(compose_filename: str) -> None:
    """source tree と認証検出用の host HOME を read-only に保つ。

    Args:
        compose_filename (str): 検査対象の Compose ファイル名

    Returns:
        None
    """

    service = _load_compose_service(compose_filename)
    volumes = cast(list[dict[str, Any]], service['volumes'])
    source_tree_mount = next(volume for volume in volumes if volume['target'] == '/code/source-tree/')
    host_home_mount = next(volume for volume in volumes if str(volume['target']).rstrip('/') == '/host-home')

    assert source_tree_mount['read_only'] is True
    assert host_home_mount['type'] == 'bind'
    assert host_home_mount['source'] == '${HOME}'
    assert host_home_mount['read_only'] is True
    assert service['environment']['GOOGLE_APPLICATION_CREDENTIALS'] == (
        '/host-home/.config/gcloud/application_default_credentials.json'
    )


@pytest.mark.parametrize('compose_filename', COMPOSE_FILENAMES)
def test_compose_only_relabels_repository_bind_mounts(compose_filename: str) -> None:
    """host root と HOME を relabel せず、リポジトリ内 bind だけ shared label z にする。

    Args:
        compose_filename (str): 検査対象の Compose ファイル名

    Returns:
        None
    """

    service = _load_compose_service(compose_filename)
    volumes = cast(list[dict[str, Any]], service['volumes'])

    for volume in volumes:
        bind = cast(dict[str, Any], volume.get('bind') or {})
        if str(volume['target']).rstrip('/') in {'/host-rootfs', '/host-home'}:
            assert bind.get('selinux') is None
        else:
            assert bind.get('selinux') == 'z'


@pytest.mark.parametrize('compose_filename', COMPOSE_FILENAMES)
def test_compose_does_not_require_individual_host_path_variables(compose_filename: str) -> None:
    """公開・Development Compose は個別の録画・Capture・認証 path 変数を要求しない。"""

    compose_text = REPOSITORY_ROOT.joinpath(compose_filename).read_text(encoding='utf-8')

    assert 'KONOMITV_RECORDED_FOLDER' not in compose_text
    assert 'KONOMITV_CAPTURE_FOLDER' not in compose_text
    assert 'KONOMITV_BS4K_CODEX_AUTH_FILE' not in compose_text
    assert 'KONOMITV_BS4K_GROK_AUTH_FILE' not in compose_text
    assert 'KONOMITV_BS4K_GOOGLE_ADC_FILE' not in compose_text
    assert '/run/konomitv-bs4k-host-auth' not in compose_text


def test_nvidia_compose_contains_all_nvidia_runtime_settings() -> None:
    """公開・Developmentで共有するoverlayにNVIDIA runtime設定を集約する。"""

    service = _load_compose_service('compose.nvidia.yaml')
    environment = cast(dict[str, str], service['environment'])
    devices = cast(list[dict[str, Any]], service['deploy']['resources']['reservations']['devices'])

    assert environment == {
        'NVIDIA_VISIBLE_DEVICES': 'all',
        'NVIDIA_DRIVER_CAPABILITIES': 'compute,utility,video',
    }
    assert devices == [{
        'driver': 'nvidia',
        'count': 'all',
        'capabilities': ['compute', 'utility', 'video'],
    }]
    for compose_filename in COMPOSE_FILENAMES:
        compose_text = REPOSITORY_ROOT.joinpath(compose_filename).read_text(encoding='utf-8')
        assert 'NVIDIA_VISIBLE_DEVICES' not in compose_text
        assert 'NVIDIA_DRIVER_CAPABILITIES' not in compose_text
        assert 'driver: nvidia' not in compose_text


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
    # F-03 / R-08: 検索専用モード。websearch のみ allow、webfetch は deny。
    episode_section = config.split('"recorded-series-episode"', 1)[1]
    assert '"webfetch": "deny"' in episode_section
    assert '"websearch": "allow"' in episode_section
    assert '"webfetch": "allow"' not in episode_section


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


def test_compose_entrypoints_are_explicit() -> None:
    """公開版と Development の完全な Compose だけを正本にし、暗黙のローカル override を残さない。"""

    gitignore = (REPOSITORY_ROOT / '.gitignore').read_text(encoding='utf-8')
    assert re.search(r'(?m)^docker-compose\.yaml$', gitignore) is None
    assert re.search(r'(?m)^compose\.override\.yaml$', gitignore) is None
    assert (REPOSITORY_ROOT / 'compose.yaml').is_file()
    assert (REPOSITORY_ROOT / 'compose.development.yaml').is_file()
    assert (REPOSITORY_ROOT / 'compose.nvidia.yaml').is_file()
    assert (REPOSITORY_ROOT / 'docker-compose.example.yaml').exists() is False


def test_public_env_example_exposes_all_host_specific_compose_settings() -> None:
    """実際の .env を追跡せず、利用者が変更する全Compose変数をexampleで公開する。"""

    gitignore = (REPOSITORY_ROOT / '.gitignore').read_text(encoding='utf-8')
    env_example = (REPOSITORY_ROOT / '.env.example').read_text(encoding='utf-8')

    assert re.search(r'(?m)^\.env$', gitignore) is not None
    assert 'COMPOSE_FILE=compose.yaml' in env_example
    assert 'KONOMITV_UID=1000' in env_example
    assert 'KONOMITV_GID=1000' in env_example
    assert 'KONOMITV_VIDEO_GID=44' in env_example
    assert 'KONOMITV_RENDER_GID=992' in env_example
    assert 'KONOMITV_CUDA_VERSION=12.4' in env_example
    assert 'KONOMITV_NONFREE=true' in env_example
