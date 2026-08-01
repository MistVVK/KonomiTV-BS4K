#!/usr/bin/env python3

from __future__ import annotations

import argparse
import email
import hashlib
import re
import subprocess
from pathlib import Path


# Bridge の Apache-2.0-LICENSE のように「*-LICENSE」で終わるファイルも拾う。
LICENSE_PATTERN = re.compile(
    r'^(?:licen[cs]e|copying|notices?|copyright|third[-_ ]party)(?:$|[._ -])'
    r'|.+[._-]licen[cs]e$',
    re.IGNORECASE,
)
COMMON_LICENSE_PATTERN = re.compile(r'/usr/share/common-licenses/([A-Za-z0-9.+-]+)')
COMMON_LICENSE_ALIASES = {
    'GFDL-3': 'GFDL-1.3',
    'GPL-1.0': 'GPL-1',
    'GPL-2.0': 'GPL-2',
    'GPL-3.0': 'GPL-3',
    'LGPL-2.0': 'LGPL-2',
    'LGPL-3.0': 'LGPL-3',
}
REJECTED_SUFFIXES = {'.py', '.pyc', '.pyo', '.so', '.a', '.o', '.class'}
MAX_SIZE = 4 * 1024 * 1024
MIT_TEXT = '''Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.'''
MIT_FULL_TEXT_MARKERS = (
    'Permission is hereby granted, free of charge',
    'THE SOFTWARE IS PROVIDED "AS IS"',
)
KNOWN_DPKG_COPYRIGHT_REPAIRS = {
    ('bsdutils', '1:2.37.2-4ubuntu3.5'): ('Bartosz Fe�ski', 'Bartosz Feński'),
    ('libblkid1', '2.37.2-4ubuntu3.5'): ('Bartosz Fe�ski', 'Bartosz Feński'),
    ('libmount1', '2.37.2-4ubuntu3.5'): ('Bartosz Fe�ski', 'Bartosz Feński'),
    ('libsmartcols1', '2.37.2-4ubuntu3.5'): ('Bartosz Fe�ski', 'Bartosz Feński'),
    ('libuuid1', '2.37.2-4ubuntu3.5'): ('Bartosz Fe�ski', 'Bartosz Feński'),
    ('mount', '2.37.2-4ubuntu3.5'): ('Bartosz Fe�ski', 'Bartosz Feński'),
    ('util-linux', '2.37.2-4ubuntu3.5'): ('Bartosz Fe�ski', 'Bartosz Feński'),
    ('libjs-jquery-metadata', '12-3'): ('J�örn Zaefferer', 'Jörn Zaefferer'),
}


