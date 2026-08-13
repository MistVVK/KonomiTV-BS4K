import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


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
