import runpy
import shutil
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pytest
from markdown_it import MarkdownIt


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ASSEMBLER_PATH = REPOSITORY_ROOT / 'docker/thirdparty/assemble-runtime-license-document.py'
CLIENT_GENERATOR_PATH = REPOSITORY_ROOT / 'client/scripts/generate-license-document.mjs'
BASE_LICENSE_DOCUMENT_PATH = REPOSITORY_ROOT / 'THIRD_PARTY_LICENSES.md'


def RunAssembler(tmp_path: Path, *, include_nonfree_runtime: bool) -> str:
    base_path = tmp_path / 'base.md'
    client_path = tmp_path / 'client.md'
    builder_path = tmp_path / 'builder.md'
    runtime_path = tmp_path / 'runtime.md'
    output_path = tmp_path / ('nonfree.md' if include_nonfree_runtime else 'free.md')
    base_path.write_text(
        '# Third-Party Software Licenses\n\n'
        '<!-- NONFREE_RUNTIME_WARNING_START -->\n'
        '> **重要: `NONFREE=true` でビルドした Docker イメージは再配布しないでください。**\n'
        '>\n> Warning.\n'
        '<!-- NONFREE_RUNTIME_WARNING_END -->\n\n'
        '<!-- Document note. -->\n\n'
        '## Directly Managed Third-Party Components\n\n'
        '### Bundled Components\n\n#### Chromium\n\nChromium licenses are in the dedicated document.\n',
        encoding = 'utf-8',
    )
    client_path.write_text(
        '## Client Third-Party Software Licenses\n\n'
        '### JavaScript Package Licenses\n\n#### example 1.0.0\n\n##### LICENSE\n\nLicense.\n',
        encoding = 'utf-8',
    )
    builder_path.write_text(
        '## Third-Party Builder Dependencies\n\n'
        '### OS and GPU Package Licenses\n\n#### builder-package 1.0\n\n##### copyright\n\nLicense.\n',
        encoding = 'utf-8',
    )
    runtime_path.write_text(
        '## Final Runtime Dependencies\n\n'
        '### Python Package Licenses\n\n#### runtime-package 1.0\n\n##### LICENSE\n\nLicense.\n',
        encoding = 'utf-8',
    )
    command = [
        sys.executable,
        str(ASSEMBLER_PATH),
        '--base', str(base_path),
        '--client', str(client_path),
        '--output', str(output_path),
        '--cuda-version', '12-4',
        '--manifest', str(builder_path),
        '--manifest', str(runtime_path),
    ]
    if include_nonfree_runtime:
        command.append('--include-nonfree-runtime')
    subprocess.run(command, check = True)
    return output_path.read_text(encoding = 'utf-8')


def test_runtime_license_assembler_places_profile_after_nonfree_warning(tmp_path: Path) -> None:
    document = RunAssembler(tmp_path, include_nonfree_runtime = True)

    warning_index = document.index('重要: `NONFREE=true` でビルドした Docker イメージは再配布しないでください。')
    warning_end_index = document.index('<!-- NONFREE_RUNTIME_WARNING_END -->')
    target_index = document.index('**Docker image build profile**')
    document_note_index = document.index('<!-- Document note. -->')
    assert warning_index < warning_end_index < target_index < document_note_index
    assert '- Profile: `cuda12.4-nonfree`' in document
    assert '- NONFREE runtime included: `true`' in document
    tokens = MarkdownIt('commonmark').parse(document)
    assert sum(token.type == 'heading_open' and token.tag == 'h1' for token in tokens) == 1
    assert [
        tokens[index + 1].content
        for index, token in enumerate(tokens)
        if token.type == 'heading_open' and token.tag == 'h2'
    ] == [
        'Directly Managed Third-Party Components',
        'Client Third-Party Software Licenses',
        'Third-Party Builder Dependencies',
        'Final Runtime Dependencies',
    ]