def readText(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_SIZE:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    for encoding in ('utf-8-sig', 'cp932', 'latin-1'):
        try:
            return data.decode(encoding).replace('\r\n', '\n').strip()
        except UnicodeDecodeError:
            pass
    return None


def licenseFiles(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob('*') if path.is_file() and path.suffix.lower() not in REJECTED_SUFFIXES and LICENSE_PATTERN.match(path.name))


def isLicenseFile(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() not in REJECTED_SUFFIXES and LICENSE_PATTERN.match(path.name) is not None


def isPackagePayload(path: Path) -> bool:
    if not path.is_file():
        return False
    value = str(path)
    if value.startswith(('/usr/share/doc/', '/usr/share/lintian/')):
        return False
    # ROCm の依存専用 metapackage が持つバージョン印は実行コードやデータではない。
    if '/.info/version-' in value:
        return False
    return True


def appendLicense(
    lines: list[str],
    title: str,
    path: Path,
    identity: str,
    emittedContents: dict[str, list[tuple[str, str]]],
    content: str | None = None,
) -> None:
    content = content if content is not None else readText(path)
    if not content:
        raise RuntimeError(f'License document is missing, empty, too large, or unreadable: {path}')
    if '\ufffd' in content:
        raise RuntimeError(f'License document contains an unrepaired Unicode replacement character: {path}')

    # 同じライセンス原文を多数のパッケージが共有する場合でも、各パッケージの帰属見出しは残す。
    # 原文は SHA-256 だけで同一判定せず文字列も比較し、偶発的なハッシュ衝突で省略しないようにする。
    contentHash = hashlib.sha256(content.encode('utf-8')).hexdigest()
    for emittedContent, emittedIdentity in emittedContents.get(contentHash, []):
        if content == emittedContent:
            lines.extend([
                f'The complete license text is identical to the document already included for `{emittedIdentity}`.',
                '',
            ])
            return

    emittedContents.setdefault(contentHash, []).append((content, identity))
    lines.extend([f'##### {title}', '', '````text', content, '````', ''])


def repairDpkgCopyrightContent(package: str, version: str, path: Path, content: str | None) -> str | None:
    if content is None or '\ufffd' not in content:
        return content

    # Ubuntu Jammy の既知の copyright ファイルだけを、binary package・version・標準パス・壊れた文字列の
    # 4条件が一致した場合に修復する。将来の別バージョンや未知の文字化けを黙って改変してはならない。
    basePackage = package.split(':')[0]
    expectedPath = Path('/usr/share/doc') / basePackage / 'copyright'
    replacement = KNOWN_DPKG_COPYRIGHT_REPAIRS.get((basePackage, version))
    if path != expectedPath or replacement is None:
        raise RuntimeError(
            f'Unknown Unicode replacement character in dpkg license document: {package} {version} ({path})'
        )
    damaged, repaired = replacement
    if content.count(damaged) != 1:
        raise RuntimeError(
            f'Known dpkg license repair no longer matches exactly once: {package} {version} ({path})'
        )
    content = content.replace(damaged, repaired)
    if '\ufffd' in content:
        raise RuntimeError(
            f'dpkg license document still contains a Unicode replacement character: {package} {version} ({path})'
        )
    return content


def repairedDpkgCopyright(package: str, version: str, path: Path) -> str | None:
    return repairDpkgCopyrightContent(package, version, path, readText(path))


def referencedCommonLicenses(documents: list[Path]) -> list[Path]:
    referenced: list[Path] = []
    for document in documents:
        for name in COMMON_LICENSE_PATTERN.findall(readText(document) or ''):
            name = name.rstrip('.,;:)')
            name = COMMON_LICENSE_ALIASES.get(name) or name
            path = Path('/usr/share/common-licenses') / name
            if not path.is_file():
                raise RuntimeError(f'Referenced common license document does not exist: {path}')
            referenced.append(path)
    return list(dict.fromkeys(referenced))


def dpkgPackages() -> list[tuple[str, str]]:
    result = subprocess.run(
        ['dpkg-query', '--show', '--showformat=${binary:Package}\t${Version}\n'],
        check=True, capture_output=True, text=True,
    )
    packages: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if '\t' not in line:
            continue
        name, version = line.split('\t', maxsplit=1)
        packages.append((name, version))
    return sorted(packages)


def dpkgPackageFiles(package: str) -> list[Path]:
    result = subprocess.run(['dpkg-query', '--listfiles', package], check=True, capture_output=True, text=True)
    return [Path(line) for line in result.stdout.splitlines() if line.startswith('/')]


def pythonPackages(root: Path) -> list[tuple[str, str, str, list[Path], str | None, list[str]]]:
    packages = []
    for metadataPath in sorted(root.rglob('*.dist-info/METADATA')):
        metadata = email.message_from_string(metadataPath.read_text(encoding='utf-8', errors='replace'))
        metadataLicense = metadata.get('License')
        expression = metadata.get('License-Expression') or metadataLicense or 'not declared'
        if expression == 'not declared':
            classifiers = [value.removeprefix('License :: ').strip() for value in metadata.get_all('Classifier', []) if value.startswith('License :: ')]
            expression = '; '.join(classifiers) or expression
        packageRoot = metadataPath.parent.parent
        local = [path for path in licenseFiles(metadataPath.parent) if path != metadataPath]

        # Core Metadata の非標準 Copyright フィールドが明示されている場合だけ、
        # 標準 MIT 本文へ補う著作権表示として利用する。Author を著作権者と推測してはならない。
        copyrightNotices: list[str] = []
        for value in metadata.get_all('Copyright', []):
            notice = value.strip()
            if notice:
                copyrightNotices.append(notice if notice.lower().startswith('copyright') else f'Copyright {notice}')
        if metadataLicense:
            copyrightNotices.extend(
                line.strip() for line in metadataLicense.splitlines()
                if line.strip().lower().startswith('copyright')
            )
        copyrightNotices = list(dict.fromkeys(copyrightNotices))

        packages.append((
            metadata.get('Name', metadataPath.parent.name),
            metadata.get('Version', 'unknown'),
            expression,
            local or licenseFiles(packageRoot / metadataPath.parent.name.removesuffix('.dist-info')),
            metadataLicense,
            copyrightNotices,
        ))
    return packages


def normalizedPythonPackageName(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def parsePythonLicenseOverride(value: str) -> tuple[tuple[str, str], Path]:
    match = re.fullmatch(r'(.+)==([^=]+)=(.+)', value)
    if match is None:
        raise argparse.ArgumentTypeError(
            'Python license overrides must use NAME==VERSION=/path/to/LICENSE format.'
        )
    name, version, path = match.groups()
    return (normalizedPythonPackageName(name), version), Path(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--root', type=Path, action='append', default=[])
    parser.add_argument('--python-root', type=Path, action='append', default=[])
    parser.add_argument('--python-license-override', action='append', default=[], type=parsePythonLicenseOverride)
    parser.add_argument('--go-module-cache', type=Path)
    parser.add_argument('--dpkg', action='store_true')
    parser.add_argument('--dpkg-prefix', action='append', default=[])
    parser.add_argument('--dpkg-exclude', action='append', default=[])
    args = parser.parse_args()

    pythonLicenseOverrides: dict[tuple[str, str], Path] = {}
    for package, path in args.python_license_override:
        if package in pythonLicenseOverrides:
            raise RuntimeError(f'Duplicate Python license override: {package[0]}=={package[1]}')
        pythonLicenseOverrides[package] = path
    usedPythonLicenseOverrides: set[tuple[str, str]] = set()

    # 結合後の文書では H2 を配布領域、H3 をライセンス種別、H4 を個別の配布物、
    # H5 をその配布物に属するライセンス資料として固定する。
    lines: list[str] = [f'## {args.stage}', '']
    emittedContents: dict[str, list[tuple[str, str]]] = {}
    if args.dpkg:
        packages = dpkgPackages()
        if args.dpkg_prefix:
            packages = [item for item in packages if any(item[0].startswith(prefix) for prefix in args.dpkg_prefix)]
        packages = [item for item in packages if item[0].split(':')[0] not in args.dpkg_exclude]
        lines.extend(['### OS and GPU Package Licenses', ''])
        for name, version in packages:
            package = name.split(':')[0]
            package_files = dpkgPackageFiles(name)
            documents = [path for path in package_files if isLicenseFile(path)]
            if not documents:
                default_document = Path('/usr/share/doc') / package / 'copyright'
                if default_document.is_file():
                    documents = [default_document]
            payload = [path for path in package_files if isPackagePayload(path)]
            if not documents and not payload:
                continue
            if not documents:
                raise RuntimeError(f'No license document found for package with payload: {name} {version}')
            lines.extend([f'#### {name} {version}', ''])
            documents = list(dict.fromkeys(documents))
            for path in [*documents, *referencedCommonLicenses(documents)]:
                content = repairedDpkgCopyright(name, version, path)
                appendLicense(lines, path.name, path, f'{name}: {path}', emittedContents, content)

    additionalDocuments = [
        (root, path)
        for root in args.root
        for path in licenseFiles(root)
    ]
    if additionalDocuments:
        lines.extend(['### Additional License Documents', ''])
        for root, path in additionalDocuments:
            relativePath = path.relative_to(root).as_posix()
            rootPath = root.as_posix().rstrip('/') or '/'
            identity = f'{rootPath}: {relativePath}'
            lines.extend([f'#### {identity}', ''])
            appendLicense(lines, path.name, path, identity, emittedContents)

    if args.go_module_cache and args.go_module_cache.exists():
        lines.extend(['### Statically Linked Go Module Licenses', ''])
        for path in licenseFiles(args.go_module_cache):
            relativePath = path.relative_to(args.go_module_cache).as_posix()
            lines.extend([f'#### {relativePath}', ''])
            appendLicense(lines, path.name, path, f'Go module: {relativePath}', emittedContents)

    pythonPackageEntries = [
        (root, package)
        for root in args.python_root
        for package in pythonPackages(root)
    ]
    if pythonPackageEntries:
        lines.extend(['### Python Package Licenses', ''])
        for root, package in pythonPackageEntries:
            name, version, declaredLicense, paths, metadataLicense, copyrightNotices = package
            rootPath = root.as_posix().rstrip('/') or '/'
            packageIdentity = (normalizedPythonPackageName(name), version)
            overridePath = pythonLicenseOverrides.get(packageIdentity)
            if not paths and overridePath is not None:
                if readText(overridePath) is None:
                    raise RuntimeError(
                        f'Python license override is missing, empty, or unreadable: {name} {version} ({overridePath})'
                    )
                paths = [overridePath]
                usedPythonLicenseOverrides.add(packageIdentity)
            lines.extend([f'#### {name} {version} — {rootPath}', '', f'- Declared license: {declaredLicense}', ''])
            if not paths:
                if 'MIT' in declaredLicense:
                    # License フィールド自体に著作権表示を含む完全な MIT 原文があれば、その原文を優先する。
                    # SPDX 式や "MIT" という略記しかない場合は、明示的な Copyright フィールドがない限り失敗させる。
                    if (
                        metadataLicense is not None and
                        all(marker in metadataLicense for marker in MIT_FULL_TEXT_MARKERS) and
                        copyrightNotices
                    ):
                        lines.extend([
                            'The installed wheel does not contain a standalone license file. '
                            'The complete MIT license text declared by its package metadata follows.',
                            '',
                            '##### MIT license text from package metadata',
                            '',
                            '````text',
                            metadataLicense.strip(),
                            '````',
                            '',
                        ])
                    elif copyrightNotices:
                        lines.extend([
                            'The installed wheel does not contain a standalone license file. '
                            'Its explicit copyright notice and the complete standard MIT license text follow.',
                            '',
                            '##### Copyright notice and standard MIT license text',
                            '',
                            '````text',
                            *copyrightNotices,
                            '',
                            MIT_TEXT,
                            '````',
                            '',
                        ])
                    else:
                        raise RuntimeError(
                            'No full MIT license text or explicit copyright notice is available for '
                            f'Python package: {name} {version} ({root})'
                        )
                else:
                    raise RuntimeError(f'No full license text available for Python package: {name} {version} ({declaredLicense})')
            for path in paths:
                try:
                    relativePath = path.relative_to(root).as_posix()
                except ValueError:
                    relativePath = f'explicit override: {path.name}'
                appendLicense(
                    lines,
                    path.name,
                    path,
                    f'{name} {version} — {rootPath}: {relativePath}',
                    emittedContents,
                )

    unusedPythonLicenseOverrides = set(pythonLicenseOverrides) - usedPythonLicenseOverrides
    if unusedPythonLicenseOverrides:
        unused = ', '.join(f'{name}=={version}' for name, version in sorted(unusedPythonLicenseOverrides))
        raise RuntimeError(f'Python license override did not match an installed package without a license file: {unused}')

    args.output.write_text('\n'.join(lines).rstrip() + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
