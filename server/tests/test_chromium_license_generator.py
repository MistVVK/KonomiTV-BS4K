import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from markdown_it import MarkdownIt


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = REPOSITORY_ROOT / 'docker/thirdparty/generate-chromium-license-document.py'
CHROMIUM_LICENSE_PATH = REPOSITORY_ROOT / 'docker/thirdparty/licenses/chromium-LICENSE'


def LoadGenerator() -> ModuleType:
    spec = importlib.util.spec_from_file_location('generate_chromium_license_document', GENERATOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR = LoadGenerator()


def test_chromium_package_version_extracts_four_part_upstream_version() -> None:
    assert GENERATOR.extractPackageChromiumVersion('150.0.7871.124~linuxmint1+virginia') == '150.0.7871.124'

    with pytest.raises(ValueError, match='Unsupported Chromium package version'):
        GENERATOR.extractPackageChromiumVersion('latest')


def test_chromium_credits_require_complete_text_and_supported_homepages() -> None:
    credits = GENERATOR.validateCredits([
        {
            'name': 'External project',
            'homepage': 'https://example.com/project',
            'license': 'First page\fSecond page',
        },
        {
            'name': 'Chromium internal project',
            'homepage': 'chrome://credits/Internal',
            'license': 'Internal license.',
        },
    ])
    assert credits[0]['license'] == 'First page\nSecond page'

    with pytest.raises(ValueError, match='unsupported homepage URL'):
        GENERATOR.validateCredits([
            {'name': 'Unsafe', 'homepage': 'javascript:alert(1)', 'license': 'License.'},
        ])

    with pytest.raises(ValueError, match='empty license text'):
        GENERATOR.validateCredits([
            {'name': 'Missing', 'homepage': 'https://example.com/', 'license': ''},
        ])

    with pytest.raises(ValueError, match='at least 100 are required'):
        GENERATOR.validateCredits([
            {'name': 'Only project', 'homepage': 'https://example.com/', 'license': 'License.'},
        ], minimum_count=100)

    with pytest.raises(ValueError, match='Unicode replacement character'):
        GENERATOR.validateCredits([
            {'name': 'Damaged', 'homepage': 'https://example.com/', 'license': 'Copyright \ufffd'},
        ])


@pytest.mark.parametrize(
    'chromium_version',
    ['150.0.7871.124', '150.0.7871.181', '150.0.7871.186', '151.0.7922.108'],
)
def test_known_chromium_credits_encoding_damage_is_repaired_only_for_fixed_versions(
    chromium_version: str,
) -> None:
    android_license = (
        GENERATOR.ANDROID_BROKEN_LICENSE_LINE + '\n' +
        GENERATOR.ANDROID_BROKEN_MULTIPLE_LICENSED_LINE + '\n' +
        GENERATOR.ANDROID_BROKEN_LICENSE_LINE
    )
    credits = [
        {'name': name, 'homepage': 'https://example.com/', 'license': android_license}
        for name in GENERATOR.ANDROID_NOTICE_REPAIR_PROJECTS
    ]
    credits.append({
        'name': 'FreeType',
        'homepage': 'https://freetype.org/',
        'license': GENERATOR.FREETYPE_BROKEN_COPYRIGHT_LINE,
    })

    repaired, repairs = GENERATOR.repairKnownCreditsEncoding(chromium_version, credits)
    assert len(repairs) == 16
    assert sum(character_count for _, character_count in repairs) == 121
    assert all('\ufffd' not in credit['license'] for credit in repaired)
    assert GENERATOR.ANDROID_REPAIRED_LICENSE_LINE in repaired[0]['license']
    assert GENERATOR.FREETYPE_REPAIRED_COPYRIGHT_LINE in repaired[-1]['license']

    with pytest.raises(ValueError, match='known repairs apply only'):
        GENERATOR.repairKnownCreditsEncoding('152.0.0.0', credits)


def test_known_chromium_credits_encoding_repair_allows_known_project_subset() -> None:
    """Chromium 151 のように損傷 project が減っても、既知集合の部分集合なら修復する。"""

    android_license = (
        GENERATOR.ANDROID_BROKEN_LICENSE_LINE + '\n' +
        GENERATOR.ANDROID_BROKEN_MULTIPLE_LICENSED_LINE + '\n' +
        GENERATOR.ANDROID_BROKEN_LICENSE_LINE
    )
    # 151.0.7922.108 で実際に U+FFFD が残っていた project 群（cloud-messaging / location は無し）
    subset_projects = [
        'common',
        'core-common',
        'feature-delivery',
        'googleid',
        'play-services-auth',
        'play-services-auth-api-phone',
        'play-services-auth-base',
        'play-services-auth-blockstore',
        'play-services-cast',
        'play-services-cast-framework',
        'play-services-instantapps',
        'play-services-time',
        'review',
    ]
    credits = [
        {'name': name, 'homepage': 'https://example.com/', 'license': android_license}
        for name in subset_projects
    ]
    credits.append({
        'name': 'FreeType',
        'homepage': 'https://freetype.org/',
        'license': GENERATOR.FREETYPE_BROKEN_COPYRIGHT_LINE,
    })

    repaired, repairs = GENERATOR.repairKnownCreditsEncoding('151.0.7922.108', credits)
    assert len(repairs) == 14
    assert all('\ufffd' not in credit['license'] for credit in repaired)

    with pytest.raises(ValueError, match='unexpected projects'):
        GENERATOR.repairKnownCreditsEncoding(
            '151.0.7922.108',
            [*credits, {'name': 'UnknownLib', 'homepage': 'https://example.com/', 'license': 'bad \ufffd'}],
        )


def test_linux_mint_package_copyright_is_strict_utf8_and_hashed(tmp_path: Path) -> None:
    copyright_path = tmp_path / 'copyright'
    copyright_path.write_bytes(b'Package notice.\n')

    copyright_text, copyright_sha256 = GENERATOR.readPackageCopyright(copyright_path)
    assert copyright_text == 'Package notice.'
    assert copyright_sha256 == hashlib.sha256(b'Package notice.\n').hexdigest()

    copyright_path.write_bytes(b'Invalid UTF-8: \xff')
    with pytest.raises(RuntimeError, match='not valid UTF-8'):
        GENERATOR.readPackageCopyright(copyright_path)

    copyright_path.write_text('Damaged: \ufffd', encoding='utf-8')
    with pytest.raises(ValueError, match='Unicode replacement character'):
        GENERATOR.readPackageCopyright(copyright_path)


def test_vendored_chromium_license_is_read_without_network_or_version_pinning(tmp_path: Path) -> None:
    chromium_license = GENERATOR.readChromiumLicense(CHROMIUM_LICENSE_PATH)
    assert chromium_license.startswith('// Copyright 2015 The Chromium Authors')
    assert 'Redistribution and use in source and binary forms' in chromium_license

    invalid_license_path = tmp_path / 'chromium-LICENSE'
    invalid_license_path.write_text('Not the Chromium license.\n', encoding='utf-8')
    with pytest.raises(RuntimeError, match='expected license text structure'):
        GENERATOR.readChromiumLicense(invalid_license_path)

    invalid_license_path.write_bytes(b'Invalid UTF-8: \xff')
    with pytest.raises(RuntimeError, match='not valid UTF-8'):
        GENERATOR.readChromiumLicense(invalid_license_path)


def test_chromium_license_document_uses_safe_dynamic_markdown_fences() -> None:
    chromium_license = '// Copyright Chromium\n````\nLicense text.'
    document = GENERATOR.buildLicenseDocument(
        '1.2.3.4~linuxmint1+virginia',
        '1.2.3.4',
        GENERATOR.CHROMIUM_LICENSE_SOURCE_URL,
        chromium_license,
        'Linux Mint package copyright.',
        '0' * 64,
        [{
            'name': 'Markup project',
            'homepage': 'https://example.com/',
            'license': '<script>alert(1)</script>\n`````\nLicense text.',
        }],
        [('Markup project', 1)],
    )

    assert 'Installed upstream Chromium version: `1.2.3.4`' in document
    assert 'Bundled project count: 1' in document
    assert 'Linux Mint package copyright file' in document
    assert '`Markup project`: 1 character' in document
    assert '`````text\n// Copyright Chromium' in document
    assert '``````text\n' in document
    rendered = MarkdownIt().render(document)
    assert '<script>alert(1)</script>' not in rendered
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in rendered
