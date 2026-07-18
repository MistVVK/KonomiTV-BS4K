import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ASSEMBLER_PATH = REPOSITORY_ROOT / 'docker/thirdparty/assemble-runtime-license-document.py'
CLIENT_GENERATOR_PATH = REPOSITORY_ROOT / 'client/scripts/generate-license-document.mjs'


def RunAssembler(tmp_path: Path, *, include_amd_runtime: bool) -> str:
    base_path = tmp_path / 'base.md'
    client_path = tmp_path / 'client.md'
    chromium_copyright_path = tmp_path / 'chromium-copyright'
    output_path = tmp_path / ('amd.md' if include_amd_runtime else 'no-amd.md')
    base_path.write_text(
        '# Third-Party Software Licenses\n\n'
        '<!-- AMD_RUNTIME_WARNING_START -->\n'
        '> **重要: このDockerイメージを再配布しないでください。**\n>\n> Warning.\n'
        '<!-- AMD_RUNTIME_WARNING_END -->\n\n'
        '<!-- Document note. -->\n\n'
        '## Chromium @CHROMIUM_VERSION@\n\n@CHROMIUM_COPYRIGHT@\n',
        encoding = 'utf-8',
    )
    client_path.write_text('# Client Third-Party Software Licenses\n', encoding = 'utf-8')
    chromium_copyright_path.write_text('Chromium copyright.', encoding = 'utf-8')
    command = [
        sys.executable,
        str(ASSEMBLER_PATH),
        '--base', str(base_path),
        '--client', str(client_path),
        '--output', str(output_path),
        '--cuda-version', '12-4',
        '--chromium-version', '1.2.3',
        '--chromium-copyright', str(chromium_copyright_path),
    ]
    if include_amd_runtime:
        command.append('--include-amd-runtime')
    subprocess.run(command, check = True)
    return output_path.read_text(encoding = 'utf-8')


def test_runtime_license_assembler_places_target_after_amd_warning(tmp_path: Path) -> None:
    document = RunAssembler(tmp_path, include_amd_runtime = True)

    warning_index = document.index('重要: このDockerイメージを再配布しないでください。')
    warning_end_index = document.index('<!-- AMD_RUNTIME_WARNING_END -->')
    target_index = document.index('**Docker build target**')
    document_note_index = document.index('<!-- Document note. -->')
    assert warning_index < warning_end_index < target_index < document_note_index
    assert '- Target: `cuda12-4-amd`' in document
    assert '- AMD proprietary runtime included: `true`' in document


def test_runtime_license_assembler_places_target_after_title_without_amd(tmp_path: Path) -> None:
    document = RunAssembler(tmp_path, include_amd_runtime = False)

    title_index = document.index('# Third-Party Software Licenses')
    target_index = document.index('**Docker build target**')
    document_note_index = document.index('<!-- Document note. -->')
    assert title_index < target_index < document_note_index
    assert '重要: このDockerイメージを再配布しないでください。' not in document
    assert 'AMD_RUNTIME_WARNING' not in document
    assert '- Target: `cuda12-4-no-amd`' in document
    assert '- AMD proprietary runtime included: `false`' in document


def test_client_license_generator_keeps_font_manifest_inside_parent_section(tmp_path: Path) -> None:
    node_path = shutil.which('node')
    if node_path is None:
        pytest.skip('Node.js is required to verify the client license generator.')
    output_path = tmp_path / 'client-licenses.md'

    subprocess.run([node_path, str(CLIENT_GENERATOR_PATH), str(output_path)], check = True)
    document = output_path.read_text(encoding = 'utf-8')

    assert '## Bundled web fonts\n\n| Work | Version | Distribution |' in document
    assert '# Bundled web font manifest' not in document
    assert '### Bundled font license texts' in document
    assert '#### Kosugi-LICENSE.txt' in document
