import runpy
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COLLECTOR_PATH = REPOSITORY_ROOT / 'docker/thirdparty/collect-license-manifest.py'


def RunCollector(tmp_path: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """テスト用の最小構成でライセンス manifest collector を実行する。"""

    return subprocess.run(
        [
            sys.executable,
            str(COLLECTOR_PATH),
            '--stage',
            'test',
            '--output',
            str(tmp_path / 'licenses.md'),
            *arguments,
        ],
        check=check,
        capture_output=True,
        text=True,
    )


def CreatePythonMetadata(root: Path, *, name: str, version: str, extra: str = '') -> None:
    """ライセンスファイルを持たない最小 dist-info を作成する。"""

    metadata_path = root / f'{name}-{version}.dist-info/METADATA'
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(
        f'Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nLicense: MIT\n{extra}',
        encoding='utf-8',
    )






def test_collector_rejects_ambiguous_mit_fallback(tmp_path: Path) -> None:
    python_root = tmp_path / 'python'
    CreatePythonMetadata(python_root, name='ambiguous-package', version='1.0.0')

    result = RunCollector(tmp_path, '--python-root', str(python_root), check=False)

    assert result.returncode != 0
    assert 'No full MIT license text or explicit copyright notice is available' in result.stderr




def test_collector_repairs_only_known_dpkg_copyright_corruption() -> None:
    collector = runpy.run_path(str(COLLECTOR_PATH))
    repair = cast(
        Callable[[str, str, Path, str | None], str | None],
        collector['repairDpkgCopyrightContent'],
    )
    path = Path('/usr/share/doc/bsdutils/copyright')
    damaged = 'Copyright:\n                     Bartosz Fe�ski <fenio@o2.pl>'

    repaired = repair('bsdutils', '1:2.37.2-4ubuntu3.5', path, damaged)
    assert repaired == 'Copyright:\n                     Bartosz Feński <fenio@o2.pl>'

    jquery_path = Path('/usr/share/doc/libjs-jquery-metadata/copyright')
    jquery_damaged = 'Copyright: John Resig, J�örn Zaefferer'
    jquery_repaired = repair('libjs-jquery-metadata', '12-3', jquery_path, jquery_damaged)
    assert jquery_repaired == 'Copyright: John Resig, Jörn Zaefferer'

    with pytest.raises(RuntimeError, match='Unknown Unicode replacement character'):
        repair('bsdutils', '1:2.37.2-4ubuntu3.6', path, damaged)
