import runpy
import subprocess
import sys
from pathlib import Path

import pytest


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






@pytest.mark.parametrize('invalid_text', ['Broken \ufffd text', 'Broken \x01 text'])
def test_runtime_license_assembler_rejects_corrupted_input(tmp_path: Path, invalid_text: str) -> None:
    document_path = tmp_path / 'invalid.md'
    document_path.write_text(invalid_text, encoding = 'utf-8')
    read_document = runpy.run_path(str(ASSEMBLER_PATH))['readDocument']

    with pytest.raises(ValueError):
        read_document(document_path)






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