def test_runtime_license_assembler_places_profile_after_title_without_nonfree(tmp_path: Path) -> None:
    document = RunAssembler(tmp_path, include_nonfree_runtime = False)

    title_index = document.index('# Third-Party Software Licenses')
    target_index = document.index('**Docker image build profile**')
    document_note_index = document.index('<!-- Document note. -->')
    assert title_index < target_index < document_note_index
    assert '重要: `NONFREE=true` でビルドした Docker イメージは再配布しないでください。' not in document
    assert 'NONFREE_RUNTIME_WARNING' not in document
    assert '- Profile: `cuda12.4-free`' in document
    assert '- NONFREE runtime included: `false`' in document


@pytest.mark.parametrize('invalid_text', ['Broken \ufffd text', 'Broken \x01 text'])
def test_runtime_license_assembler_rejects_corrupted_input(tmp_path: Path, invalid_text: str) -> None:
    document_path = tmp_path / 'invalid.md'
    document_path.write_text(invalid_text, encoding = 'utf-8')
    read_document = runpy.run_path(str(ASSEMBLER_PATH))['readDocument']

    with pytest.raises(ValueError):
        read_document(document_path)


def test_runtime_license_assembler_normalizes_form_feed_page_breaks(tmp_path: Path) -> None:
    document_path = tmp_path / 'page-break.md'
    document_path.write_text('First page\f\nSecond page\n', encoding = 'utf-8')
    read_document = runpy.run_path(str(ASSEMBLER_PATH))['readDocument']

    assert read_document(document_path) == 'First page\n\nSecond page\n'


def test_base_license_document_uses_one_consistent_component_hierarchy() -> None:
    document = BASE_LICENSE_DOCUMENT_PATH.read_text(encoding='utf-8')
    tokens = MarkdownIt('commonmark').parse(document)
    headings = [
        (int(token.tag[1]), tokens[index + 1].content)
        for index, token in enumerate(tokens)
        if token.type == 'heading_open'
    ]

    assert [title for level, title in headings if level == 1] == ['Third-Party Software Licenses']
    assert [title for level, title in headings if level == 2] == ['Directly Managed Third-Party Components']
    assert [title for level, title in headings if level == 3] == [
        'Bundled Components',
        'Corresponding Source and Local Modifications',
    ]
    assert 'Chromium' in [title for level, title in headings if level == 4]
    assert 'CM analysis runtime' in [title for level, title in headings if level == 4]
    for (previous_level, _), (level, title) in pairwise(headings):
        assert level <= previous_level + 1, f'Heading hierarchy jumps before {title!r}.'


def test_runtime_license_assembler_rejects_heading_level_jumps_and_ignores_fenced_headings(tmp_path: Path) -> None:
    validate_document_hierarchy = runpy.run_path(str(ASSEMBLER_PATH))['ValidateDocumentHierarchy']
    document_path = tmp_path / 'hierarchy.md'

    with pytest.raises(ValueError, match='jumps from H2 to H4'):
        validate_document_hierarchy(
            document_path,
            '# Third-Party Software Licenses\n\n## Client\n\n#### Package\n',
            contains_title = True,
        )

    validate_document_hierarchy(
        document_path,
        '# Third-Party Software Licenses\n\n## Client\n\n```text\n###### License heading\n```\n',
        contains_title = True,
    )


def test_client_license_generator_keeps_font_manifest_inside_parent_section(tmp_path: Path) -> None:
    node_path = shutil.which('node')
    if node_path is None:
        pytest.skip('Node.js is required to verify the client license generator.')
    output_path = tmp_path / 'client-licenses.md'

    subprocess.run([node_path, str(CLIENT_GENERATOR_PATH), str(output_path)], check = True)
    document = output_path.read_text(encoding = 'utf-8')

    assert '### Bundled web fonts\n\n| Work | Version | Distribution |' in document
    assert '# Bundled web font manifest' not in document
    assert '### JavaScript Package Licenses' in document
    assert '\n## JavaScript Package Licenses\n' not in document
    assert '\n#### Kosugi\n' in document
    assert '\n##### Kosugi-LICENSE.txt\n' in document
    assert '\n#### Kosugi-LICENSE.txt\n' not in document
    assert '##### LICENSE' in document
